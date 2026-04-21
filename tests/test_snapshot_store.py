"""Round-trip tests for snapshot_store: save → read in same schema as live output."""

import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        # Point snapshot DB to a fresh temp file, reset any existing conn
        self._tmp = tempfile.mkdtemp()
        os.environ["_SNAPSHOT_TEST_DIR"] = self._tmp

        import alpha_agents.data.snapshot_store as store
        store.SNAPSHOTS_DB_PATH = Path(self._tmp) / "snapshots.db"
        # Force new connection on next access
        if hasattr(store._local, "conn"):
            store._local.conn.close()
            del store._local.conn
        self.store = store

    def tearDown(self):
        import shutil
        if hasattr(self.store._local, "conn"):
            self.store._local.conn.close()
            del self.store._local.conn
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fake_industry_df(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"行业": "半导体", "行业-涨跌幅": 3.2, "净额": 12.5,
             "领涨股": "中芯国际", "领涨股-涨跌幅": 9.8, "公司家数": 120},
            {"行业": "白酒", "行业-涨跌幅": -1.1, "净额": -5.3,
             "领涨股": "贵州茅台", "领涨股-涨跌幅": -0.5, "公司家数": 40},
        ])

    def test_round_trip_industry(self):
        """Save then read back — same sector names, same numeric columns."""
        df = self._fake_industry_df()
        n = self.store.save_sector_flow(df, scope="industry",
                                        captured_at="2026-04-18 10:30")
        self.assertEqual(n, 2)

        out = self.store.read_sector_flow("industry", "2026-04-18 10:30")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 2)
        self.assertEqual(set(out["行业"]), {"半导体", "白酒"})
        row_semi = out[out["行业"] == "半导体"].iloc[0]
        self.assertAlmostEqual(row_semi["净额"], 12.5)
        self.assertEqual(row_semi["领涨股"], "中芯国际")

    def test_replay_picks_latest_before_as_of(self):
        """Two snapshots; replay at 11:00 should see the 10:30 one."""
        df1 = self._fake_industry_df()
        self.store.save_sector_flow(df1, "industry", "2026-04-18 10:30")

        df2 = self._fake_industry_df()
        df2.loc[0, "净额"] = 99.9  # change 半导体 to distinguish
        self.store.save_sector_flow(df2, "industry", "2026-04-18 14:30")

        out_before = self.store.read_sector_flow("industry", "2026-04-18 11:00")
        self.assertAlmostEqual(
            out_before[out_before["行业"] == "半导体"].iloc[0]["净额"], 12.5
        )

        out_after = self.store.read_sector_flow("industry", "2026-04-18 15:00")
        self.assertAlmostEqual(
            out_after[out_after["行业"] == "半导体"].iloc[0]["净额"], 99.9
        )

    def test_replay_returns_none_when_no_data(self):
        out = self.store.read_sector_flow("industry", "2020-01-01")
        self.assertIsNone(out)

    def test_scope_isolation(self):
        """industry and concept scopes don't bleed."""
        df = self._fake_industry_df()
        self.store.save_sector_flow(df, "industry", "2026-04-18 10:30")

        out = self.store.read_sector_flow("concept", "2026-04-18 10:30")
        self.assertIsNone(out)

    def test_date_only_as_of_works(self):
        """Bare 'YYYY-MM-DD' as-of must see same-day snapshots (lexical fix)."""
        df = self._fake_industry_df()
        self.store.save_sector_flow(df, "industry", "2026-04-18 14:30")

        out = self.store.read_sector_flow("industry", "2026-04-18")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 2)


