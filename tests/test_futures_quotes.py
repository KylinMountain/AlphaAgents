import json
from unittest.mock import patch, MagicMock
import pandas as pd

from alpha_agents.tools.futures_quotes import (
    get_futures_quotes_fn,
    get_futures_inventory_fn,
    get_futures_basis_fn,
    get_cftc_positions_fn,
)


def _make_ohlcv_df():
    return pd.DataFrame([
        {"日期": "2026-04-01", "开盘价": 75000, "最高价": 76000, "最低价": 74500,
         "收盘价": 75500, "成交量": 100000, "持仓量": 50000, "动态结算价": 75200},
        {"日期": "2026-04-02", "开盘价": 75500, "最高价": 76500, "最低价": 75000,
         "收盘价": 76000, "成交量": 120000, "持仓量": 52000, "动态结算价": 75800},
        {"日期": "2026-04-03", "开盘价": 76000, "最高价": 77000, "最低价": 75500,
         "收盘价": 76500, "成交量": 110000, "持仓量": 51000, "动态结算价": 76200},
    ])


def _make_inventory_df():
    return pd.DataFrame([
        {"日期": "2026-03-31", "库存": 220000, "增减": -5000.0},
        {"日期": "2026-04-01", "库存": 215000, "增减": -5000.0},
        {"日期": "2026-04-02", "库存": 210000, "增减": -5000.0},
    ])


def _make_basis_df():
    return pd.DataFrame([
        {"date": "20260402", "symbol": "CU", "spot_price": 76000,
         "near_contract": "cu2605", "near_contract_price": 75500,
         "dominant_contract": "cu2606", "dominant_contract_price": 75800,
         "near_month": "2605", "dominant_month": "2606",
         "near_basis": 500, "dom_basis": 200,
         "near_basis_rate": 0.0066, "dom_basis_rate": 0.0026},
    ])


@patch("akshare.futures_main_sina")
def test_get_futures_quotes(mock_sina):
    mock_sina.return_value = _make_ohlcv_df()
    result = json.loads(get_futures_quotes_fn(symbols="沪铜", days=3))
    assert result["count"] == 1
    q = result["quotes"][0]
    assert q["name"] == "沪铜"
    assert q["latest_close"] == 76500
    assert len(q["history"]) == 3
    # Change pct: (76500-76000)/76000 ≈ 0.66%
    assert abs(q["change_pct"] - 0.66) < 0.1


@patch("akshare.futures_main_sina")
def test_get_futures_quotes_empty(mock_sina):
    mock_sina.return_value = pd.DataFrame()
    result = json.loads(get_futures_quotes_fn(symbols="沪铜"))
    assert result["count"] == 0


@patch("akshare.futures_main_sina")
def test_get_futures_quotes_error(mock_sina):
    mock_sina.side_effect = Exception("network error")
    result = json.loads(get_futures_quotes_fn(symbols="沪铜"))
    assert result["count"] == 0


@patch("akshare.futures_inventory_em")
def test_get_futures_inventory(mock_inv):
    mock_inv.return_value = _make_inventory_df()
    result = json.loads(get_futures_inventory_fn(symbol="沪铜"))
    assert result["latest_inventory"] == 210000
    assert result["trend"] == "去库存"
    assert len(result["data"]) == 3


@patch("akshare.futures_inventory_em")
def test_get_futures_inventory_error(mock_inv):
    mock_inv.side_effect = Exception("bad symbol")
    result = json.loads(get_futures_inventory_fn(symbol="不存在"))
    assert "error" in result
    assert result["data"] == []


@patch("akshare.futures_spot_price")
def test_get_futures_basis(mock_spot):
    mock_spot.return_value = _make_basis_df()
    result = json.loads(get_futures_basis_fn(date="20260402"))
    assert result["count"] == 1
    d = result["data"][0]
    assert d["symbol"] == "CU"
    assert d["basis"] == 200
    assert d["basis_rate"] == 0.0026


@patch("akshare.futures_spot_price")
def test_get_futures_basis_error(mock_spot):
    mock_spot.side_effect = Exception("no data")
    result = json.loads(get_futures_basis_fn())
    assert result["data"] == []
    assert "error" in result


def _make_cftc_df():
    return pd.DataFrame([
        {"日期": "2026-03-21", "纽约原油_多单": 300000, "纽约原油_空单": 200000, "纽约原油_净多": 100000,
         "黄金_多单": 250000, "黄金_空单": 100000, "黄金_净多": 150000},
        {"日期": "2026-03-28", "纽约原油_多单": 310000, "纽约原油_空单": 190000, "纽约原油_净多": 120000,
         "黄金_多单": 240000, "黄金_空单": 110000, "黄金_净多": 130000},
    ])


@patch("akshare.macro_usa_cftc_c_holding")
def test_get_cftc_positions_single(mock_cftc):
    mock_cftc.return_value = _make_cftc_df()
    result = json.loads(get_cftc_positions_fn(commodity="原油"))
    assert result["commodity"] == "原油"
    assert result["latest_net"] == 120000
    assert result["trend"] == "净多增加"
    assert len(result["data"]) == 2


@patch("akshare.macro_usa_cftc_c_holding")
def test_get_cftc_positions_all(mock_cftc):
    mock_cftc.return_value = _make_cftc_df()
    result = json.loads(get_cftc_positions_fn())
    assert "summary" in result
    names = [s["commodity"] for s in result["summary"]]
    assert "原油" in names
    assert "黄金" in names


@patch("akshare.macro_usa_cftc_c_holding")
def test_get_cftc_positions_error(mock_cftc):
    mock_cftc.side_effect = Exception("network error")
    result = json.loads(get_cftc_positions_fn())
    assert "error" in result
