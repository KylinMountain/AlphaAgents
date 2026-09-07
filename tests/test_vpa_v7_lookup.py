"""v7 candidate-id lookup tests (spec §2.4)."""

import pytest
from alpha_agents.tools.vpa import _extract_selected_climax, _validate_vph_conflict_evidence


def test_parse_selected_climax_from_clean_verdict_json():
    report = '''
    Some narrative.

    <!-- VERDICT: {"direction": "看空", "phase": "派发尾声", "selected_climax": {"candidate_id": "cand-2025-11-25", "climax_type": "BC", "rationale": "BC at 11-25"}} -->
    '''
    sc = _extract_selected_climax(report)
    assert sc["candidate_id"] == "cand-2025-11-25"
    assert sc["climax_type"] == "BC"


def test_parse_selected_climax_returns_default_on_missing():
    report = '''<!-- VERDICT: {"direction": "看多", "phase": "拉升"} -->'''
    sc = _extract_selected_climax(report)
    assert sc["candidate_id"] is None
    assert sc["climax_type"] is None


def test_parse_selected_climax_handles_explicit_null():
    report = '''<!-- VERDICT: {"selected_climax": {"candidate_id": null, "climax_type": null, "rationale": "no conflict"}} -->'''
    sc = _extract_selected_climax(report)
    assert sc["candidate_id"] is None
    assert sc["climax_type"] is None
    assert sc.get("rationale") == "no conflict"


SAMPLE_CANDIDATE = {
    "id": "cand-2025-11-25",
    "date": "2025-11-25",
    "open": 36.0, "high": 38.0, "low": 35.0, "close": 35.5,
    "volume": 100_000_000, "bar_type": "阴线",
    "vol_ratio": 2.5, "vol_ratio_pct": 0.95,
    "range_vs_5d_avg": 1.8,
    "bar_spread": 0.083, "spread_pct": 0.92,
    "close_position": 0.17,
    "upper_shadow": 0.55, "lower_shadow": 0.05,
    "pct_change": -0.018, "abspct_pct": 0.65,
    "obv": 1.2e9,
    "post_bar_reverse_3d": -0.04,
    "post_bars_observed": 3,
}

CTX_BC = {
    "vp_harmony_score": "bullish",
    "vol_ratio_p90": 1.5, "abspct_p90": 0.025,
    "upper_shadow_p90": 0.4, "lower_shadow_p90": 0.4,
    "range_vs_5d_avg_p90": 1.5,
    "trend_20d_pct": 18.5,  # uptrend so BC trend floor passes
    "as_of_date": "2025-12-01",
    "candidates": [SAMPLE_CANDIDATE],
}


def test_validator_uses_real_ohlcv_from_candidate_dict():
    """LLM picks candidate, validator extracts numbers from candidate dict
    (NOT from LLM-supplied vph_conflict numbers — those are ignored)."""
    data = {
        "phase": "派发尾声", "direction": "看空",
        "confirmation_level": 3, "phase_change": {"confirmed": True},
        "signals": [],
        "selected_climax": {
            "candidate_id": "cand-2025-11-25",
            "climax_type": "BC",
            "rationale": "BC pattern",
        },
        # Old-style LLM-supplied numbers — these MUST be ignored
        "vph_conflict": {"climax_volume_ratio": 999.0, "climax_shadow_ratio": 0.0},
    }
    _validate_vph_conflict_evidence(data, CTX_BC)
    # SAMPLE_CANDIDATE meets all BC conditions, so validator should accept
    assert data.get("vph_conflict_validated") is True
    # The phase shouldn't be demoted
    assert data["phase"] == "派发尾声"


def test_validator_fails_when_candidate_id_not_in_pool():
    data = {
        "phase": "派发尾声", "direction": "看空",
        "confirmation_level": 3, "phase_change": {"confirmed": True},
        "signals": [],
        "selected_climax": {
            "candidate_id": "cand-9999-99-99",  # not in pool
            "climax_type": "BC",
            "rationale": "hallucinated date",
        },
    }
    _validate_vph_conflict_evidence(data, CTX_BC)
    # Must fail-closed: phase demoted to family floor
    assert data["phase"] == "派发初期"
    assert data["vph_conflict_validated"] is False
    assert any("not in candidate" in f.lower() or "not found" in f.lower()
               for f in data.get("vph_conflict_failures", []))


def test_validator_fails_when_no_candidate_selected_under_conflict():
    """phase=派发 + vph=bullish (conflict) but selected_climax.candidate_id=null
    → fail-closed."""
    data = {
        "phase": "派发尾声", "direction": "看空",
        "confirmation_level": 3, "phase_change": {"confirmed": True},
        "signals": [],
        "selected_climax": {"candidate_id": None, "climax_type": None, "rationale": ""},
    }
    _validate_vph_conflict_evidence(data, CTX_BC)
    assert data["phase"] == "派发初期"
    assert data["vph_conflict_validated"] is False
