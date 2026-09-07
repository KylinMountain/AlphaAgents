"""Structured metadata that the flash sources carry and used to discard."""

from alpha_agents.sources.jin10 import _parse_item as jin10_parse
from alpha_agents.sources.sina_7x24 import _normalise_symbol
from alpha_agents.sources.sina_7x24 import _parse_item as sina_parse


class TestSinaSymbolNormalisation:
    """A-share codes must join with the rest of the project on 6 digits."""

    def test_strips_exchange_prefix(self):
        for symbol, code in [("sh603828", "603828"), ("sz300308", "300308"),
                             ("bj430139", "430139")]:
            got = _normalise_symbol({"market": "cn", "symbol": symbol, "key": "X"})
            assert got == {"code": code, "name": "X", "market": "cn"}

    def test_keeps_commodity_symbols_verbatim(self):
        got = _normalise_symbol({"market": "commodity", "symbol": "nf_cu0", "key": "铜"})
        assert got == {"code": "nf_cu0", "name": "铜", "market": "commodity"}

    def test_keeps_hk_symbols_verbatim(self):
        got = _normalise_symbol({"market": "hk", "symbol": "01810", "key": "小米"})
        assert got["code"] == "01810"

    def test_unprefixed_cn_symbol_is_left_alone(self):
        got = _normalise_symbol({"market": "cn", "symbol": "notacode", "key": "X"})
        assert got["code"] == "notacode"

    def test_empty_symbol_is_dropped(self):
        assert _normalise_symbol({"market": "cn", "symbol": "", "key": "X"}) is None


class TestSinaParse:
    def test_extracts_linked_stocks_and_tags(self):
        item = {
            "rich_text": "【*ST利达：收到行政处罚告知书】公司公告称……",
            "create_time": "2026-09-07 22:21:26",
            "tag": [{"id": "3", "name": "公司"}],
            "ext": {
                "stocks": [{"market": "cn", "symbol": "sh603828", "key": "*ST利达"}],
                "docurl": "https://finance.sina.com.cn/7x24/x.shtml",
            },
        }
        got = sina_parse(item)
        assert got["title"] == "*ST利达：收到行政处罚告知书"
        assert got["stocks"] == [{"code": "603828", "name": "*ST利达", "market": "cn"}]
        assert got["tags"] == ["公司"]
        assert got["link"].endswith("x.shtml")
        assert got["source"] == "新浪7x24"

    def test_ext_may_arrive_as_a_json_string(self):
        item = {
            "rich_text": "消息",
            "ext": '{"stocks": [{"market": "cn", "symbol": "sz300308", "key": "中际旭创"}]}',
        }
        assert sina_parse(item)["stocks"][0]["code"] == "300308"

    def test_malformed_ext_degrades_to_no_stocks(self):
        got = sina_parse({"rich_text": "消息", "ext": "{not json"})
        assert got["stocks"] == []
        assert got["title"] == "消息"

    def test_untitled_item_falls_back_to_leading_text(self):
        got = sina_parse({"rich_text": "市场消息：某某事件发生了" * 5})
        assert len(got["title"]) == 50

    def test_missing_fields_do_not_raise(self):
        got = sina_parse({})
        assert got["title"] == "" and got["stocks"] == [] and got["tags"] == []


class TestJin10Parse:
    """Jin10 flags its own market-moving items; that is the only ranking
    signal in the payload."""

    def test_keeps_the_important_flag(self):
        item = {"important": 1, "type": 0, "time": "2026-09-07 21:54:12",
                "data": {"content": "【重磅】某事"}}
        got = jin10_parse(item)
        assert got["important"] is True
        assert got["title"] == "重磅"

    def test_plain_item_is_not_important(self):
        got = jin10_parse({"important": 0, "data": {"content": "普通快讯"}})
        assert got["important"] is False

    def test_vip_item_uses_vip_title_as_body(self):
        item = {"data": {"content": "", "vip_title": "道明证券前瞻美国8月CPI"}}
        got = jin10_parse(item)
        assert "CPI" in got["title"]

    def test_carries_origin_and_link(self):
        item = {"data": {"content": "某事", "source": "新华社",
                         "source_link": "https://example.com/a"}}
        got = jin10_parse(item)
        assert got["origin"] == "新华社"
        assert got["link"] == "https://example.com/a"

    def test_type_distinguishes_flash_from_feature(self):
        assert jin10_parse({"type": 2, "data": {"content": "要闻"}})["type"] == 2
        assert jin10_parse({"data": {"content": "快讯"}})["type"] == 0

    def test_missing_fields_do_not_raise(self):
        got = jin10_parse({})
        assert got["title"] == "" and got["important"] is False
