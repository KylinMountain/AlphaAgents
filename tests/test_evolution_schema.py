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
