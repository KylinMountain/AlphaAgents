"""v7 VPA module. Public API re-exported here from focused submodules.

Pure refactor of the prior single-file ``alpha_agents/tools/vpa.py`` into
a package with focused submodules. No behavior changes — every symbol
that was importable from ``alpha_agents.tools.vpa`` before remains so.

Submodule split (dependency DAG):
  data → verdict → validator → guard → llm → api
                          ↘ scanner / narrative (leaves)
"""

# Data layer: OHLCV loading + derivation + helpers + indicator helpers
from .data import (
    _daily_limit_pct,
    _prev_trading_day,
    _load_ohlcv,
    _fetch_realtime_bar,
    _append_realtime_with_derived,
    _load_ohlcv_60min,
    _compute_derived,
    _compute_5d_vph,
    _obv_trend,
    _volume_regime,
    _detect_test_phase,
    _compute_context,
    _detect_patterns,
)

# Scanner: candidate pool generation
from .scanner import (
    _scan_climax_candidates,
    _build_candidate_dict,
    _compute_post_bar_reverse,
)

# Narrative: user message construction
from .narrative import (
    _render_price_action_narrative,
    _format_user_message,
    _format_text,
    _format_60min_section,
)

# Verdict: parsing LLM output + phase keyword constants
from .verdict import (
    ACCUMULATION_KEYWORDS,
    MARKUP_KEYWORDS,
    DISTRIBUTION_KEYWORDS,
    MARKDOWN_KEYWORDS,
    NEUTRAL_KEYWORDS,
    BULLISH_PHASE_FAMILIES,
    BEARISH_PHASE_FAMILIES,
    ACTION_SIGNAL_KEYWORDS,
    DECISIVE_CONFIRMATION_KEYWORDS,
    UNCONFIRMED_SIGNAL_MARKERS,
    PHASE_TRANSITION_REQUIREMENTS,
    ENTRY_PHASE_BY_FAMILY,
    PHASE_STAGE_RANK_KEYWORDS,
    _extract_json_object_with_key,
    _normalize_phase,
    _contains_phase_keyword,
    _phase_family,
    _coerce_confidence,
    _coerce_bool,
    _normalize_phase_change,
    _normalize_signal,
    _signal_text,
    _signal_requires_action_confirmation,
    _is_confirmed_decisive_signal,
    _is_structural_phase_change,
    _derive_confirmation_level,
    _extract_raw_verdict,
    _normalize_verdict_data,
    _extract_verdict,
    _extract_selected_climax,
    _replace_verdict_in_report,
    _extract_previous_state,
    _format_previous_state_block,
)

# Validator: cross-family + climax validation
from .validator import (
    _BEARISH_FAMILIES,
    _BULLISH_FAMILIES,
    _TREND_FLOORS_PCT,
    _VALID_CLIMAX_TYPES,
    _VERDICT_DEMOTE_MAP,
    _trading_day_distance,
    _failures_for,
    _phase_in,
    _candidate_to_vc_shape,
    _validate_vph_conflict_evidence,
)

# Guard: phase state machine + ranging override
from .guard import (
    _BULLISH_DIRS,
    _BEARISH_DIRS,
    _has_decisive_directional_signal,
    _requires_phase_transition_confirmation,
    _is_allowed_phase_transition,
    _phase_stage_rank,
    _has_same_family_upgrade_evidence,
    _looks_like_ranging_context,
    _phase_guard_context_from_df,
    _strip_stale_phase_guard_reason,
    _refresh_confirmation_fields,
    _apply_vp_harmony_correction,
    _apply_phase_state_guard,
    _apply_phase_state_guard_core,
)

# LLM client + Anna Coulling system prompt
from .llm import (
    ANNA_COULLING_PROMPT,
    _call_llm_vpa,
)

# High-level entry points
from .api import (
    compute_vpa_with_llm,
    get_vpa_analysis_fn,
)

__all__ = [
    # data
    "_daily_limit_pct",
    "_prev_trading_day",
    "_load_ohlcv",
    "_fetch_realtime_bar",
    "_append_realtime_with_derived",
    "_load_ohlcv_60min",
    "_compute_derived",
    "_compute_5d_vph",
    "_obv_trend",
    "_volume_regime",
    "_detect_test_phase",
    "_compute_context",
    "_detect_patterns",
    # scanner
    "_scan_climax_candidates",
    "_build_candidate_dict",
    "_compute_post_bar_reverse",
    # narrative
    "_render_price_action_narrative",
    "_format_user_message",
    "_format_text",
    "_format_60min_section",
    # verdict
    "ACCUMULATION_KEYWORDS",
    "MARKUP_KEYWORDS",
    "DISTRIBUTION_KEYWORDS",
    "MARKDOWN_KEYWORDS",
    "NEUTRAL_KEYWORDS",
    "BULLISH_PHASE_FAMILIES",
    "BEARISH_PHASE_FAMILIES",
    "ACTION_SIGNAL_KEYWORDS",
    "DECISIVE_CONFIRMATION_KEYWORDS",
    "UNCONFIRMED_SIGNAL_MARKERS",
    "PHASE_TRANSITION_REQUIREMENTS",
    "ENTRY_PHASE_BY_FAMILY",
    "PHASE_STAGE_RANK_KEYWORDS",
    "_extract_json_object_with_key",
    "_normalize_phase",
    "_contains_phase_keyword",
    "_phase_family",
    "_coerce_confidence",
    "_coerce_bool",
    "_normalize_phase_change",
    "_normalize_signal",
    "_signal_text",
    "_signal_requires_action_confirmation",
    "_is_confirmed_decisive_signal",
    "_is_structural_phase_change",
    "_derive_confirmation_level",
    "_extract_raw_verdict",
    "_normalize_verdict_data",
    "_extract_verdict",
    "_extract_selected_climax",
    "_replace_verdict_in_report",
    "_extract_previous_state",
    "_format_previous_state_block",
    # validator
    "_BEARISH_FAMILIES",
    "_BULLISH_FAMILIES",
    "_TREND_FLOORS_PCT",
    "_VALID_CLIMAX_TYPES",
    "_VERDICT_DEMOTE_MAP",
    "_trading_day_distance",
    "_failures_for",
    "_phase_in",
    "_candidate_to_vc_shape",
    "_validate_vph_conflict_evidence",
    # guard
    "_BULLISH_DIRS",
    "_BEARISH_DIRS",
    "_has_decisive_directional_signal",
    "_requires_phase_transition_confirmation",
    "_is_allowed_phase_transition",
    "_phase_stage_rank",
    "_has_same_family_upgrade_evidence",
    "_looks_like_ranging_context",
    "_phase_guard_context_from_df",
    "_strip_stale_phase_guard_reason",
    "_refresh_confirmation_fields",
    "_apply_vp_harmony_correction",
    "_apply_phase_state_guard",
    "_apply_phase_state_guard_core",
    # llm
    "ANNA_COULLING_PROMPT",
    "_call_llm_vpa",
    # api
    "compute_vpa_with_llm",
    "get_vpa_analysis_fn",
]
