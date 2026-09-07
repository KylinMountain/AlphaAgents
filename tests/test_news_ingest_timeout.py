"""One slow source must not cancel the whole ingest sweep.

http_client allows 15s per request with three retries and backoff, so a
single unreachable domain can burn 67s, and a source that walks several
sub-URLs multiplies that. On the deployed box two such sources in one
sweep exceeded the task's own timeout and cancelled everything, including
the sources that had already answered.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from alpha_agents.pipeline.tasks import news_ingest


def _ok(n=3):
    return json.dumps({"news": [{"title": f"t{i}"} for i in range(n)],
                       "count": n}, ensure_ascii=False)


class TestPerSourceTimeout:
    @pytest.mark.asyncio
    async def test_slow_source_is_dropped_not_fatal(self):
        def hangs():
            time.sleep(30)
            return _ok()

        with patch.object(news_ingest, "_PER_SOURCE_TIMEOUT", 0.3):
            name, count, err = await news_ingest._ingest_one("s", "慢源", hangs)

        assert count == 0
        assert "超过" in err          # reported, not raised

    @pytest.mark.asyncio
    async def test_fast_source_is_unaffected(self):
        with patch.object(news_ingest, "_PER_SOURCE_TIMEOUT", 5):
            name, count, err = await news_ingest._ingest_one("s", "快源", _ok)
        assert count == 3 and err == ""

    @pytest.mark.asyncio
    async def test_the_sweep_survives_a_hanging_source(self):
        """The behaviour that actually failed in production."""
        def hangs():
            time.sleep(30)
            return _ok()

        sources = [("a", "好源A", _ok), ("b", "卡死源", hangs),
                   ("c", "好源C", _ok)]

        with patch.object(news_ingest, "NEWS_SOURCES", sources), \
             patch.object(news_ingest, "_PER_SOURCE_TIMEOUT", 0.3), \
             patch.object(news_ingest, "log_activity"):
            start = time.time()
            await news_ingest.run_news_ingest()
            elapsed = time.time() - start

        # Bounded by the per-source ceiling, not by the hanging source.
        assert elapsed < 5, f"{elapsed:.1f}s — 卡死源拖垮了整轮"

    @pytest.mark.asyncio
    async def test_source_raising_is_reported_not_raised(self):
        def boom():
            raise ConnectionError("dead")

        name, count, err = await news_ingest._ingest_one("s", "坏源", boom)
        assert count == 0 and "ConnectionError" in err

    @pytest.mark.asyncio
    async def test_source_returning_an_error_payload_is_counted_as_failed(self):
        def errored():
            return json.dumps({"news": [], "count": 0, "error": "API 4xx"})

        name, count, err = await news_ingest._ingest_one("s", "错误源", errored)
        assert count == 0 and "API 4xx" in err
