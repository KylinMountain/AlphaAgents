"""Where to buy, decided by the trader rather than by a constant.

The intraday path selects stocks with a scoring function — fund-flow
anomaly, sector beta, liquidity — and then priced its own orders at
``price * 0.97``. 109 of the system's 113 orders came out of that line.
Measured against what the stocks actually do, the constant was not a
conservative default, it was a coin flip: 中钨高新 has an average daily
range of 5.6% and 通源石油 6.1%, so a 3% band on either is narrower than
one ordinary session, and both were cancelled with 价格已涨走.

Selection and pricing are different jobs. A scoring function can rank
"the money is buying this name"; it cannot say where this particular
stock has found buyers before, and it has no way to notice that its own
band is half a day's move. So the scorer now only selects, and this
module asks a model to price what it selected, with ``get_price_levels``
to compute against.

Per trader, because the answer should differ. Given the same candidate and
the same levels, a trader instructed to wait for a pullback and one
instructed to buy strength should produce different orders — and now that
difference comes from what each was told rather than from a different
constant in a dict.

Every failure path returns no order rather than a guessed one. An order
priced by a fallback constant is exactly what this replaces; refusing to
place it costs one missed candidate, and placing it costs a position
nobody decided to take.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# Three shapes, because the model reliably produces the decision and
# unreliably produces the wrapper. A run that reasoned correctly and then
# fenced its JSON instead of commenting it is not a run that declined to
# trade, and treating it as one throws away the whole pass.
_BLOCK_PATTERNS = (
    re.compile(r"<!--\s*ORDERS:\s*(\[.*?\])\s*-->", re.DOTALL),
    re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL),
    re.compile(r"(\[\s*\{.*?\}\s*\])", re.DOTALL),
)

# Measured, not guessed: a real pricing pass made four model calls at
# roughly 30s each against the configured endpoint, and 120s cut it off
# mid-decision. The traders run concurrently (see _price_all), so this is
# the whole pass's budget rather than a per-trader tax on the cycle.
_TIMEOUT = int(os.environ.get("ENTRY_PRICING_TIMEOUT", "240"))

# How far the entry band may sit below the stock's own daily range before
# it is worth saying so. Purely a warning: the agent decides, and a narrow
# band is sometimes deliberate. But a band at half the daily range is the
# exact shape of the 3% constant this replaced, and 109 orders' worth of
# cancellations say it mostly does not fill.
_NARROW_BAND_RATIO = 0.5

# Beyond this the model is being asked to price a list rather than think
# about names. The scorer routinely proposes more; the extras are dropped
# with a log rather than silently, because a candidate that never reached
# a decision is not the same as one that was declined.
MAX_CANDIDATES = 5

_INSTRUCTIONS = """你是这个虚拟组合的交易员，负责**定价**。选股不归你管——\
这些标的是盘中资金异动扫描选出来的，你要决定的是：**买不买，买在哪里，\
止损放哪里，买多少。**

## 你的工作

对每一个候选标的：

1. 调用 `get_price_levels(code)` 拿到它的价位结构
2. 判断这个位置**值不值得买**。不值得就 skip，这是完全正当的答案——
   扫描器负责找出资金在买什么，它不判断价格位置
3. 值得买的，自己定介入区间、止损、仓位

## 定价要点

- **先看 atr_pct**：这是这只票一天的正常波动。介入区间比它还窄基本不会
  成交——atr_pct=6 的票挂 3% 宽的区间，一天就跳过去了。这不是理论，
  系统之前 109 笔订单全是固定 3%，绝大多数没成交
- **position_pct** 是当前价在 20 日区间的位置。85 以上是高位，追高要有
  更硬的理由；15 以下是低位，但低位也可能是在跌
- 等回调：挂在 support_below 上方一点，别正好挂在支撑位上——那个位置
  所有人都看得见
- 追突破：挂在 resistance_above 上方一点，用突破确认换更差的成本
- **止损放在论点失效的位置**，不是固定百分比。跌破哪个价说明你买的理由
  错了，那里就是止损

