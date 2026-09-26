"""Evidence-gated Trader learning: graded decisions -> Lesson -> Rule.

The unit of evidence is a decision the market has graded
(``decision_outcomes``), grouped by what the Trader did and the situation it
did it in — (action, situation tag, timeframe, horizon, scope). Nothing a
model writes enters the count: not a support number, not a confidence, and
not the wording of a claim.

It used to group candidates by the exact claim text a reviewing model wrote,
with the counts that model also wrote. Over a 30-day replay that produced 343
candidates and no Lesson — no two reviews ever phrased a finding the same way
— and the counts it would have promoted on were the model grading itself.

A cell becomes a Lesson once it has ``LESSON_MIN_N`` right/wrong verdicts and
leans clearly one way; a Rule once it has ``RULE_MIN_N`` (``AGENTS.md``:
n < 50 does not ship). A cell that is mostly *wrong* is a lesson too — "this
is usually a mistake" — and is phrased as one. Rules are versioned, expiring
and reversibly retired through append-only events.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import sqlite3
import statistics

from alpha_agents.data import trader_learning as D, trader_session

LESSON_MIN_N = 10
RULE_MIN_N = 50
#: How far from a coin flip a cell must lean, in either direction.
LEAN = 0.60
RULE_TTL_DAYS = 90

_ACTION_WORDS = {
    "buy": "买入", "add": "加仓", "hold": "持有", "sell": "卖出",
    "reduce": "减仓", "reject": "放弃", "wait": "等待",
}


def _cells(outcomes: list[dict]) -> dict[tuple, list[dict]]:
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for row in outcomes:
        if row["verdict"] == "flat":
            continue
        for tag in json.loads(row["tags_json"] or "[]"):
            cells[(row["action"], tag, row["evidence_timeframe"],
                   row["decision_horizon"], row["evidence_scope"])].append(row)
    return cells


def _key(cell: tuple, polarity: str) -> str:
    return hashlib.sha256(json.dumps(
        [*cell, polarity], ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim(action: str, tag: str, right: int, n: int, median_excess: float,
           favorable: bool) -> str:
    from alpha_agents.evolution.decision_outcomes import TAGS
    situation = TAGS.get(tag, tag)
    verb = _ACTION_WORDS.get(action, action)
    if favorable:
        return (f"{situation}时{verb}：行情判对 {right}/{n}，"
                f"中位超额 {median_excess:+.2f}pp")
    return (f"{situation}时{verb}：行情判错 {n - right}/{n}，"
            f"中位超额 {median_excess:+.2f}pp——这类决策多数时候是错的")


def advance(
    *, trader_id: str, as_of: str, run_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Materialize Lessons/Rules from market-graded decision cells."""
    run = trader_session.namespace(run_id)
    outcomes = D.decision_outcomes(
        run_id=run, trader_id=trader_id, up_to=as_of, conn=conn)
    made_lessons = made_rules = 0
    lesson_ids: list[int] = []
    rule_ids: list[int] = []

    for cell, rows in sorted(_cells(outcomes).items()):
        n = len(rows)
        if n < LESSON_MIN_N:
            continue
        right = sum(1 for row in rows if row["verdict"] == "right")
        rate = right / n
        if (1 - LEAN) < rate < LEAN:
            continue
        favorable = rate >= LEAN
        polarity = "favorable" if favorable else "unfavorable"
        action, tag, timeframe, horizon, scope = cell
        median_excess = statistics.median(
            float(row["excess_pct"]) for row in rows)
        claim = _claim(action, tag, right, n, median_excess, favorable)
        support = right if favorable else n - right
        counter = n - support
        confidence = support / n
        refs = [int(row["id"]) for row in rows]
        key = _key(cell, polarity)
        lesson_id, created = D.save_lesson_version(
            run_id=run, trader_id=trader_id, lesson_key=key,
            source_date=as_of, claim=claim, action=action,
            applicable_context=tag, evidence_timeframe=timeframe,
            decision_horizon=horizon, evidence_scope=scope,
            support_count=support, counterexample_count=counter,
            confidence=confidence, evidence_refs=refs, conn=conn)
        lesson_ids.append(lesson_id)
        made_lessons += int(created)
        if n < RULE_MIN_N:
            continue
        expires = (date.fromisoformat(as_of)
                   + timedelta(days=RULE_TTL_DAYS)).isoformat()
        rule_id, created = D.save_rule_version(
            run_id=run, trader_id=trader_id, rule_key=key,
            lesson_id=lesson_id, source_date=as_of, claim=claim,
            action=action, applicable_context=tag,
            evidence_timeframe=timeframe, decision_horizon=horizon,
            evidence_scope=scope, support_count=support,
            counterexample_count=counter, confidence=confidence,
            evidence_refs=refs, expires_on=expires, conn=conn)
        rule_ids.append(rule_id)
        made_rules += int(created)

    if conn is not None:
        conn.commit()
    return {
        "candidates": len(outcomes),
        "lessons_created": made_lessons,
        "rules_created": made_rules,
        "lesson_ids": lesson_ids,
        "rule_ids": rule_ids,
    }


