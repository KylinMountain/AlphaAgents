"""Event source discovery is read-only and honest about uncertainty."""

import sqlite3

from alpha_agents.data import event_source_probe as P


def test_local_probe_finds_candidate_tables_without_assigning_pit_grade(tmp_path):
    db = tmp_path / "custom.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE analyst_forecast "
        "(code TEXT, ann_date TEXT, eps REAL)")
    conn.commit()
    conn.close()

    rows = P.probe_local(tmp_path)
    hit = next(row for row in rows if row.get("dataset") == "analyst_forecast")
    assert hit["available"] is True
    assert "ann_date" in hit["time_fields"]
    assert hit["point_in_time_grade"] == "U"


def test_offline_sdk_probe_never_requires_credentials(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TS_TOKEN", raising=False)
    rows = P.probe_sdks()
    tushare = next(row for row in rows if row["provider"] == "tushare")
    assert tushare["credential_available"] is False
    assert tushare["live_probe_run"] is False


def test_live_tushare_refuses_cleanly_without_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TS_TOKEN", raising=False)
    got = P.probe_tushare_live(token=None)
    assert got[0]["available"] is False
    assert "TOKEN" in got[0]["error"]
