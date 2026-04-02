import json
from pathlib import Path

import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.tools.stock_search import search_stocks_fn


@pytest.fixture
def populated_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT INTO concepts (name, source) VALUES ('国产替代', 'ths')")
    conn.execute("INSERT INTO concepts (name, source) VALUES ('光刻胶', 'ths')")
    conn.execute("INSERT INTO concepts (name, source) VALUES ('白酒', 'ths')")
    conn.execute(
        "INSERT INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
        "VALUES ('688001', '华兴源创', 10000000000, '半导体', 0, 0)"
    )
    conn.execute(
        "INSERT INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
        "VALUES ('300236', '上海新阳', 8000000000, '化工', 0, 0)"
    )
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (1, '688001')")
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (2, '688001')")
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (2, '300236')")
    conn.commit()
    conn.close()
    return db_path


def test_search_exact_match(populated_db):
    result = search_stocks_fn("国产替代", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 1
    assert parsed["matches"][0]["concept"] == "国产替代"
    assert len(parsed["matches"][0]["stocks"]) == 1
    assert parsed["matches"][0]["stocks"][0]["code"] == "688001"


def test_search_fuzzy_match(populated_db):
    result = search_stocks_fn("光刻", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 1
    assert parsed["matches"][0]["concept"] == "光刻胶"


def test_search_no_match(populated_db):
    result = search_stocks_fn("火星探索", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 0


def test_search_multiple_matches(populated_db):
    result = search_stocks_fn("国产", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) >= 1
