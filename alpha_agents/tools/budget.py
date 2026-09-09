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
import functools
import logging
import os
import time

logger = logging.getLogger(__name__)

# Generous by design. This is not a latency target — it is the point past
# which a call is almost certainly stuck rather than slow. Healthy tools
# in this system return in well under ten seconds; the ones that hang do
# so on connect timeouts and retries, which land far beyond this.
TOOL_TIMEOUT = int(os.environ.get("TOOL_TIMEOUT", "25"))

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
        started = time.monotonic()
        future = _POOL.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=limit)
        except concurrent.futures.TimeoutError:
            logger.warning("Tool %s exceeded %ds — reported as unavailable "
                           "so the run continues", fn.__name__, limit)
            return (f'{{"error": "数据源 {fn.__name__} 超过 {limit}秒未响应，'
                    f'本次不可用。不要重试这个工具，用你已有的信息继续，'
                    f'并在报告里说明缺了什么。"}}')
        except Exception as e:
            elapsed = time.monotonic() - started
            logger.warning("Tool %s failed after %.1fs: %s",
                           fn.__name__, elapsed, e)
            return (f'{{"error": "数据源 {fn.__name__} 调用失败：'
                    f'{str(e)[:120]}。不要重试，用已有信息继续。"}}')

    return wrapper
