"""Headline extraction and Jin10's duplicate English feed.

Both defects were visible on the deployed dashboard: a headline cut as
"Total turnover on the Shanghai and Shenzhen exchan", and every Chinese
flash followed four to six seconds later by its English translation as a
separate row.
"""

from alpha_agents.sources.flash_text import (
    drop_translation_twins, has_chinese, headline,
)


class TestHeadline:
    def test_bracket_wrapper_wins(self):
        assert headline("【中金公司：预计2028年市场规模达148亿元】金十数据9月8日讯，中金公司研报认为…") \
            == "中金公司：预计2028年市场规模达148亿元"

    def test_short_content_returned_whole(self):
        assert headline("现货黄金站上4430美元/盎司，日内涨0.54%。") \
            == "现货黄金站上4430美元/盎司，日内涨0.54%。"

    def test_cuts_at_sentence_end(self):
        text = ("美元兑日元日内跌超0.50%，现报153.54。此前公布的日本二季度GDP"
                "环比折年率上修至2.2%，高于市场预期的1.8%，为连续第五个季度扩张。")
        assert headline(text) == "美元兑日元日内跌超0.50%，现报153.54"

    def test_latin_text_never_splits_a_word(self):
        text = ("Total turnover on the Shanghai and Shenzhen exchanges fell to "
                "1.95 trillion yuan on Sept. 7, down from the prior session")
        got = headline(text)
        assert "exchan…" not in got
        assert got.rstrip("…").split()[-1] in text.split()

    def test_empty(self):
        assert headline("") == ""
        assert headline(None) == ""


class TestHasChinese:
    def test_detects(self):
        assert has_chinese("现货黄金站上4430美元")
        assert not has_chinese("Spot gold traded above $4,430/oz.")


class TestDropTranslationTwins:
    def _flash(self, time, summary):
        return {"title": summary[:20], "summary": summary, "time": time,
                "source": "金十数据"}

    def test_english_repost_is_dropped(self):
        items = [
            self._flash("2026-09-08 08:21:31", "Spot gold traded above $4,430/oz."),
            self._flash("2026-09-08 08:21:27", "现货黄金站上4430美元/盎司，日内涨0.54%。"),
        ]

        kept = drop_translation_twins(items)

        assert [k["summary"] for k in kept] == ["现货黄金站上4430美元/盎司，日内涨0.54%。"]

    def test_english_only_flash_survives(self):
        """The reason this is a window match and not "drop all English"."""
        items = [
            self._flash("2026-09-08 08:21:27", "现货黄金站上4430美元/盎司。"),
            self._flash("2026-09-08 03:00:00", "Fed's Powell speaks at Jackson Hole."),
        ]

        kept = drop_translation_twins(items)

        assert len(kept) == 2

    def test_all_chinese_feed_untouched(self):
        items = [self._flash("2026-09-08 08:2%d:00" % i, f"快讯{i}") for i in range(5)]
        assert drop_translation_twins(items) == items

    def test_international_feed_is_not_collateral(self):
        """The filter is scoped to Jin10 for this reason.

        The dashboard's feed is mixed. Chinese flashes arrive every few
        seconds, so an unscoped window match would find a "twin" for every
        BBC headline and wipe the international feed off the panel.
        """
        items = [
            {"title": "China stocks slip", "summary": "China stocks slip",
             "time": "2026-09-08 08:21:29", "source": "BBC Business"},
            self._flash("2026-09-08 08:21:27", "现货黄金站上4430美元/盎司。"),
        ]

        kept = drop_translation_twins(items)

        assert len(kept) == 2
        assert any(k["source"] == "BBC Business" for k in kept)

    def test_unparseable_timestamp_is_kept(self):
        items = [
            self._flash("", "Some English headline with no usable stamp"),
            self._flash("2026-09-08 08:21:27", "中文快讯"),
        ]
        assert len(drop_translation_twins(items)) == 2
