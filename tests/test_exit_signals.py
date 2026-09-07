"""Exit timing — regime-conditional holding cap plus display indicators."""

from unittest.mock import patch

from alpha_agents.tools import exit_signals as ex


def _bars(closes, highs=None, vols=None):
    """Build bar dicts; highs default to closes, volumes to a flat baseline."""
    highs = highs or closes
    vols = vols or [1_000_000] * len(closes)
    return [
        {"date": f"2026-01-{i + 1:02d}", "open": c, "high": h,
         "low": c * 0.99, "close": c, "volume": v}
        for i, (c, h, v) in enumerate(zip(closes, highs, vols))
    ]


class TestComputeRsi:
    def test_seeds_only_after_period(self):
        rsi = ex.compute_rsi([10.0] * 20, period=14)
        assert rsi[:14] == [None] * 14
        assert rsi[14] is not None

    def test_all_gains_is_100(self):
        assert ex.compute_rsi([10.0 + i for i in range(20)], period=14)[-1] == 100.0

    def test_all_losses_is_0(self):
        assert ex.compute_rsi([100.0 - i for i in range(20)], period=14)[-1] == 0.0

    def test_too_short_returns_all_none(self):
        assert ex.compute_rsi([1.0, 2.0, 3.0], period=14) == [None, None, None]

    def test_range_is_bounded(self):
        closes = [float(10 + (i % 5) - 2) for i in range(60)]
        for v in ex.compute_rsi(closes):
            if v is not None:
                assert 0.0 <= v <= 100.0


class TestMarketRegime:
    """Thresholds are ±3% on the index's 20-day return."""

    def test_classifies_strong_neutral_weak(self):
        cases = [(1.10, "strong"), (1.00, "neutral"), (0.90, "weak")]
        for ratio, expected in cases:
            rows = [{"close": 100.0 * ratio}] + [{"close": 100.0}] * 20
            with patch.object(ex, "_connect") as conn:
                conn.return_value.execute.return_value.fetchall.return_value = rows
                regime, _ = ex.get_market_regime()
            assert regime == expected

    def test_unknown_without_db(self):
        with patch.object(ex, "_connect", return_value=None):
            assert ex.get_market_regime() == ("unknown", 0.0)

    def test_unknown_on_short_history(self):
        with patch.object(ex, "_connect") as conn:
            conn.return_value.execute.return_value.fetchall.return_value = [{"close": 1.0}]
            assert ex.get_market_regime() == ("unknown", 0.0)


class TestHoldingPeriodCap:
    """The cap applies only to positions already up >=15% over 20 days."""

    def test_small_runup_is_never_capped(self):
        with patch.object(ex, "get_runup_pct", return_value=5.0):
            should_close, _ = ex.check_holding_period("000001", holding_days=99)
        assert should_close is False

    def test_weak_market_closes_immediately(self):
        with patch.object(ex, "get_runup_pct", return_value=20.0), \
             patch.object(ex, "get_market_regime", return_value=("weak", -5.0)):
            should_close, reason = ex.check_holding_period("000001", holding_days=0)
        assert should_close is True
        assert "弱势" in reason

    def test_strong_market_holds_until_day_three(self):
        with patch.object(ex, "get_runup_pct", return_value=20.0), \
             patch.object(ex, "get_market_regime", return_value=("strong", 5.0)):
            assert ex.check_holding_period("000001", 2)[0] is False
            assert ex.check_holding_period("000001", 3)[0] is True

    def test_neutral_market_holds_until_day_two(self):
        with patch.object(ex, "get_runup_pct", return_value=20.0), \
             patch.object(ex, "get_market_regime", return_value=("neutral", 0.5)):
            assert ex.check_holding_period("000001", 1)[0] is False
            assert ex.check_holding_period("000001", 2)[0] is True

    def test_unknown_regime_never_caps(self):
        with patch.object(ex, "get_runup_pct", return_value=20.0), \
             patch.object(ex, "get_market_regime", return_value=("unknown", 0.0)):
            assert ex.check_holding_period("000001", 99)[0] is False

    def test_missing_history_never_caps(self):
        with patch.object(ex, "get_runup_pct", return_value=None):
            assert ex.check_holding_period("000001", 99)[0] is False


class TestDisplayPatterns:
    """Kept for reporting only — these carry no exit edge (see module docs)."""

    def test_churning_detects_volume_without_progress(self):
        bars = _bars([10.0] * 24 + [10.05], vols=[1_000_000] * 24 + [3_000_000])
        factor, reason = ex.detect_churning(bars)
        assert factor == 0.96 and "放量滞涨" in reason

    def test_churning_ignores_a_real_advance(self):
        bars = _bars([10.0] * 24 + [10.6], vols=[1_000_000] * 24 + [3_000_000])
        assert ex.detect_churning(bars) is None

    def test_no_demand_needs_both_high_price_and_dry_volume(self):
        closes = [10 + i * 0.1 for i in range(24)]
        dry = _bars(closes + [12.2], vols=[1_000_000] * 24 + [400_000])
        assert ex.detect_no_demand_at_high(dry)[0] == 0.98
        wet = _bars(closes + [12.2], vols=[1_000_000] * 25)
        assert ex.detect_no_demand_at_high(wet) is None

    def test_confirmed_high_is_not_a_divergence(self):
        assert ex.detect_bearish_divergence(_bars([10 + i * 0.5 for i in range(60)])) is None

    def test_too_few_bars_returns_none(self):
        assert ex.detect_bearish_divergence(_bars([10.0] * 10)) is None
