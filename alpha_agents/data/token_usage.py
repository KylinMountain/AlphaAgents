"""Where the tokens went, by module.

Two questions this answers that the logs could not. *How much does a
trading day cost* — and the one that decides what to fix — *which part of
the day costs it*. A morning scan is a digest plus one agent run per
trader; an intraday cycle is usually free and occasionally a追因 plus a
pricing pass per trader. Those are wildly different bills and nothing in
the system distinguished them, so "we are burning tokens" was as specific
as anyone could get. It cost a stray uvicorn three days of quiet spend
before anyone noticed.

**Capture is deliberately not at the call sites.** There are a dozen of
them and adding one is easy; a counter that has to be remembered at each
is a counter that undercounts, and it repeats the mistake of putting the
guard on the caller rather than on the thing being guarded. So there are
exactly two hooks:

  * a ``TracingProcessor`` that the agents SDK calls for every generation
    span, which covers every ``Runner.run`` in the codebase and every one
    added later, without a line of change at the call;
  * ``instrument()``, which wraps a raw OpenAI client's ``create`` so the
    handful of direct ``chat.completions`` and ``embeddings`` calls are
    counted the same way.

Attribution comes from a context variable rather than a parameter, for
the same reason: ``track("digest")`` is set once where the work starts and
everything underneath inherits it, including nested agent runs. When
nothing set it, the agent's own name is the fallback — those already carry
the module (``morning_analyst``, ``entry_pricer``, ``exit_trader``).

Recording never raises. A usage counter that can break a trading task is
worse than no usage counter.
"""

from __future__ import annotations

import contextvars
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

_DB = DATA_DIR / "usage.db"
_lock = threading.Lock()
_local = threading.local()

# The module currently doing work. Set by track(), read by every hook.
_module: contextvars.ContextVar[str] = contextvars.ContextVar(
    "usage_module", default="")

# Agent-name prefix -> module, for runs that never entered a track()
# block. The names are already descriptive because they were built to be
# read in logs; this reuses them rather than inventing a second registry
# that would drift from the first.
_AGENT_MODULE = {
    "morning_analyst": "晨扫",
    "entry_pricer": "盘中定价",
    "cause_analyst": "盘中追因",
    "exit_trader": "卖出决策",
    "review_analyst": "复盘",
    "night_analyst": "夜扫",
    "weekly_analyst": "周报",
    "chat_analyst": "对话",
    "strategist": "策略分析",
    "futures_strategist": "期货分析",
    "reflection": "自省",
    "reviser": "自省修订",
    "geopolitical": "地缘分析",
    "cross_validator": "交叉验证",
}

