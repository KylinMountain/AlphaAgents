"""world_news fetches its feeds in parallel.

Five of the fourteen RSS feeds are dead and each burns a connect timeout
plus retries. Fetched serially those added up past the ingest task's
ceiling and cancelled the whole sweep — including feeds that had already
answered. Observed on the deployed box: news_ingest timed out at both
120s and 150s.
"""

import time
from unittest.mock import MagicMock, patch

from alpha_agents.sources import world_news


def _rss(title: str) -> str:
    return (
        '<?xml version="1.0"?><rss><channel>'
        f"<item><title>{title}</title><description>d</description>"
        "<pubDate>Mon, 07 Sep 2026 10:00:00 GMT</pubDate>"
        "<link>https://example.com/a</link></item>"
        "</channel></rss>"
    )


class TestParallelFetch:
    def test_slow_feeds_overlap_rather_than_accumulate(self):
        """The whole point: cost is the slowest feed, not their sum."""
        delay = 0.3

        def slow_get(url):
            time.sleep(delay)
            resp = MagicMock()
            resp.text = _rss("headline")
            resp.raise_for_status = lambda: None
            return resp

        client = MagicMock()
        client.get.side_effect = slow_get

        with patch.object(world_news, "client_session") as session, \
             patch.object(world_news, "replay_news_response", return_value=None,
                          create=True), \
             patch("alpha_agents.data.snapshot_store.replay_news_response",
                   return_value=None), \
             patch("alpha_agents.data.snapshot_store.save_news",
                   return_value=0):
            session.return_value.__enter__.return_value = client

            start = time.time()
            world_news.get_world_news_fn(limit=5)
            elapsed = time.time() - start

        n = len(world_news.RSS_FEEDS)
        # Serial would be n * delay; parallel should be a small multiple
        # of delay. Generous bound so the test is not timing-flaky.
        assert elapsed < n * delay * 0.6, (
            f"{elapsed:.2f}s for {n} feeds at {delay}s each — looks serial"
        )

    def test_one_failing_feed_does_not_lose_the_others(self):
        def flaky_get(url):
            if "fail" in url:
                raise ConnectionError("dead feed")
            resp = MagicMock()
            resp.text = _rss("good headline")
            resp.raise_for_status = lambda: None
            return resp

        client = MagicMock()
        client.get.side_effect = flaky_get

        feeds = [("Good", "https://ok.example/rss"),
                 ("Dead", "https://fail.example/rss")]

        with patch.object(world_news, "RSS_FEEDS", feeds), \
             patch.object(world_news, "client_session") as session, \
             patch("alpha_agents.data.snapshot_store.replay_news_response",
                   return_value=None), \
             patch("alpha_agents.data.snapshot_store.save_news",
                   return_value=0):
            session.return_value.__enter__.return_value = client
            import json
            got = json.loads(world_news.get_world_news_fn(limit=10))

        assert got["count"] == 1
        assert got["news"][0]["source"] == "Good"

    def test_every_feed_is_attempted(self):
        client = MagicMock()
        resp = MagicMock()
        resp.text = _rss("h")
        resp.raise_for_status = lambda: None
        client.get.return_value = resp

        with patch.object(world_news, "client_session") as session, \
             patch("alpha_agents.data.snapshot_store.replay_news_response",
                   return_value=None), \
             patch("alpha_agents.data.snapshot_store.save_news",
                   return_value=0):
            session.return_value.__enter__.return_value = client
            world_news.get_world_news_fn(limit=100)

        assert client.get.call_count == len(world_news.RSS_FEEDS)
