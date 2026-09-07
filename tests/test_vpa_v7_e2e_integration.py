"""v7 end-to-end integration test (catches unit-vs-production gaps).

Unit tests for _validate_vph_conflict_evidence pass hand-crafted
phase_context dicts with all fields populated. The production path
(compute_vpa_with_llm -> _phase_guard_context_from_df -> validator) builds
the context from real OHLCV. Past bugs slipped through because unit
context shape != production context shape.

This test exercises the full chain with a real OHLCV df, mocks ONLY the
LLM call, and asserts the validator can run without spurious "X missing"
failures.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from alpha_agents.tools import vpa as vpa_mod


REPO = Path(__file__).resolve().parent.parent


def _real_300136_df():
    """Load 80 bars of 300136 from market_history.db ending 2026-01-26.
    Skip if DB unavailable."""
    db = REPO / "data" / "market_history.db"
    if not db.exists():
        pytest.skip("market_history.db not present in this environment")
    conn = sqlite3.connect(str(db))
    rs = conn.execute(
        "SELECT date,open,high,low,close,volume FROM daily_kline "
        "WHERE code='300136' AND date <= '2026-01-26' ORDER BY date DESC LIMIT 80"
    ).fetchall()
    conn.close()
    if len(rs) < 60:
        pytest.skip("not enough 300136 history in db")
    df = pd.DataFrame(rs, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.iloc[::-1].reset_index(drop=True)
    df["code"] = "300136"
    df["name"] = "信维通信"
    return df


def _install_fake_llm(monkeypatch, fake_verdict: str):
    """Mock only the LLM call. Set AGENT_API_KEY so _call_llm_vpa picks the
    AGENT branch and reaches the OpenAI client construction."""

    class FakeMessage:
        content = fake_verdict

    class FakeChoice:
        message = FakeMessage()

    class FakeResp:
        choices = [FakeChoice()]
        usage = None

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.chat = FakeChat()

    # vpa.py does `from openai import OpenAI` inside _call_llm_vpa, so we
    # have to patch the openai module's symbol, not vpa's.
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    from alpha_agents.tools.vpa.llm import _CLIENT_CACHE
    _CLIENT_CACHE.clear()
    monkeypatch.setenv("VPA_LLM_PROVIDER", "agent")

    # Force the AGENT branch to win the provider-selection cascade so the
    # FakeClient gets used. raising=False handles environments where the
    # symbol isn't already set.
    from alpha_agents import config as agent_config
    monkeypatch.setattr(agent_config, "AGENT_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(agent_config, "AGENT_BASE_URL", "https://test", raising=False)
    monkeypatch.setattr(agent_config, "AGENT_MODEL", "test-model", raising=False)


def test_e2e_no_spurious_missing_field_failures_when_llm_picks_a_candidate(monkeypatch):
    """Production path with real OHLCV + mocked LLM that picks a candidate
    must NOT produce vph_conflict_failures of the form 'X missing'.

    A 'X missing' failure means the validator's input contract isn't met
    by the production phase_context — a unit-test-vs-production gap.
    """
    df = _real_300136_df()
    df_derived = vpa_mod._compute_derived(df, window=20)
    candidates = vpa_mod._scan_climax_candidates(df_derived)
    if not candidates:
        pytest.skip("scanner returned no candidates for 300136 — corpus mismatch")

    # Pick a candidate, then craft a verdict that conflicts with the actual
    # vph direction so the validator actually runs (it's a no-op when phase
    # and vph agree).
    cand = candidates[0]
    cand_id = cand["id"]

    _, vph_dir, _, _ = vpa_mod._compute_5d_vph(df_derived)
    if vph_dir == "bullish":
        forced_phase = "派发尾声"
        forced_direction = "看空"
        forced_climax = "BC"
    elif vph_dir == "bearish":
        forced_phase = "拉升尾声"
        forced_direction = "看多"
        forced_climax = "SC"
    else:
        # Neutral vph means the validator gate won't open. Force a bearish
        # phase anyway and rely on the structural assertion (test 2) for
        # field-shape coverage; this assertion still holds because the
        # validator simply returns without producing any failures.
        forced_phase = "派发尾声"
        forced_direction = "看空"
        forced_climax = "BC"

    fake_verdict = (
        '<!-- VERDICT: {"direction": "' + forced_direction + '", '
        '"phase": "' + forced_phase + '", '
        '"confidence": 0.7, "reason": "test", '
        '"phase_change": {"from": "", "to": "' + forced_phase + '", "confirmed": false, "invalidated_by": "", "denial_level": "none"}, '
        '"selected_climax": {"candidate_id": "' + cand_id + '", '
        '"climax_type": "' + forced_climax + '", '
        '"rationale": "e2e test"}, '
        '"signals": [], "scenarios": []} -->'
    )

    _install_fake_llm(monkeypatch, fake_verdict)

    result = vpa_mod.compute_vpa_with_llm(
        code="300136",
        name="信维通信",
        as_of="2026-01-26",
        skip_save=True,
    )
    assert result.get("ok") is True

    failures = result.get("vph_conflict_failures", [])
    missing_field_failures = [f for f in failures if "missing" in f.lower()]
    assert not missing_field_failures, (
        f"Production phase_context is missing fields the validator expects. "
        f"This indicates a unit-test-vs-production gap. Missing-field failures: "
        f"{missing_field_failures}. All failures: {failures}"
    )


def test_e2e_phase_guard_context_has_all_fields_validator_reads():
    """The phase_context built by _phase_guard_context_from_df must contain
    every field that _candidate_to_vc_shape and _failures_for read."""
    df = _real_300136_df()
    df_derived = vpa_mod._compute_derived(df, window=20)
    ctx = vpa_mod._phase_guard_context_from_df(df_derived, as_of="2026-01-26")

    # Field _candidate_to_vc_shape reads (was Critical #1 bug):
    assert ctx.get("trend_20d_pct") is not None, (
        "trend_20d_pct must be in phase_context (read by _candidate_to_vc_shape "
        "for extended_trend_20d_pct floor check; was Critical #1 bug)"
    )

    # Fields _failures_for reads (per-stock p90 thresholds)
    for field in (
        "vol_ratio_p90",
        "abspct_p90",
        "upper_shadow_p90",
        "lower_shadow_p90",
        "range_vs_5d_avg_p90",
    ):
        assert field in ctx, f"phase_context missing {field} (read by _failures_for)"
        assert ctx[field] is not None, f"phase_context.{field} is None"

    # Fields _looks_like_ranging_context reads (v7 §3.3 ranks)
    for field in (
        "abs_trend_10d_pct_rank",
        "range_10d_pct_rank",
        "vol_ratio_5d_rank",
    ):
        assert field in ctx, f"phase_context missing {field} (read by ranging override)"

    # vp_harmony_score (read by validator gate)
    assert ctx.get("vp_harmony_score") in ("bullish", "bearish", "neutral"), (
        "vp_harmony_score must be set"
    )

    # as_of_date (read by _failures_for for null-post_bar recency check)
    assert ctx.get("as_of_date") == "2026-01-26", (
        "as_of_date must propagate from the as_of arg for the "
        "null-post_bar recency cross-check"
    )
