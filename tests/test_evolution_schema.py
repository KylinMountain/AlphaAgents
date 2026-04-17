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


def test_daily_lessons_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_lessons)")}
    conn.close()
    expected = {"id", "date", "lesson_type", "theme", "content",
                "source", "relevance_tags", "consolidated_into"}
    assert expected <= cols


def test_trading_principles_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trading_principles)")}
    conn.close()
    expected = {"id", "principle", "pattern_description", "category",
                "action_guidance", "evidence", "evidence_count", "win_rate",
                "first_learned", "last_reinforced", "status"}
    assert expected <= cols


def test_insert_daily_lesson_and_query(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    ms.insert_daily_lesson("2026-04-17", "failure", "数据中心",
                            "东方国信破5日线才预警", tags="5日线,止损时机")
    rows = ms.get_recent_daily_lessons(days=7)
    assert len(rows) == 1
    assert rows[0]["lesson_type"] == "failure"
    assert rows[0]["theme"] == "数据中心"
    assert "东方国信" in rows[0]["content"]


def test_insert_daily_lesson_deduplicates(tmp_path, monkeypatch):
    """UNIQUE(date, content) — second insert same day same content is ignored."""
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms.insert_daily_lesson("2026-04-17", "insight", None, "相同内容")
    ms.insert_daily_lesson("2026-04-17", "insight", None, "相同内容")
    rows = ms.get_recent_daily_lessons(days=1)
    assert len(rows) == 1


def test_upsert_trading_principle_create_and_reinforce(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pid = ms.create_trading_principle(
        principle="巨量长下影 = 买入高峰",
        pattern_description="跌幅>5%下影>30%",
        category="vpa_signal",
        action_guidance="等缩量不破低再介入",
        evidence=[{"code": "300274", "date": "04-01", "outcome": "+8.2% in 10d"}],
        today="2026-04-17",
    )
    assert pid > 0

    ms.reinforce_trading_principle(pid, today="2026-04-18",
                                    new_case={"code": "000001", "date": "04-18",
                                              "outcome": "+5% in 5d"})
    rows = ms.get_active_principles()
    assert len(rows) == 1
    assert rows[0]["evidence_count"] == 2
    assert rows[0]["last_reinforced"] == "2026-04-18"


def test_weaken_and_retire_principle(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pid = ms.create_trading_principle(
        principle="P", pattern_description="d", category="x",
        action_guidance="a", evidence=[], today="2026-04-17",
    )
    ms.set_principle_status(pid, "weakened")
    assert ms.get_active_principles() == []
    all_rows = ms.get_all_principles_including_weakened()
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "weakened"