def _latest(rows: list[dict], key_field: str) -> list[dict]:
    latest = {}
    for row in rows:
        key = row[key_field]
        if key not in latest or int(row["version"]) > int(latest[key]["version"]):
            latest[key] = row
    return list(latest.values())


def _rule_active(
    rule: dict, *, as_of: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    if rule["expires_on"] < as_of:
        return False
    events = D.rule_events(int(rule["id"]), conn=conn)
    if not events:
        return False
    return events[-1]["event"] in {"activate", "reinstate"}


def inject(
    *, trader_id: str, decision_horizon: str, as_of: str,
    run_id: str | None = None, conn: sqlite3.Connection | None = None,
) -> str:
    """Render only this run/trader's current Lessons and active Rules.

    Evidence timeframe/scope are printed verbatim. A daily replay lesson may
    inform an intraday decision, but it remains visibly daily evidence.
    """
    run = trader_session.namespace(run_id)
    lesson_rows = _latest(
        D.lessons(run_id=run, trader_id=trader_id, up_to=as_of, conn=conn),
        "lesson_key")
    rule_rows = _latest(
        D.rules(run_id=run, trader_id=trader_id, up_to=as_of, conn=conn),
        "rule_key")
    rule_rows = [
        row for row in rule_rows
        if row["decision_horizon"] == decision_horizon
        and _rule_active(row, as_of=as_of, conn=conn)
    ]
    active_keys = {row["rule_key"] for row in rule_rows}
    lesson_rows = [
        row for row in lesson_rows
        if row["decision_horizon"] == decision_horizon
        and row["lesson_key"] not in active_keys
    ]
    if not lesson_rows and not rule_rows:
        return ""

    lines = ["【Trader Runtime 经验（按证据域标注）】"]
    for row in lesson_rows:
        lines.append(
            f"• LESSON [{row['evidence_timeframe']} / "
            f"{row['evidence_scope']} / n={row['support_count']} / "
            f"反例={row['counterexample_count']}] "
            f"{row['applicable_context']} → {row['claim']}")
    for row in rule_rows:
        lines.append(
            f"• RULE v{row['version']} [{row['evidence_timeframe']} / "
            f"{row['evidence_scope']} / n={row['support_count']} / "
            f"反例={row['counterexample_count']} / 到期={row['expires_on']}] "
            f"{row['applicable_context']} → {row['claim']} "
            f"(action={row['action']})")
    return "\n".join(lines)


def retire_rule(
    rule_id: int, *, reason: str, at: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    events = D.rule_events(rule_id, conn=conn)
    if not events or events[-1]["event"] not in {"activate", "reinstate"}:
        raise ValueError("only an active Trader rule can be retired")
    return D.append_rule_event(
        rule_id, event="retire", reason=reason, at=at, conn=conn)


def reinstate_rule(
    rule_id: int, *, reason: str, at: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    events = D.rule_events(rule_id, conn=conn)
    if not events or events[-1]["event"] != "retire":
        raise ValueError("only a retired Trader rule can be reinstated")
    return D.append_rule_event(
        rule_id, event="reinstate", reason=reason, at=at, conn=conn)
