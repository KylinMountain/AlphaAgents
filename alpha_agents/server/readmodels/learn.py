"""Learn journal: what was decided, what it turned into, and what it taught.

Four sections. The separation is the point of §9, and it survives into the
read model: a decision can be a correct forecast and a losing trade at the
same time, so this page must never merge them into one "performance" figure.

* ``episodes`` — the decisions themselves, and the coverage answer the book
  could not give before: "we decided N times and filled M". Counted over
  episodes, so a decision that produced nothing is in both the numerator and
  the denominator.
* ``outcomes`` — the three label kinds side by side (forecast / trade /
  process), each with its own lifecycle. Labels are not merged.
* ``candidates`` — the quarantine: proposals, their five-state lifecycle, and
  the transitions that moved them.
* ``forecasts`` — the raw material the calibration curve is drawn from. Its
  key fact is a calendar, not a gap: a Brier score exists only after a
  forecast's evidence window (its own horizon, in trading days) has closed,
  and ``pricing`` reports each unscored batch's progress toward that — so
  "zero brier" reads as how much market is still owed, not as a defect.
  The one defect shape here is a window that closed with no score behind
  it, and ``pricing`` names that separately.
"""

from __future__ import annotations

import logging

from alpha_agents.server.readmodels import Need, Section, section, workspace

logger = logging.getLogger(__name__)


def _read_episodes() -> tuple[dict, int]:
    from alpha_agents.data import episodes as ep
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    live = ep.open_episodes(conn, limit=100)
    return {
        "coverage": ep.coverage(conn),
        "open": live,
    }, len(live)


def _read_outcomes() -> tuple[dict, int]:
    from alpha_agents.data import memory_store
    from alpha_agents.data import outcomes as oc
    conn = memory_store._get_conn()
    counts = oc.counts(conn)
    rows = sum(sum(states.values()) for states in counts.values())
    return {
        "counts": counts,
        "pending": oc.pending_labels(conn, limit=200),
        "integrity": oc.integrity(conn),
    }, rows


def _read_candidates() -> tuple[dict, int]:
    from alpha_agents.data import learning_candidates as lc
    counts = lc.counts()
    rows = sum(counts.values())
    return {
        "counts": counts,
        "integrity": lc.integrity(),
    }, rows


def _pricing(conn, default_horizon: int) -> dict:
    """The unscored probabilistic rows, split by the state of their window.

    Three outcomes, and the page must not merge them: a window still open is
    **maturity** (the design's own calendar — D10), a window closed with no
    score is the **review step not having reached the row** (the only defect
    shape here), and an unreadable archive is a **refusal to claim** — no
    progress bar over a market nobody can see. The window arithmetic comes
    from ``scoring.window_progress``, so the page can never disagree with
    the grader about when a window shuts.

    The fallback horizon is the same one the grader uses
    (``DEFAULT_HORIZON_DAYS`` for rows written before horizons were
    declared) — otherwise the page would measure a different window than
    the score that eventually lands on the row.
    """
    from alpha_agents.data.scoring import window_progress
    rows = conn.execute(
        "SELECT date, horizon_days, COUNT(*) rows FROM predictions "
        "WHERE prob IS NOT NULL AND brier IS NULL "
        "GROUP BY date, horizon_days ORDER BY date").fetchall()
    batches: list[dict] = []
    ripe = unripe = 0
    readable = True
    min_remaining: int | None = None
    for row in rows:
        horizon = row["horizon_days"] or default_horizon
        batch: dict = {"date": row["date"], "rows": int(row["rows"]),
                       "horizon": horizon, "need": horizon + 1,
                       "have": None, "remaining": None}
        progress = window_progress(row["date"], horizon)
        if progress is None:
            readable = False
        else:
            batch["have"] = progress["have"]
            batch["remaining"] = progress["remaining"]
            if progress["closed"]:
                ripe += batch["rows"]
            else:
                unripe += batch["rows"]
                if min_remaining is None or \
                        progress["remaining"] < min_remaining:
                    min_remaining = progress["remaining"]
        batches.append(batch)
    return {"unscored": sum(b["rows"] for b in batches),
            "ripe_unscored": ripe, "unripe": unripe,
            "min_remaining_days": min_remaining,
            "archive_readable": readable, "batches": batches}


def _read_forecasts() -> tuple[dict, int]:
    """Scored forecasts, and how much of the table the evaluator can price.

    Three denominators, named apart on purpose: ``scored`` counts rows with
    a ``hit``, ``brier_scored`` counts rows with a ``brier``, and
    ``prob_rows`` counts rows a Brier score could ever apply to — a row
    without a probability is not a pending evaluation, it is not in this
    population at all, and counting it into the gap manufactures 184
    evaluations that nobody ever made. ``pricing`` then splits the unscored
    probabilistic rows by batch (see :func:`_pricing`).
    """
    from alpha_agents.data import memory_store
    from alpha_agents.data.scoring import DEFAULT_HORIZON_DAYS
    conn = memory_store._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) rows, SUM(hit IS NOT NULL) scored, "
        "  SUM(brier IS NOT NULL) brier_scored, "
        "  SUM(prob IS NOT NULL) prob_rows FROM predictions").fetchone()
    totals = {k: int(v or 0) for k, v in zip(
        ("rows", "scored", "brier_scored", "prob_rows"), tuple(row))}
    totals["recent_hit_rate"] = memory_store.get_prediction_stats(30)
    totals["pricing"] = _pricing(conn, DEFAULT_HORIZON_DAYS)
    return totals, totals["rows"]


def snapshot() -> dict:
    """The learn journal."""
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    return workspace("learn", {
        "episodes": section(conn, Section(
            source="episodes + episode_events",
            needs=(Need("episodes", (
                        "trader_id", "code", "status", "intent_id",
                        "order_id", "position_id", "opened_at")),
                   Need("episode_events", ("episode_id", "kind", "ref_id"))),
            read=_read_episodes,
            note=("episode 的写者是 intent.submit_intent 与 portfolio 的成交/撤单钩子，"
                  "所以「决策 N 次、成交 M 次」在这里而不是在持仓表里。"))),
        "outcomes": section(conn, Section(
            source="outcomes",
            needs=(Need("outcomes", (
                "kind", "state", "subject_type", "subject_id", "episode_id",
                "supersedes_id", "evidence_json")),),
            read=_read_outcomes,
            note=("三类标签互不覆盖：forecast / trade / process 各有自己的生命周期，"
                  "这一页不把它们并成一个数字。"))),
        "candidates": section(conn, Section(
            source="learning_candidates + candidate_transitions",
            needs=(Need("learning_candidates", (
                        "status", "entity_type", "evidence_episode_ids")),
                   Need("candidate_transitions", (
                        "candidate_id", "from_status", "to_status", "actor"))),
            read=_read_candidates,
            note=("候选生命周期**刻意**不被管线驱动：advance_candidate 是 status 的唯一"
                  "写者，只有人或脚本能推进它。「验证不等于授权」的代价就是这里长期为空。"))),
        "forecasts": section(conn, Section(
            source="predictions",
            needs=(Need("predictions", (
                "date", "report_type", "hit", "brier", "confidence")),),
            read=_read_forecasts,
            note=("brier 只在证据窗口收口后写（`scoring.evidence_window_closed` "
                  "是唯一判据），所以「已有 Brier = 0」在窗口未收口时是**成熟度**，"
                  "不是缺口 —— `pricing` 按批给出还差几个交易日；"
                  "窗口收了还没分才是要修的。"))),
    })
