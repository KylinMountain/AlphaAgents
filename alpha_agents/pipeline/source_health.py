"""Data source health monitoring.

Tracks success/failure rates for each news source and exposes
health status via API. Unhealthy sources are automatically skipped
to avoid wasting time on known-dead endpoints.
"""

import logging
import time
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Sources failing this many consecutive times are marked unhealthy
UNHEALTHY_THRESHOLD = 3
# Unhealthy sources are retried after this many seconds
RETRY_AFTER_SECONDS = 600  # 10 minutes


@dataclass
class SourceStatus:
    source_id: str
    name: str
    total_calls: int = 0
    total_success: int = 0
    total_items: int = 0
    consecutive_failures: int = 0
    last_success_time: float = 0.0
    last_failure_time: float = 0.0
    last_error: str = ""
    healthy: bool = True


class SourceHealthTracker:
    """Tracks health of all news sources."""

    def __init__(self, retry_after: float = RETRY_AFTER_SECONDS):
        self._sources: dict[str, SourceStatus] = {}
        self._retry_after = retry_after

    def register(self, source_id: str, name: str):
        if source_id not in self._sources:
            self._sources[source_id] = SourceStatus(source_id=source_id, name=name)

    def record_success(self, source_id: str, item_count: int = 0):
        s = self._sources.get(source_id)
        if not s:
            return
        s.total_calls += 1
        s.total_success += 1
        s.total_items += item_count
        s.consecutive_failures = 0
        s.last_success_time = time.time()
        if not s.healthy:
            logger.info("Source %s recovered", s.name)
            s.healthy = True

    def record_failure(self, source_id: str, error: str = ""):
        s = self._sources.get(source_id)
        if not s:
            return
        s.total_calls += 1
        s.consecutive_failures += 1
        s.last_failure_time = time.time()
        s.last_error = error[:200]
        if s.consecutive_failures >= UNHEALTHY_THRESHOLD and s.healthy:
            logger.warning("Source %s marked unhealthy after %d consecutive failures",
                           s.name, s.consecutive_failures)
            s.healthy = False

    def should_skip(self, source_id: str) -> bool:
        """Return True if source is unhealthy and not due for retry."""
        s = self._sources.get(source_id)
        if not s or s.healthy:
            return False
        # Allow periodic retry of unhealthy sources
        if time.time() - s.last_failure_time > self._retry_after:
            logger.info("Retrying unhealthy source %s", s.name)
            return False
        return True

    def get_status(self) -> list[dict]:
        """Return health status of all sources."""
        result = []
        for s in self._sources.values():
            d = asdict(s)
            if s.total_calls > 0:
                d["success_rate"] = round(s.total_success / s.total_calls * 100, 1)
            else:
                d["success_rate"] = 0.0
            result.append(d)
        return result


# Global singleton
health_tracker = SourceHealthTracker()
