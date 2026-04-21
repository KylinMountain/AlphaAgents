"""Phase 4 — Evolution meta-metrics. Is the evolution system actually helping?

Pure SQL aggregates + Python counts, no LLM. Computed daily at review end.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    upsert_evolution_metrics,
    get_active_principles,
    get_all_principles_including_weakened,
    get_all_playbooks,
    get_recent_daily_lessons,
)

logger = logging.getLogger(__name__)


def _query_intraday_buckets(days: int = 7, as_of: str | None = None) -> dict:
    """SQL: verified intraday predictions bucketed by whether they matched a playbook.

    Deduplicates by (date, code) — the intraday monitor runs every 5 min and
    re-emits the same recommendation many times, which would otherwise inflate
    counts by 30x. We take the earliest record per (date, code) as the
    representative judgement for that stock on that day.

    ``as_of`` (YYYY-MM-DD) anchors the 7-day window end. Defaults to today.
    During replay, callers must pass the replay target_date so the metric
    reflects that historical day's perspective, not today's.

    A prediction is 'matched' if its features_json matches any currently active
    playbook. Since matching is a runtime decision not persisted, we re-run
    match_playbook here against today's active set — conservative but honest.
    """
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.evolution.playbook import match_playbook

    end = (datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now())
    cutoff = (end - timedelta(days=days)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    # One representative row per (date, code): earliest id wins.
    rows = _get_conn().execute(
        "SELECT features_json, hit FROM predictions "
        "WHERE id IN ("
        "    SELECT MIN(id) FROM predictions "
        "    WHERE report_type = 'intraday' AND hit IS NOT NULL "
        "      AND date >= ? AND date <= ? "
        "    GROUP BY date, code"
        ")",
        (cutoff, end_str),
    ).fetchall()

    total = hits = m_total = m_hits = u_total = u_hits = 0
    for r in rows:
        total += 1
        if r["hit"]:
            hits += 1
        try:
            features = json.loads(r["features_json"] or "{}")
        except json.JSONDecodeError:
            features = {}
        matched = bool(features and match_playbook(features))
        if matched:
            m_total += 1
            if r["hit"]:
                m_hits += 1
        else:
            u_total += 1
            if r["hit"]:
                u_hits += 1
    return {
        "intraday_hit_rate_7d": (hits / total) if total else 0.0,
        "intraday_count_7d": total,
        "matched_hit_rate_7d": (m_hits / m_total) if m_total else 0.0,
        "matched_count_7d": m_total,
        "unmatched_hit_rate_7d": (u_hits / u_total) if u_total else 0.0,
        "unmatched_count_7d": u_total,
    }


def compute_evolution_metrics(today: str) -> dict:
    """Aggregate a daily snapshot of evolution-system health and persist it.
    Returns the computed dict."""
    buckets = _query_intraday_buckets(days=7, as_of=today)

    active_pr = len(get_active_principles())
    all_pr = get_all_principles_including_weakened()
    weakened_pr = sum(1 for p in all_pr if p.get("status") == "weakened")

    playbooks = get_all_playbooks()
    active_pb = sum(1 for p in playbooks if p.get("status") == "active")
    degraded_pb = sum(1 for p in playbooks if p.get("status") == "degraded")

    lessons_7d = len(get_recent_daily_lessons(days=7))

    metrics = {
        **buckets,
        "active_principles": active_pr,
        "weakened_principles": weakened_pr,
        "active_playbooks": active_pb,
        "degraded_playbooks": degraded_pb,
        "lessons_count_7d": lessons_7d,
    }
    try:
        upsert_evolution_metrics(today, metrics)
    except Exception as e:
        logger.warning("Failed to persist evolution metrics: %s", e)
    return metrics


def get_evolution_metrics_trend(days: int = 30) -> list[dict]:
    """Re-export from memory_store so callers can import from one place."""
    from alpha_agents.data.memory_store import get_evolution_metrics_trend as _fn
    return _fn(days=days)


def format_metrics_trend(rows: list[dict]) -> str:
    """Pretty-print a trend for display in reports / chat."""
    if not rows:
        return "（尚无 evolution_metrics 数据）"
    lines = ["【进化系统自评】"]
    lines.append(
        f"{'date':<12} {'7d胜率':>8} {'匹配胜率':>10} {'未匹配':>10}"
        f" {'原则':>5} {'Playbook':>10} {'lessons':>8}"
    )
    for r in rows:
        matched = (f"{r['matched_hit_rate_7d']*100:.0f}%"
                   f"/{r['matched_count_7d']}")
        unmatched = (f"{r['unmatched_hit_rate_7d']*100:.0f}%"
                     f"/{r['unmatched_count_7d']}")
        lines.append(
            f"{r['date']:<12} "
            f"{r['intraday_hit_rate_7d']*100:>6.0f}%  "
            f"{matched:>10} "
            f"{unmatched:>10} "
            f"{r['active_principles']:>5d} "
            f"{r['active_playbooks']:>10d} "
            f"{r.get('lessons_count_7d', 0):>8d}"
        )
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        h_delta = (last["intraday_hit_rate_7d"] - first["intraday_hit_rate_7d"]) * 100
        lines.append(f"\n7d胜率变化：{h_delta:+.1f}pp （{first['date']} → {last['date']}）")
    return "\n".join(lines)
