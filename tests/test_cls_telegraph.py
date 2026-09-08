"""财联社电报 — the current endpoint and its local signing.

akshare's stock_info_global_cls calls cls.cn/nodeapi/telegraphList, which
CLS retired around 2026-05 and now 404s, wrapped in a ten-attempt retry
so it presented as a multi-minute hang. This module calls
/v1/roll/get_roll_list directly.
"""

import hashlib
import json
import time
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from alpha_agents.sources import cls_telegraph as cls


class TestSigning:
    def test_matches_md5_of_sha1_of_sorted_query(self):
        params = {"b": "2", "a": "1", "c": "3"}
        expected = hashlib.md5(
            hashlib.sha1(b"a=1&b=2&c=3").hexdigest().encode()
        ).hexdigest()
        assert cls._sign(params) == expected

    def test_key_order_does_not_change_the_signature(self):
        assert cls._sign({"a": 1, "b": 2}) == cls._sign({"b": 2, "a": 1})

    def test_different_params_sign_differently(self):
        assert cls._sign({"rn": 20}) != cls._sign({"rn": 30})


class TestBuildUrl:
    def test_targets_the_current_endpoint(self):
        assert cls._build_url(20).startswith(cls.ROLL_URL)
        assert "nodeapi/telegraphList" not in cls._build_url(20)

    def test_carries_a_signature(self):
        q = parse_qs(urlparse(cls._build_url(20)).query)
        assert len(q["sign"][0]) == 32          # md5 hex

    def test_signature_covers_the_other_params(self):
        """The signature must be reproducible from the URL as sent.

        keep_blank_values matters: category is sent empty, and parse_qs
        drops empty values by default — reconstructing without it yields
        a different query string and a signature that never matches.
        """
        q = parse_qs(urlparse(cls._build_url(20)).query, keep_blank_values=True)
        sent = {k: v[0] for k, v in q.items() if k != "sign"}
        assert cls._sign(sent) == q["sign"][0]

    def test_limit_is_clamped_to_the_api_maximum(self):
        q = parse_qs(urlparse(cls._build_url(9999)).query)
        assert int(q["rn"][0]) == 50
        q = parse_qs(urlparse(cls._build_url(0)).query)
        assert int(q["rn"][0]) == 1


class TestParseItem:
    def test_reads_content_and_timestamp(self):
        got = cls._parse_item({
            "content": "【某某涨价】详情……", "ctime": 1757289600, "level": "C",
        })
        assert got["title"] == "某某涨价"
        assert got["source"] == "财联社电报"
        assert got["time"].startswith("2025-") or got["time"][:2] == "20"

    def test_explicit_title_wins_over_derivation(self):
        got = cls._parse_item({"title": "正式标题", "content": "【别的】x"})
        assert got["title"] == "正式标题"

    def test_untitled_content_falls_back_to_a_prefix(self):
        """Prefix length is the headline helper's call, not a fixed 50.

        It cuts at a clause boundary where one exists, so asserting an
        exact length here would just re-pin the old mid-word slice.
        """
        got = cls._parse_item({"content": "没有方括号的一段正文" * 10})
        assert 40 < len(got["title"]) < 70
        assert got["title"].startswith("没有方括号的一段正文")

    def test_level_a_is_marked_important(self):
        assert cls._parse_item({"content": "x", "level": "A"})["important"]
        assert not cls._parse_item({"content": "x", "level": "C"})["important"]

    def test_bad_timestamp_does_not_raise(self):
        assert cls._parse_item({"content": "x", "ctime": "不是数字"})["time"] == ""

    def test_empty_row_is_dropped(self):
        assert cls._parse_item({}) is None


class TestFetch:
    def _resp(self, payload):
        r = MagicMock()
        r.text = json.dumps(payload, ensure_ascii=False)
        return r

    def test_parses_a_successful_response(self):
        payload = {"errno": 0, "data": {"roll_data": [
            {"content": "【一】a", "ctime": int(time.time()), "level": "A"},
            {"content": "【二】b", "ctime": int(time.time()), "level": "C"},
        ]}}
        with patch.object(cls, "fetch", return_value=self._resp(payload)), \
             patch("alpha_agents.data.snapshot_store.save_news", return_value=0):
            got = json.loads(cls.get_cls_telegraph_fn(limit=10))
        assert got["count"] == 2
        assert got["important_count"] == 1

    def test_api_level_error_is_surfaced(self):
        payload = {"errno": 10012, "msg": "签名错误"}
        with patch.object(cls, "fetch", return_value=self._resp(payload)):
            got = json.loads(cls.get_cls_telegraph_fn())
        assert got["count"] == 0
        assert "10012" in got["error"]

    def test_network_failure_is_reported_not_raised(self):
        with patch.object(cls, "fetch", side_effect=ConnectionError("down")):
            got = json.loads(cls.get_cls_telegraph_fn())
        assert got["count"] == 0 and "down" in got["error"]

    def test_important_only_filters(self):
        payload = {"errno": 0, "data": {"roll_data": [
            {"content": "【重要】a", "ctime": int(time.time()), "level": "A"},
            {"content": "【普通】b", "ctime": int(time.time()), "level": "C"},
        ]}}
        with patch.object(cls, "fetch", return_value=self._resp(payload)), \
             patch("alpha_agents.data.snapshot_store.save_news", return_value=0):
            got = json.loads(cls.get_cls_telegraph_fn(important_only=True))
        assert got["count"] == 1
        assert got["news"][0]["title"] == "重要"
