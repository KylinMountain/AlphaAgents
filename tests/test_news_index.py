"""Semantic search over the flashes this system already collects.

Attribution asks "why did this sector move" and the answer is usually in
the 2500 flashes a day already in news_items. The agents instead reached
for DuckDuckGo — which on a mainland host returns SEO listicles when it
works at all — so the corpus this project maintains went unread.
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import news_index


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data.vector_store import VectorStore
    s = VectorStore(tmp_path / "news.db", table="news_vectors")
    monkeypatch.setattr(news_index, "_store", s)
    yield s
    s.close()


def fake_embed(texts):
    """A toy embedding: three axes keyed to words the tests care about."""
    out = []
    for t in texts:
        out.append([
            1.0 if ("原油" in t or "油价" in t or "crude" in t.lower()) else 0.0,
            1.0 if ("光刻机" in t or "半导体" in t) else 0.0,
            0.1,
        ])
    return out


class TestDocument:
    def test_summary_repeating_the_headline_is_not_doubled(self):
        row = {"title": "油价大涨", "summary": "油价大涨，布伦特逼近100美元"}
        assert news_index._document(row) == "油价大涨。，布伦特逼近100美元"

    def test_headline_only(self):
        assert news_index._document({"title": "油价大涨", "summary": ""}) == "油价大涨"

    def test_identical_summary_collapses(self):
        row = {"title": "油价大涨", "summary": "油价大涨"}
        assert news_index._document(row) == "油价大涨"


class TestIndexing:
    def _rows(self):
        return [
            {"title": "沙特能源设施遇袭 布伦特原油逼近100美元", "summary": "",
             "time": "2026-09-08 16:27:00", "source": "新浪7x24", "url": ""},
            {"title": "阿斯麦新一代EUV光刻机获台积电承诺", "summary": "",
             "time": "2026-09-08 17:12:00", "source": "财联社电报", "url": ""},
            {"title": "短", "summary": "", "time": "2026-09-08 17:00:00",
             "source": "金十数据", "url": ""},
        ]

    def test_indexes_and_skips_the_too_short(self, store):
        with patch.object(news_index, "prune_old", return_value=0), \
             patch("alpha_agents.data.snapshot_store.read_latest_news",
                   return_value=self._rows()), \
             patch("alpha_agents.data.embeddings.embed_texts", fake_embed):
            stats = news_index.index_recent_news(hours=24 * 365)

        assert stats["indexed"] == 2, "the one-character flash carries nothing"
        assert store.count() == 2

    def test_second_run_embeds_nothing(self, store):
        """Ids derive from source+time+headline, so overlap is free."""
        with patch.object(news_index, "prune_old", return_value=0), \
             patch("alpha_agents.data.snapshot_store.read_latest_news",
                   return_value=self._rows()), \
             patch("alpha_agents.data.embeddings.embed_texts", fake_embed) as emb:
            news_index.index_recent_news(hours=24 * 365)
            emb.reset_mock() if hasattr(emb, "reset_mock") else None
            stats = news_index.index_recent_news(hours=24 * 365)

        assert stats["indexed"] == 0
        assert stats["skipped"] == 2

    def test_a_failed_batch_does_not_pair_vectors_wrongly(self, store, caplog):
        """A short vector list would silently attach text to the wrong row."""
        with patch.object(news_index, "prune_old", return_value=0), \
             patch("alpha_agents.data.snapshot_store.read_latest_news",
                   return_value=self._rows()), \
             patch("alpha_agents.data.embeddings.embed_texts",
                   lambda texts: fake_embed(texts)[:1]), \
             caplog.at_level("ERROR"):
            stats = news_index.index_recent_news(hours=24 * 365)

        assert stats["indexed"] == 0
        assert store.count() == 0
        assert any("vectors for" in r.getMessage() for r in caplog.records)


class TestSearch:
    def test_finds_the_right_flash(self, store):
        store.upsert(
            ids=["a", "b"],
            embeddings=fake_embed(["原油上涨", "光刻机出货"]),
            documents=["沙特遇袭 原油上涨", "阿斯麦光刻机出货"],
            stamps=["2026-09-08 16:00:00", "2026-09-08 17:00:00"],
            metas=[{"source": "新浪7x24", "title": "沙特遇袭 原油上涨"},
                   {"source": "财联社电报", "title": "阿斯麦光刻机出货"}],
        )
        with patch("alpha_agents.data.embeddings.embed_texts", fake_embed), \
             patch.object(news_index, "datetime") as dt:
            dt.now.return_value.__sub__ = lambda *a: type(
                "X", (), {"strftime": lambda self, f: "2026-09-08 00:00:00"})()
            dt.now.return_value.strftime.return_value = "2026-09-09 00:00:00"
            hits = news_index.search_news("原油", hours=24)

        assert hits and hits[0]["source"] == "新浪7x24"

    def test_low_similarity_returns_nothing(self, store):
        """Cosine always returns something; a weak match is worse than none."""
        store.upsert(
            ids=["a"], embeddings=fake_embed(["原油上涨"]),
            documents=["沙特遇袭 原油上涨"], stamps=["2026-09-08 16:00:00"],
            metas=[{"source": "新浪7x24", "title": "沙特遇袭"}],
        )
        with patch("alpha_agents.data.embeddings.embed_texts", fake_embed), \
             patch.object(news_index, "datetime") as dt:
            dt.now.return_value.__sub__ = lambda *a: type(
                "X", (), {"strftime": lambda self, f: "2026-09-08 00:00:00"})()
            dt.now.return_value.strftime.return_value = "2026-09-09 00:00:00"
            hits = news_index.search_news("完全无关的东西", hours=24,
                                          min_score=0.9)
        assert hits == []

    def test_empty_query(self, store):
        assert news_index.search_news("", hours=24) == []

    def test_an_unreadable_index_is_not_an_empty_one(self, store, caplog):
        """Returning [] for a failure is how an outage became a market fact.

        Live 2026-09-22: 343 embedding calls returned 402 "account balance
        is insufficient" over three and a half hours. search_news swallowed
        every one into an empty list, _news_for_theme swallowed that into
        an empty list, and the exit prompt told the agent
        "无（主线无新消息，不等于逻辑破坏）" for every theme it held. Three
        layers each turning a failure into "no data", ending in a claim
        about the market.
        """
        with patch("alpha_agents.data.embeddings.embed_texts",
                   side_effect=RuntimeError("api down")), \
             caplog.at_level("WARNING"), \
             pytest.raises(news_index.NewsSearchUnavailable):
            news_index.search_news("原油", hours=24)
        assert any("unavailable" in r.getMessage() for r in caplog.records)

    def test_it_is_still_not_fatal_to_a_caller(self, store):
        """The original intent survives: both callers catch it and carry on,
        they just get to say which thing happened."""
        import inspect
        from alpha_agents.pipeline.tasks import anomaly_scan, exit_decision
        for src in (inspect.getsource(anomaly_scan._news_block
                                      if hasattr(anomaly_scan, "_news_block")
                                      else anomaly_scan),
                    inspect.getsource(exit_decision._news_for_theme)):
            assert "except Exception" in src

    def test_a_genuinely_quiet_theme_still_returns_empty(self, store):
        """The distinction only means something if the other side works."""
        with patch("alpha_agents.data.embeddings.embed_texts",
                   fake_embed):
            assert news_index.search_news("完全不相关的主题", hours=24,
                                          min_score=0.99) == []


class TestRetention:
    def test_prune_drops_old_vectors(self, store):
        store.upsert(
            ids=["old", "new"],
            embeddings=fake_embed(["原油", "原油"]),
            documents=["旧闻", "新闻"],
            stamps=["2026-01-01 00:00:00", "2026-09-08 16:00:00"],
        )
        gone = store.prune_before("2026-06-01 00:00:00")
        assert gone == 1
        assert store.count() == 1


class TestAttributionEvidence:
    """The intraday task looks the news up itself rather than hoping the
    model reaches for its search_news tool."""

    def test_sectors_with_news_are_quoted(self):
        from alpha_agents.pipeline.tasks.intraday_monitor import _news_for_sectors

        hits = [{"title": "沙特能源设施遇袭 布伦特原油逼近100美元",
                 "source": "新浪7x24", "time": "2026-09-08 16:27:00",
                 "score": 0.65, "text": "", "url": ""}]
        with patch("alpha_agents.data.news_index.search_news",
                   return_value=hits):
            out = _news_for_sectors(["石油加工贸易"])

        assert "石油加工贸易" in out
        assert "16:27" in out
        assert "布伦特原油" in out

    def test_a_sector_with_no_news_says_so(self):
        """Silence is a finding: flow can lead the news, or be pure flow."""
        from alpha_agents.pipeline.tasks.intraday_monitor import _news_for_sectors

        with patch("alpha_agents.data.news_index.search_news", return_value=[]):
            out = _news_for_sectors(["小金属概念"])

        assert "无相关快讯" in out
        assert "资金异动可能先于新闻" in out

    def test_no_sectors_returns_nothing(self):
        from alpha_agents.pipeline.tasks.intraday_monitor import _news_for_sectors

        assert _news_for_sectors([]) == ""

    def test_lookup_failure_degrades_quietly(self):
        """A dead index must not take the whole attribution down."""
        from alpha_agents.pipeline.tasks.intraday_monitor import _news_for_sectors

        with patch("alpha_agents.data.news_index.search_news",
                   side_effect=RuntimeError("index gone")):
            assert _news_for_sectors(["石油加工贸易"]) == ""
