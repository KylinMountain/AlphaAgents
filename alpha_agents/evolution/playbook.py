"""L3 Playbook — match candidates, adjust score by weight, track stats."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    get_active_playbooks,
    get_all_playbooks,
)
from alpha_agents.data.learning_candidates import (
    save_candidate,
)
from alpha_agents.data.token_usage import instrument

logger = logging.getLogger(__name__)

_MIN_ACTIVE_FOR_MATCHING = 2  # regime-change fallback threshold

_DEGRADE_MIN_TRADES = 5
_DEGRADE_HIT_THRESHOLD = 0.4
_RESTORE_MIN_TRADES = 5
_RESTORE_HIT_THRESHOLD = 0.6
_BOOST_MIN_TRADES = 10
_BOOST_HIT_THRESHOLD = 0.7
_DEPRECATE_DAYS = 14


def _check_condition(field_value, op: str, target) -> bool:
    """Evaluate one condition. Return False on any type mismatch — never raise."""
    try:
        if op == "==":
            return field_value == target
        if op == "in":
            return field_value in target
        if op == "contains":
            return isinstance(field_value, str) and str(target) in field_value
        if op == ">=":
            return float(field_value) >= float(target)
        if op == "<=":
            return float(field_value) <= float(target)
        if op == ">":
            return float(field_value) > float(target)
        if op == "<":
            return float(field_value) < float(target)
    except (TypeError, ValueError):
        return False
    logger.debug("Unknown playbook op: %s", op)
    return False


def _matches_all(candidate: dict, conditions: list[dict]) -> bool:
    """AND semantics across all conditions."""
    for cond in conditions:
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        if field is None or op is None:
            return False
        actual = candidate.get(field)
        if not _check_condition(actual, op, value):
            return False
    return True


def match_playbook(candidate: dict) -> dict | None:
    """Match existing active knowledge by weight DESC, then stable ID.

    Observed hit rates must not silently re-rank equal-weight rules. The
    existing fewer-than-two-active fallback is unchanged.
    """
    playbooks = sorted(get_active_playbooks(),
                       key=lambda pb: (-pb.get("weight", 1.0), pb["id"]))
    if len(playbooks) < _MIN_ACTIVE_FOR_MATCHING:
        return None
    for pb in playbooks:
        try:
            pattern = json.loads(pb.get("pattern_json", "{}"))
        except json.JSONDecodeError:
            continue
        conditions = pattern.get("conditions", []) or []
        if not conditions:
            continue
        if _matches_all(candidate, conditions):
            return pb
    return None


def _recent_hit_rate(playbook_id: int, days: int = 7) -> tuple[float, int]:
    """Compute hit rate on predictions re-matched to this playbook's pattern
    over last N days. Conservative: returns (0.0, 0) on any error."""
    from alpha_agents.data.memory_store import _get_conn

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        row = _get_conn().execute(
            "SELECT pattern_json FROM playbooks WHERE id = ?", (playbook_id,)
        ).fetchone()
        if not row:
            return (0.0, 0)
        pattern = json.loads(row["pattern_json"])
        conditions = pattern.get("conditions", [])
        if not conditions:
            return (0.0, 0)

        preds = _get_conn().execute(
            "SELECT features_json, hit FROM predictions "
            "WHERE report_type = 'intraday' AND hit IS NOT NULL AND date >= ?",
            (cutoff,),
        ).fetchall()
        hits = 0
        total = 0
        for p in preds:
            try:
                features = json.loads(p["features_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if _matches_all(features, conditions):
                total += 1
                if p["hit"] == 1:
                    hits += 1
        return (hits / total, total) if total else (0.0, 0)
    except Exception as e:
        logger.debug("_recent_hit_rate error: %s", e)
        return (0.0, 0)


def annotate_degraded(playbook: dict) -> str:
    """Ask LLM (short call) why this playbook's hit rate dropped. Graceful degrade."""
    try:
        from openai import OpenAI
        from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

        pattern = json.loads(playbook.get("pattern_json", "{}"))
        msg = (
            f"Playbook: {playbook['name']}\n"
            f"Pattern: {pattern.get('description', '')} / {pattern.get('conditions')}\n"
            f"Stats: hit_rate={playbook.get('hit_rate', 0):.1%}, "
            f"total={playbook.get('total_trades', 0)}, "
            f"avg_return={playbook.get('avg_return', 0):.2f}%\n\n"
            "用一句话（不超过30字）解释这个 playbook 近期为什么失灵。"
        )
        client = instrument(OpenAI(api_key=AGENT_API_KEY,
                                   base_url=AGENT_BASE_URL),
                            module="playbook")
        resp = client.chat.completions.create(
            model=AGENT_MODEL or "qwen-plus",
            messages=[{"role": "user", "content": msg}],
            max_tokens=80,
            timeout=30,
        )
        return (resp.choices[0].message.content or "").strip()[:60]
    except Exception as e:
        logger.debug("annotate_degraded failed: %s", e)
        return "近期胜率下滑"


