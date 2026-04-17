"""Tests for Phase 1 schema extensions."""
import sqlite3
from pathlib import Path


def _fresh_db(path: Path) -> None:
    """Initialize a memory.db at path using the real schema."""
    import alpha_agents.data.memory_store as ms
    original = ms.MEMORY_DB_PATH
    ms.MEMORY_DB_PATH = path
    # Force fresh connection bound to this path
    if hasattr(ms._local, "conn"):
        del ms._local.conn
    ms._get_conn()  # triggers _ensure_schema
    ms.MEMORY_DB_PATH = original
    if hasattr(ms._local, "conn"):
        del ms._local.conn


def test_predictions_has_features_json_column(tmp_path):
    db = tmp_path / "memory.db"
    _fresh_db(db)
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert "features_json" in cols


def test_alter_is_idempotent_on_existing_db(tmp_path):
    """Running _ensure_schema twice must not error (simulates app restart)."""
    db = tmp_path / "memory.db"
    _fresh_db(db)
    # Second init: should be no-op, not raise
    _fresh_db(db)
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert "features_json" in cols


import json


def test_save_prediction_writes_features_json(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    features = {"vpa_verdict": "bullish", "theme_strength": 9, "score": 62}
    pred_id = ms.save_prediction(
        date="2026-04-17",
        report_type="intraday",
        code="300857",
        name="协创数据",
        direction="bullish",
        confidence="medium",
        theme_line="CPO",
        entry_price=303.96,
        reason="测试",
        features=features,
    )
    assert pred_id > 0
    row = ms._get_conn().execute(
        "SELECT features_json FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    assert json.loads(row["features_json"]) == features


def test_save_prediction_defaults_features_to_empty(tmp_path, monkeypatch):
    """Legacy callers that don't pass features must still work."""
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pred_id = ms.save_prediction(
        date="2026-04-17", report_type="morning", code="000001", name="平安银行",
        direction="bullish", confidence="low", theme_line="", entry_price=None,
        reason="legacy test",
    )
    row = ms._get_conn().execute(
        "SELECT features_json FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    # Either "{}" or empty string is acceptable as "no features"
    assert row["features_json"] in ("{}", "", None)
