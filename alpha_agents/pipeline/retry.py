"""Retry helper for agent LLM calls with exponential backoff."""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from alpha_agents.config import AGENT_MAX_RETRIES, AGENT_RETRY_BASE_DELAY

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def run_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "agent",
    max_retries: int | None = None,
    base_delay: float | None = None,
) -> T:
    """Run an async function with exponential backoff retry.

    Does NOT retry on TimeoutError (already handled by caller's wait_for).
    Only retries on transient errors (API rate limits, network issues).

    Args:
        fn: Async callable to execute.
        label: Name for logging.
        max_retries: Override config default.
        base_delay: Override config default.

    Returns:
        The result of fn().

    Raises:
        The last exception if all retries are exhausted.
    """
    retries = max_retries if max_retries is not None else AGENT_MAX_RETRIES
    delay = base_delay if base_delay is not None else AGENT_RETRY_BASE_DELAY
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except asyncio.TimeoutError:
            raise  # Don't retry timeouts — let caller handle
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = delay * (2 ** (attempt - 1))
                logger.warning("%s attempt %d/%d failed: %s — retrying in %.1fs",
                               label, attempt, retries, e, wait)
                await asyncio.sleep(wait)
            else:
                logger.error("%s failed after %d attempts: %s", label, retries, e)

    raise last_err  # type: ignore[misc]
