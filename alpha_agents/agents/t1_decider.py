"""The T+1 daily decider — the panel the agent may choose from, and its orders.

Why this is a module and not a function inside the runner
---------------------------------------------------------
``scripts/walk_forward.py`` owns the calendar, the settlement and the report.
It deliberately owns no trading logic. A decider is trading logic: what the
agent is shown, what it is allowed to name, and what happens when it names
something it was not shown. That belongs on the ``agents`` layer where it can
be read and tested without a corpus, a replay directory or a clock.

What "as of" means here
-----------------------
The decision stands at 09:00 on ``day``. It is given:

* a **panel** — the securities it may order, drawn from the previous session's
  bars. This is the whole universe rule: *anything not on the panel is
  refused*. A model asked about a 2020 window has read 2026 and will name a
  security that had not listed; the panel is what makes that a refusal rather
  than a purchase.
* a **news window** ending at 09:00 on ``day``.
* its own book and the knowledge in force.

It is *not* given today's bar, and it cannot ask for one: this module has no
corpus handle, so the day it trades is not reachable from here at all.

Tools, and why they are not a leak
----------------------------------
The decider used to run with ``tools=[]`` and ``max_turns=1``: it read a
package the platform had assembled (panel, breadth, news, book) and picked
from it. It could not say "I am unsure about this one — let me check three
things". That is the gap between a driver with a dashboard and one who can
turn their head.

The six tools in ``alpha_agents.tools.trader_tools`` close it. They are not
a hole in the "no today's bar" rule above, and the reason is that they do not
carry the date: each reads ``evolution.replay_mode``'s as-of and truncates
there. Under a replay the as-of is the decision instant, so a 09:00 call sees
yesterday's close and a 14:55 call sees the session so far — the same boundary
the rest of the run obeys. Outside a replay the as-of is None and they answer
from now, which is what a live scan wants.

The boundary is enforced twice: ``tests/test_trader_tools_time_travel.py``
drives each tool at two clocks over known data, and each tool declares its
time column in ``trader_tools.AS_OF_FIELDS``.

What it refuses
---------------
A code outside the panel, a malformed zone (``entry_low``, when given, not
below ``entry_high``), a stop that is not below the entry, or a reply that is not
JSON. Every refusal is **returned as a value**, not swallowed: the runner
counts them and the report prints them, because a decider that quietly
returns fewer orders looks identical to a decider that found nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from alpha_agents.agents.json_reply import picks_line
import re
import time
from pathlib import Path

from agents import Agent, Runner

from alpha_agents.model_factory import run_agent
from agents.exceptions import MaxTurnsExceeded

from alpha_agents.config import PROMPTS_DIR
from alpha_agents.data.decision_frame import (
    DecisionFrame, FrameError, TraderState, fingerprint,
)
from alpha_agents.data.decision_capture import Capture
from alpha_agents.trader import Condition, TraderRuntimeError

logger = logging.getLogger(__name__)

#: Names the decider in the run report and in every order's ``reason`` prefix.
DECIDER_NAME = "t1_llm"

#: Turns one decision may take, including tool round-trips.
#:
#: The number lives here and nowhere else. A tool call costs a turn, so this
#: is ``1 + tool budget``.
#:
#: **Sized from measurement, twice.** The first tool-enabled recording showed
#: three rounds of up to six parallel calls; 8 was set from that. A 3-day
#: window then measured what 8 actually bought: 211 tool calls across 9
#: decisions — ~23 per decision — with only **one** repeated (tool, arguments)
#: pair among them, so it was reconnoitering different names rather than
#: looping. Even so, 5 of 9 decisions exhausted the budget, and a decision
#: that never answers is a day with no order.
#:
#: 24 is that measurement taken seriously rather than trimmed: the observed
#: worst case was 15 cumulative calls, and reconnaissance over a 40-name
#: panel is legitimately deep. It stays a ceiling — a model that loops still
#: cannot spend the window — and the report now counts tool calls so the cost
#: of a decision is visible next to its result.
#:
#: It was briefly declared twice — here and in the runner's ``--max-turns``.
#: The two disagreed (8 against 3), the runner always passes its own value,
#: so the value here was unreachable: a 20-day window spent 2.5 hours and
#: produced 40 unreadable decisions and no trades. The runner now defaults to
#: ``None`` and defers to this constant, and a test asserts nothing overrides
#: it accidentally.
DEFAULT_MAX_TURNS = 24

#: The prompt is the *user message*, not the system instructions: it is the
#: task, and it holds every placeholder the runner fills. Kept in a file so a
#: prompt change does not need a code change — and so the text the model was
#: given can be read by someone who does not read Python.
PROMPT_FILE = "t1_decide.md"

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

#: A fenced block anywhere in the reply, prose before and after allowed. Used
#: to find the answer inside a model's narration — see `_strip_fence`.
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

SYSTEM_INSTRUCTIONS = (
    "你是一名 A 股日频交易员。你只根据用户消息里给出的信息做决定，"
    "不使用你记忆中的任何具体行情或个股事实。"
    "输出必须是单个 JSON 对象，没有其它文字。"
)


class DeciderError(RuntimeError):
    """The decider could not produce a usable answer at all."""


def _condition_vocabulary() -> str:
    """The invalidation kinds an agent may write, from the evaluator's table."""
    from alpha_agents.data.thesis import prompt_vocabulary
    return prompt_vocabulary()


