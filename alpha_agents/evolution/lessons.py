"""L2 Lessons — extract observations and quarantine principle proposals."""

from __future__ import annotations

import json
import logging
import re

from alpha_agents.data.memory_store import (
    insert_daily_lesson,
    get_recent_daily_lessons,
    get_all_principles_including_weakened,
    get_all_playbooks,
)
from alpha_agents.data.learning_candidates import record_observation, save_candidate
from alpha_agents.data.token_usage import instrument

logger = logging.getLogger(__name__)

# Parallel to vpa.py's <!-- VERDICT: {...} --> pattern.
_LESSONS_TAG_RE = re.compile(r"<!--\s*LESSONS:\s*(\[.*?\])\s*-->", re.DOTALL)


def extract_daily_lessons(report: str, today: str) -> int:
    """Parse the <!-- LESSONS: [...] --> tag from a review report and persist rows.

    Returns the count of lessons successfully inserted. Unparseable/missing
    tag returns 0 (warn-logs only — don't raise, the review itself succeeded).
    """
    m = _LESSONS_TAG_RE.search(report)
    if not m:
        logger.info("No LESSONS tag in review report; skipping extraction")
        return 0

    raw = m.group(1).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LESSONS JSON malformed: %s", e)
        return 0

    if not isinstance(items, list):
        logger.warning("LESSONS payload is not a list")
        return 0

    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        lesson_type = (item.get("type") or "").strip()
        if not content or lesson_type not in ("success", "failure", "insight"):
            continue
        try:
            insert_daily_lesson(
                date=today,
                lesson_type=lesson_type,
                theme=(item.get("theme") or None) or None,
                content=content,
                tags=(item.get("tags") or ""),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to insert lesson %s: %s", content[:40], e)
    return count


_CONSOLIDATION_SYSTEM_PROMPT = """Extract candidate trading lessons, not approved rules.
Read today's daily_lessons (observations) and existing trading_principles.
Propose CREATE for a reusable pattern or REINFORCE for a new cited case.
All proposals are quarantined. Neither this response nor historical outcome
measurements authorize creating, reactivating, weakening or retiring live rules.
A candidate-bound forward version gate is not implemented in this phase.

Return only JSON in this shape:
{"operations": [
  {"op": "create", "principle": "...", "pattern_description": "...", "category": "vpa_signal|theme_timing|entry|exit|risk", "action_guidance": "...", "evidence": [{"code":"...", "date":"...", "outcome":"..."}]},
  {"op": "reinforce", "principle_id": 123, "new_case": {"code":"...", "date":"...", "outcome":"..."}}
]}

Requirements:
- Describe a specific price/volume pattern, its location and volume context.
- CREATE requires pattern_description, action_guidance and at least one cited case.
- Cite only historical recommendations supplied in the inputs; never invent evidence.
- Do not judge existing rules or request deletion, weakening or approval.
- If there is nothing worth proposing, return {"operations": []}."""


def _call_consolidation_llm(lessons: list[dict], principles: list[dict]) -> dict:
    """Call the consolidation LLM. Returns parsed JSON dict with 'operations' list."""
    from openai import OpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

    user_content = (
        "【今天新 lessons】\n" + json.dumps(lessons, ensure_ascii=False, indent=2)
        + "\n\n【已有 principles】\n" + json.dumps(
            [{"id": p["id"], "principle": p["principle"], "status": p["status"],
              "evidence_count": p.get("evidence_count", 0)} for p in principles],
            ensure_ascii=False, indent=2,
        )
    )
    client = instrument(OpenAI(api_key=AGENT_API_KEY,
                               base_url=AGENT_BASE_URL), module="lessons")
    resp = client.chat.completions.create(
        model=AGENT_MODEL or "qwen-plus",
        messages=[
            {"role": "system", "content": _CONSOLIDATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # 4000, not 3000: the observed failure was a response that began
        # with valid JSON and simply stopped — cut off mid-object, which no
        # regex salvage can repair. Principles are the only long-term
        # memory this system has, so the call that produces them should not
        # be the one running closest to its ceiling.
        max_tokens=4000,
        timeout=90,
    )
    content = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r'\{.*"operations".*\}', content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError as e:
                logger.debug("Consolidation JSON recovery failed: %s", e)
        _log_consolidation_failure("Invalid JSON", content)
        logger.warning("Consolidation LLM returned unparseable content: %s",
                       content[:200])
        return {"operations": []}


def _log_consolidation_failure(kind: str, detail: str) -> None:
    """Record missing research proposals without implying rule promotion."""
    try:
        from alpha_agents.data.activity_log import log_activity
        log_activity("learning_stalled", task="consolidate_principles",
                     status="degraded",
                     message="No learning candidates (%s): %s" % (kind, detail[:300]),
                     detail={"kind": kind})
    except Exception as e:
        logger.debug("Could not log consolidation failure: %s", e)


def consolidate_principles(today: str) -> dict:
    """Persist create/reinforce proposals without changing existing principles.

    The legacy mutation counts remain zero. `candidates` counts persisted
    proposals (including exact retries), not approvals; `failed` is explicit.
    Raw proposals and their lesson inputs are retained for later inspection.
    """
    counts = {"created": 0, "reinforced": 0, "weakened": 0,
              "candidates": 0, "failed": 0}
    # Input ordering is not new evidence and must not produce new candidates,
    # so the payload is sorted into a canonical order. Rows without an id are
    # ordered first rather than raising: a malformed row must not be able to
    # abort the day's consolidation for every other lesson.
    lessons = sorted(
        (lesson for lesson in get_recent_daily_lessons(days=1)
         if lesson.get("date") == today),
        key=lambda lesson: lesson.get("id") or 0,
    )
    if not lessons:
        return counts

    principles = get_all_principles_including_weakened()
    try:
        result = _call_consolidation_llm(lessons, principles)
    except Exception as e:
        logger.warning("Consolidation LLM call failed: %s", e)
        counts["failed"] += 1
        return counts

    ops = result.get("operations") if isinstance(result, dict) else None
    if not isinstance(ops, list):
        logger.warning("Consolidation operations must be a list: %s", result)
        counts["failed"] += 1
        return counts
    if not ops:
        _log_consolidation_failure(
            "No proposals", "Input: %d lessons, %d existing principles" %
            (len(lessons), len(principles)))
    for op in ops:
        if not isinstance(op, dict):
            logger.warning("Invalid consolidation operation: %s", op)
            counts["failed"] += 1
            continue
        kind = op.get("op")
        if kind not in ("create", "reinforce"):
            logger.info("Ignoring unsupported consolidation operation: %s", op)
            continue
        try:
            target_id = op.get("principle_id") if kind == "reinforce" else None
            if kind == "reinforce" and (type(target_id) is not int or target_id <= 0):
                raise ValueError("Reinforcement requires a positive integer principle_id")
            # A proposal has to restate itself as a claim about the world
            # before it is worth storing. If the model did not name what it
            # is proposing, the operation is a failure rather than a
            # candidate with a blank claim.
            if kind == "create":
                claim = op.get("principle") or op.get("pattern_description")
                context = op.get("pattern_description")
                delta = {"create_principle": op.get("principle"),
                         "action_guidance": op.get("action_guidance"),
                         "category": op.get("category")}
            else:
                claim = (f"Principle #{target_id} gains a further case: "
                         f"{json.dumps(op.get('new_case'), ensure_ascii=False, sort_keys=True)}")
                context = f"existing principle #{target_id} under consolidation"
                delta = {"reinforce_principle": target_id}
            candidate_id = save_candidate(
                entity_type="principle", operation=kind, target_id=target_id,
                source="consolidate_principles", source_date=today,
                payload={"proposal": op, "lessons": lessons},
                claim=claim, applicable_context=context,
                proposed_behavior_delta=delta,
                # Today's lessons are prose, not T1 episodes: nothing here
                # can name a decision. Recorded empty, not omitted.
                evidence_episode_ids={"supporting": [], "opposing": []},
            )
            counts["candidates"] += 1
            logger.info("Quarantined principle %s candidate #%d", kind, candidate_id)
        except Exception as e:
            counts["failed"] += 1
            logger.warning("Consolidation candidate %s failed: %s", op, e)
    return counts


def _measure_principles(today: str) -> int:
    """Store descriptive scores separately, without invoking lifecycle rescoring.

    score_principle only reads cited prediction outcomes. Its retirement hint
    is an observation, not forward validation or permission to change a rule.
    """
    from datetime import datetime
    from alpha_agents.evolution.principle_scoring import score_principle

    as_of = datetime.strptime(today, "%Y-%m-%d")
    measured = 0
    for principle in get_all_principles_including_weakened():
        result = score_principle(principle, as_of)
        record_observation(
            entity_type="principle", target_id=principle["id"],
            source="post_review", source_date=today,
            payload={**result, "evidence": principle.get("evidence"), "as_of": today},
        )
        measured += 1
    return measured


def _collect_playbook_candidates(today: str) -> dict:
    """Reuse discovery heuristics without invoking any active lifecycle API.

    Cluster counts are research inputs, not holdout evidence. Active capacity
    does not constrain quarantine, and no slot is freed or weight assigned.
    """
    from alpha_agents.evolution.playbook import (
        _AUTO_CREATE_LOOKBACK_DAYS, _AUTO_CREATE_MIN_TOTAL,
        _pattern_from_cluster, _pattern_signature, _query_hit_clusters,
    )

    clusters = _query_hit_clusters()
    counts = {"candidates": 0, "failed": 0}
    if not clusters:
        return counts
    existing_sigs = {_pattern_signature(pb.get("pattern_json", "{}"))
                     for pb in get_all_playbooks()}
    for cluster in clusters:
        try:
            if cluster["total"] < _AUTO_CREATE_MIN_TOTAL:
                continue
            pattern = _pattern_from_cluster(cluster)
            sig = _pattern_signature(json.dumps(pattern))
            if not sig or sig in existing_sigs:
                continue
            name_parts = [str(cluster[key]) for key in ("theme", "vpa_verdict")
                          if cluster.get(key)]
            if cluster.get("institutional_present"):
                name_parts.append("institutional")
            name = "Auto: " + "-".join(name_parts)
            candidate_id = save_candidate(
                entity_type="playbook", operation="create",
                source="post_review", source_date=today,
                payload={"name": name,
                         "pattern_json": pattern, "cluster": cluster,
                         "lookback_days": _AUTO_CREATE_LOOKBACK_DAYS},
                claim=(f"Pattern {name!r} recurs {cluster['hits']}/"
                       f"{cluster['total']} times over "
                       f"{_AUTO_CREATE_LOOKBACK_DAYS} days and no existing "
                       f"playbook covers it"),
                applicable_context=json.dumps(pattern, ensure_ascii=False,
                                              sort_keys=True),
                proposed_behavior_delta={"create_playbook": name,
                                         "pattern_json": pattern},
                evidence_episode_ids={"supporting": [], "opposing": []},
            )
            existing_sigs.add(sig)
            counts["candidates"] += 1
            logger.info("Quarantined playbook pattern candidate #%d", candidate_id)
        except Exception as e:
            counts["failed"] += 1
            logger.warning("Playbook pattern candidate %s failed: %s", cluster, e)
    return counts


async def post_review(today: str, review_report: str) -> str:
    """Extract lessons, quarantine proposals, and measure; never promote.

    There is no candidate-bound forward validation protocol in phase one.
    Running a generic daily gate would imply approval evidence we do not have.
    """
    import asyncio

    lesson_count = await asyncio.to_thread(extract_daily_lessons, review_report, today)
    counts = {"candidates": 0, "failed": 0}
    if lesson_count > 0:
        counts = await asyncio.to_thread(consolidate_principles, today)

    scored = 0
    failures = counts["failed"]
    try:
        scored = await asyncio.to_thread(_measure_principles, today)
    except Exception as e:
        logger.warning("Principle observation failed: %s", e)
        failures += 1

    # Do not call lifecycle APIs, even if another caller has quarantined them.
    # This scheduled path must remain safe with their original mutating behavior.
    playbook_counts = {"candidates": 0, "failed": 0}
    try:
        playbook_counts = await asyncio.to_thread(_collect_playbook_candidates, today)
    except Exception as e:
        logger.warning("Playbook discovery failed: %s", e)
        failures += 1
    failures += playbook_counts["failed"]

    try:
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        metrics = await asyncio.to_thread(compute_evolution_metrics, today)
    except Exception as e:
        logger.warning("Evolution metrics computation failed: %s", e)
        metrics = None
        failures += 1

    if not (lesson_count or counts["candidates"] or scored
            or playbook_counts["candidates"] or failures):
        return ""

    lines = ["[Learning candidates — not approved; existing rules unchanged]"]
    if lesson_count:
        lines.append(f"- Extracted {lesson_count} lessons")
    if counts["candidates"]:
        lines.append(f"- Stored {counts['candidates']} principle candidates")
    if scored:
        lines.append(f"- Recorded {scored} principle measurements (no rule updates)")
    if playbook_counts["candidates"]:
        lines.append(f"- Stored {playbook_counts['candidates']} playbook pattern candidates")
    if failures:
        lines.append(f"- Learning/measurement failures: {failures}; inspect warning logs")
    if metrics:
        lines.append(
            f"[Observations] 7d hit rate {metrics['intraday_hit_rate_7d']:.0%}; "
            f"matched {metrics['matched_count_7d']} / {metrics['matched_hit_rate_7d']:.0%}"
        )
    return "\n".join(lines)