class TestEvolutionMetricsDedup(unittest.TestCase):
    """Regression guard: hit-rate computation must dedupe by (date, code)."""

    def test_dedup_by_date_code(self):
        """30 records of same (date, code) must count as 1 for hit rate."""
        from alpha_agents.data import memory_store
        from alpha_agents.evolution.metrics import _query_intraday_buckets
        from datetime import datetime, timedelta
        import json
        import tempfile
        import os
        from pathlib import Path

        # Use a throwaway memory DB
        tmp = tempfile.mkdtemp()
        orig_path = memory_store.MEMORY_DB_PATH
        memory_store.MEMORY_DB_PATH = Path(tmp) / "mem.db"
        if hasattr(memory_store._local, "conn"):
            memory_store._local.conn.close()
            del memory_store._local.conn
        try:
            memory_store._get_conn()  # triggers schema init
            today = datetime.now().strftime("%Y-%m-%d")
            # 30 duplicate records for code A (all hit=1)
            for _ in range(30):
                memory_store.save_prediction(
                    date=today, report_type="intraday",
                    code="600001", name="foo", direction="bullish",
                    confidence="medium", theme_line="", entry_price=10.0,
                    reason="", features={},
                )
            # 1 record for code B (hit=0)
            memory_store.save_prediction(
                date=today, report_type="intraday",
                code="600002", name="bar", direction="bullish",
                confidence="medium", theme_line="", entry_price=10.0,
                reason="", features={},
            )
            # Set hit directly via SQL
            conn = memory_store._get_conn()
            conn.execute("UPDATE predictions SET hit = 1 WHERE code = '600001'")
            conn.execute("UPDATE predictions SET hit = 0 WHERE code = '600002'")
            conn.commit()

            buckets = _query_intraday_buckets(days=7)
            # With dedup: 2 unique picks, 1 hit → 50%
            # Without dedup: 31 records, 30 hits → 97% (bug)
            self.assertEqual(buckets["intraday_count_7d"], 2)
            self.assertAlmostEqual(buckets["intraday_hit_rate_7d"], 0.5)
        finally:
            if hasattr(memory_store._local, "conn"):
                memory_store._local.conn.close()
                del memory_store._local.conn
            memory_store.MEMORY_DB_PATH = orig_path
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestReplayEodCutDate(unittest.TestCase):
    """effective_eod_cut_date rolls back pre-close replay times to T-1 to
    prevent future-data leak in daily EOD data sources."""

    def test_bare_date_stays_same(self):
        from alpha_agents.evolution.replay_mode import (
            effective_eod_cut_date, replay_as_of,
        )
        with replay_as_of("2026-03-20"):
            self.assertEqual(effective_eod_cut_date(), "2026-03-20")

    def test_post_close_stays_same(self):
        from alpha_agents.evolution.replay_mode import (
            effective_eod_cut_date, replay_as_of,
        )
        with replay_as_of("2026-03-20 15:30"):
            self.assertEqual(effective_eod_cut_date(), "2026-03-20")
        # 15:00 exact is still "after bell"
        with replay_as_of("2026-03-20 15:00"):
            self.assertEqual(effective_eod_cut_date(), "2026-03-20")

    def test_pre_close_rolls_back_one_day(self):
        from alpha_agents.evolution.replay_mode import (
            effective_eod_cut_date, replay_as_of,
        )
        with replay_as_of("2026-03-20 06:30"):  # morning
            self.assertEqual(effective_eod_cut_date(), "2026-03-19")
        with replay_as_of("2026-03-20 10:30"):  # intraday
            self.assertEqual(effective_eod_cut_date(), "2026-03-19")
        with replay_as_of("2026-03-20 14:59"):  # last second pre-close
            self.assertEqual(effective_eod_cut_date(), "2026-03-19")

    def test_monday_morning_rolls_to_sunday(self):
        """SQL WHERE trade_date <= '2026-03-22' (Sun, no row) resolves to
        the last Friday's row — this helper doesn't need to skip weekends."""
        from alpha_agents.evolution.replay_mode import (
            effective_eod_cut_date, replay_as_of,
        )
        with replay_as_of("2026-03-23 06:30"):
            self.assertEqual(effective_eod_cut_date(), "2026-03-22")

    def test_none_when_no_replay_active(self):
        from alpha_agents.evolution.replay_mode import effective_eod_cut_date
        self.assertIsNone(effective_eod_cut_date())


if __name__ == "__main__":
    unittest.main()