def update_playbook_stats(today: str) -> list[str]:
    """Inspect trade statistics and quarantine lifecycle proposals only.

    Outcome counting stays in record_playbook_trade. The old thresholds are
    proposal heuristics, not validation: no weights, statuses, annotations or
    version history are changed, including when this function is called alone.
    """
    ops: list[str] = []
    for pb in get_all_playbooks():
        pid = pb["id"]
        status = pb["status"]
        total = pb["total_trades"]
        hr = pb["hit_rate"] or 0.0
        proposal = None
        recent = None

        if status == "active" and total >= _DEGRADE_MIN_TRADES and hr < _DEGRADE_HIT_THRESHOLD:
            proposal = {"status": "degraded", "weight": 0.5,
                        "reason": f"hit_rate={hr:.2f} < {_DEGRADE_HIT_THRESHOLD}"}
        elif status == "degraded":
            rec_hr, rec_n = _recent_hit_rate(pid, days=7)
            recent = {"hit_rate": rec_hr, "total": rec_n}
            if rec_n >= _RESTORE_MIN_TRADES and rec_hr >= _RESTORE_HIT_THRESHOLD:
                proposal = {"status": "active", "weight": 1.0,
                            "reason": f"recent hit_rate={rec_hr:.2f} >= {_RESTORE_HIT_THRESHOLD}"}
            else:
                try:
                    last = datetime.strptime(pb["last_updated"], "%Y-%m-%d")
                    today_dt = datetime.strptime(today, "%Y-%m-%d")
                    if (today_dt - last).days > _DEPRECATE_DAYS:
                        proposal = {"status": "deprecated", "weight": 0.0,
                                    "reason": "Degraded for more than 14 days"}
                except (ValueError, TypeError) as e:
                    logger.warning("Invalid lifecycle date for playbook #%s: %s", pid, e)
        elif (status == "active" and total >= _BOOST_MIN_TRADES
                and hr >= _BOOST_HIT_THRESHOLD and pb.get("weight", 1.0) < 1.5):
            proposal = {"status": "active", "weight": 1.5,
                        "reason": f"hit_rate={hr:.2f} >= {_BOOST_HIT_THRESHOLD} boost"}

        if proposal is not None:
            candidate_id = save_candidate(
                entity_type="playbook", operation="update", target_id=pid,
                source="update_playbook_stats", source_date=today,
                payload={"proposal": proposal, "playbook": pb, "recent": recent},
                claim=(f"Playbook #{pid} ({pb['name']}) should move to "
                       f"{proposal['status']}: {proposal['reason']}"),
                applicable_context=(f"playbook #{pid} ({pb['name']}) sits at "
                                    f"{status} after {total} trades "
                                    f"(hit_rate={hr:.2f})"),
                proposed_behavior_delta={"status": proposal["status"],
                                         "weight": proposal["weight"]},
                # Playbook statistics are counts of finished trades, not
                # decisions, so there is no episode to cite. Stated as an
                # empty pair rather than omitted: the candidate is not
                # evidence-linked, and that is worth being able to see.
                evidence_episode_ids={"supporting": [], "opposing": []},
            )
            ops.append(f"{pb['name']}: candidate #{candidate_id} "
                       f"({proposal['reason']}; not applied)")
            logger.info("Quarantined lifecycle candidate #%d for playbook #%d", candidate_id, pid)
    return ops


