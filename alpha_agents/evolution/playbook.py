"""L3 Playbook — match candidates, adjust score by weight, track stats."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    get_active_playbooks,
    get_all_playbooks,
    update_playbook_status,
    set_playbook_annotation,
    create_playbook,
)

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
    """Return the first matching playbook (by weight DESC) or None.

    Regime-change fallback: if fewer than 2 active playbooks exist, skip
    matching entirely (return None). Prevents ranking on a broken model
    during market regime transitions.
    """
    playbooks = get_active_playbooks()
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
        client = OpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
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
    """Daily rule engine. Returns list of human-readable operation strings.

    Rule priority (each playbook hits at most one rule per day):
      1. active, hit_rate<0.4 with >=5 trades → degraded + weight=0.5 + LLM annotate
      2. degraded, recent_hit_rate>=0.6 with >=5 trades → active + weight=1.0
      3. degraded, days_since_last_update>14 → deprecated + weight=0.0
      4. active, hit_rate>=0.7 with >=10 trades → weight 1.0→1.5
    """
    ops: list[str] = []
    playbooks = get_all_playbooks()
    for pb in playbooks:
        pid = pb["id"]
        name = pb["name"]
        status = pb["status"]
        total = pb["total_trades"]
        hr = pb["hit_rate"] or 0.0

        if status == "active" and total >= _DEGRADE_MIN_TRADES and hr < _DEGRADE_HIT_THRESHOLD:
            annotation = annotate_degraded(pb)
            update_playbook_status(pid, status="degraded", weight=0.5,
                                   reason=f"hit_rate={hr:.2f} < {_DEGRADE_HIT_THRESHOLD}",
                                   hit_rate_at_change=hr, today=today)
            set_playbook_annotation(pid, annotation=annotation, today=today)
            ops.append(f"{name}: active→degraded ({hr:.1%}) — {annotation}")
            continue

        if status == "degraded":
            rec_hr, rec_n = _recent_hit_rate(pid, days=7)
            if rec_n >= _RESTORE_MIN_TRADES and rec_hr >= _RESTORE_HIT_THRESHOLD:
                update_playbook_status(pid, status="active", weight=1.0,
                                       reason=f"recent hit_rate={rec_hr:.2f} >= {_RESTORE_HIT_THRESHOLD}",
                                       hit_rate_at_change=rec_hr, today=today)
                ops.append(f"{name}: degraded→active (近7天 {rec_hr:.1%})")
                continue
            try:
                last = datetime.strptime(pb["last_updated"], "%Y-%m-%d")
                today_dt = datetime.strptime(today, "%Y-%m-%d")
                if (today_dt - last).days > _DEPRECATE_DAYS:
                    update_playbook_status(pid, status="deprecated", weight=0.0,
                                           reason="degraded超过14天未恢复",
                                           hit_rate_at_change=hr, today=today)
                    ops.append(f"{name}: degraded→deprecated (超时)")
                    continue
            except (ValueError, TypeError):
                pass

        if (status == "active" and total >= _BOOST_MIN_TRADES
                and hr >= _BOOST_HIT_THRESHOLD and pb.get("weight", 1.0) < 1.5):
            update_playbook_status(pid, status="active", weight=1.5,
                                   reason=f"hit_rate={hr:.2f} >= {_BOOST_HIT_THRESHOLD} boost",
                                   hit_rate_at_change=hr, today=today)
            ops.append(f"{name}: weight 1.0→1.5 (boost, {hr:.1%})")
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


def scan_and_auto_create(today: str) -> list[int]:
    """Scan hit clusters and create a new playbook for each novel pattern.
    Returns list of newly-created playbook IDs."""
    clusters = _query_hit_clusters()
    if not clusters:
        return []

    existing = get_all_playbooks()
    existing_sigs = {_pattern_signature(pb.get("pattern_json", "{}"))
                     for pb in existing}

    created = []
    for c in clusters:
        if c["total"] < _AUTO_CREATE_MIN_TOTAL:
            continue
        pattern = _pattern_from_cluster(c)
        sig = frozenset(
            (cond["field"], cond["op"], str(cond["value"]))
            for cond in pattern["conditions"]
        )
        if sig in existing_sigs:
            continue
        name_parts = []
        if c.get("theme"):
            name_parts.append(str(c["theme"]))
        if c.get("vpa_verdict"):
            name_parts.append(str(c["vpa_verdict"]))
        if c.get("institutional_present"):
            name_parts.append("机构")
        name = f"Auto: {'-'.join(name_parts)}"
        pid = create_playbook(name=name, pattern_json=pattern, today=today)
        existing_sigs.add(sig)
        created.append(pid)
        logger.info("Auto-created playbook #%d: %s (hits=%d/%d)",
                    pid, name, c["hits"], c["total"])
    return created