## 仓位

`size_pct` 是总资金的比例，你自己定。把握大就多买，这是这套系统里最值钱
的判断之一。复盘会把仓位和结果一起回看。

## 输出格式

先用一两句说整体判断，然后必须输出一个 JSON 块：

<!-- ORDERS: [{"code":"600835","action":"buy","entry_low":18.20,\
"entry_high":18.90,"stop_loss":17.40,"size_pct":0.04,\
"reason":"回调到20日线18.3附近，atr 3.2%所以区间给3.8%宽，\
跌破17.4则是主线资金离场，买入理由不成立","confidence":"high"}] -->

字段：
- **action**: `buy` 下单 / `skip` 不买（skip 只需要 code、action、reason）
- **entry_low / entry_high**: 介入区间，两个都要填
- **stop_loss**: 止损价，必填
- **size_pct**: 总资金比例，如 0.04 表示 4%
- **reason**: 必须说出**为什么是这个价位**，带上数字。写"技术面支撑"
  这种没有信息量的理由等于没写——它会存进交易记录，复盘时要用它判断
  你当时想对了没有
- **confidence**: high / medium / low

价位数据拿不到（工具返回 error）就 skip，**不要瞎猜**。"""


def enabled() -> bool:
    """Off switch. On by default — the constant it replaces was worse."""
    return os.environ.get("AGENT_ENTRY_PRICING", "1").lower() in (
        "1", "true", "yes", "on")


def build_context(candidates: list[dict], trader) -> str:
    """The candidates and what this trader has to spend, as one block."""
    from alpha_agents.data.portfolio import get_available_capital

    try:
        available = get_available_capital(trader.id)
    except Exception as e:
        logger.debug("Capital lookup failed for %s: %s", trader.id, e)
        available = 0

    lines = [f"【可用资金】{available:,.0f}元"
             f"（单票上限 {trader.max_position_pct:.0%}）", "",
             "【候选标的】盘中资金异动扫描选出，尚未定价：", ""]
    for c in candidates:
        lines.append(
            f"● {c['code']} {c.get('name', '')} 现价 {c.get('price', 0):.2f} "
            f"今日 {c.get('change_pct', 0):+.2f}%")
        lines.append(f"  主线: {c.get('theme', '')}")
        if c.get("note"):
            lines.append(f"  选中原因: {c['note']}")
        if c.get("institutional"):
            lines.append(f"  机构: {c['institutional']}")
        lines.append("")
    return "\n".join(lines)


async def price(candidates: list[dict], trader) -> dict[str, dict]:
    """Ask the trader where to buy each candidate.

    Returns {code: decision} for the ones it wants, keyed so the caller can
    look up a candidate it may have skipped. An empty result means no
    order — never a fallback price.
    """
    if not candidates:
        return {}

    from agents import Agent, Runner

    from alpha_agents.model_factory import create_model, create_model_settings
    from alpha_agents.tools.registry import (
        get_price_levels, get_stock_fund_flow, get_stock_quotes,
    )

    if len(candidates) > MAX_CANDIDATES:
        logger.info("定价：%d 个候选截断到 %d 个",
                    len(candidates), MAX_CANDIDATES)
        candidates = candidates[:MAX_CANDIDATES]

    instructions = _INSTRUCTIONS
    if getattr(trader, "extra_prompt", ""):
        instructions += f"\n\n---\n\n## 你是谁\n\n{trader.extra_prompt.strip()}\n"

    # Building the agent is inside the try with the call: a missing API key
    # and a timeout mean the same thing here — nobody priced this, so
    # nothing is ordered. Letting setup raise past this point would make
    # the caller responsible for a decision that belongs in one place.
    try:
        agent = Agent(
            name=f"entry_pricer:{trader.id}",
            instructions=instructions,
            model=create_model(),
            model_settings=create_model_settings(),
            tools=[get_price_levels, get_stock_quotes, get_stock_fund_flow],
        )
        result = await asyncio.wait_for(
            Runner.run(agent, build_context(candidates, trader), max_turns=25),
            timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("交易员 %s 定价超时（%ds）— 本轮不下单", trader.id, _TIMEOUT)
        return {}
    except Exception as e:
        logger.warning("交易员 %s 定价失败（%s）— 本轮不下单", trader.id, e)
        return {}

    orders = parse_orders(result.final_output or "")
    _flag_narrow_bands(orders)
    return orders


def _flag_narrow_bands(orders: dict[str, dict]) -> None:
    """Say so when an entry band is narrower than the stock's daily range.

    Not a rejection — the trader priced it and the trader is allowed to be
    wrong. But this is the one property that predicted whether an order
    ever became a position, and it belongs in the log where the review can
    read it rather than being discovered a week later in the cancellations.
    """
    from alpha_agents.tools.price_levels import get_price_levels_fn

    for code, d in orders.items():
        try:
            atr = json.loads(get_price_levels_fn(code)).get("atr_pct")
        except Exception as e:
            logger.debug("Band check unavailable for %s: %s", code, e)
            continue
        if not atr:
            continue
        width = (d["entry_high"] - d["entry_low"]) / d["entry_high"] * 100
        if width < atr * _NARROW_BAND_RATIO:
            logger.warning(
                "%s 的介入区间只有 %.1f%%，这只票日均波动 %.1f%% — "
                "大概率一天就跳过去，成交不了", code, width, atr)


def parse_orders(output: str) -> dict[str, dict]:
    """Pull the ORDERS block out. A malformed block places nothing.

    Deliberately strict about prices: an order missing a level, or one
    whose zone is inverted, is dropped rather than repaired. Repairing it
    would put the constant back in through the side door.
    """
    items = _extract(output)
    if items is None:
        logger.warning("定价返回里没有可解析的 ORDERS — 本轮不下单。原文开头：%s",
                       (output or "")[:200].replace("\n", " "))
        return {}

    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        if not code or action not in ("buy", "skip"):
            continue
        if action == "skip":
            logger.info("定价：%s 被跳过 — %s", code, reason or "未说明")
            continue

        lo, hi = _num(item.get("entry_low")), _num(item.get("entry_high"))
        stop = _num(item.get("stop_loss"))
        if lo is None or hi is None or lo > hi:
            logger.warning("定价：%s 的介入区间无效（%s-%s）— 丢弃", code, lo, hi)
            continue
        if stop is None:
            logger.warning("定价：%s 没有止损位 — 丢弃", code)
            continue
        if stop >= lo:
            logger.warning("定价：%s 的止损 %.2f 在介入区间之内 — 丢弃",
                           code, stop)
            continue
        if not reason:
            logger.warning("定价：%s 没有理由 — 丢弃（复盘无法评价看不见的决策）",
                           code)
            continue

        out[code] = {
            "entry_low": lo, "entry_high": hi, "stop_loss": stop,
            "size_pct": _num(item.get("size_pct")),
            "reason": reason,
            "confidence": str(item.get("confidence", "")).strip() or "medium",
        }
    if out:
        logger.info("定价结果: %s", ", ".join(
            f"{c} {d['entry_low']:.2f}-{d['entry_high']:.2f} 止损{d['stop_loss']:.2f}"
            for c, d in out.items()))
    return out


def _extract(output: str) -> list | None:
    """The order list, however the model chose to wrap it.

    Tries the documented comment form first, then a fenced block, then a
    bare array. ``json_repair`` handles the trailing commas and unquoted
    keys that models produce; a genuinely unparseable output returns None,
    which the caller treats as "no order" rather than repairing further.
    """
    from json_repair import repair_json

    for pattern in _BLOCK_PATTERNS:
        m = pattern.search(output or "")
        if not m:
            continue
        raw = m.group(1)
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            try:
                items = repair_json(raw, return_objects=True)
            except Exception:
                continue
        if isinstance(items, list) and items:
            return items
    return None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