_AUTO_CREATE_MIN_WINS = 3
_AUTO_CREATE_MIN_TOTAL = 3
_AUTO_CREATE_LOOKBACK_DAYS = 14


def _query_hit_clusters(days: int = _AUTO_CREATE_LOOKBACK_DAYS) -> list[dict]:
    """Group recent verified intraday predictions by decision-feature cluster.
    Returns rows where wins >= _AUTO_CREATE_MIN_WINS. CRITICAL: only uses
    report_type='intraday' (excludes 'intraday_signal' limit-up observations
    which would dominate clustering with useless patterns)."""
    from alpha_agents.data.memory_store import _get_conn

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = """
    SELECT
        json_extract(features_json, '$.vpa_verdict') as vpa_verdict,
        json_extract(features_json, '$.theme') as theme,
        CASE WHEN json_extract(features_json, '$.institutional') IS NOT NULL
                  AND json_extract(features_json, '$.institutional') != ''
             THEN 1 ELSE 0 END as institutional_present,
        COUNT(*) FILTER (WHERE hit=1) as hits,
        COUNT(*) as total,
        AVG(CASE WHEN hit=1 THEN next_day_return ELSE 0 END) as avg_return
    FROM predictions
    WHERE report_type = 'intraday'
      AND hit IS NOT NULL
      AND features_json IS NOT NULL
      AND features_json != '{}'
      AND date >= ?
    GROUP BY vpa_verdict, theme, institutional_present
    HAVING hits >= ?
    ORDER BY hits DESC
    """
    rows = _get_conn().execute(q, (cutoff, _AUTO_CREATE_MIN_WINS)).fetchall()
    return [dict(r) for r in rows]


def _pattern_from_cluster(cluster: dict) -> dict:
    """Build the pattern_json for a feature cluster."""
    conditions = []
    if cluster.get("theme"):
        conditions.append({"field": "theme", "op": "==", "value": cluster["theme"]})
    if cluster.get("vpa_verdict"):
        conditions.append({"field": "vpa_verdict", "op": "==",
                           "value": cluster["vpa_verdict"]})
    if cluster.get("institutional_present"):
        conditions.append({"field": "institutional", "op": "contains",
                           "value": "机构"})
    desc_parts = []
    if cluster.get("theme"):
        desc_parts.append(cluster["theme"])
    if cluster.get("vpa_verdict"):
        desc_parts.append(f"VPA{cluster['vpa_verdict']}")
    if cluster.get("institutional_present"):
        desc_parts.append("机构买入")
    return {"description": "+".join(desc_parts), "conditions": conditions}


def _pattern_signature(pattern_json_str: str) -> frozenset:
    """Return a comparable signature (field+op+value) of a pattern's conditions."""
    try:
        pattern = json.loads(pattern_json_str)
    except json.JSONDecodeError:
        return frozenset()
    return frozenset(
        (c.get("field"), c.get("op"), str(c.get("value")))
        for c in pattern.get("conditions", [])
    )


# Capacity target for retirement proposals, not an automatic eviction rule.
# Existing active sets are preserved; discoveries remain quarantined.
MAX_ACTIVE_PLAYBOOKS = 12


