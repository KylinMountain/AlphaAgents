import json
from pathlib import Path

import pytest

from alpha_agents.tools.watchlist import get_watchlist_fn


@pytest.fixture
def watchlist_file(tmp_path):
    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "stocks": [
            {"code": "600519", "name": "贵州茅台", "concepts": ["白酒", "消费"]},
        ]
    }), encoding="utf-8")
    return wl_path


def test_get_watchlist(watchlist_file):
    result = get_watchlist_fn(watchlist_file)
    parsed = json.loads(result)
    assert len(parsed["stocks"]) == 1
    assert parsed["stocks"][0]["code"] == "600519"
    assert "白酒" in parsed["stocks"][0]["concepts"]


def test_get_watchlist_empty(tmp_path):
    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({"stocks": []}), encoding="utf-8")
    result = get_watchlist_fn(wl_path)
    parsed = json.loads(result)
    assert len(parsed["stocks"]) == 0
