"""Evidence-gated Trader learning: Candidate -> Lesson -> Rule.

The unit of support is a distinct reviewed experience (source_ref), not a number
the reviewing model claims. A single review can therefore never manufacture a
Lesson by writing support_count=100. Rules are versioned, expiring and
reversibly retired through append-only events.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import sqlite3

from alpha_agents.data import trader_learning as D, trader_session

LESSON_SUPPORT_FLOOR = 3
RULE_SUPPORT_FLOOR = 5
RULE_CONFIDENCE_FLOOR = 0.60
RULE_MAX_COUNTER_RATIO = 0.40
RULE_TTL_DAYS = 90


def _key(row: dict) -> str:
    identity = [
        str(row["claim"]).strip(),
        str(row["action"]).strip().lower(),
        str(row["applicable_context"]).strip(),
        str(row["evidence_timeframe"]).strip(),
        str(row["decision_horizon"]).strip(),
        str(row["evidence_scope"]).strip(),
    ]
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()


def _group_candidates(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    meta: dict[str, dict] = {}
    for row in rows:
        key = _key(row)
        # One reviewed experience is one vote. Exact retries or multiple
        # candidate rows pointing at the same decision/trade do not multiply it.
        grouped[key][str(row["source_ref"])] = row
        meta[key] = row

    out = []
    for key, by_ref in grouped.items():
        rows_for_key = list(by_ref.values())
        example = meta[key]
        support = len(rows_for_key)
        counter = sum(
            1 for row in rows_for_key
            if int(row.get("counterexample_count") or 0) > 0)
        confidence = (
            sum(float(row.get("confidence") or 0) for row in rows_for_key)
            / support if support else 0.0)
        out.append({
            "key": key,
            "example": example,
            "support": support,
            "counter": counter,
            "confidence": confidence,
            "candidate_ids": sorted(int(row["id"]) for row in rows_for_key),
        })
    return out


def advance(
    *, trader_id: str, as_of: str, run_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Materialize Lessons/Rules from repeated, independent candidates."""
    run = trader_session.namespace(run_id)
    candidates = D.lesson_candidates(
        run_id=run, trader_id=trader_id, up_to=as_of, conn=conn)
    made_lessons = 0
    made_rules = 0
    lesson_ids = []
    rule_ids = []

    for group in _group_candidates(candidates):
        if group["support"] < LESSON_SUPPORT_FLOOR:
            continue
        row = group["example"]
        lesson_id, created = D.save_lesson_version(
            run_id=run,
            trader_id=trader_id,
            lesson_key=group["key"],
            source_date=as_of,
            claim=row["claim"],
            action=row["action"],
            applicable_context=row["applicable_context"],
            evidence_timeframe=row["evidence_timeframe"],
            decision_horizon=row["decision_horizon"],
            evidence_scope=row["evidence_scope"],
            support_count=group["support"],
            counterexample_count=group["counter"],
            confidence=group["confidence"],
            evidence_refs=group["candidate_ids"],
            conn=conn,
        )
        lesson_ids.append(lesson_id)
        made_lessons += int(created)

        ratio = (
            group["counter"] / group["support"]
            if group["support"] else 1.0)
        if (group["support"] < RULE_SUPPORT_FLOOR
                or group["confidence"] < RULE_CONFIDENCE_FLOOR
                or ratio > RULE_MAX_COUNTER_RATIO):
            continue
        expires = (
            date.fromisoformat(as_of) + timedelta(days=RULE_TTL_DAYS)
        ).isoformat()
        rule_id, created = D.save_rule_version(
            run_id=run,
            trader_id=trader_id,
            rule_key=group["key"],
            lesson_id=lesson_id,
            source_date=as_of,
            claim=row["claim"],
            action=row["action"],
            applicable_context=row["applicable_context"],
            evidence_timeframe=row["evidence_timeframe"],
            decision_horizon=row["decision_horizon"],
            evidence_scope=row["evidence_scope"],
            support_count=group["support"],
            counterexample_count=group["counter"],
            confidence=group["confidence"],
            evidence_refs=group["candidate_ids"],
            expires_on=expires,
            conn=conn,
        )
        rule_ids.append(rule_id)
        made_rules += int(created)

    if conn is not None:
        conn.commit()
    return {
        "candidates": len(candidates),
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
