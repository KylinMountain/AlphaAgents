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
  key fact today is a gap, not a number: 202 predictions, 170 with ``hit``,
  and zero with ``brier``. The evaluation currency does not exist yet, which
  is why the gate abstains and why this section leads with that instead of a
  hit rate that would look like progress.
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


def _read_forecasts() -> tuple[dict, int]:
    """Scored forecasts, and how much of the table the evaluator can price.

    Two different denominators, named apart on purpose: ``scored`` counts rows
    with a ``hit``, ``brier_scored`` counts rows with a ``brier``. Their
    difference *is* the D7-shaped gap — the gate needs a Brier score to
    compare a challenger, and cannot get one.
    """
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) rows, SUM(hit IS NOT NULL) scored, "
        "  SUM(brier IS NOT NULL) brier_scored FROM predictions").fetchone()
    totals = {k: int(v or 0) for k, v in zip(
        ("rows", "scored", "brier_scored"), tuple(row))}
    totals["recent_hit_rate"] = memory_store.get_prediction_stats(30)
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
            note=("`scored` 与 `brier_scored` 的差是当前最要紧的一个数字："
                  "没有 brier 就没有评估货币，闸门只能 abstain。"))),
    })
