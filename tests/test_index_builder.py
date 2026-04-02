import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.data.index_builder import build_index


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_stocks.db"
    init_db(db_path)
    return db_path


def _mock_concept_names():
    return pd.DataFrame({
        "概念名称": ["国产替代", "光刻胶"],
    })


def _mock_concept_cons(symbol):
    data = {
        "国产替代": pd.DataFrame({
            "代码": ["688001", "688002"],
            "名称": ["华兴源创", "睿创微纳"],
        }),
        "光刻胶": pd.DataFrame({
            "代码": ["300236", "688001"],
            "名称": ["上海新阳", "华兴源创"],
        }),
    }
    return data.get(symbol, pd.DataFrame({"代码": [], "名称": []}))


def _mock_stock_info():
    """Mock baostock format: code is 'sh.688001' style, name is 'code_name'."""
    return pd.DataFrame({
        "code": ["sh.688001", "sh.688002", "sz.300236"],
        "code_name": ["华兴源创", "睿创微纳", "上海新阳"],
    })


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_concepts(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    concepts = conn.execute("SELECT name FROM concepts ORDER BY name").fetchall()
    conn.close()
    assert [r["name"] for r in concepts] == ["光刻胶", "国产替代"]


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_stocks(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    stocks = conn.execute("SELECT code, name FROM stocks ORDER BY code").fetchall()
    conn.close()
    assert len(stocks) == 3
    assert stocks[0]["code"] == "300236"


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_mappings(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    mappings = conn.execute(
        """
        SELECT c.name FROM concept_stocks cs
        JOIN concepts c ON c.id = cs.concept_id
        WHERE cs.stock_code = '688001'
        ORDER BY c.name
        """
    ).fetchall()
    conn.close()
    assert [r["name"] for r in mappings] == ["光刻胶", "国产替代"]


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_is_idempotent(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    concepts = conn.execute("SELECT COUNT(*) as cnt FROM concepts").fetchone()
    conn.close()
    assert concepts["cnt"] == 2
