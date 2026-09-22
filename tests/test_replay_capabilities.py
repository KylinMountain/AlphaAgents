"""Replay reports what historical eyes actually exist."""

import sqlite3

from alpha_agents.evolution import replay_capabilities as C


def _db(path, statements):
    conn = sqlite3.connect(path)
    for sql, rows in statements:
        conn.execute(sql)
        if rows:
            conn.executemany(
                sql.replace("CREATE TABLE", "INSERT INTO") if False else
                "SELECT 1", [])
    return conn


def _make(path, schema, insert, rows):
    conn = sqlite3.connect(path)
    conn.execute(schema)
    conn.executemany(insert, rows)
    conn.commit()
    conn.close()


class TestCapabilityMatrix:
    def test_it_measures_source_days_not_just_file_presence(self, tmp_path):
        _make(
            tmp_path / "market_history.db",
            "CREATE TABLE daily_kline(date TEXT)",
            "INSERT INTO daily_kline(date) VALUES (?)",
            [("2026-09-01",), ("2026-09-02",), ("2026-09-03",)])
        snap = sqlite3.connect(tmp_path / "market_snapshots.db")
        snap.executescript(
            "CREATE TABLE news_items(published_at TEXT);"
            "CREATE TABLE market_breadth_snapshots(captured_at TEXT);"
            "CREATE TABLE limit_pool_snapshots(captured_at TEXT);"
            "CREATE TABLE all_quote_snapshots(captured_at TEXT);"
            "CREATE TABLE stock_fund_flow_daily(date TEXT);")
        snap.executemany(
            "INSERT INTO news_items VALUES (?)",
            [("2026-09-01 08:00:00",), ("2026-09-03 08:00:00",)])
        snap.execute(
            "INSERT INTO market_breadth_snapshots VALUES "
            "('2026-09-03 09:25:00')")
        snap.executemany(
            "INSERT INTO all_quote_snapshots VALUES (?)",
            [("2026-09-02 10:00:00",), ("2026-09-03 10:00:00",)])
        snap.commit()
        snap.close()

        matrix = C.build(
            ["2026-09-01", "2026-09-02", "2026-09-03"], tmp_path)
        caps = matrix["capabilities"]

        assert caps["daily_price"]["coverage_pct"] == 100.0
        assert caps["news"]["coverage_pct"] == 66.7
        assert caps["market_regime"]["observed_days"] == 1
        assert caps["limit_pool"]["status"] == "no_rows_in_window"
        assert "theme_state" not in caps, (
            "the key collided with data/theme_state.py and made the report "
            "read 0% for a module that produced every ranking the run used")
        assert caps["intraday_shape"]["coverage_pct"] == 66.7

    def test_current_concepts_are_never_reported_as_historical_coverage(
            self, tmp_path):
        matrix = C.build(["2020-01-02"], tmp_path)
        concept = matrix["capabilities"]["concept_membership"]
        assert concept["status"] == "current_only"
        assert concept["point_in_time"] is False
        assert concept["coverage_pct"] is None

    def test_missing_sources_are_explicit(self, tmp_path):
        matrix = C.build(["2020-01-02", "2020-01-03"], tmp_path)
        daily = matrix["capabilities"]["daily_price"]
        assert daily["status"] == "missing_file"
        assert daily["coverage_pct"] == 0.0

    def test_summary_labels_non_pit_sources(self, tmp_path):
        matrix = C.build(["2020-01-02"], tmp_path)
        text = "\n".join(C.summary_lines(matrix))
        assert "concept_membership" in text
        assert "非 PIT" in text
        assert "current_only" in text
