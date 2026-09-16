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

What it refuses
---------------
A code outside the panel, a malformed zone (``entry_low`` not below
``entry_high``), a stop that is not below the entry, or a reply that is not
JSON. Every refusal is **returned as a value**, not swallowed: the runner
counts them and the report prints them, because a decider that quietly
returns fewer orders looks identical to a decider that found nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from agents import Agent, Runner

from alpha_agents.config import PROMPTS_DIR

logger = logging.getLogger(__name__)

#: Names the decider in the run report and in every order's ``reason`` prefix.
DECIDER_NAME = "t1_llm"

#: The prompt is the *user message*, not the system instructions: it is the
#: task, and it holds every placeholder the runner fills. Kept in a file so a
#: prompt change does not need a code change — and so the text the model was
#: given can be read by someone who does not read Python.
PROMPT_FILE = "t1_decide.md"

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

SYSTEM_INSTRUCTIONS = (
    "你是一名 A 股日频交易员。你只根据用户消息里给出的信息做决定，"
    "不使用你记忆中的任何具体行情或个股事实。"
    "输出必须是单个 JSON 对象，没有其它文字。"
)


class DeciderError(RuntimeError):
    """The decider could not produce a usable answer at all."""


def load_prompt(path: Path | None = None) -> str:
    """The decision prompt template."""
    return (path or (PROMPTS_DIR / PROMPT_FILE)).read_text(encoding="utf-8")


def format_panel(panel: list[dict]) -> str:
    """Render the allowed securities, one per line, with what is knowable."""
    if not panel:
        return "（今天没有可交易的候选）"
    lines = ["| 代码 | 名称 | T-1 收盘 | T-1 涨幅 | ADV20(手) |",
             "|---|---|---|---|---|"]
    for row in panel:
        adv = row.get("adv20")
        lines.append(
            f"| {row['code']} | {row.get('name', '')} | {row.get('close')} | "
            f"{row.get('change_pct')}% | "
            f"{'-' if adv is None else int(adv)} |")
    return "\n".join(lines)


def format_news(items: list[dict]) -> str:
    """Render the news window. Empty is stated, not left blank."""
    if not items:
        return "（截至今日 09:00，没有读到任何快讯）"
    return "\n".join(
        f"• {n.get('time', '')} [{n.get('source', '')}] {n.get('title', '')}"
        for n in items)


def _strip_fence(text: str) -> str:
    """Drop a ```json fence if the model wrapped its answer in one.

    Not a nicety: a fenced reply is otherwise a parse failure, and a parse
    failure is indistinguishable in the report from a model that chose to
    order nothing. Stripping is the difference between "it said no" and
    "we could not read it".
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    return stripped


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
        try:
            low = float(raw["entry_low"])
            high = float(raw["entry_high"])
            stop = float(raw["stop_loss"])
        except (KeyError, TypeError, ValueError) as exc:
            refused.append({"code": code, "why": "bad_prices",
                            "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if not low < high:
            refused.append({"code": code, "why": "inverted_zone",
                            "detail": f"{low} !< {high}"})
            continue
        if not stop < low:
            refused.append({"code": code, "why": "stop_not_below_entry",
                            "detail": f"{stop} !< {low}"})
            continue
        if low <= 0:
            refused.append({"code": code, "why": "non_positive_price",
                            "detail": str(low)})
            continue
        orders.append({
            "code": code, "entry_low": round(low, 2),
            "entry_high": round(high, 2), "stop_loss": round(stop, 2),
            "reason": str(raw.get("reason") or "").strip()[:200],
        })
    return {"orders": orders, "refused": refused, "parse_error": None}


def build_message(*, day: str, prev_day: str, panel: list[dict],
                  news: list[dict], book: str, knowledge: str,
                  trader_note: str, picks: int, template: str) -> str:
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
    fields = {"day": day, "prev_day": prev_day, "panel": format_panel(panel),
              "news": format_news(news),
              "book": book or "（空仓）",
              "knowledge": knowledge or "（还没有任何经验在生效）",
              "trader_note": trader_note or "", "picks": picks}
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
                  max_turns: int = 1) -> dict:
    """Ask the model for today's orders.

    ``model`` defaults to the journaled model from ``model_factory``. That is
    not a convenience: it is what makes a replay reproducible, because the
    call is recorded and a second run of the same window answers from the
    recording instead of re-sampling.
    """
    if model is None:
        from alpha_agents.model_factory import create_model
        model = create_model()
    message = build_message(
        day=day, prev_day=prev_day, panel=panel, news=news, book=book,
        knowledge=knowledge, trader_note=trader_note, picks=picks,
        template=template if template is not None else load_prompt())

    agent = Agent(name=f"t1_decider:{DECIDER_NAME}",
                  instructions=SYSTEM_INSTRUCTIONS, model=model)
    result = await Runner.run(agent, message, max_turns=max_turns)
    raw = result.final_output or ""
    parsed = parse_orders(raw, {row["code"] for row in panel})
    parsed["raw"] = raw
    logger.info("%s: decider proposed %d, refused %d, parse_error=%s",
                day, len(parsed["orders"]), len(parsed["refused"]),
                parsed["parse_error"])
    return parsed


def propose_sync(**kwargs) -> dict:
    """``propose`` for the runner, which is synchronous.

    ``scripts/walk_forward.py`` walks a calendar in a plain loop and its day
    boundary is a context manager, not an await. Wrapping one call per day is
    the smallest change that keeps the runner's shape; the alternative — an
    async runner — would put ``await`` inside the ``replay_as_of`` blocks that
    currently read as three instants of one day.
    """
    return asyncio.run(propose(**kwargs))