# Modules that never come from an agent name — the raw-client callers name
# themselves. Kept beside the table above so the two are read together.
_RAW_MODULE_LABELS = {
    "digest": "新闻筛选",
    "embedding": "向量索引",
    "lessons": "教训归并",
    "playbook": "playbook 标注",
    "event_linker": "事件串联",
    "daily_review": "每日复盘(旧)",
    "context_compressor": "上下文压缩",
    "chat": "对话",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    module TEXT NOT NULL,
    model TEXT,
    -- 'chat' bills differently from 'embedding', and mixing them in one
    -- total makes the cheap half look like the expensive one.
    kind TEXT NOT NULL DEFAULT 'chat',
    requests INTEGER DEFAULT 1,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_date ON token_usage(date);
CREATE INDEX IF NOT EXISTS idx_usage_module ON token_usage(date, module);
"""


def _conn() -> sqlite3.Connection:
    """One connection per thread, on its own file.

    Its own database rather than a table in memory.db: this is written on
    every model call from several threads, and it must never be the
    reason a trading write waits on a lock.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


@contextmanager
def track(module: str):
    """Attribute everything inside this block to ``module``."""
    token = _module.set(module)
    try:
        yield
    finally:
        _module.reset(token)


def current_module() -> str:
    return _module.get() or ""


def record(module: str, model: str | None, input_tokens: int,
           output_tokens: int, *, kind: str = "chat",
           requests: int = 1) -> None:
    """Store one call's usage. Never raises."""
    try:
        total = (input_tokens or 0) + (output_tokens or 0)
        if not total and not requests:
            return
        now = datetime.now()
        with _lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO token_usage (ts, date, module, model, kind, "
                " requests, input_tokens, output_tokens, total_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
                 module or "unknown", model or "?", kind, requests,
                 input_tokens or 0, output_tokens or 0, total),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Usage record failed (%s) — ignored", e)


# ── Hook 1: every Runner.run, via the SDK's tracing ─────────

class _UsageProcessor:
    """Counts generation spans. Registered once at startup.

    The agents SDK emits one generation span per model request, carrying
    the model name and the provider's usage block. Reading them here means
    a new agent is counted the moment it runs, with no registration and no
    call-site change — which is the whole point of putting the hook here
    rather than at the twelve places that call Runner.run.
    """

    def __init__(self):
        # Agent name of the innermost agent span still open on this
        # thread, used only when nothing called track().
        self._agents: dict[str, str] = {}

    def on_trace_start(self, trace): pass

    def on_trace_end(self, trace): pass

    def on_span_start(self, span):
        try:
            name = getattr(span.span_data, "name", None)
            if name and type(span.span_data).__name__ == "AgentSpanData":
                self._agents[span.trace_id] = name
        except Exception as e:
            # Only costs the fallback: without the agent name a run that
            # never entered track() lands as "unknown" instead of its
            # module. The tokens are still counted, so this must not be
            # loud — but it must not be invisible either, because a
            # dashboard full of "unknown" would otherwise have no cause.
            logger.debug("Agent-name capture failed (%s) — module falls "
                         "back to unknown", e)

    def on_span_end(self, span):
        try:
            data = span.span_data
            if type(data).__name__ != "GenerationSpanData":
                return
            usage = getattr(data, "usage", None) or {}
            record(
                module=_module.get() or _module_from_agent(
                    self._agents.get(span.trace_id, "")),
                model=getattr(data, "model", None),
                input_tokens=_int(usage, "input_tokens", "prompt_tokens"),
                output_tokens=_int(usage, "output_tokens", "completion_tokens"),
                kind="chat",
            )
        except Exception as e:
            logger.debug("Usage span read failed (%s) — ignored", e)

    def force_flush(self): pass

    def shutdown(self): pass


def _int(usage, *keys) -> int:
    for k in keys:
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _module_from_agent(agent_name: str) -> str:
    """Map an agent name onto a module, or say plainly that we cannot.

    Agent names are ``role:trader_id``; the role is the module. An unknown
    role is stored as-is rather than bucketed into "other", so a new agent
    shows up in the report as itself instead of quietly joining a pile.
    """
    if not agent_name:
        return "unknown"
    role = agent_name.split(":")[0]
    return _AGENT_MODULE.get(role, role)


_installed = False


def install() -> None:
    """Register the tracing hook. Idempotent, safe to call anywhere."""
    global _installed
    if _installed:
        return
    try:
        import os

        from agents import set_trace_processors, set_tracing_disabled

        # Replace the processor list rather than appending to it. The
        # default one exports to OpenAI and logs "OPENAI_API_KEY is not
        # set" on every run — which is why tracing was globally disabled
        # here in the first place, and why the counter silently recorded
        # nothing for eight agent runs. Owning the list fixes both: no
        # exporter, no warning, and the hook actually fires.
        set_trace_processors([_UsageProcessor()])
        os.environ.pop("OPENAI_AGENTS_DISABLE_TRACING", None)
        set_tracing_disabled(False)
        _installed = True
        logger.info("Token usage tracking installed (tracing enabled, "
                    "no external export)")
    except Exception as e:
        logger.warning("Token usage tracking unavailable: %s", e)


# ── Hook 2: raw OpenAI clients ──────────────────────────────

def instrument(client, module: str | None = None):
    """Wrap a client's ``create`` calls so their usage is recorded.

    Returns the same client. Handles sync and async, chat and embeddings,
    and swallows its own failures — an instrumented client must behave
    exactly like an uninstrumented one in every respect except that the
    numbers land.
    """
    try:
        _wrap(client.chat.completions, "chat", module)
    except Exception as e:
        logger.debug("Chat instrumentation skipped: %s", e)
    try:
        _wrap(client.embeddings, "embedding", module)
    except Exception as e:
        logger.debug("Embedding instrumentation skipped: %s", e)
    return client


def _wrap(holder, kind: str, module: str | None) -> None:
    import inspect

    original = holder.create
    if getattr(original, "_usage_wrapped", False):
        return

    def _log(resp):
        usage = getattr(resp, "usage", None)
        if usage is None:
            return resp
        record(module=_RAW_MODULE_LABELS.get(module, module)
                      or _module.get() or "unknown",
               model=getattr(resp, "model", None),
               input_tokens=_int(usage, "input_tokens", "prompt_tokens"),
               output_tokens=_int(usage, "output_tokens", "completion_tokens"),
               kind=kind)
        return resp

    def wrapper(*args, **kwargs):
        out = original(*args, **kwargs)
        # Decide from the *result*, not from the function. The async
        # client's `create` is a plain method returning a coroutine, so
        # `iscoroutinefunction` says False — and a sync wrapper then reads
        # `.usage` off an un-awaited coroutine, finds none, and drops the
        # record. The call still succeeds, so the loss is silent: digest
        # ran, billed, and counted zero.
        if inspect.isawaitable(out):
            async def _awaited():
                return _log(await out)
            return _awaited()
        return _log(out)

    wrapper._usage_wrapped = True
    holder.create = wrapper


# ── Reading it back ─────────────────────────────────────────

def summary(days: int = 7) -> dict:
    """Totals, plus the same numbers split by module, model and day."""
    try:
        conn = _conn()
        since = f"-{max(days - 1, 0)} days"
        rows = conn.execute(
            "SELECT module, model, kind, COUNT(*) calls, "
            " SUM(requests) requests, SUM(input_tokens) inp, "
            " SUM(output_tokens) outp, SUM(total_tokens) total "
            "FROM token_usage WHERE date >= date('now', ?) "
            "GROUP BY module, kind ORDER BY total DESC", (since,)).fetchall()
        by_day = conn.execute(
            "SELECT date, kind, SUM(total_tokens) total, COUNT(*) calls "
            "FROM token_usage WHERE date >= date('now', ?) "
            "GROUP BY date, kind ORDER BY date", (since,)).fetchall()
        models = conn.execute(
            "SELECT model, kind, COUNT(*) calls, SUM(total_tokens) total "
            "FROM token_usage WHERE date >= date('now', ?) "
            "GROUP BY model, kind ORDER BY total DESC", (since,)).fetchall()
        today = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) c, "
            " COALESCE(SUM(input_tokens),0) inp, "
            " COALESCE(SUM(output_tokens),0) outp "
            "FROM token_usage WHERE date = date('now')").fetchone()
        # Since the counter was installed, not since the window. The
        # window answers "what is it costing lately"; this answers "what
        # has it cost", which is the number anyone actually budgets on.
        alltime = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) c, "
            " COALESCE(SUM(input_tokens),0) inp, "
            " COALESCE(SUM(output_tokens),0) outp, "
            " MIN(date) since, COUNT(DISTINCT date) days "
            "FROM token_usage").fetchone()
        # Same columns as the window query: the UI renders both with one
        # component, and a missing column there showed as a dash in every
        # row — a table pretending to have data it never asked for.
        alltime_modules = conn.execute(
            "SELECT module, kind, COUNT(*) calls, SUM(requests) requests, "
            " SUM(input_tokens) inp, SUM(output_tokens) outp, "
            " SUM(total_tokens) total "
            "FROM token_usage GROUP BY module, kind "
            "ORDER BY total DESC").fetchall()
    except Exception as e:
        logger.warning("Usage summary unavailable: %s", e)
        return {"days": days, "modules": [], "by_day": [], "models": [],
                "today": _empty(), "window": _empty(), "alltime": _empty(),
                "alltime_modules": []}

    modules = [dict(r) for r in rows]
    return {
        "days": days,
        "modules": modules,
        "by_day": [dict(r) for r in by_day],
        "models": [dict(r) for r in models],
        "today": {"total_tokens": today["t"], "calls": today["c"],
                  "input_tokens": today["inp"], "output_tokens": today["outp"]},
        "window": {"total_tokens": sum(m["total"] or 0 for m in modules),
                   "calls": sum(m["calls"] or 0 for m in modules)},
        "alltime": {"total_tokens": alltime["t"], "calls": alltime["c"],
                    "input_tokens": alltime["inp"],
                    "output_tokens": alltime["outp"],
                    "since": alltime["since"], "active_days": alltime["days"]},
        "alltime_modules": [dict(r) for r in alltime_modules],
        # Kept so an older UI build does not read undefined.
        "total_tokens": sum(m["total"] or 0 for m in modules),
        "total_calls": sum(m["calls"] or 0 for m in modules),
    }


def _empty() -> dict:
    return {"total_tokens": 0, "calls": 0, "input_tokens": 0,
            "output_tokens": 0}


def recent(limit: int = 50) -> list[dict]:
    """The last N calls, newest first — for reading a spike."""
    try:
        rows = _conn().execute(
            "SELECT ts, module, model, kind, input_tokens, output_tokens, "
            " total_tokens FROM token_usage ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Usage log unavailable: %s", e)
        return []
