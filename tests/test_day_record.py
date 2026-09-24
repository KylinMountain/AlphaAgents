"""The day's own record the close review diagnoses."""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.evolution import day_record as DR


@pytest.fixture()
def dbs():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE virtual_portfolio (id INTEGER PRIMARY KEY, code TEXT, "
                 "name TEXT, theme TEXT, order_date TEXT, open_date TEXT, "
                 "open_price REAL, shares INT, status TEXT, close_date TEXT, "
                 "return_pct REAL, reason TEXT, close_reason TEXT, trader_id TEXT)")
    rows = [  # held since the 5th and added to on the 6th; one unfilled; one sold
        ("600001", "甲", "算力", "2026-01-05", "2026-01-05", 10.0, 100, "open", None,
         None, "资金流入", None),
        ("600001", "甲", "算力", "2026-01-06", "2026-01-06", 10.5, 100, "open", None,
         None, "加仓", None),
        ("600002", "乙", "地产", "2026-01-06", None, None, 0, "pending", None,
         None, "等回踩", None),
        ("600003", "丙", "光伏", "2026-01-02", "2026-01-02", 8.0, 100, "expired",
         "2026-01-06", -4.5, "反弹", "资金转出"),
    ]
    conn.executemany("INSERT INTO virtual_portfolio (code, name, theme, order_date, "
                     "open_date, open_price, shares, status, close_date, return_pct, "
                     "reason, close_reason, trader_id) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,'default')", rows)
    hist = sqlite3.connect(":memory:")
    hist.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, close REAL)")
    hist.executemany("INSERT INTO daily_kline VALUES (?,?,?)", [
        ("600001", "2026-01-05", 10.0), ("600001", "2026-01-06", 11.0),
        ("600001", "2026-01-07", 99.0)])  # the future, which must not be read
    yield conn, hist
    conn.close()
    hist.close()


def test_orders_holdings_and_sells(dbs):
    conn, hist = dbs
    text = DR.render(conn, hist, trader_id="default", day="2026-01-06",
                     directions=DR.directions_text([("算力", "资金连续流入")], ["地产"]))
    assert "- 算力：资金连续流入" in text and "早盘看到但没选：地产" in text
    assert "600001 甲（算力）已成交 10.5" in text
    assert "600002 乙（地产）未成交" in text
    assert "今日 +10.00%，持仓 +10.00%" in text      # 11.0, not the 99.0 after it
    assert "（今天加仓）" in text
    assert "600003 丙（光伏）到手 -4.50%；理由：资金转出" in text


def test_an_empty_day_says_so(dbs):
    conn, hist = dbs
    text = DR.render(conn, hist, trader_id="nobody", day="2026-01-06")
    assert "今天没有下单" in text and "收盘时空仓" in text
