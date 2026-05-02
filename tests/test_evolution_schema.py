"""Tests for Phase 1 schema extensions."""
import sqlite3
from datetime import date
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


def test_intraday_metrics_use_saved_playbook_attribution(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    from alpha_agents.evolution.metrics import _query_intraday_buckets

    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    today = date.today().isoformat()
    matched_id = ms.save_prediction(
        date=today, report_type="intraday", code="000001", name="A",
        direction="bullish", confidence="medium", theme_line="AI",
        entry_price=10.0, reason="matched",
        features={"playbook_id": 123, "playbook_name": "PIT"},
    )
    unmatched_id = ms.save_prediction(
        date=today, report_type="intraday", code="000002", name="B",
        direction="bullish", confidence="medium", theme_line="AI",
        entry_price=10.0, reason="unmatched",
        features={"score": 99, "playbook_matched": False},
    )
    ms.update_prediction_result(matched_id, next_day_return=1.0, hit=1)
    ms.update_prediction_result(unmatched_id, next_day_return=1.0, hit=1)

    buckets = _query_intraday_buckets(days=7, as_of=today)
    assert buckets["intraday_count_7d"] == 2
    assert buckets["matched_count_7d"] == 1
    assert buckets["unmatched_count_7d"] == 1


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

    today = date.today().isoformat()
    ms.insert_daily_lesson(today, "failure", "数据中心",
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
    today = date.today().isoformat()
    ms.insert_daily_lesson(today, "insight", None, "相同内容")
    ms.insert_daily_lesson(today, "insight", None, "相同内容")
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


def test_playbooks_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(playbooks)")}
    conn.close()
    expected = {"id", "name", "pattern_json", "created_date", "last_updated",
                "status", "weight", "total_trades", "wins", "hit_rate",
                "avg_return", "annotation", "annotation_date", "version_history"}
    assert expected <= cols


def test_create_and_get_active_playbooks(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pid = ms.create_playbook(
        name="CPO突破+机构买入",
        pattern_json={"conditions": [
            {"field": "vpa_verdict", "op": "==", "value": "bullish"},
            {"field": "theme", "op": "==", "value": "CPO"},
        ]},
        today="2026-04-17",
    )
    assert pid > 0
    rows = ms.get_active_playbooks()
    assert len(rows) == 1
    assert rows[0]["name"] == "CPO突破+机构买入"
    assert rows[0]["status"] == "active"
    assert rows[0]["weight"] == 1.0


def test_update_playbook_status_and_history(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    pid = ms.create_playbook(name="X", pattern_json={"conditions": []}, today="2026-04-17")
    ms.update_playbook_status(pid, status="degraded", weight=0.5,
                               reason="胜率跌破40%", hit_rate_at_change=0.35,
                               today="2026-04-18")
    row = ms.get_all_playbooks()[0]
    assert row["status"] == "degraded"
    assert row["weight"] == 0.5
    import json
    history = json.loads(row["version_history"])
    assert len(history) == 1
    assert history[0]["new_status"] == "degraded"
    assert history[0]["reason"] == "胜率跌破40%"


def test_evolution_metrics_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evolution_metrics)")}
    conn.close()
    expected = {"date", "intraday_hit_rate_7d", "intraday_count_7d",
                "matched_hit_rate_7d", "matched_count_7d",
                "unmatched_hit_rate_7d", "unmatched_count_7d",
                "active_principles", "weakened_principles",
                "active_playbooks", "degraded_playbooks",
                "lessons_count_7d"}
    assert expected <= cols


def test_upsert_evolution_metrics_and_trend(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    today = date.today().isoformat()
    ms.upsert_evolution_metrics(today, {
        "intraday_hit_rate_7d": 0.62, "intraday_count_7d": 200,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 160,
        "active_principles": 5, "weakened_principles": 1,
        "active_playbooks": 3, "degraded_playbooks": 0,
        "lessons_count_7d": 18,
    })
    # Upserts re-insert same date:
    ms.upsert_evolution_metrics(today, {
        "intraday_hit_rate_7d": 0.65, "intraday_count_7d": 201,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 160,
        "active_principles": 5, "weakened_principles": 1,
        "active_playbooks": 3, "degraded_playbooks": 0,
        "lessons_count_7d": 18,
    })
    trend = ms.get_evolution_metrics_trend(days=7)
    assert len(trend) == 1  # same-date upsert replaces, not duplicates
    assert abs(trend[0]["intraday_hit_rate_7d"] - 0.65) < 1e-9


def test_record_playbook_trade_updates_stats(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    pid = ms.create_playbook(name="X", pattern_json={"conditions": []}, today="2026-04-17")
    ms.record_playbook_trade(pid, hit=True, return_pct=2.5)
    ms.record_playbook_trade(pid, hit=True, return_pct=1.0)
    ms.record_playbook_trade(pid, hit=False, return_pct=-1.5)
    row = ms.get_all_playbooks()[0]
    assert row["total_trades"] == 3
    assert row["wins"] == 2
    assert abs(row["hit_rate"] - 2/3) < 0.01
    assert abs(row["avg_return"] - (2.5 + 1.0 - 1.5) / 3) < 0.01
