import json
from pathlib import Path

import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.tools.stock_filter import filter_stocks_fn


@pytest.fixture
def populated_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT INTO stocks VALUES ('000001', '平安银行', 200000000000, '银行', 0, 0)")
    conn.execute("INSERT INTO stocks VALUES ('000002', 'ST世纪', 500000000, '房地产', 1, 0)")
    conn.execute("INSERT INTO stocks VALUES ('000003', '微型公司', 100000000, '其他', 0, 0)")
    conn.execute("INSERT INTO stocks VALUES ('000004', '停牌股', 5000000000, '科技', 0, 1)")
    conn.commit()
    conn.close()
    return db_path


def test_filter_removes_st(populated_db):
    result = filter_stocks_fn(["000001", "000002"], populated_db)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000001" in codes
    assert "000002" not in codes


def test_filter_removes_suspended(populated_db):
    result = filter_stocks_fn(["000001", "000004"], populated_db)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000004" not in codes


def test_filter_removes_small_cap(populated_db):
    result = filter_stocks_fn(["000001", "000003"], populated_db, min_market_cap=1e9)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000003" not in codes


def test_filter_reports_removed(populated_db):
    result = filter_stocks_fn(["000001", "000002", "000003", "000004"], populated_db)
    parsed = json.loads(result)
    assert len(parsed["removed"]) == 3