def load_prompt(path: Path | None = None) -> str:
    """The decision prompt template."""
    return (path or (PROMPTS_DIR / PROMPT_FILE)).read_text(encoding="utf-8")


def format_panel(panel: list[dict]) -> str:
    """Render the allowed securities, one per line, with what is knowable.

    The columns are what a person would look at before deciding, minus the
    ones this system does not have. 换手率 and 概念 were added after a 20-day
    replay in which every order was a bare `code / close / change / adv20`
    row — five numbers and no way to tell a small-cap theme stock from a
    large-cap blue chip, which are two different bets.

    概念 is labelled **当前成分** because the membership table has no as-of
    date: it is what the name is tagged with now, not what it was tagged with
    on the replayed session. Saying so in the header is the honest minimum;
    dropping the column would cost the sector context that A-share judgement
    mostly runs on.
    """
    if not panel:
        return "（今天没有可交易的候选）"
    sector_first = any(row.get("primary_theme") for row in panel)
    if sector_first:
        lines = [
            "| 代码 | 名称 | T-1 收盘 | T-1 涨幅 | 换手% | ADV20(手) | 连板 | 主力净额(万) | 主方向去自身5日相对 | 主方向 | 辅方向 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    else:
        lines = [
            "| 代码 | 名称 | T-1 收盘 | T-1 涨幅 | 换手% | ADV20(手) | 连板 | 主力净额(万) | 概念（当前成分） |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    for row in panel:
        adv = row.get("adv20")
        turn = row.get("turnover_rate")
        concepts = row.get("concepts") or []
        streak = row.get("consecutive_limits")
        net = row.get("net_amount")
        if sector_first:
            primary = row.get("primary_theme") or "-"
            supporting = row.get("supporting_themes") or []
            peer_rel = row.get("primary_theme_peer_relative_5d_pct")
            peer_covered = row.get("primary_theme_peer_covered")
            peer_total = row.get("primary_theme_peer_total")
            peer_text = (
                "-"
                if peer_rel is None
                else f"{peer_rel:+.2f}% ({peer_covered}/{peer_total})"
            )
            tail = (
                f"{peer_text} | {primary} | "
                f"{'、'.join(supporting) if supporting else '-'} |"
            )
        else:
            tail = f"{'、'.join(concepts) if concepts else '-'} |"
        lines.append(
            f"| {row['code']} | {row.get('name', '')} | {row.get('close')} | "
            f"{row.get('change_pct')}% | "
            f"{'-' if turn is None else turn} | "
            f"{'-' if adv is None else int(adv)} | "
            f"{'-' if not streak else str(int(streak)) + '板'} | "
            f"{'-' if net is None else f'{net:+,.0f}'} | {tail}")
    return "\n".join(lines)


def format_market(state: dict) -> str:
    """The previous session's breadth, or an explicit absence.

    Answers "what kind of day am I deciding into". The advancer share in the
    first replay window ran from 8.1% to 78.3%, and the model was never told
    which — every day looked the same from inside its prompt.
    """
    if not state:
        return "（未提供：本次运行没有可用的市场宽度数据）"
    return (
        f"上一交易日 {state.get('prev_day')}："
        f"上涨家数 {state.get('advancers_pct')}%，"
        f"全市场中位涨幅 {state.get('median_change_pct')}%，"
        f"涨停 {state.get('limit_up')} 家 / 跌停 {state.get('limit_down')} 家"
        f"（样本 {state.get('n')} 只）")


def format_news(items: list[dict]) -> str:
    """Render the news window. Empty is stated, not left blank."""
    if not items:
        return "（截至今日 09:00，没有读到任何快讯）"
    return "\n".join(
        f"• {n.get('time', '')} [{n.get('source', '')}] {n.get('title', '')}"
        for n in items)


def _strip_fence(text: str) -> str:
    """Pull the JSON object out of a reply, fences and prose included.

    Not a nicety: an unreadable reply is indistinguishable in the report from
    a model that chose to order nothing, and stripping is the difference
    between "it said no" and "we could not read it".

    Three shapes, and the third is the one that cost 14 decisions:

    1. a bare object — returned as is;
    2. the whole reply wrapped in a ```json fence — the fence comes off;
    3. **prose followed by a fenced object.** This is what a tool-using model
       does: it narrates ("Let me finalize my decision… Rationale summary:")
       and then emits the answer. The first version only handled case 2,
       because it tested ``startswith("```")`` — so once tools made the model
       explain itself, 14 of 20 buy decisions parsed as garbage while their
       replies contained a perfectly valid order. The runner counted them as
       ``decider_unreadable`` and placed nothing.

    The last fenced block wins, because a model that thinks out loud may
    quote a malformed example early and put its real answer at the end.
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if stripped.startswith("```"):
        return _FENCE_RE.sub("", stripped).strip()
    fences = _FENCE_BLOCK.findall(stripped)
    for block in reversed(fences):
        body = block.strip()
        if body.startswith("{") or body.startswith("["):
            return body
    # No fence: an object embedded in prose. Anchored on the last `{` that
    # closes at the end of the text, so an example quoted mid-answer is not
    # mistaken for the reply.
    return _trailing_json_object(stripped)


def _trailing_json_object(text: str) -> str:
    """The last balanced ``{...}`` at the end of ``text``, or ``text``.

    Returns ``text`` unchanged when it already looks like JSON or when no
    balanced object is found — the caller's ``json.loads`` then produces the
    real error, rather than this helper inventing one.
    """
    end = text.rfind("}")
    if end == -1:
        return text
    start = text.rfind("{", 0, end + 1)
    while start != -1:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            start = text.rfind("{", 0, start)
            continue
        return candidate
    return text


def _parse_watch_items(payload: dict, panel_codes: set[str]) -> tuple[list[dict], str | None]:
    """Normalize WAIT decisions into the Trader Runtime condition grammar."""
    raw_watch = payload.get("watch", [])
    if not isinstance(raw_watch, list):
        return [], "watch must be a list"

    watch: list[dict] = []
    seen: set[str] = set()
    for item in raw_watch:
        if not isinstance(item, dict):
            return [], "watch entries must be objects"
        code = item.get("code")
        reason = item.get("reason")
        next_check = item.get("next_check")
        invalidations = item.get("invalidations", [])
        if (not isinstance(code, str) or code not in panel_codes or code in seen
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(next_check, list) or not next_check
                or not isinstance(invalidations, list)):
            return [], (
                "watch needs a unique panel code, non-empty reason, "
                "non-empty next_check list and invalidations list")
        try:
            checks = [Condition.from_dict(value).as_dict() for value in next_check]
            invalid = [Condition.from_dict(value).as_dict()
                       for value in invalidations]
            confidence = float(item.get("confidence", 0.5))
        except (KeyError, TypeError, ValueError, TraderRuntimeError):
            return [], "watch contains an invalid condition or confidence"
        if not 0 <= confidence <= 1:
            return [], "watch confidence must be in [0, 1]"
        seen.add(code)
        watch.append({
            "code": code,
            "reason": reason.strip(),
            "confidence": confidence,
            "next_check": checks,
            "invalidations": invalid,
        })
    return watch, None


def parse_orders(text: str, panel_codes: set[str]) -> dict:
    """Turn the model's reply into orders, naming every refusal.

    Returns ``{"orders": [...], "refused": [...], "parse_error": str | None}``.
    ``refused`` entries are ``{"code", "why", "detail"}`` so the runner can
    count them by reason rather than only knowing that something was dropped.
    """
    refused: list[dict] = []
    body = _strip_fence(text or "")
    if not body:
        return {"orders": [], "refused": [], "parse_error": "empty reply"}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"orders": [], "refused": [],
                "parse_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
        return {"orders": [], "refused": [],
                "parse_error": "reply is not an object with an 'orders' list"}

    # An abstention is a decision too. Never infer its rationale from the
    # absence of orders, and never resample until the model agrees to buy.
    explanation = payload.get("no_trade_reason")
    if explanation is None:
        explanation = payload.get("reason")  # Legacy top-level spelling.
    explanation = explanation.strip() if isinstance(explanation, str) else ""
    watch, watch_error = _parse_watch_items(payload, panel_codes)
    rejected = []
    raw_rejected = payload.get("rejected", [])
    explanation_error = watch_error
    if not isinstance(raw_rejected, list):
        explanation_error = "rejected must be a list"
    else:
        seen = set()
        for item in raw_rejected:
            if not isinstance(item, dict):
                explanation_error = "rejected entries must be objects"
                break
            code, reason = item.get("code"), item.get("reason")
            ids = item.get("rule_ids", [])
            if (not isinstance(code, str) or code not in panel_codes or code in seen
                    or not isinstance(reason, str) or not reason.strip()
                    or not isinstance(ids, list)
                    or any(not isinstance(rid, str) or not rid.strip() for rid in ids)):
                explanation_error = "rejected needs a unique panel code, reason and rule_ids list"
                break
            seen.add(code)
            rejected.append({"code": code, "reason": reason.strip(),
                             "rule_ids": [rid.strip() for rid in ids]})
    if not payload["orders"] and not watch and not explanation:
        explanation_error = (
            "empty orders without watch require a non-empty no_trade_reason")
    if explanation_error:
        return {"orders": [], "watch": watch, "refused": [],
                "parse_error": explanation_error,
                "decision_status": "incomplete", "no_trade_reason": explanation,
                "rejected": rejected}

    orders: list[dict] = []
    for i, raw in enumerate(payload["orders"]):
        if not isinstance(raw, dict):
            refused.append({"code": "", "why": "not_an_object", "detail": str(i)})
            continue
        code = str(raw.get("code") or "").strip()
        if code not in panel_codes:
            # The one that matters: a security the decision was not shown.
            refused.append({"code": code, "why": "outside_panel", "detail": ""})
            continue
        # ``entry_low`` is optional. An order is "the most I will pay"; a
        # floor under it is a choice the trader may make (a breakout that only
        # counts above a level), not a shape the system imposes. The forced
        # two-sided band was the rule that kept orders out: a gap *down*,
        # cheaper than the trader asked for, fell under the floor and did
        # not fill.
        try:
            raw_low = raw.get("entry_low")
            low = (None if raw_low is None or raw_low == ""
                   else float(raw_low))
            high = float(raw["entry_high"])
            stop = float(raw["stop_loss"])
        except (KeyError, TypeError, ValueError) as exc:
            refused.append({"code": code, "why": "bad_prices",
                            "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if low is not None and not low < high:
            refused.append({"code": code, "why": "inverted_zone",
                            "detail": f"{low} !< {high}"})
            continue
        floor = low if low is not None else high
        if not stop < floor:
            refused.append({"code": code, "why": "stop_not_below_entry",
                            "detail": f"{stop} !< {floor}"})
            continue
        if min(floor, high) <= 0 or stop <= 0:
            refused.append({"code": code, "why": "non_positive_price",
                            "detail": str(floor)})
            continue
        # ``target_price`` is optional, and its absence is meaningful rather
        # than an error: a position with no target has exactly one exit, the
        # stop. That is what every order in the first 20-day replay looked
        # like, and it is why all 11 exits were stops.
        target = raw.get("target_price")
        if target is not None and target != "":
            try:
                target = float(target)
            except (TypeError, ValueError):
                refused.append({"code": code, "why": "bad_target",
                                "detail": str(target)[:40]})
                continue
            if not target > high:
                # A target at or below the entry ceiling is not a target: the
                # order would exit at a price it could have been filled at,
                # which is a stop with a different name.
                refused.append({"code": code, "why": "target_not_above_entry",
                                "detail": f"{target} !> {high}"})
                continue
            target = round(target, 2)
        else:
            target = None
        orders.append({
            "code": code,
            "entry_low": round(low, 2) if low is not None else None,
            "entry_high": round(high, 2), "stop_loss": round(stop, 2),
            "target_price": target,
            # Whole. This string becomes the Thesis ``claim`` — the thing a
            # later exit declares falsified and the review scores the
            # reasoning of. Cut at 200 it stopped mid-clause: the 002230
            # thesis written on the first autonomous run ends at "…也是我能
            # 确认的最近真实买盘区；", on a semicolon, with the rest of the
            # argument gone. Rendering widths belong to the renderer.
            "reason": str(raw.get("reason") or "").strip(),
            # The thesis half. Absent means "the agent did not say", which
            # the caller renders as its trader's default — not as zero.
            **_thesis_fields(raw, code),
        })
    ordered_codes = {order["code"] for order in orders}
    watch_codes = {item["code"] for item in watch}
    rejected_codes = {item["code"] for item in rejected}
    if (ordered_codes & watch_codes or ordered_codes & rejected_codes
            or watch_codes & rejected_codes):
        return {"orders": [], "watch": watch, "refused": refused,
                "parse_error": (
                    "a code cannot be simultaneously ordered, watched or rejected"),
                "decision_status": "incomplete", "no_trade_reason": explanation,
                "rejected": rejected}

    if orders:
        status = "ordered"
    elif watch:
        status = "waiting"
    elif rejected or refused:
        status = "refused"
    else:
        status = "abstained"
    return {"orders": orders, "watch": watch, "refused": refused,
            "parse_error": None, "decision_status": status,
            "no_trade_reason": explanation, "rejected": rejected}


#: What the agent may state about a position beyond its price plan. Each is
#: optional and each falls back to the trader's own configuration, because a
#: plan written before sizing was a decision must keep working.
#:
#: ``size_pct`` has no upper bound. It had 0.5, and the bound was worse than
#: it looks: an out-of-range value is *dropped*, so an agent asking for 60%
#: of the book got the trader's 3% default and no indication that its answer
#: had been replaced. "How much to bet" is the decision this design exists to
#: hand over, and a ceiling a person picked is the same mechanical rail the
#: stop-loss was. What stops a position from exceeding the account is cash
#: and T+1 settlement, which ``reservations`` enforces because they are facts
#: about the market rather than opinions about risk.
#:
#: The floor stays above zero: an order for 0% of the book is not an order,
#: and reading it as one would put a row in the book that can never fill.
_THESIS_BOUNDS = {
    "size_pct": (0.0001, None),    # share of the whole book; no ceiling
    "prob": (0.0, 1.0),
    "conviction": (0.0, 1.0),
    "horizon_days": (1, 60),
}


def _thesis_fields(raw: dict, code: str) -> dict:
    """Size, probability, horizon and invalidations, or nothing.

    These are what turn an order into a thesis, and the thesis is what makes
    sizing and exits the agent's own decisions rather than constants in a
    file: ``portfolio_sizing._wanted_pct`` reads ``size_pct`` from the active
    thesis and only falls back to ``traders/*.yaml`` when there is none, and
    ``thesis.evaluate`` is the code path that decides a claim has been
    falsified without asking a model.

    Out-of-range numbers are dropped rather than clamped, for the fields that
    still have a range. A clamp would let a model asking for 90% of the book
    quietly become the sanity limit and read, afterwards, as if it had asked
    for that — which is exactly what ``size_pct``'s old 0.5 ceiling did, only
    it substituted the trader's 3% default instead. ``size_pct`` now has no
    ceiling: what the agent says is what the position is.
    """
    out: dict = {}
    for field, (lo, hi) in _THESIS_BOUNDS.items():
        if raw.get(field) in (None, ""):
            continue
        try:
            value = float(raw[field])
        except (TypeError, ValueError):
            logger.warning("%s: %s is not a number (%r) — ignored",
                           code, field, raw[field])
            continue
        if value < lo or (hi is not None and value > hi):
            logger.warning("%s: %s=%s outside [%s, %s] — ignored",
                           code, field, value, lo,
                           "∞" if hi is None else hi)
            continue
        out[field] = int(value) if field == "horizon_days" else value

    from alpha_agents.data.thesis import validate_condition
    conditions = []
    for item in (raw.get("invalidations") or []):
        cond = validate_condition(item)
        if cond is not None:
            conditions.append(cond.as_dict())
    if conditions:
        out["invalidations"] = conditions
    return out


#: The two moments a buy decision can be taken at, and what each may see.
#:
#: The user's model: the open decision sees yesterday's close plus today's
#: open and fills at the open; the close decision sees the whole session and
#: fills at the close. Slippage is ignored, per the same instruction.
_SESSIONS = {
    "open": {
        "session": "现在站在 **{day} 开盘前 09:00**。",
        "sight": (
            "- 你能看到的最后一根日线是 **{prev_day}** 的收盘。"
            "**今天（{day}）的开盘、最高、最低、收盘、成交量，你全都不知道**，"
            "也不许猜。\n"
            "- 你的挂单今天以**开盘价**成交（或按限价判定）。你没有盘中路径。"),
        "news_cutoff": "{day} 09:00",
        "news_window": "昨夜到今早（{prev_day} 收盘后 → {day} 09:00）",
        "fills_how": (
            "  `entry_high` 是你今天最多愿意付的价：开盘价不高于它就按开盘价成交。"
            "`entry_low` 可以不写；写了就表示开盘低于它你也不买。"),
    },
    "close": {
        "session": "现在站在 **{day} 收盘前 14:55**。",
        "sight": (
            "- 今天（{day}）的**开盘、最高、最低、收盘、成交量你已经全部看到**，"
            "下面面板里就是今日收盘数据。\n"
            "- 你的买入**以今日收盘价成交**。"),
        "news_cutoff": "{day} 14:55",
        "news_window": "今日盘中（{day} 09:00 → 14:55）",
        "fills_how": (
            "  `entry_high` 是你最多愿意付的价，系统用今日收盘价对照它判定是否成交；"
            "`entry_low` 可以不写，写了就表示收盘低于它你也不买。"),
    },
}


def build_message(*, day: str, prev_day: str, panel: list[dict],
                  news: list[dict], book: str, knowledge: str,
                  trader_note: str, picks: int, template: str,
                  market: dict | None = None, phase: str = "open",
                  research_packet: dict | None = None) -> str:
    """Fill the prompt. Every placeholder must be consumed.

    The ``{VOCAB}`` lesson from ``agents/morning.py`` applies: a template
    rendered by a stale code path ships literal braces to the model, which
    then invents around them. So an unfilled placeholder is an error here.

    That guard has to wrap ``format`` rather than follow it. ``str.format``
    raises ``KeyError`` for a key it was not given, so the first version —
    which checked the rendered text for leftover braces — could never fire
    for the case it was written for. It was found by a test that expected
    ``DeciderError`` and got ``KeyError``, and the same gap then broke a real
    40-day run: ``news`` was passed to ``format`` but ``format_news`` was
    never called, and the moment the template gained a ``{news}`` section
    every day after it died with ``KeyError: 'news'``.
    """
    if phase not in _SESSIONS:
        raise DeciderError(
            f"phase must be one of {sorted(_SESSIONS)}, not {phase!r}")
    moment = {k: v.format(day=day, prev_day=prev_day)
              for k, v in _SESSIONS[phase].items()}
    fields = {"day": day, "prev_day": prev_day, "panel": format_panel(panel),
              "news": format_news(news), "market": format_market(market or {}),
              **moment,
              "book": book or "（空仓）",
              "knowledge": knowledge or "（还没有任何经验在生效）",
              "trader_note": trader_note or "", "picks": picks_line(picks),
              "research_packet": (
                  json.dumps(
                      research_packet, ensure_ascii=False, sort_keys=True,
                      indent=2)
                  if research_packet else "（无共享研究包）"),
              # A rendered field rather than a replace() on the loaded file:
              # the guard below only protects what the renderer supplies, and
              # a template hole that bypasses it is exactly the failure this
              # function's docstring already records for {news}. The text
              # comes from thesis._CONDITIONS, the same table validate_condition
              # and evaluate read, so the kinds the agent is told about cannot
              # drift from the kinds that will actually be checked.
              "VOCAB": _condition_vocabulary()}
    try:
        text = template.format(**fields)
    except (KeyError, IndexError) as exc:
        raise DeciderError(
            f"prompt {PROMPT_FILE} asks for {exc} and the renderer does not "
            f"supply it — the template and the code disagree. Supplied: "
            f"{', '.join(sorted(fields))}") from exc
    # Still worth checking, for a different case: ``{{name}}`` renders as a
    # literal ``{name}``, so a doubled brace ships braces to the model while
    # ``format`` succeeds.
    leftover = re.search(r"\{[A-Za-z_]{3,}\}", text)
    if leftover:
        raise DeciderError(
            f"prompt {PROMPT_FILE} still shows {leftover.group(0)} after "
            "rendering — a doubled brace, or a placeholder the renderer "
            "does not supply")
    return text


async def propose(*, day: str, prev_day: str, panel: list[dict],
                  news: list[dict], book: str = "", knowledge: str = "",
                  trader_note: str = "", picks: int = 2,
                  model=None, template: str | None = None,
                  max_turns: int | None = None, market: dict | None = None,
                  phase: str = "open", tools: list | None = None,
                  research_budget=None, research_packet: dict | None = None,
                  knowledge_intervention: dict | None = None,
                  trader_id: str | None = None, run_id: str | None = None,
                  origin: str = "t1_decider") -> dict:
    """Ask the model for today's orders.

    ``model`` defaults to the journaled model from ``model_factory``. That is
    not a convenience: it is what makes a replay reproducible, because the
    call is recorded and a second run of the same window answers from the
    recording instead of re-sampling.

    ``max_turns`` defaults to :data:`DEFAULT_MAX_TURNS`, and passing ``None``
    means that default rather than zero. It is declared once, here, because
    this module owns the decision budget: the runner's ``--max-turns`` also
    defaults to ``None`` and simply does not override it. Defining the number
    in both places is what produced the worst run of this project — the
    decider said 8, the CLI said 3, the CLI won, and 2.5 hours of model calls
    produced 40 unreadable decisions and no trades.
    six more in round two (including two memories and its own state), and was
    cut off before it could answer. Giving a trader tools makes its reasoning
    longer, and a budget set before that was known was a guess.

    Eight is ``1 + a tool budget``: enough for the three rounds the recording
    actually shows plus room to answer, and still a ceiling — a model that
    loops cannot spend the day's budget. ``tools=None`` keeps the bare-picker
    behaviour for a caller that wants it.

    Every tool call is journaled like the rest of the exchange
    (``llm_journal`` records ``tool_calls`` and ``tool_results``), so a replay
    can show *which* questions the agent decided to ask — the evidence that
    this is a tool-using trader and not a scorer.
    """
    if model is None:
        from alpha_agents.model_factory import create_model
        model = create_model()
    if max_turns is None:
        max_turns = DEFAULT_MAX_TURNS
    chosen_template = template if template is not None else load_prompt()
    message = build_message(
        day=day, prev_day=prev_day, panel=panel, news=news, book=book,
        knowledge=knowledge, trader_note=trader_note, picks=picks,
        template=chosen_template,
        market=market, phase=phase, research_packet=research_packet)

    # A tool-less stage may still carry the shared decision budget.
    from alpha_agents.tools.budget import ResearchBudget, use_research_budget

    budget = research_budget
    if tools:
        budget = budget or ResearchBudget()
        message += "\n\n" + budget.prompt_hint()

    from alpha_agents.data.trader_session import namespace
    frame = make_frame(
        day=day, phase=phase, message=message,
        state=TraderState.create(book=book or "", knowledge=knowledge or "",
                                 trader_note=trader_note or ""),
        panel_codes=[row["code"] for row in panel], model=model,
        template=chosen_template, max_turns=max_turns, tools=tools or [],
        trader_id=trader_id or "unbound", run_id=namespace(run_id), origin=origin)
    if knowledge_intervention is not None:
        frame = frame.change_knowledge(**knowledge_intervention)
    with Capture(frame, enabled=trader_id is not None) as captured:
        parsed = await _run_frame(frame, model=model, tools=tools or [], budget=budget)
        parsed["frame_hash"] = frame.frame_hash
        parsed["decision_id"] = captured.invocation_id
        captured.output = parsed
        return parsed


# The loaded adapter identity must not change when files are edited under a
# long-running process. This is a planner hash, not a whole-project policy hash.
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_PLAN_CODE_HASH = fingerprint({
    path: (_PLAN_ROOT / path).read_text(encoding="utf-8") for path in (
        "alpha_agents/agents/t1_decider.py", "alpha_agents/agents/json_reply.py",
        "alpha_agents/data/thesis.py", "alpha_agents/model_factory.py",
        "alpha_agents/data/decision_frame.py",
        "alpha_agents/llm_journal.py", "pyproject.toml", "uv.lock",
    )
})


def make_frame(*, day: str, phase: str, message: str, state: TraderState,
               panel_codes: list[str], model, template: str, max_turns: int,
               tools: list, trader_id: str, run_id: str, origin: str) -> DecisionFrame:
    """Freeze what is actually handed to the SDK; never reconstruct it later."""
    if phase not in _SESSIONS:
        raise FrameError("Unsupported planner session")
    name = getattr(model, "model", None)
    name = name if isinstance(name, str) and name else "unbound"
    return DecisionFrame.create(
        identity={"run_id": run_id, "trader_id": trader_id, "stage": "trade_plan",
                  "phase": phase, "session_day": day, "origin": origin,
                  "information_cutoff": f"{day} {'15:00:00' if phase == 'close' else '09:00:00'}",
                  "information_grade": "synthetic_close" if phase == "close"
                                       else "declared_daily_open"},
        state=state,
        request={"agent_name": f"t1_decider:{DECIDER_NAME}",
                 "instructions": SYSTEM_INSTRUCTIONS, "message": message,
                 "max_turns": max_turns, "model": name, "model_settings": None,
                 "tools": [getattr(t, "name", type(t).__name__) for t in tools]},
        panel_codes=panel_codes,
        producer={"contract": "t1_orders.v1", "code_hash": _PLAN_CODE_HASH,
                  "template_hash": fingerprint(template)})


def require_frozen_plan(frame: DecisionFrame) -> dict:
    """A tool-bearing initial input is not a complete replayable trajectory."""
    value = frame.as_dict()
    if value["producer"]["contract"] != "t1_orders.v1":
        raise FrameError("Only the tool-free T+1 plan contract can be rerun")
    if value["producer"]["code_hash"] != _PLAN_CODE_HASH:
        raise FrameError("Planner code differs from the frozen frame; use its recorded revision")
    if value["request"]["tools"] or value["request"]["model_settings"] is not None:
        raise FrameError("Tool-bearing/custom-settings frames cannot be rerun as bare plans")
    if value["identity"]["trader_id"] == "unbound":
        raise FrameError("A frozen experiment requires a known trader identity")
    return value


def parse_frozen_plan(frame: DecisionFrame, raw: str) -> dict:
    """Parser regression only, not a new sample from a model."""
    value = require_frozen_plan(frame)
    return {**parse_orders(raw, set(value["panel_codes"])), "raw": raw}


async def propose_frame(frame: DecisionFrame, *, model, timeout: float = 180) -> dict:
    """Run the original seam on a frozen input, with no tools or source writes.

    The caller supplies a pinned model and private experiment storage. There is
    one logical attempt; SDK transport retries must also be disabled by caller.
    """
    value = require_frozen_plan(frame)
    if value["request"]["model"] == "unbound" or getattr(model, "model", None) != value["request"]["model"]:
        raise FrameError("Configured model does not match the frozen declared model")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        raise FrameError("Frozen decision timeout must be in (0, 600]")
    return await _run_frame(frame, model=model, tools=[], budget=None,
                            timeout=timeout, attempts=1)


async def _run_frame(frame: DecisionFrame, *, model, tools: list, budget,
                     timeout: float | None = None, attempts: int | None = None) -> dict:
    """The shared execution/parser seam; it never executes a trading order."""
    from alpha_agents.tools.budget import use_research_budget
    value = frame.as_dict()
    request = value["request"]
    day = value["identity"]["session_day"]
    message, max_turns = request["message"], request["max_turns"]
    call_options = {"max_turns": max_turns, "label": "t1_decide"}
    # Preserve ordinary provider retry/timeout defaults unless explicitly set.
    if timeout is not None:
        call_options["timeout"] = timeout
    if attempts is not None:
        call_options["attempts"] = attempts
    agent = Agent(name=request["agent_name"],
                  instructions=request["instructions"], model=model,
                  tools=list(tools) if tools else [])
    started = time.monotonic()
    try:
        if budget is None:
            result = await run_agent(agent, message, **call_options)
        else:
            with use_research_budget(budget):
                result = await run_agent(agent, message, **call_options)
    except MaxTurnsExceeded as exc:
        # A model that spends its whole turn budget asking questions has not
        # said what to buy, and "it did not answer" is not "buy nothing" — the
        # two must not share an outcome. Measured: the first tool-enabled run
        # died on its opening decision because a six-call round plus a second
        # round exhausted a budget of three, and because the exception escaped
        # `propose` it took the **whole window** down rather than one day.
        #
        # Returned as an unreadable reply rather than raised: the runner
        # already counts `decider_unreadable`, the day is recorded as a
        # failure, and the rest of the window still runs. Raising here would
        # make one over-thinking morning cost forty simulated days.
        logger.warning(
            "%s: decider exceeded %d turns without answering — treating the "
            "day as undecided (not as 'no orders')", day, max_turns)
        parsed = {"orders": [], "watch": [], "refused": [], "raw": "",
                  "parse_error": f"MaxTurnsExceeded after {max_turns} turns "
                                 f"({exc})"}
        parsed["research_budget"] = budget.summary() if budget else None
        parsed["research_trace"] = budget.trace() if budget else []
        parsed["model_elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return parsed
    raw = result.final_output or ""
    parsed = parse_orders(raw, set(value["panel_codes"]))
    parsed["raw"] = raw
    parsed["research_budget"] = budget.summary() if budget else None
    parsed["research_trace"] = budget.trace() if budget else []
    parsed["model_elapsed_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("%s: decider proposed %d, refused %d, parse_error=%s",
                day, len(parsed["orders"]), len(parsed["refused"]),
                parsed["parse_error"])
    return parsed


def propose_sync(*, loop: asyncio.AbstractEventLoop | None = None,
                 **kwargs) -> dict:
    """``propose`` for the runner, which is synchronous.

    ``scripts/walk_forward.py`` walks a calendar in a plain loop and its day
    boundary is a context manager, not an await. Wrapping one call per day is
    the smallest change that keeps the runner's shape; the alternative — an
    async runner — would put ``await`` inside the ``replay_as_of`` blocks that
    currently read as three instants of one day.

    ``loop`` is how a **run** says "one loop outlives every call in me", and
    passing it is not an optimisation. The runner builds its model once for the
    whole window, so one ``AsyncOpenAI`` — and the httpx connection pool inside
    it — is shared by every simulated day. httpx pools connections bound to the
    loop that opened them, so a fresh loop per day makes that day's first
    request pick a connection from a loop that is closed: it fails at the
    transport layer, before a status exists, and the SDK's own retry answers it
    on the second attempt. Measured across two 120-day windows: 120 and 121
    ``Retrying request`` lines, **119 of each** being this defect — exactly
    ``days - 1``, because the first day has nothing pooled to trip over.

    ``loop=None`` keeps ``asyncio.run``, which is right for a one-shot caller
    and for a test: it is only wrong when the *client* outlives the call.
    """
    if loop is None:
        return asyncio.run(propose(**kwargs))
    return loop.run_until_complete(propose(**kwargs))
