"""Trader 工具的时间契约：读未来必须失败，而不是静默返回旧值。

回放目录里 `market_snapshots.db` / `market_history.db` 是指向**生产库**的
符号链接（`walk_bootstrap._link_corpus`）。库隔离挡不住泄漏——两个库是
同一个文件。唯一能挡住"2026-08 的决策读到 2026-09 的数据"的东西，是
按 `replay_mode` 的 as-of 截断。

## 为什么用自建数据而不是真实库

`tests/conftest.py` 禁止测试打开 `data/` 下的任何 SQLite 文件
（`StorageIsolationError`）。这是对的，不该绕过。所以这里造一个**小的、
已知内容的**快照库与行情库：截断断言只有在"数据长什么样我知道"时才
有意义——用真实库时，`<= cut` 通过可能只是因为那天恰好没数据。

数据是编的，契约是真的：每个工具在 as-of 之前有数据、之后也有数据，
然后断言它**只看得到前面那段**。

## 契约

1. `AS_OF_FIELDS` 里声明的每个工具都能被这套 fixture 驱动。
2. 读未来（要求读一个尚未到达的时刻）**抛错**——那是故障；而"那个
   时刻没有数据"返回 `available: False`，两者必须可区分。
3. 每个工具只输出事实，不输出操作建议。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alpha_agents.evolution import replay_mode
from alpha_agents.tools import trader_tools as T

#: 五个交易日，价格每天 +1，方便一眼看出截断停在哪一天。
DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05",
        "2026-03-06"]


@pytest.fixture(autouse=True)
def _reset_as_of():
    yield
    replay_mode.set_replay_as_of(None)


@pytest.fixture()
def market_dbs(tmp_path, monkeypatch):
    """A small snapshot DB and history DB with known contents.

    Both hold data on **every** day in ``DAYS``, so a truncation assertion
    cannot pass merely because the later days happen to be empty.
    """
    snap = tmp_path / "market_snapshots.db"
    hist = tmp_path / "market_history.db"

    s = sqlite3.connect(snap)
    s.executescript("""
        CREATE TABLE all_quote_snapshots (
            captured_at TEXT, code TEXT, name TEXT, price REAL,
            change_pct REAL, volume REAL, amount_yi REAL, high REAL,
            low REAL, open REAL, prev_close REAL, turnover_rate REAL,
            volume_ratio REAL, amplitude REAL, pe REAL, pb REAL,
            market_cap_yi REAL, float_cap_yi REAL);
        CREATE TABLE limit_pool_snapshots (
            captured_at TEXT, code TEXT, pool_type TEXT, name TEXT,
            change_pct REAL, turnover_rate REAL, seal_amount_yi REAL,
            first_seal_time TEXT, break_count INTEGER,
            consecutive_limits INTEGER, sector TEXT);
        CREATE TABLE market_breadth_snapshots (
            captured_at TEXT, advances INTEGER, declines INTEGER, flat INTEGER,
            limit_up INTEGER, limit_down INTEGER, real_limit_up INTEGER,
            real_limit_down INTEGER, ad_ratio REAL, sentiment TEXT,
            activity_pct TEXT);
        CREATE TABLE sector_flow_snapshots (
            captured_at TEXT, scope TEXT, sector_name TEXT, change_pct REAL,
            net_flow_yi REAL, leader TEXT, leader_change_pct REAL,
            company_count INTEGER);
        CREATE TABLE stock_fund_flow_daily (
            date TEXT, code TEXT, main_net_yi REAL, main_net_pct REAL,
            turnover_rate REAL);
    """)
    for i, day in enumerate(DAYS):
        px = 10.0 + i                       # 10, 11, 12, 13, 14
        # 5 分钟一个点，和真实快照同粒度——`get_intraday_shape` 需要至少
        # 3 个点才画形态，稀疏的 fixture 会让"截断正确"和"数据不够"混在
        # 一起，测不出想测的东西。
        for hhmm in ("09:35", "09:40", "09:45", "10:00", "10:30",
                     "11:00", "13:30", "14:00", "14:30"):
            s.execute(
                "INSERT INTO all_quote_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{day} {hhmm}", "600127", "金健米业", px, 1.0,
                 1_000_000 + i, 1.0 + i, px + 0.2, px - 0.2, 10.0, 10.0,
                 5.0, 1.2, 2.0, 20.0, 3.0, 100.0, 90.0))
        s.execute(
            "INSERT INTO limit_pool_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{day} 15:00", "600127", "up", "金健米业", 10.0, 5.0, 1.0,
             "0930", 0, 1 + i, "农业"))
        s.execute(
            "INSERT INTO market_breadth_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{day} 15:00", 3000 + i, 1000, 100, 50, 2, 50, 2, 3.0,
             "乐观", "60"))
        s.execute(
            "INSERT INTO sector_flow_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (f"{day} 15:00", "concept", "农业", 2.0, 5.0, "600127", 10.0, 30))
        s.execute(
            "INSERT INTO stock_fund_flow_daily VALUES (?,?,?,?,?)",
            (day, "600127", 1.0 + i, 3.0, 5.0))
    s.commit()
    s.close()

    h = sqlite3.connect(hist)
    h.executescript("""
        CREATE TABLE daily_kline (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, turnover_rate REAL, change_pct REAL);
    """)
    for i, day in enumerate(DAYS):
        px = 10.0 + i
        h.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
                  ("600127", day, px, px + 0.3, px - 0.3, px, 1_000_000,
                   5.0, 1.0))
    h.commit()
    h.close()

    monkeypatch.setattr(T, "_SNAPSHOTS", snap)
    monkeypatch.setattr(T, "_HISTORY", hist)
    return {"snapshots": snap, "history": hist}


class TestTheContractIsDeclared:
    def test_every_tool_declares_its_time_source(self):
        """A tool with no declared boundary cannot be trusted in a replay.
        The declaration is data, not prose, so it can be checked."""
        assert set(T.AS_OF_FIELDS) == {
            "get_market_regime", "get_theme_state", "get_stock_context",
            "get_intraday_shape", "get_stock_memory", "get_my_state",
        }
        for name, (db, column, question) in T.AS_OF_FIELDS.items():
            assert db.endswith(".db"), name
            assert column, name
            assert question, f"{name} does not say what question it answers"

    def test_every_declared_tool_has_an_implementation(self):
        for name in T.AS_OF_FIELDS:
            assert callable(getattr(T, f"{name}_fn", None)), name


class TestTheReplayCutIsApplied:
    """The as-of is a ceiling on the data, not a hint.

    Every fixture day carries data, so "it stopped at the right day" is a
    statement about the cut and not about an empty tail.
    """

    def test_stock_context_never_reads_past_the_cut(self, market_dbs):
        cut = DAYS[2]
        replay_mode.set_replay_as_of(cut)
        out = json.loads(T.get_stock_context_fn("600127"))
        assert out["available"] is True
        assert out["last_bar"]["date"] == cut, (
            f"expected the cut day's bar, got {out['last_bar']['date']}")

    def test_stock_context_returns_fewer_bars_earlier_in_the_window(
            self, market_dbs):
        """The cut must actually shrink the history, not just relabel it."""
        replay_mode.set_replay_as_of(DAYS[-1])
        full = json.loads(T.get_stock_context_fn("600127"))
        replay_mode.set_replay_as_of(DAYS[1])
        early = json.loads(T.get_stock_context_fn("600127"))
        assert early["bars_used"] < full["bars_used"]
        assert early["last_bar"]["date"] == DAYS[1]

    def test_an_eod_reader_rolls_back_when_the_day_has_not_closed(
            self, market_dbs):
        """10:00 on day T must not see T's close — the same rule
        ``replay_mode.effective_eod_cut_date`` states, applied through the
        tool rather than asserted next to it."""
        cut_day = DAYS[3]
        replay_mode.set_replay_as_of(f"{cut_day} 10:00")
        out = json.loads(T.get_stock_context_fn("600127"))
        assert out["last_bar"]["date"] == DAYS[2], (
            "a pre-close decision saw the same day's close")

    def test_market_regime_never_reads_past_the_cut(self, market_dbs):
        cut = DAYS[2]
        replay_mode.set_replay_as_of(cut)
        out = json.loads(T.get_market_regime_fn())
        assert out["available"] is True
        # The ladder is per-day; day 3 has 2 limit-ups in this fixture.
        assert out["ladder"]["highest_streak"] == 3, out

    def test_intraday_shape_stops_at_the_cut_time(self, market_dbs):
        cut = f"{DAYS[3]} 11:00"
        replay_mode.set_replay_as_of(cut)
        out = json.loads(T.get_intraday_shape_fn("600127"))
        assert out["available"] is True
        assert out["last_snapshot"] == f"{DAYS[3]} 11:00", (
            f"intraday shape read {out['last_snapshot']} past the cut {cut}")

    def test_a_later_snapshot_is_not_visible_earlier(self, market_dbs):
        """The same query at two clocks must not return the same last point."""
        replay_mode.set_replay_as_of(f"{DAYS[3]} 11:00")
        a = json.loads(T.get_intraday_shape_fn("600127"))
        replay_mode.set_replay_as_of(f"{DAYS[3]} 14:30")
        b = json.loads(T.get_intraday_shape_fn("600127"))
        assert a["last_snapshot"] < b["last_snapshot"]

    def test_theme_state_uses_the_cut_day_ladder(self, market_dbs):
        replay_mode.set_replay_as_of(DAYS[1])
        out = json.loads(T.get_theme_state_fn("农业"))
        assert out["available"] is True
        assert out["highest_streak"] == 2, out


class TestReadingTheFutureFails:
    """Two different things must not collapse into one output.

    "You asked me to read a moment that has not happened" is a **fault**.
    "That moment has no data" is a **fact**. Returning ``available: False``
    for the first would let a look-ahead bug look exactly like a quiet
    market — which is how a leak reaches the learning data with healthy logs.
    """

    def test_the_guard_raises_for_a_future_stamp(self):
        from alpha_agents.data.clock import LookAheadError
        replay_mode.set_replay_as_of("2020-01-01")
        with pytest.raises(LookAheadError):
            T._guard_read("test read", "2030-01-01")

    def test_the_guard_allows_the_present(self):
        replay_mode.set_replay_as_of("2026-01-01")
        T._guard_read("test read", "2026-01-01")   # must not raise

    def test_an_absent_day_is_unavailable_not_an_error(self, market_dbs):
        """A day the corpus does not hold is a fact about the data, not a
        fault. It must be reported, not raised."""
        replay_mode.set_replay_as_of("2026-03-20")
        out = json.loads(T.get_market_regime_fn())
        assert out["available"] is False
        assert out["reason"], "unavailable with no reason is unreadable"

    def test_the_cut_helper_uses_eod_semantics_for_a_bare_date(
            self, market_dbs):
        replay_mode.set_replay_as_of(DAYS[1])
        assert T._eod_cut() == DAYS[1]
        replay_mode.set_replay_as_of(f"{DAYS[1]} 09:00")
        assert T._eod_cut() < DAYS[1], (
            "a pre-close instant must roll the EOD cut back to T-1")


class TestTheToolsReportFactsNotVerdicts:
    """The other half of the contract, and the one that is easy to lose.

    A tool that answers "可介入" has made the decision the trader was
    supposed to make, using a rule nobody versioned. See
    ``scripts/lint_policy.py`` for the static check; this is the runtime
    counterpart on the returned payload.
    """

    FORBIDDEN = ("建议", "可介入", "轻仓", "回避", "观望", "适合看多",
                 "recommendation", "操作建议")

    def _payloads(self, market_dbs):
        replay_mode.set_replay_as_of(DAYS[-1])
        yield "market_regime", T.get_market_regime_fn()
        yield "theme_state", T.get_theme_state_fn("农业")
        yield "stock_context", T.get_stock_context_fn("600127")
        yield "intraday_shape", T.get_intraday_shape_fn("600127")
        yield "stock_memory", T.get_stock_memory_fn("600127")
        yield "my_state", T.get_my_state_fn()

    def test_no_tool_returns_an_action_word(self, market_dbs):
        for name, raw in self._payloads(market_dbs):
            for word in self.FORBIDDEN:
                assert word not in raw, (
                    f"{name} returned '{word}' — a tool answers questions, "
                    f"it does not hand over a verdict")

    def test_every_answer_is_valid_json_with_an_availability_flag(
            self, market_dbs):
        for name, raw in self._payloads(market_dbs):
            out = json.loads(raw)
            assert "available" in out, name
            if out["available"] is False:
                assert out.get("reason"), f"{name} unavailable with no reason"
