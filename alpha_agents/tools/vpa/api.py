"""High-level VPA entry points.

``compute_vpa_with_llm`` is the orchestration function that ties data
loading, scanner, narrative, LLM call, guard, and validator together.
``get_vpa_analysis_fn`` is the thin tool-registry wrapper.
"""

import json
import logging

from .data import (
    _append_realtime_with_derived,
    _compute_derived,
    _detect_patterns,
    _fetch_realtime_bar,
    _load_ohlcv,
    _load_ohlcv_60min,
    _obv_trend,
    _prev_trading_day,
    _volume_regime,
)
from .guard import _phase_guard_context_from_df
from .llm import _call_llm_vpa
from .narrative import _format_60min_section, _format_user_message
from .scanner import _scan_climax_candidates

logger = logging.getLogger(__name__)


def compute_vpa_with_llm(code: str, name: str = "", days: int = 60,
                          window: int = 20, as_of: str | None = None,
                          skip_save: bool = False,
                          previous_analysis_override: str | None = None,
                          prior_state: dict | None = None) -> dict:
    """VPA with LLM interpretation using Anna Coulling rules.

    Steps:
      1. Code pre-computes all VPA indicators (same as compute_vpa)
      2. LLM reads pre-computed text + Anna Coulling prompt → verdict

    Args:
        as_of: YYYY-MM-DD. If set, analyzes the stock as viewed FROM that
            date (ignores future K-lines, skips realtime bar and 60-min
            data). Used by backtest/replay.
        skip_save: If True, do not persist the analysis to memory_store.
            Backtest uses this to avoid polluting production history.
        previous_analysis_override: If set, use this as the previous analysis
            context instead of reading from memory_store. Backtest passes an
            in-memory dict entry here to preserve continuity without writing
            to the shared store.

    Returns dict with both code signals and LLM verdict.
    """
    # v7 §2.5: strict prior_state date alignment. Defense mode demands no
    # silent degradation — if the caller hands us a stale prior_state, we
    # fail loudly so the backtest harness operator notices, not silently
    # propagate a wrong baseline through the entire run.
    if prior_state is not None and as_of:
        expected_prev = _prev_trading_day(as_of, code=code)
        actual = (prior_state.get("analysis_date") or "")[:10]
        if expected_prev and actual != expected_prev:
            raise RuntimeError(
                f"prior_state date misalignment: expected {expected_prev} "
                f"(prev trading day of as_of={as_of}), got {actual}. "
                f"Defense mode requires strict date alignment to prevent silent "
                f"degradation in the backtest harness."
            )

    # Step 1: load settled bars and compute derived stats on the settled
    # series only. The intraday partial bar (if any) is grafted on
    # afterwards so that vol_ma / percentile ranks are not contaminated by
    # a non-stationary in-progress bar.
    df = _load_ohlcv(code, days=days, as_of=as_of)
    if df is None or len(df) < window + 5:
        return {"code": code, "name": name, "ok": False, "error": "数据不足"}

    df = _compute_derived(df, window=window)

    # Step 1a: live mode only — append today's intraday bar with derived
    # columns evaluated against the settled distribution.
    if not as_of:
        rt_bar = _fetch_realtime_bar(code)
        if rt_bar is not None:
            df = _append_realtime_with_derived(df, rt_bar)

    patterns = _detect_patterns(df)
    obv = _obv_trend(df)
    vr = _volume_regime(df)

    # Deterministic price/volume layer — independent of LLM phase verdict.
    # Reviewer 2026-05-11: trade_setup must be visible in every result
    # envelope so cache consumers (backtest, replay, plotter) can use the
    # deterministic gate instead of (or alongside) llm_confirmation_level.
    from .regime import (
        classify_regime, compute_ma_structure,
        derive_trade_setup, detect_supply_warnings,
    )
    _det_regime = classify_regime(df)
    _det_setup = derive_trade_setup(df)
    _det_warnings = detect_supply_warnings(df)
    _det_ma = compute_ma_structure(df)
    _det_fields = {
        "price_regime": _det_regime.get("regime", "unknown"),
        "price_regime_detail": _det_regime,
        "trade_setup": _det_setup.get("setup", "none"),
        "trade_setup_score": _det_setup.get("score", 0.0),
        "trade_setup_reason": _det_setup.get("reason", ""),
        "trade_setup_features": _det_setup.get("features", {}),
        "supply_warnings": [w.get("pattern") for w in _det_warnings],
        "supply_warnings_detail": _det_warnings,
        "ma_structure": _det_ma,
    }
    # v7 §2.2: replace v5.x _format_text with the 5-section _format_user_message.
    # Candidates are scanned from the same derived df. _format_text is kept in
    # the codebase for now (cleanup PR will remove it) but no longer called.
    candidates = _scan_climax_candidates(df)
    text = _format_user_message(code, name, df, candidates, prior_state=prior_state, window=window, as_of=as_of)

    # Step 1b: 60-min VPA (multi-timeframe). Skipped in backtest replay
    # because Sina's 60-min API only returns current-day bars.
    if not as_of:
        try:
            df_60min = _load_ohlcv_60min(code, bars=80)
            if df_60min is not None and len(df_60min) >= 20:
                df_60min = _compute_derived(df_60min, window=12)
                patterns_60min = _detect_patterns(df_60min)
                section_60 = _format_60min_section(df_60min, patterns_60min)
                if section_60:
                    text = text + section_60
        except Exception as e:
            logger.debug("60-min VPA for %s failed (non-fatal): %s", code, e)

    # Step 2: Fetch previous analysis for context continuity
    # In replay mode, only see analyses BEFORE as_of (prevent future leakage)
    if previous_analysis_override is not None:
        previous_report = previous_analysis_override
    else:
        previous_report = ""
        try:
            from alpha_agents.data.memory_store import get_latest_vpa_analysis
            prev = get_latest_vpa_analysis(code, as_of=as_of)
            if prev and prev.get("report"):
                previous_report = prev["report"]
                logger.debug("Found previous VPA analysis for %s from %s", code, prev.get("analysis_date"))
        except Exception:
            pass

    # Step 3: LLM interpretation with history context
    llm_result = _call_llm_vpa(
        code,
        text,
        previous_analysis=previous_report,
        as_of=as_of,
        phase_context=_phase_guard_context_from_df(df, as_of=as_of),
        prior_state=prior_state,
        candidates=candidates,
    )
    llm_valid = bool(llm_result.get("analysis_valid", llm_result.get("status") == "ok"))
    llm_status = llm_result.get("status", "ok" if llm_valid else "error")

    # Step 4: Save analysis to history (skipped in backtest)
    import time as _time
    # In replay mode, analysis_date = as_of (not real today)
    analysis_date = as_of or _time.strftime("%Y-%m-%d")
    if skip_save:
        return {
            "code": code,
            "name": name,
            "ok": llm_valid,
            "error": None if llm_valid else llm_result.get("reason", "llm_invalid"),
            "code_patterns": patterns,
            "obv_trend": obv,
            "volume_regime": vr,
            "llm_status": llm_status,
            "llm_analysis_valid": llm_valid,
            "llm_error_type": llm_result.get("error_type"),
            "llm_schema_errors": llm_result.get("schema_errors", []),
            "llm_schema_warnings": llm_result.get("schema_warnings", []),
            "llm_prompt_version": llm_result.get("prompt_version"),
            "llm_model": llm_result.get("model"),
            "llm_provider": llm_result.get("provider"),
            "llm_usage": llm_result.get("usage"),
            "llm_report": llm_result.get("report", ""),
            "llm_verdict": llm_result.get("verdict", "中性"),
            "llm_confidence": llm_result.get("confidence", 0.5),
            "llm_phase": llm_result.get("phase", ""),
            "llm_raw_phase": llm_result.get("raw_phase", llm_result.get("phase", "")),
            "llm_warning_phase": llm_result.get("warning_phase", ""),
            "llm_phase_change": llm_result.get("phase_change", {}),
            "llm_phase_state_changed": llm_result.get("phase_state_changed", False),
            "llm_phase_guard_reason": llm_result.get("phase_guard_reason", ""),
            "llm_previous_phase": llm_result.get("previous_phase", ""),
            "llm_confirmed": llm_result.get("confirmed", False),
            "llm_action_confirmed": llm_result.get("action_confirmed", llm_result.get("confirmed", False)),
            "llm_confirmed_any_signal": llm_result.get("confirmed_any_signal", False),
            "llm_confirmed_all_signals": llm_result.get("confirmed_all_signals", False),
            "llm_action_signal_count": llm_result.get("action_signal_count", 0),
            "llm_action_confirmed_signal_count": llm_result.get("action_confirmed_signal_count", 0),
            "llm_decisive_confirmed_signal_count": llm_result.get("decisive_confirmed_signal_count", 0),
            "llm_structural_phase_change_confirmed": llm_result.get("structural_phase_change_confirmed", False),
            "llm_partial_confirmed": llm_result.get("partial_confirmed", False),
            "llm_phase_confidence": llm_result.get("phase_confidence", llm_result.get("confidence", 0.5)),
            "llm_confirmation_level": llm_result.get("confirmation_level", 0),
            "llm_confirmation_tier": llm_result.get("confirmation_tier", "none"),
            "vp_harmony_score": llm_result.get("vp_harmony_score", "neutral"),
            "vp_harmony_alignment": llm_result.get("vp_harmony_alignment", "neutral"),
            "vp_harmony_level_suggestion": llm_result.get("vp_harmony_level_suggestion"),
            "vp_harmony_level_adjustment": llm_result.get("vp_harmony_level_adjustment", 0),
            "vph_conflict": llm_result.get("vph_conflict", {"exists": False}),
            "vph_conflict_failures": llm_result.get("vph_conflict_failures", []),
            "vph_conflict_validated": llm_result.get("vph_conflict_validated", False),
            # v7 §2.5 / Task 7.1: slim cache observability fields. The backtest
            # picks these up to (a) audit which climax candidate the LLM chose,
            # (b) replay prior_state-driven defense mode, and (c) flag any
            # validator-driven cross-family revert.
            "selected_climax": llm_result.get(
                "selected_climax", {"candidate_id": None, "climax_type": None, "rationale": ""}
            ),
            "candidates_snapshot": candidates if candidates is not None else [],
            "prior_phase": (prior_state or {}).get("phase", "") or "",
            "prior_verdict": (prior_state or {}).get("verdict", "") or "",
            "prior_candidate_id": (prior_state or {}).get("selected_candidate_id"),
            "cross_family_blocked": bool(llm_result.get("cross_family_blocked", False)),
            "llm_reason": llm_result.get("reason", ""),
            "llm_target_low": llm_result.get("target_low"),
            "llm_target_high": llm_result.get("target_high"),
            "llm_scenarios": llm_result.get("scenarios", []),
            "text": text,
            **_det_fields,
        }
    if not llm_valid:
        return {
            "code": code,
            "name": name,
            "ok": False,
            "error": llm_result.get("reason", "llm_invalid"),
            "code_patterns": patterns,
            "obv_trend": obv,
            "volume_regime": vr,
            "llm_status": llm_status,
            "llm_analysis_valid": False,
            "llm_error_type": llm_result.get("error_type"),
            "llm_schema_errors": llm_result.get("schema_errors", []),
            "llm_schema_warnings": llm_result.get("schema_warnings", []),
            "llm_prompt_version": llm_result.get("prompt_version"),
            "llm_model": llm_result.get("model"),
            "llm_provider": llm_result.get("provider"),
            "llm_usage": llm_result.get("usage"),
            "llm_report": llm_result.get("report", ""),
            "llm_verdict": llm_result.get("verdict", "中性"),
            "llm_confidence": llm_result.get("confidence", 0.5),
            "llm_phase": llm_result.get("phase", ""),
            "llm_raw_phase": llm_result.get("raw_phase", llm_result.get("phase", "")),
            "llm_warning_phase": llm_result.get("warning_phase", ""),
            "llm_phase_change": llm_result.get("phase_change", {}),
            "llm_phase_state_changed": llm_result.get("phase_state_changed", False),
            "llm_phase_guard_reason": llm_result.get("phase_guard_reason", ""),
            "llm_previous_phase": llm_result.get("previous_phase", ""),
            "llm_confirmed": llm_result.get("confirmed", False),
            "llm_action_confirmed": llm_result.get("action_confirmed", llm_result.get("confirmed", False)),
            "llm_confirmed_any_signal": llm_result.get("confirmed_any_signal", False),
            "llm_confirmed_all_signals": llm_result.get("confirmed_all_signals", False),
            "llm_action_signal_count": llm_result.get("action_signal_count", 0),
            "llm_action_confirmed_signal_count": llm_result.get("action_confirmed_signal_count", 0),
            "llm_decisive_confirmed_signal_count": llm_result.get("decisive_confirmed_signal_count", 0),
            "llm_structural_phase_change_confirmed": llm_result.get("structural_phase_change_confirmed", False),
            "llm_partial_confirmed": llm_result.get("partial_confirmed", False),
            "llm_phase_confidence": llm_result.get("phase_confidence", llm_result.get("confidence", 0.5)),
            "llm_confirmation_level": llm_result.get("confirmation_level", 0),
            "llm_confirmation_tier": llm_result.get("confirmation_tier", "none"),
            "vp_harmony_score": llm_result.get("vp_harmony_score", "neutral"),
            "vp_harmony_alignment": llm_result.get("vp_harmony_alignment", "neutral"),
            "vp_harmony_level_suggestion": llm_result.get("vp_harmony_level_suggestion"),
            "vp_harmony_level_adjustment": llm_result.get("vp_harmony_level_adjustment", 0),
            "vph_conflict": llm_result.get("vph_conflict", {"exists": False}),
            "vph_conflict_failures": llm_result.get("vph_conflict_failures", []),
            "vph_conflict_validated": llm_result.get("vph_conflict_validated", False),
            "selected_climax": llm_result.get(
                "selected_climax", {"candidate_id": None, "climax_type": None, "rationale": ""}
            ),
            "candidates_snapshot": candidates if candidates is not None else [],
            "prior_phase": (prior_state or {}).get("phase", "") or "",
            "prior_verdict": (prior_state or {}).get("verdict", "") or "",
            "prior_candidate_id": (prior_state or {}).get("selected_candidate_id"),
            "cross_family_blocked": bool(llm_result.get("cross_family_blocked", False)),
            "llm_reason": llm_result.get("reason", ""),
            "llm_target_low": llm_result.get("target_low"),
            "llm_target_high": llm_result.get("target_high"),
            "llm_scenarios": llm_result.get("scenarios", []),
            "text": text,
            **_det_fields,
        }
    try:
        from alpha_agents.data.memory_store import (
            save_vpa_analysis, save_vpa_signal, save_vpa_scenario,
        )
        # v7 review I#82: persist selected_candidate_id so live-mode
        # get_prior_state_live can return it through the next-day defense
        # chain. selected_climax may be None on null-candidate days.
        _selected_climax = llm_result.get("selected_climax") or {}
        analysis_id = save_vpa_analysis(
            code=code, name=name,
            analysis_date=analysis_date,
            verdict=llm_result.get("verdict", "中性"),
            confidence=llm_result.get("confidence", 0.5),
            phase=llm_result.get("phase", ""),
            confirmed=llm_result.get("confirmed", False),
            reason=llm_result.get("reason", ""),
            report=llm_result.get("report", ""),
            signals_json=json.dumps(llm_result.get("signals", []), ensure_ascii=False),
            target_low=llm_result.get("target_low"),
            target_high=llm_result.get("target_high"),
            selected_candidate_id=_selected_climax.get("candidate_id"),
        )

        # Auto-create pending signals from VERDICT
        signals = llm_result.get("signals", [])
        for sig in signals:
            if not sig.get("confirmed", True):  # Only track unconfirmed signals
                save_vpa_signal(
                    code=code, name=name,
                    signal_type=sig.get("name", "unknown"),
                    signal_date=sig.get("date", analysis_date),
                    direction=llm_result.get("verdict", "中性"),
                    expected_confirmation=sig.get("need", ""),
                    expected_denial=sig.get("deny", ""),
                    source_analysis_id=analysis_id,
                )

        # Problem 7: Auto-create pending scenarios from VERDICT
        scenarios = llm_result.get("scenarios", [])
        for sc in scenarios:
            status = sc.get("status", "pending")
            if status != "pending":
                continue  # Already confirmed/denied by LLM — no need to track
            save_vpa_scenario(
                code=code, name=name,
                scenario_name=sc.get("name", "unknown"),
                phase=sc.get("phase", ""),
                signal_names=sc.get("signal_names", []),
                confirmation=sc.get("confirmation", ""),
                denial=sc.get("denial", ""),
                scenario_date=analysis_date,
                source_analysis_id=analysis_id,
            )
    except Exception as e:
        logger.debug("Failed to save VPA analysis: %s", e)

    return {
        "code": code,
        "name": name,
        "ok": True,
        "error": None,
        "code_patterns": patterns,
        "obv_trend": obv,
        "volume_regime": vr,
        "llm_status": llm_status,
        "llm_analysis_valid": True,
        "llm_error_type": llm_result.get("error_type"),
        "llm_schema_errors": llm_result.get("schema_errors", []),
        "llm_schema_warnings": llm_result.get("schema_warnings", []),
        "llm_prompt_version": llm_result.get("prompt_version"),
        "llm_model": llm_result.get("model"),
        "llm_provider": llm_result.get("provider"),
        "llm_usage": llm_result.get("usage"),
        "llm_report": llm_result.get("report", ""),
        "llm_verdict": llm_result.get("verdict", "中性"),
        "llm_confidence": llm_result.get("confidence", 0.5),
        "llm_phase": llm_result.get("phase", ""),
        "llm_raw_phase": llm_result.get("raw_phase", llm_result.get("phase", "")),
        "llm_warning_phase": llm_result.get("warning_phase", ""),
        "llm_phase_change": llm_result.get("phase_change", {}),
        "llm_phase_state_changed": llm_result.get("phase_state_changed", False),
        "llm_phase_guard_reason": llm_result.get("phase_guard_reason", ""),
        "llm_previous_phase": llm_result.get("previous_phase", ""),
        "llm_confirmed": llm_result.get("confirmed", False),
        "llm_action_confirmed": llm_result.get("action_confirmed", llm_result.get("confirmed", False)),
        "llm_confirmed_any_signal": llm_result.get("confirmed_any_signal", False),
        "llm_confirmed_all_signals": llm_result.get("confirmed_all_signals", False),
        "llm_action_signal_count": llm_result.get("action_signal_count", 0),
        "llm_action_confirmed_signal_count": llm_result.get("action_confirmed_signal_count", 0),
        "llm_decisive_confirmed_signal_count": llm_result.get("decisive_confirmed_signal_count", 0),
        "llm_structural_phase_change_confirmed": llm_result.get("structural_phase_change_confirmed", False),
        "llm_partial_confirmed": llm_result.get("partial_confirmed", False),
        "llm_phase_confidence": llm_result.get("phase_confidence", llm_result.get("confidence", 0.5)),
        "llm_confirmation_level": llm_result.get("confirmation_level", 0),
        "llm_confirmation_tier": llm_result.get("confirmation_tier", "none"),
        "vp_harmony_score": llm_result.get("vp_harmony_score", "neutral"),
        "vp_harmony_alignment": llm_result.get("vp_harmony_alignment", "neutral"),
        "vp_harmony_level_suggestion": llm_result.get("vp_harmony_level_suggestion"),
        "vp_harmony_level_adjustment": llm_result.get("vp_harmony_level_adjustment", 0),
        "vph_conflict": llm_result.get("vph_conflict", {"exists": False}),
        "vph_conflict_failures": llm_result.get("vph_conflict_failures", []),
        "vph_conflict_validated": llm_result.get("vph_conflict_validated", False),
        # v7 §2.5 / Task 7.1: slim cache observability fields. The backtest
        # picks these up to (a) audit which climax candidate the LLM chose,
        # (b) replay prior_state-driven defense mode, and (c) flag any
        # validator-driven cross-family revert.
        "selected_climax": llm_result.get(
            "selected_climax", {"candidate_id": None, "climax_type": None, "rationale": ""}
        ),
        "candidates_snapshot": candidates if candidates is not None else [],
        "prior_phase": (prior_state or {}).get("phase", "") or "",
        "prior_verdict": (prior_state or {}).get("verdict", "") or "",
        "prior_candidate_id": (prior_state or {}).get("selected_candidate_id"),
        "cross_family_blocked": bool(llm_result.get("cross_family_blocked", False)),
        "llm_reason": llm_result.get("reason", ""),
        "llm_target_low": llm_result.get("target_low"),
        "llm_target_high": llm_result.get("target_high"),
        "llm_scenarios": llm_result.get("scenarios", []),
        "text": text,
        **_det_fields,
    }


def get_vpa_analysis_fn(code: str, name: str = "") -> str:
    """Tool wrapper: return VPA analysis as JSON string.

    Args:
        code: 6-digit stock code
        name: Optional stock name for display
    """
    try:
        if not code or not code.strip().isdigit() or len(code.strip()) != 6:
            return json.dumps({"code": code, "ok": False, "error": "无效股票代码"}, ensure_ascii=False)
        result = compute_vpa_with_llm(code.strip(), name=name or "")
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_vpa_analysis failed for %s: %s", code, e)
        return json.dumps({"code": code, "ok": False, "error": str(e)}, ensure_ascii=False)
