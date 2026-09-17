"""Which fund-flow endpoint the archive can actually read.

`daily_archive` called `moneyflow_dc`, the richer endpoint, which the current
token cannot read — measured 2026-09-17, "您没有接口(moneyflow_dc)访问权限".
`stock_fund_flow_daily` held zero rows for as long as that was true, and the
failure looked like a schema problem rather than a permission one.

Plain `moneyflow` answers the whole market (5550 rows for one session) but
names the quantities differently and reports gross legs rather than net
amounts with rates. The writer read `net_amount` unconditionally, so a
`moneyflow` frame wrote zero rows **silently** — an all-missing frame looks
exactly like an empty one, which is the shape this repository keeps paying
for.
"""

from __future__ import annotations

import pandas as pd
import pytest

from alpha_agents.data import snapshot_store as SS
from alpha_agents.data import tushare_store as TS


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A sandbox snapshot database, closed afterwards.

    `tushare_store` writes through `snapshot_store._get_conn`, so that is the
    module whose path has to move — patching `memory_store` leaves the writer
    pointed at the real corpus.
    """
    monkeypatch.setattr(SS, "SNAPSHOTS_DB_PATH", tmp_path / "snapshots.db",
                        raising=False)
    monkeypatch.setattr(SS._local, "conn", None, raising=False)
    conn = SS._get_conn()
    yield conn
    conn.close()
    SS._local.conn = None


def _dc_row():
    return pd.DataFrame([{
        "ts_code": "600584.SH", "trade_date": "20260916", "name": "长电科技",
        "pct_change": 9.07, "close": 85.85,
        "net_amount": 12345.6, "net_amount_rate": 3.2,
        "buy_elg_amount": 5000.0, "buy_elg_amount_rate": 1.3,
        "buy_lg_amount": 4000.0, "buy_lg_amount_rate": 1.0,
        "buy_md_amount": 2000.0, "buy_md_amount_rate": 0.5,
        "buy_sm_amount": 1000.0, "buy_sm_amount_rate": 0.2,
    }])


def _mf_row():
    return pd.DataFrame([{
        "ts_code": "600584.SH", "trade_date": "20260916",
        "buy_sm_amount": 1000.0, "sell_sm_amount": 900.0,
        "buy_lg_amount": 4000.0, "sell_lg_amount": 3500.0,
        "buy_elg_amount": 5000.0, "sell_elg_amount": 100.0,
        "net_mf_amount": 12345.6,
    }])


class TestBothEndpointsWrite:
    def _rows(self, conn):
        return conn.execute(
            "SELECT * FROM stock_fund_flow_daily").fetchall()

    def test_the_dc_endpoint_still_writes(self, store):
        assert TS.save_stock_fund_flow_daily(_dc_row()) == 1
        row = self._rows(store)[0]
        assert row["code"] == "600584"
        assert row["net_amount"] == 12345.6
        assert row["net_amount_rate"] == 3.2

    def test_the_moneyflow_endpoint_writes_too(self, store):
        """The one the token can actually reach."""
        assert TS.save_stock_fund_flow_daily(_mf_row()) == 1
        row = self._rows(store)[0]
        assert row["code"] == "600584"
        assert row["net_amount"] == 12345.6, (
            "a moneyflow frame wrote no net amount; the writer read the dc "
            "column name and found nothing")

    def test_a_rate_the_endpoint_does_not_supply_is_none_not_zero(
            self, store):
        """`moneyflow` has no rates. Writing 0.0 would be a claim that the
        net was 0% of turnover."""
        TS.save_stock_fund_flow_daily(_mf_row())
        row = self._rows(store)[0]
        assert row["net_amount_rate"] is None
        assert row["buy_lg_amount_rate"] is None

    def test_the_gross_legs_survive(self, store):
        TS.save_stock_fund_flow_daily(_mf_row())
        row = self._rows(store)[0]
        assert row["buy_lg_amount"] == 4000.0
        assert row["buy_elg_amount"] == 5000.0

    def test_an_empty_frame_is_zero_not_an_error(self):
        assert TS.save_stock_fund_flow_daily(None) == 0
        assert TS.save_stock_fund_flow_daily(pd.DataFrame()) == 0

    def test_the_column_mapping_is_chosen_by_the_frame(self):
        assert TS._fund_flow_columns(_dc_row())["net_amount"] == "net_amount"
        assert TS._fund_flow_columns(_mf_row())["net_amount"] == "net_mf_amount"


class TestTheArchiveCallsTheReachableEndpoint:
    def test_it_does_not_ask_for_moneyflow_dc(self):
        """The endpoint the token cannot read is named in a comment, not in
        the job list."""
        import inspect
        from alpha_agents.pipeline.tasks import daily_archive
        src = inspect.getsource(daily_archive._tushare_archive)
        assert '"moneyflow"' in src
        assert '"moneyflow_dc"' not in src