def _playbook_utility(pb: dict) -> float:
    """Ranking score for eviction. Higher survives.

    Hit rate is the signal; trade count is confidence in it. An untested
    playbook sits at the 0.5 base rate rather than at zero, so a brand new
    one is not evicted before it has had a chance to be measured.
    """
    trades = pb.get("total_trades", 0) or 0
    hit_rate = pb.get("hit_rate", 0.0) or 0.0
    if trades < 3:
        hit_rate = 0.5
    # Shrink toward the base rate when evidence is thin.
    confidence = min(trades / 10.0, 1.0)
    return 0.5 + (hit_rate - 0.5) * confidence


def enforce_capacity(today: str) -> list[int]:
    """Record retirement candidates for excess capacity, without evicting.

    Returns candidate IDs, never retired playbook IDs. Legacy over-capacity
    sets remain intact; discovery cannot enlarge the active set in phase one.
    """
    active = [pb for pb in get_all_playbooks() if pb.get("status") == "active"]
    if len(active) <= MAX_ACTIVE_PLAYBOOKS:
        return []

    ranked = sorted(active, key=lambda pb: (_playbook_utility(pb), pb["id"]))
    candidates = []
    for pb in ranked[:len(active) - MAX_ACTIVE_PLAYBOOKS]:
        candidate_id = save_candidate(
            entity_type="playbook", operation="retire", target_id=pb["id"],
            source="enforce_capacity", source_date=today,
            payload={"playbook": pb, "capacity": MAX_ACTIVE_PLAYBOOKS,
                     "active_count": len(active), "utility": _playbook_utility(pb),
                     "proposal": {"status": "deprecated", "weight": 0.0}},
            claim=(f"Playbook #{pb['id']} ({pb['name']}) is the weakest of "
                   f"{len(active)} active playbooks (utility "
                   f"{_playbook_utility(pb):.3f}) and should be retired to "
                   f"hold the cap at {MAX_ACTIVE_PLAYBOOKS}"),
            applicable_context=(f"{len(active)} active playbooks exceeds "
                                f"MAX_ACTIVE_PLAYBOOKS={MAX_ACTIVE_PLAYBOOKS}"),
            proposed_behavior_delta={"status": "deprecated", "weight": 0.0},
            evidence_episode_ids={"supporting": [], "opposing": []},
        )
        candidates.append(candidate_id)
        logger.info("Quarantined capacity candidate #%d for playbook #%d (utility=%.3f)",
                    candidate_id, pb["id"], _playbook_utility(pb))
    return candidates


def scan_and_auto_create(today: str) -> list[int]:
    """Quarantine novel cluster proposals; return candidate IDs, not playbooks.

    Discovery does not enforce active capacity or assign live weights. Raw
    cluster statistics are retained as observations, not holdout evidence.
    """
    clusters = _query_hit_clusters()
    if not clusters:
        return []

    existing_sigs = {_pattern_signature(pb.get("pattern_json", "{}"))
                     for pb in get_all_playbooks()}
    candidates = []
    for cluster in clusters:
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
        name = f"Auto: {'-'.join(name_parts)}"
        candidate_id = save_candidate(
            entity_type="playbook", operation="create", source="scan_and_auto_create",
            source_date=today,
            payload={"name": name, "pattern_json": pattern, "cluster": cluster,
                     "lookback_days": _AUTO_CREATE_LOOKBACK_DAYS},
            claim=(f"Cluster {name!r} recurs {cluster['hits']}/{cluster['total']} "
                   f"times over {_AUTO_CREATE_LOOKBACK_DAYS} days and no "
                   f"existing playbook covers its pattern"),
            applicable_context=json.dumps(pattern, ensure_ascii=False,
                                          sort_keys=True),
            proposed_behavior_delta={"create_playbook": name,
                                     "pattern_json": pattern},
            # Cluster statistics are aggregate counts over a lookback
            # window; they name no single decision to cite.
            evidence_episode_ids={"supporting": [], "opposing": []},
        )
        existing_sigs.add(sig)
        candidates.append(candidate_id)
        logger.info("Quarantined discovery candidate #%d: %s (hits=%d/%d)",
                    candidate_id, name, cluster["hits"], cluster["total"])
    return candidates
