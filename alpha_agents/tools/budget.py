"""No single data source may starve the whole scan.

The morning agent runs under one overall deadline. Every tool call spends
from it, and a source that hangs spends far more than its share: a run
that timed out at 300s had its five slowest calls take 65s, 49s, 39s,
38s and 30s — 220 seconds, most of it waiting on retries against a host
that was returning connect timeouts and a 500. Twenty-five calls
completed, fewer than the thirty-three of a run that finished
comfortably. The scan did not fail because it did too much; it failed
because a handful of calls did nothing, slowly.

Wrapping each call in its own deadline turns that from a fatal failure
into a missing input. The agent is told the source did not answer and
carries on with what it has, which is what a person would do. The
alternative — one dead endpoint taking the whole morning down — has
already cost a full scan.

The message returned on timeout is deliberately written for the model:
it says the source is unavailable and not to retry, because a bare error
string invites exactly the retry that just burned the budget.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import functools
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Generous by design. This is not a latency target — it is the point past
# which a call is almost certainly stuck rather than slow. Healthy tools
# in this system return in well under ten seconds; the ones that hang do
# so on connect timeouts and retries, which land far beyond this.
TOOL_TIMEOUT = int(os.environ.get("TOOL_TIMEOUT", "25"))



# ── T1 research convergence budget ─────────────────────────────────────
#
# Per-source timeout and per-decision research convergence are different
# controls. A healthy tool can answer quickly and still be called forty times.
# This budget is inactive unless a caller explicitly installs it, so morning
# analysis and every existing tool user keep their current behaviour.

_MARKET_TOOLS = frozenset({"get_market_regime"})
_THEME_TOOLS = frozenset({"get_theme_state"})
_STOCK_TOOLS = frozenset({
    "get_stock_context", "get_intraday_shape", "get_event_context",
    "get_stock_memory",
})
_SELF_TOOLS = frozenset({"get_my_state"})


@dataclass
class ResearchBudget:
    """Finite fact-gathering budget for one Trader decision.

    The defaults come from the measured failure mode: recent replay decisions
    averaged roughly 23 tool calls and one window used about 46 calls per buy.
    Twenty calls plus a four-name deep-dive ceiling forces a funnel while
    leaving room for broad market/theme context.

    This is a resource invariant, not a trading rule. It never says what to
    buy; it only limits how much evidence one decision may gather.
    """

    max_total_calls: int = 20
    max_market_calls: int = 2
    max_theme_calls: int = 4
    max_stock_calls: int = 12
    max_self_calls: int = 2
    max_deep_dive_names: int = 4
    max_calls_per_name: int = 4
    total_calls: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)
    deep_dive_names: set[str] = field(default_factory=set)
    denied: int = 0
    trace_records: list[dict] = field(default_factory=list, repr=False)

    def _category(self, tool_name: str) -> str:
        if tool_name in _MARKET_TOOLS:
            return "market"
        if tool_name in _THEME_TOOLS:
            return "theme"
        if tool_name in _STOCK_TOOLS:
            return "stock"
        if tool_name in _SELF_TOOLS:
            return "self"
        return "other"

    def _name(self, tool_name: str, args: tuple, kwargs: dict) -> str | None:
        if tool_name not in _STOCK_TOOLS:
            return None
        value = kwargs.get("code")
        if value is None and args:
            value = args[0]
        text = str(value or "").strip()
        return text or None

    def _limit_for(self, category: str) -> int | None:
        return {
            "market": self.max_market_calls,
            "theme": self.max_theme_calls,
            "stock": self.max_stock_calls,
            "self": self.max_self_calls,
        }.get(category)

    def claim(self, tool_name: str, args: tuple, kwargs: dict) -> tuple[bool, str]:
        """Reserve one call before worker/thread/network work begins."""
        category = self._category(tool_name)
        name = self._name(tool_name, args, kwargs)

        if self.total_calls >= self.max_total_calls:
            return self._deny(
                f"总研究预算 {self.max_total_calls} 次已用完")

        category_limit = self._limit_for(category)
        used_category = self.by_category.get(category, 0)
        if category_limit is not None and used_category >= category_limit:
            return self._deny(
                f"{category} 类预算 {category_limit} 次已用完")

        if name is not None:
            if (name not in self.deep_dive_names
                    and len(self.deep_dive_names) >= self.max_deep_dive_names):
                return self._deny(
                    f"最多深挖 {self.max_deep_dive_names} 只股票；"
                    f"{name} 不在已选深挖名单")
            if self.by_name.get(name, 0) >= self.max_calls_per_name:
                return self._deny(
                    f"{name} 已达到单票 {self.max_calls_per_name} 次查询上限")

        self.total_calls += 1
        self.by_category[category] = used_category + 1
        if name is not None:
            self.deep_dive_names.add(name)
            self.by_name[name] = self.by_name.get(name, 0) + 1
        return True, ""

    def _deny(self, reason: str) -> tuple[bool, str]:
        self.denied += 1
        return False, reason

    def record_tool(self, tool_name: str, args: tuple, kwargs: dict,
                    result, *, status: str, elapsed_ms: int | None = None) -> None:
        """Preserve exactly the facts a model actually received from a tool.

        This trace is evidence, not a second source of truth. The payload is
        the wrapper's returned value (the same value handed back to the Agent),
        hashed canonically so a downstream research packet can prove it was not
        rewritten while moving between stages.
        """
        name = self._name(tool_name, args, kwargs)
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            payload = str(result)
        try:
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            payload = str(payload)
            canonical = json.dumps(payload, ensure_ascii=False)
        self.trace_records.append({
            "tool": tool_name,
            "code": name,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "result_hash": hashlib.sha256(
                canonical.encode("utf-8")).hexdigest(),
            "payload": payload,
        })

    def trace(self) -> list[dict]:
        """JSON-safe copy of actual tool results in call order."""
        return json.loads(json.dumps(
            self.trace_records, ensure_ascii=False, allow_nan=False))

    def summary(self) -> dict:
        return {
            "used": self.total_calls,
            "limit": self.max_total_calls,
            "remaining": max(0, self.max_total_calls - self.total_calls),
            "by_category": dict(sorted(self.by_category.items())),
            "deep_dive_names": sorted(self.deep_dive_names),
            "by_name": dict(sorted(self.by_name.items())),
            "denied": self.denied,
            "limits": {
                "market": self.max_market_calls,
                "theme": self.max_theme_calls,
                "stock": self.max_stock_calls,
                "self": self.max_self_calls,
                "deep_dive_names": self.max_deep_dive_names,
                "calls_per_name": self.max_calls_per_name,
            },
        }

    def prompt_hint(self) -> str:
        return (
            "【研究预算（系统硬约束）】"
            f"最多 {self.max_total_calls} 次工具调用；市场最多 "
            f"{self.max_market_calls} 次、主题最多 {self.max_theme_calls} 次、"
            f"个股深挖最多 {self.max_stock_calls} 次；最多深挖 "
            f"{self.max_deep_dive_names} 只股票，每只最多 "
            f"{self.max_calls_per_name} 次。预算用于收敛研究范围；"
            "接近上限时停止扩展候选，用已经取得的事实完成本次决策。"
        )


_ACTIVE_RESEARCH_BUDGET: contextvars.ContextVar[ResearchBudget | None] = (
    contextvars.ContextVar("alphaagents_research_budget", default=None)
)


@contextmanager
def use_research_budget(budget: ResearchBudget):
    """Install a budget for exactly one decision/tool-call context."""
    token = _ACTIVE_RESEARCH_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_RESEARCH_BUDGET.reset(token)


def current_research_budget() -> ResearchBudget | None:
    return _ACTIVE_RESEARCH_BUDGET.get()


def _budget_refusal(tool_name: str, reason: str,
                    active: ResearchBudget) -> str:
    return json.dumps({
        "available": False,
        "error": "research_budget_exhausted",
        "tool": tool_name,
        "reason": reason,
        "budget": active.summary(),
        "instruction": "不要重试这个查询；用已经取得的事实完成本次决策。",
    }, ensure_ascii=False)


# One shared pool: a per-call pool would create a thread for every tool
# invocation, and the abandoned ones already leak a thread each.
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool")


def with_timeout(fn, timeout: int | None = None):
    """Wrap a synchronous tool function in its own deadline.

    On timeout the worker thread is *not* killed — Python cannot — so it
    keeps running until its blocking call returns and then discards the
    result. That is acceptable: it is a daemon thread doing a read, and
    the cost of leaving it is one thread, while the cost of waiting for
    it is the whole scan.
    """
    limit = timeout or TOOL_TIMEOUT

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        active = current_research_budget()
        if active is not None:
            allowed, reason = active.claim(fn.__name__, args, kwargs)
            if not allowed:
                logger.info("Research budget refused %s: %s", fn.__name__, reason)
                result = _budget_refusal(fn.__name__, reason, active)
                active.record_tool(
                    fn.__name__, args, kwargs, result, status="denied")
                return result

        started = time.monotonic()
        future = _POOL.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=limit)
            if active is not None:
                active.record_tool(
                    fn.__name__, args, kwargs, result, status="ok",
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            return result
        except concurrent.futures.TimeoutError:
            logger.warning("Tool %s exceeded %ds — reported as unavailable "
                           "so the run continues", fn.__name__, limit)
            result = (f'{{"error": "数据源 {fn.__name__} 超过 {limit}秒未响应，'
                      f'本次不可用。不要重试这个工具，用你已有的信息继续，'
                      f'并在报告里说明缺了什么。"}}')
            if active is not None:
                active.record_tool(
                    fn.__name__, args, kwargs, result, status="timeout",
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            return result
        except Exception as e:
            elapsed = time.monotonic() - started
            logger.warning("Tool %s failed after %.1fs: %s",
                           fn.__name__, elapsed, e)
            result = (f'{{"error": "数据源 {fn.__name__} 调用失败：'
                      f'{str(e)[:120]}。不要重试，用已有信息继续。"}}')
            if active is not None:
                active.record_tool(
                    fn.__name__, args, kwargs, result, status="error",
                    elapsed_ms=int(elapsed * 1000))
            return result

    return wrapper
