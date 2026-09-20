"""Provider event ingest preserves point-in-time semantics."""

import sqlite3

from alpha_agents.data import event_expectations as E
from alpha_agents.data import event_ingest as I


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    E.init_schema(conn)
    return conn


def test_date_only_provider_value_becomes_visible_at_eod():
    assert I.pit_timestamp("20260919") == "2026-09-19 23:59:59"
    assert I.pit_timestamp("2026-09-19") == "2026-09-19 23:59:59"


def test_company_forecast_is_guidance_not_consensus():
    conn = _db()
    got = I.ingest_tushare_forecast([{
        "ts_code": "600000.SH",
        "ann_date": "20260919",
        "first_ann_date": "20260919",
        "end_date": "20261231",
        "type": "预增",
        "p_change_min": 50.0,
        "p_change_max": 80.0,
        "summary": "test",
    }], conn=conn)
    assert got["written"] == 1

    before = E.context(
        as_of="2026-09-19 09:00:00", subject="600000", conn=conn)
    after = E.context(
        as_of="2026-09-20 09:00:00", subject="600000", conn=conn)
    assert before == []
    assert after[0]["expectation"] is None
    assert after[0]["realization"]["actual"]["kind"] == "management_guidance"


def test_reingesting_the_same_provider_row_is_idempotent():
    conn = _db()
    row = {
        "ts_code": "600000.SH",
        "ann_date": "20260919",
        "end_date": "20261231",
        "type": "预增",
    }
    first = I.ingest_tushare_forecast([row], conn=conn)
    second = I.ingest_tushare_forecast([row], conn=conn)
    assert first["rows"][0]["event_id"] == second["rows"][0]["event_id"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM event_calendar_snapshots").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) n FROM event_realizations").fetchone()["n"] == 1


def test_forecast_revisions_remain_separate_realizations():
    conn = _db()
    I.ingest_tushare_forecast([{
        "ts_code": "600000.SH", "ann_date": "20260910",
        "end_date": "20261231", "p_change_min": 20.0,
    }, {
        "ts_code": "600000.SH", "ann_date": "20260919",
        "end_date": "20261231", "p_change_min": 50.0,
    }], conn=conn)

    early = E.context(
        as_of="2026-09-15 09:00:00", subject="600000", conn=conn)
    late = E.context(
        as_of="2026-09-20 09:00:00", subject="600000", conn=conn)
    assert early[0]["realization"]["actual"]["profit_change_min"] == 20.0
    assert late[0]["realization"]["actual"]["profit_change_min"] == 50.0


def test_disclosure_calendar_uses_ann_date_as_knowledge_time():
    conn = _db()
    I.ingest_tushare_disclosure([{
        "ts_code": "600000.SH",
        "ann_date": "20260910",
        "end_date": "20261231",
        "pre_date": "20261030",
        "actual_date": None,
        "modify_date": None,
    }], conn=conn)
    before = E.context(
        as_of="2026-09-10 09:00:00", subject="600000", conn=conn,
        days_ahead=60)
    after = E.context(
        as_of="2026-09-11 09:00:00", subject="600000", conn=conn,
        days_ahead=60)
    assert before == []
    assert after[0]["scheduled_at"] == "2026-10-30 23:59:59"


def test_explicit_consensus_snapshot_stays_separate_from_guidance():
    conn = _db()
    E.record_event(
        event_key="cn-stock:600000:earnings-report:20261231",
        event_type="earnings_report", scope="stock", subject="600000",
        scheduled_at="2027-03-30 23:59:59",
        captured_at="2026-09-01 23:59:59",
        source="test", conn=conn)
    got = I.ingest_expectation_snapshots([{
        "event_key": "cn-stock:600000:earnings-report:20261231",
        "captured_at": "20260919",
        "consensus": {"eps": 1.23},
    }], source="test-consensus", conn=conn)
    assert got["written"] == 1
    rows = E.context(
        as_of="2026-09-20 09:00:00", subject="600000",
        conn=conn, days_ahead=365)
    assert rows[0]["expectation"]["consensus"]["eps"] == 1.23
