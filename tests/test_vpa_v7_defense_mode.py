"""v7 defense mode tests (spec §2.4 cross-family rule)."""

from alpha_agents.tools.vpa import _validate_vph_conflict_evidence


CTX_BC_CONFLICT = {
    "vp_harmony_score": "bullish",
    "vol_ratio_p90": 1.5, "abspct_p90": 0.025,
    "upper_shadow_p90": 0.4, "lower_shadow_p90": 0.4,
    "range_vs_5d_avg_p90": 1.5,
    "trend_20d_pct": 18.5,  # uptrend so BC trend floor passes
    "as_of_date": "2025-12-01",
}

GOOD_BC_CANDIDATE = {
    "id": "cand-2025-11-25", "date": "2025-11-25",
    "vol_ratio": 2.3, "range_vs_5d_avg": 1.7,
    "upper_shadow": 0.55, "lower_shadow": 0.05,
    "close_position": 0.32,
    "post_bar_reverse_3d": -0.04, "post_bars_observed": 3,
}


def test_within_family_no_cross_family_rule():
    """拉升中期 → 拉升尾声 is within markup, no cross-family rule fires."""
    ctx = {**CTX_BC_CONFLICT, "candidates": []}
    data = {
        "phase": "拉升尾声",  # within markup family
        "direction": "看多",
        "selected_climax": {"candidate_id": None, "climax_type": None},
        "prior_phase": "拉升中期",  # also markup
        "prior_verdict": "看多",
    }
    _validate_vph_conflict_evidence(data, ctx)
    # No conflict (vph=bullish + phase=拉升 are aligned), no demote
    assert data["phase"] == "拉升尾声"
    assert data.get("cross_family_blocked", False) is False


def test_cross_family_with_valid_climax_and_post_3d_observed_accepts():
    """拉升 → 派发 with valid BC + post_bar_reverse observed: accept."""
    ctx = {**CTX_BC_CONFLICT, "candidates": [GOOD_BC_CANDIDATE]}
    data = {
        "phase": "派发尾声", "direction": "看空",
        "selected_climax": {
            "candidate_id": "cand-2025-11-25", "climax_type": "BC",
            "rationale": "BC at 11-25",
        },
        "prior_phase": "拉升中期",  # cross-family from markup
        "prior_verdict": "看多",
    }
    _validate_vph_conflict_evidence(data, ctx)
    assert data.get("cross_family_blocked", False) is False
    assert data["phase"] == "派发尾声"
    assert data["vph_conflict_validated"] is True


def test_cross_family_with_post_bars_observed_lt_3_blocks():
    """Cross-family revision with candidate too recent (post_bars_observed=1)
    → block, revert to prior phase."""
    recent_candidate = {**GOOD_BC_CANDIDATE, "post_bars_observed": 1, "post_bar_reverse_3d": -0.01}
    ctx = {**CTX_BC_CONFLICT, "candidates": [recent_candidate]}
    data = {
        "phase": "派发尾声", "direction": "看空",
        "confirmation_level": 3, "confirmed": True, "action_confirmed": True,
        "selected_climax": {
            "candidate_id": "cand-2025-11-25", "climax_type": "BC",
            "rationale": "BC, recent",
        },
        "prior_phase": "拉升中期", "prior_verdict": "看多",
    }
    _validate_vph_conflict_evidence(data, ctx)
    # Atomic revert: phase = prior_phase, direction = prior_verdict, all confirmed flags False
    assert data["phase"] == "拉升中期"
    assert data["direction"] == "看多"
    assert data["confirmed"] is False
    assert data["action_confirmed"] is False
    assert data["confirmation_level"] == 1
    assert data.get("cross_family_blocked") is True


def test_cross_family_with_no_candidate_selected_blocks():
    ctx = {**CTX_BC_CONFLICT, "candidates": []}
    data = {
        "phase": "派发尾声", "direction": "看空",
        "selected_climax": {"candidate_id": None, "climax_type": None},
        "prior_phase": "拉升中期", "prior_verdict": "看多",
    }
    _validate_vph_conflict_evidence(data, ctx)
    assert data.get("cross_family_blocked") is True
    assert data["phase"] == "拉升中期"


def test_cold_start_no_prior_no_cross_family_rule():
    ctx = {**CTX_BC_CONFLICT, "candidates": [GOOD_BC_CANDIDATE]}
    data = {
        "phase": "派发尾声", "direction": "看空",
        "selected_climax": {
            "candidate_id": "cand-2025-11-25", "climax_type": "BC",
            "rationale": "BC",
        },
        # No prior_phase / prior_verdict
    }
    _validate_vph_conflict_evidence(data, ctx)
    # Without prior, cross-family rule doesn't fire even on conflict
    assert data.get("cross_family_blocked", False) is False
