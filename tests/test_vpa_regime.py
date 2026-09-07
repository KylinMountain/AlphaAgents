"""Golden-case tests for deterministic price/volume regime classification.

These three stocks exposed the LLM-phase-as-trend confusion:
* 301379 on 2025-11-12: mimo labeled "拉升初期" while the daily MA
  structure had already gone bearish (MA5<MA10<MA20, ret_10d ~ -10%).
* 300429 on 2026-01-07: day after a +15.8%巨量上冲, today high-opened
  and closed in the lower half on huge volume — a textbook
  failed-SOS / supply-on-rally pattern that the LLM ignored.
* 300443 from 2025-11-26 onward: price recovered from 26 → 30+ over
  five weeks (+18%), but mimo's phase stayed "下跌初期" for ~6 weeks.

The deterministic regime classifier must call these correctly.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from alpha_agents.tools.vpa.regime import (
    classify_regime,
    derive_trade_setup,
    detect_supply_warnings,
)


DB = Path(__file__).resolve().parents[1] / "data" / "market_history.db"


def _load(code: str, start: str, end: str) -> pd.DataFrame:
    if not DB.exists():
        pytest.skip(f"market_history.db not at {DB} — golden cases need local DB")
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM daily_kline "
        "WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
        (code, start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def test_301379_2025_11_12_must_be_downtrend():
    """301379 had MA5<MA10<MA20 and ~-10% over 10 days on 2025-11-12.
    The deterministic regime must classify it as a downtrend variant
    (the LLM-driven pipeline kept calling it "拉升初期")."""
    df = _load("301379", "2025-09-01", "2025-11-12")
    r = classify_regime(df, as_of="2025-11-12")
    assert r["regime"] in ("downtrend", "downtrend_weak"), (
        f"expected downtrend* on 301379 2025-11-12, got {r['regime']} "
        f"(ma5={r.get('ma5'):.2f}, ma10={r.get('ma10'):.2f}, ma20={r.get('ma20'):.2f}, "
        f"ret_10d={r.get('ret_10d'):.2f}%)"
    )


def test_300429_2026_01_07_must_emit_supply_warning():
    """300429 on 2026-01-07 was the failed follow-through after 01-06's
    +15.8% 1.44亿成交 SOS day: today gap-up to 16.26, closed 15.94 in
    the lower half on 1.31亿. Must emit at least one supply warning
    (failed_sos_followthrough or upthrust or supply_on_rally)."""
    df = _load("300429", "2025-10-01", "2026-01-07")
    warnings = detect_supply_warnings(df, as_of="2026-01-07")
    pats = {w["pattern"] for w in warnings}
    expected = {"failed_sos_followthrough", "upthrust", "supply_on_rally"}
    assert pats & expected, (
        f"expected at least one of {expected} on 300429 2026-01-07, "
        f"got {pats or 'no warnings'}"
    )


def test_300443_2025_12_15_must_not_be_downtrend():
    """300443 had been "下跌初期" in the LLM pipeline for 6 weeks while
    price climbed 26 → 28.64 (+10%). By 2025-12-15 the deterministic
    regime must be uptrend* or range, not downtrend (the LLM stuck on
    bearish phase under weekly-markup justification)."""
    df = _load("300443", "2025-10-01", "2025-12-15")
    r = classify_regime(df, as_of="2025-12-15")
    assert r["regime"] not in ("downtrend", "downtrend_weak"), (
        f"expected non-downtrend on 300443 2025-12-15, got {r['regime']} "
        f"(ret_10d={r.get('ret_10d'):.2f}%, ret_20d={r.get('ret_20d'):.2f}%)"
    )


def test_classify_regime_handles_short_history():
    """≤20 bars must return insufficient_data, never crash or guess."""
    df = pd.DataFrame({
        "date": [f"2025-09-{d:02d}" for d in range(1, 11)],
        "open": [10.0] * 10, "high": [10.5] * 10, "low": [9.5] * 10,
        "close": [10.0] * 10, "volume": [1_000_000] * 10,
    })
    r = classify_regime(df)
    assert r["regime"] == "insufficient_data"


def test_detect_supply_warnings_handles_short_history():
    df = pd.DataFrame({
        "date": ["2025-09-01", "2025-09-02"],
        "open": [10.0, 10.5], "high": [10.5, 11.0], "low": [9.5, 10.0],
        "close": [10.0, 10.5], "volume": [1_000_000, 1_500_000],
    })
    assert detect_supply_warnings(df) == []


# ── trade_setup golden cases ──────────────────────────────────────
# Each case checks that the deterministic trade-trigger fires (or doesn't)
# on the day a human trader would expect. These break decoupling from
# llm_phase/confirmation_level — i.e. fire on the SOS day, not 6 days later.


def test_300429_2026_01_06_must_be_long_actionable():
    """The +15.8% 1.44亿成交 breakout. mimo gave phase=震荡 L=1 — useless
    as a buy trigger. trade_setup must escalate to long_actionable on the
    breakout day itself, not wait for "by:" confirmation 6 days later."""
    df = _load("300429", "2025-09-01", "2026-01-06")
    r = derive_trade_setup(df, as_of="2026-01-06")
    assert r["setup"] in ("long_actionable", "long_confirmed"), (
        f"expected long_actionable on 300429 2026-01-06 SOS day, got {r['setup']} "
        f"(reason={r.get('reason')})"
    )


def test_300429_2026_01_07_must_be_short_watch_or_none():
    """The day after the SOS: gap-up + close in lower half + huge volume.
    A trade-trigger that's any flavor of long here is broken; it should
    be short_watch (supply warning fires) or none (no clear edge)."""
    df = _load("300429", "2025-09-01", "2026-01-07")
    r = derive_trade_setup(df, as_of="2026-01-07")
    assert r["setup"] in ("short_watch", "short_actionable", "none"), (
        f"expected non-long on 300429 2026-01-07 failed-SOS day, got {r['setup']}"
    )


def test_301379_2025_11_12_must_not_be_long():
    """The day mimo still called 拉升初期 while MA5<MA10<MA20 + ret_10d=-10%.
    The deterministic trade_setup must not classify this as any long
    setup (no actionable, no confirmed, no watch)."""
    df = _load("301379", "2025-09-01", "2025-11-12")
    r = derive_trade_setup(df, as_of="2025-11-12")
    assert not r["setup"].startswith("long_"), (
        f"expected non-long on 301379 2025-11-12 downtrend day, got {r['setup']}"
    )


def test_300443_2025_12_05_must_be_long():
    """The +5.6% 巨量 breakout after the November lows. mimo locked
    phase=下跌初期 for 6 more weeks. trade_setup must catch the recovery
    breakout here as some flavor of long (actionable/confirmed/watch)."""
    df = _load("300443", "2025-09-01", "2025-12-05")
    r = derive_trade_setup(df, as_of="2025-12-05")
    assert r["setup"].startswith("long_"), (
        f"expected long_* on 300443 2025-12-05 recovery breakout, got {r['setup']} "
        f"(reason={r.get('reason')})"
    )


def test_002066_2025_09_23_must_be_long():
    """v91 actually bought 002066 on 9-24 via mimo C2 bullish and it ran
    +30% — so the deterministic setup must also flag this entry, otherwise
    we'd lose the system's only consistent winner. Without the in-trend
    continuation branch, this stock sat in regime=uptrend with vol_x=2.3
    but no 20-day breakout → none, which would have starved the strategy."""
    df = _load("002066", "2025-07-01", "2025-09-23")
    r = derive_trade_setup(df, as_of="2025-09-23")
    assert r["setup"].startswith("long_"), (
        f"expected long_* on 002066 2025-09-23 (v91 winner entry), got {r['setup']} "
        f"(reason={r.get('reason')})"
    )


def test_derive_trade_setup_short_history_safe():
    """≤22 bars must return 'none' with a reason, not crash."""
    df = pd.DataFrame({
        "date": [f"2025-09-{d:02d}" for d in range(1, 11)],
        "open": [10.0] * 10, "high": [10.5] * 10, "low": [9.5] * 10,
        "close": [10.0] * 10, "volume": [1_000_000] * 10,
    })
    r = derive_trade_setup(df)
    assert r["setup"] == "none"
    assert "insufficient" in r["reason"]
