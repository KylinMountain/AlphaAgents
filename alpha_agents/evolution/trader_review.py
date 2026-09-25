"""Review the continuous Trader's decisions without activating rules.

The review grades the decision at its own information boundary, execution
separately, and outcome separately. A useful "next time" statement becomes a
quarantined LessonCandidate carrying the decision's original timeframe,
horizon and evidence scope. Nothing in this module writes handbook/rules.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from alpha_agents.data import trader_learning, trader_session, trader_state_store

logger = logging.getLogger(__name__)

_TIMEOUT = 120

_INSTRUCTIONS = """你正在复盘同一个交易员今天已经封存的决策。

严格分开三件事：
1. decision_quality：只按决策当时已经可见的信息，评价判断是否合理；
2. execution_quality：评价挂单/成交/仓位执行，缺少执行事实就写 unknown；
3. outcome_quality：评价后来实际结果，缺少结果就写 unknown。

禁止用后来涨跌反写当时动机；禁止补造未记录的事实。WAIT/HOLD/REJECT 也是
决策，不能因为没有成交就跳过。若有一条具体、以后可检验的经验，可以给
lesson；否则 lesson=null。lesson 只能是候选经验，不是永久规则。

只输出 JSON：
{
  "reviews": [
    {
      "decision_id": "...",
      "decision_quality": "good|bad|uncertain",
      "execution_quality": "good|bad|unknown",
      "outcome_quality": "good|bad|unknown",
      "reason": "引用已给事实",
      "lesson": null 或 {
        "claim": "下次可检验的一句话",
        "action": "wait|buy|hold|reduce|sell|reject|adjust",
        "applicable_context": "适用场景",
        "support_count": 1,
        "counterexample_count": 0,
        "confidence": 0.5
      }
    }
  ]
}
"""


def _parse(text: str) -> dict:
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"reviews": []}
    try:
        value = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"reviews": []}
    if not isinstance(value, dict) or not isinstance(value.get("reviews"), list):
        return {"reviews": []}
    return value


def _decision_map(state, day: str) -> dict[str, dict]:
    return {
        item.decision_id: item.as_dict()
        for item in state.recent_decisions
        if item.made_at.date().isoformat() == day
    }


def _review_message(decisions: dict[str, dict], *, facts: str, record: str) -> str:
    return (
        "## 当天封存的决策\n\n"
        + json.dumps(list(decisions.values()), ensure_ascii=False, indent=2)
        + "\n\n## 当天市场事实\n\n"
        + (facts or "（没有更多可用市场事实）")
        + "\n\n## 当天执行/账户记录\n\n"
        + (record or "（没有更多执行记录）")
    )


async def review_decisions(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    day: str,
    model,
    facts: str = "",
    record: str = "",
    run_id: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Review today's persisted TraderDecisions and quarantine lessons."""
    run = trader_session.namespace(run_id)
    state = trader_state_store.load_latest(
        run_id=run, trader_id=trader_id, conn=conn)
    if state is None:
        return {"decision_reviews": 0, "lesson_candidates": 0}
    decisions = _decision_map(state, day)
    if not decisions or model is None:
        return {"decision_reviews": 0, "lesson_candidates": 0}

    from agents import Agent
    from alpha_agents.model_factory import run_agent

    try:
        result = await run_agent(
            Agent(
                name="trader_decision_review",
                instructions=_INSTRUCTIONS,
                model=model,
                tools=[],
            ),
            _review_message(decisions, facts=facts, record=record),
            max_turns=2,
            timeout=timeout if timeout is not None else _TIMEOUT,
            label="trader_decision_review",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Trader decision review [%s/%s] failed: %s",
            trader_id, day, exc)
        return {"decision_reviews": 0, "lesson_candidates": 0}

    parsed = _parse(result.final_output or "")
    review_count = 0
    candidate_count = 0
    seen: set[str] = set()

    for row in parsed.get("reviews") or []:
        if not isinstance(row, dict):
            continue
        decision_id = str(row.get("decision_id") or "").strip()
        decision = decisions.get(decision_id)
        if decision is None or decision_id in seen:
            continue
        seen.add(decision_id)

        decision_quality = str(row.get("decision_quality") or "").strip()
        execution_quality = str(row.get("execution_quality") or "").strip()
        outcome_quality = str(row.get("outcome_quality") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if decision_quality not in {"good", "bad", "uncertain"}:
            continue
        if execution_quality not in {"good", "bad", "unknown"}:
            continue
        if outcome_quality not in {"good", "bad", "unknown"}:
            continue
        if not reason:
            continue

        saved = trader_learning.save_decision_review(
            run_id=run,
            trader_id=trader_id,
            decision=decision,
            review_date=day,
            decision_quality=decision_quality,
            execution_quality=execution_quality,
            outcome_quality=outcome_quality,
            reason=reason,
            payload={
                "facts": facts,
                "record": record,
                "review": row,
            },
            conn=conn,
        )
        review_count += int(saved)

        lesson = row.get("lesson")
        if not saved or not isinstance(lesson, dict):
            continue
        claim = str(lesson.get("claim") or "").strip()
        action = str(lesson.get("action") or "").strip().lower()
        applicable = str(lesson.get("applicable_context") or "").strip()
        if not claim or not action or not applicable:
            continue
        try:
            support = int(lesson.get("support_count", 1))
            oppose = int(lesson.get("counterexample_count", 0))
            confidence = float(lesson.get("confidence", 0.5))
        except (TypeError, ValueError):
            continue
        trader_learning.save_lesson_candidate(
            run_id=run,
            trader_id=trader_id,
            source_type="decision_review",
            source_ref=decision_id,
            source_date=day,
            claim=claim,
            action=action,
            applicable_context=applicable,
            evidence_timeframe=decision["timeframe"],
            decision_horizon=decision["decision_horizon"],
            evidence_scope=decision["evidence_scope"],
            support_count=support,
            counterexample_count=oppose,
            confidence=confidence,
            evidence={
                "decision_id": decision_id,
                "decision": decision,
                "qualities": {
                    "decision": decision_quality,
                    "execution": execution_quality,
                    "outcome": outcome_quality,
                },
            },
            conn=conn,
        )
        candidate_count += 1

    conn.commit()
    return {
        "decision_reviews": review_count,
        "lesson_candidates": candidate_count,
    }


def lessons_from_trade_reviews(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    day: str,
    run_id: str | None = None,
) -> int:
    """Turn completed trade "next time" notes into quarantined candidates."""
    from alpha_agents.evolution import replay_mode, trade_review

    run = trader_session.namespace(run_id)
    scope = (
        "replay_daily" if replay_mode.replay_process() else "live_daily")
    count = 0
    for facts, words in trade_review.reviews_for(
            conn, trader_id, up_to=day):
        claim = str(words.get("next_time") or "").strip()
        if not claim:
            continue
        position_id = facts.get("position_id")
        source_date = str(facts.get("close_date") or day)
        context = " / ".join(
            value for value in (
                str(facts.get("theme") or "").strip(),
                str(facts.get("code") or "").strip(),
            ) if value) or "closed trade"
        trader_learning.save_lesson_candidate(
            run_id=run,
            trader_id=trader_id,
            source_type="trade_review",
            source_ref=f"position:{position_id}",
            source_date=source_date,
            claim=claim,
            action="adjust",
            applicable_context=context,
            evidence_timeframe="1d",
            decision_horizon="3-5d",
            evidence_scope=scope,
            support_count=1,
            counterexample_count=0,
            confidence=0.5,
            evidence={
                "position_id": position_id,
                "return_pct": facts.get("return_pct"),
                "review": words,
            },
            conn=conn,
        )
        count += 1
    conn.commit()
    return count


def lessons_from_market_review(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    day: str,
    run_id: str | None = None,
) -> int:
    """Quarantine executable board lessons from today's market review."""
    from alpha_agents.evolution import market_review, replay_mode

    market_review.ensure(conn)
    row = conn.execute(
        "SELECT review_json FROM market_reviews "
        "WHERE trader_id=? AND date=?",
        (trader_id, day)).fetchone()
    if row is None:
        return 0
    try:
        review = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return 0

    run = trader_session.namespace(run_id)
    scope = (
        "replay_daily" if replay_mode.replay_process() else "live_daily")
    count = 0
    for index, board in enumerate(review.get("boards") or []):
        if not isinstance(board, dict):
            continue
        claim = str(
            board.get("lesson") or board.get("next_time") or "").strip()
        name = str(board.get("name") or "").strip()
        if not claim or not name:
            continue
        kind = str(board.get("kind") or "").strip()
        trader_learning.save_lesson_candidate(
            run_id=run,
            trader_id=trader_id,
            source_type="market_review",
            source_ref=f"market:{day}:{index}:{name}",
            source_date=day,
            claim=claim,
            action="adjust",
            applicable_context=(
                f"{kind} / {name}" if kind else name),
            evidence_timeframe="1d",
            decision_horizon="3-5d",
            evidence_scope=scope,
            support_count=1,
            counterexample_count=0,
            confidence=0.5,
            evidence={"board": board},
            conn=conn,
        )
        count += 1
    conn.commit()
    return count
