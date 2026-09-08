"""An empty digest must say which kind of empty it is.

Observed on a live run: 303 flashes went in, both batches hit
APITimeoutError, digest_news returned [], and the monitor logged "No
significant events after digest filtering". On the dashboard that is
indistinguishable from a calm session — the pipeline was dead and nothing
on screen said so.
"""

from unittest.mock import AsyncMock, patch

import pytest

from alpha_agents.pipeline import digest


def _items(n):
    return [
        {"title": f"快讯 {i}", "summary": "正文", "time": "2026-09-08 09:30:00",
         "source": "财联社电报"}
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def digest_env(monkeypatch):
    """Snapshot capture and the API-key guard are not what is under test."""
    monkeypatch.setattr("alpha_agents.data.snapshot_store.save_news",
                        lambda *a, **k: 0)
    monkeypatch.setattr(digest, "DIGEST_API_KEY", "test-key")


class TestBatching:
    def test_batching_is_token_bound_not_item_bound(self):
        """Measured: latency tracks output size, not input count.

        10 items took 79s and 150 took 110s against the same model, so an
        item cap would only multiply the calls at roughly the same cost
        each. A short list must stay a single batch.
        """
        assert len(digest._split_into_batches(_items(150))) == 1

    def test_no_item_is_dropped(self):
        items = _items(137)
        batches = digest._split_into_batches(items)
        assert sum(len(b) for b in batches) == len(items)

    def test_empty_input(self):
        assert digest._split_into_batches([]) == []


class TestFailureIsVisible:
    @pytest.mark.asyncio
    async def test_total_failure_is_logged_as_a_failure(self, caplog):
        with patch.object(digest, "_get_client"), \
             patch.object(digest, "_digest_batch",
                          AsyncMock(side_effect=TimeoutError("timed out"))), \
             patch("alpha_agents.data.activity_log.log_activity") as log_act:
            with caplog.at_level("ERROR"):
                events = await digest.digest_news(_items(120))

        assert events == []
        assert any("all" in r.message and "batches failed" in r.message
                   for r in caplog.records), caplog.text
        assert log_act.called, "the dashboard needs an activity row too"
        assert log_act.call_args.kwargs["status"] == "failed"

    @pytest.mark.asyncio
    async def test_partial_failure_says_the_list_is_incomplete(
            self, caplog, monkeypatch):
        # Batching is token-bound, so a small budget is what forces two
        # batches — an item count no longer does.
        monkeypatch.setattr(digest, "MAX_INPUT_TOKENS", 400)
        calls = {"n": 0}

        async def flaky(_client, batch):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return [{"event": "某事件", "importance": 5}]

        with patch.object(digest, "_get_client"), \
             patch.object(digest, "_digest_batch", flaky), \
             patch("alpha_agents.data.activity_log.log_activity") as log_act:
            with caplog.at_level("WARNING"):
                events = await digest.digest_news(_items(120))

        assert events, "the batches that worked still count"
        assert any("partial" in r.message for r in caplog.records), caplog.text
        assert not log_act.called, "partial success is not a pipeline failure"

    @pytest.mark.asyncio
    async def test_genuinely_quiet_market_logs_nothing_alarming(self, caplog):
        with patch.object(digest, "_get_client"), \
             patch.object(digest, "_digest_batch", AsyncMock(return_value=[])), \
             patch("alpha_agents.data.activity_log.log_activity") as log_act:
            with caplog.at_level("WARNING"):
                events = await digest.digest_news(_items(30))

        assert events == []
        assert not log_act.called
        assert not caplog.records, "no news is not an error"
