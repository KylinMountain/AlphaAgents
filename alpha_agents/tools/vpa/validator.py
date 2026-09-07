"""Cross-family + per-climax structural validation.

Maps the LLM's selected_climax against the candidate pool's real OHLCV
features and demotes phase/verdict on missing or failing evidence. This
is Anna's first-layer challenge gate, code-enforced.
"""

from .verdict import _phase_family


_BEARISH_FAMILIES = ("派发", "下跌", "抛售高峰")
_BULLISH_FAMILIES = ("吸筹", "拉升", "买入高峰", "卖压衰竭")

# Trend-extension floors are absolute (Anna's qualitative "已延伸"). Other
# magnitude floors (vol, range, |pct|) come from per-stock 60d 80th-pct in
# phase_context — see ``_phase_guard_context_from_df``. Intra-bar geometry
# (shadow, close-position) is normalized by definition so stays absolute.
_TREND_FLOORS_PCT = {
    "BC": +10.0, "SOS": +10.0, "UTAD": +10.0,        # require uptrend before
    "SC": -10.0, "SOW": -10.0, "Spring": -10.0,      # require downtrend before
    # AR removed in v5.1 #3: AR is a confirmation/reaction bar, not a
    # standalone climactic event. LLM must label the climax (BC/SC/etc)
    # and discuss AR as supporting evidence in narrative.
}

_VALID_CLIMAX_TYPES = frozenset({"BC", "SC", "SOS", "SOW", "UTAD", "Spring"})

_VERDICT_DEMOTE_MAP = {
    "看空": "偏空", "偏空": "偏空",
    "看多": "偏多", "偏多": "偏多",
    "中性": "中性",
}


def _trading_day_distance(climax_date: str, as_of_date: str) -> int | None:
    """Approximate trading days between climax_date (MM-DD) and as_of_date
    (YYYY-MM-DD). Approximation: weekends excluded, holidays not. Returns
    None when climax_date can't be parsed. v5.1 #5."""
    if not climax_date or not as_of_date:
        return None
    try:
        from datetime import date, timedelta
        # climax_date is "MM-DD" — infer year from as_of
        as_of_d = date.fromisoformat(as_of_date[:10])
        try:
            mo, dy = climax_date.split("-")
            cx_d = date(as_of_d.year, int(mo), int(dy))
        except (ValueError, IndexError):
            return None
        # If climax_date is after as_of_date in the same year, it's actually
        # in the prior calendar year (window crosses year boundary).
        if cx_d > as_of_d:
            cx_d = date(as_of_d.year - 1, int(mo), int(dy))
        days = (as_of_d - cx_d).days
        if days < 0:
            return None
        # Approximate weekends excluded: 5/7 of calendar days.
        return max(0, int(days * 5 / 7))
    except Exception:
        return None


# Per-climax-type rule sets (v5 fix to Anna's #4 critique). v5.1 #3+#5+#7:
# - AR removed (not a standalone climax)
# - climax_date vs as_of cross-check for null post_bar
# - climax_type fail-closed when missing
def _failures_for(climax_type: str, vc: dict, ctx: dict | None) -> list[str]:
    fails: list[str] = []
    ctx = ctx or {}

    def _f(field: str) -> float | None:
        v = vc.get(field)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # v5.2 fix: all magnitude floors come from the stock's own 60d
    # distribution (top 10% as the "qualitatively elevated" cutoff).
    # Previously hardcoded 1.5/0.4/etc — Anna's same-disease critique:
    # those are picked numbers without backtest validation.
    vol_floor = float(ctx.get("vol_ratio_p90", 1.5) or 1.5)
    abspct_floor = float(ctx.get("abspct_p90", 0.025) or 0.025) * 100  # to percent
    range_floor = float(ctx.get("range_vs_5d_avg_p90", 1.5) or 1.5)
    upper_shadow_floor = float(ctx.get("upper_shadow_p90", 0.4) or 0.4)
    lower_shadow_floor = float(ctx.get("lower_shadow_p90", 0.4) or 0.4)

    # ── Trend extension (absolute, as Anna defines) ──────────────────────
    trend_floor = _TREND_FLOORS_PCT.get(climax_type, 0.0)
    if abs(trend_floor) > 0.0:
        ext = _f("extended_trend_20d_pct")
        if ext is None:
            fails.append("extended_trend_20d_pct missing")
        elif trend_floor > 0 and ext < trend_floor:
            fails.append(f"trend_20d {ext:+.1f}% < +{trend_floor:.0f}% (uptrend required for {climax_type})")
        elif trend_floor < 0 and ext > trend_floor:
            fails.append(f"trend_20d {ext:+.1f}% > {trend_floor:.0f}% (downtrend required for {climax_type})")

    # ── Volume magnitude (per-stock 60d p90) ─────────────────────────────
    if climax_type in _VALID_CLIMAX_TYPES:
        vrx = _f("climax_volume_ratio")
        if vrx is None:
            fails.append("climax_volume_ratio missing")
        elif vrx < vol_floor:
            fails.append(f"vol_ratio {vrx:.2f} < p90 {vol_floor:.2f}")

    # ── Bar range (qualitative wide) — only for the climactic-event types
    # (BC/SC). SOS/SOW/UTAD/Spring are characterized by structure, not
    # necessarily wider-than-usual range.
    if climax_type in {"BC", "SC"}:
        rgx = _f("climax_range_vs_5d_avg_x")
        if rgx is None:
            fails.append("climax_range_vs_5d_avg_x missing")
        elif rgx < range_floor:
            fails.append(f"range_x {rgx:.2f} < {range_floor:.1f}")

    # ── Shadow + close-position (intra-bar shape).
    #
    # close_position floors are at 0.5 (semantic — "lower half" / "upper
    # half" comes straight from Anna's text and the meaning of "half" is
    # 0.5 by definition). v5.1's 0.55 / 0.45 asymmetry on UTAD/Spring was
    # invented and got removed in v5.2.
    #
    # shadow floors are now per-stock 90th percentile of upper_shadow /
    # lower_shadow, computed in phase_context. v5.1's 0.4 hardcoded was
    # arbitrary picked from "long shadow ≈ 40%" intuition.
    sh = _f("climax_shadow_ratio")
    cp = _f("climax_close_position")
    if climax_type == "BC":
        if sh is None or sh < upper_shadow_floor:
            fails.append(f"BC needs upper-shadow ≥p90 {upper_shadow_floor:.2f} (got {sh})")
        if cp is None or cp >= 0.5:
            fails.append(f"BC needs close in lower half (got {cp})")
    elif climax_type == "SC":
        if sh is None or sh < lower_shadow_floor:
            fails.append(f"SC needs lower-shadow ≥p90 {lower_shadow_floor:.2f} (got {sh})")
        if cp is None or cp <= 0.5:
            fails.append(f"SC needs close in upper half (got {cp})")
    elif climax_type == "UTAD":
        if cp is None or cp >= 0.5:
            fails.append(f"UTAD needs close back in lower half (got {cp})")
    elif climax_type == "Spring":
        if cp is None or cp <= 0.5:
            fails.append(f"Spring needs close back in upper half (got {cp})")
    elif climax_type == "SOS":
        if cp is None or cp <= 0.5:
            fails.append(f"SOS needs close in upper half (got {cp})")
    elif climax_type == "SOW":
        if cp is None or cp >= 0.5:
            fails.append(f"SOW needs close in lower half (got {cp})")

    # ── Post-bar follow-through, with v5.1 #5 climax_date recency check ──
    # null is the LLM's "待观察" — only legitimate when climax_date is
    # within the last 3 trading days. Validator now enforces this for the
    # climax types that USE post_bar_reverse direction (BC/SC/UTAD/Spring).
    # SOS/SOW expect continuation in the climax direction, not "reversal" —
    # post_bar_reverse doesn't fit their validation, so null is fine
    # regardless of recency.
    if climax_type in {"BC", "SC", "UTAD", "Spring"}:
        post = _f("post_bar_reverse_pct_3d")
        climax_date = vc.get("climax_date") or ""
        as_of_date = ctx.get("as_of_date", "")
        distance = _trading_day_distance(str(climax_date), str(as_of_date))
        if post is None:
            if distance is not None and distance > 3:
                fails.append(
                    f"post_bar_reverse_pct_3d=null only allowed when climax_date "
                    f"is within 3 trading days; got distance={distance}"
                )
        else:
            # v5.2: floor = the stock's own p90 |pct_change|. v5.1's
            # 0.6× multiplier was a picked number without justification;
            # going to "magnitude in the top 10% of recent moves" is a
            # cleaner Anna-style "qualitatively significant" cutoff.
            floor_abs = abspct_floor
            if climax_type in {"BC", "UTAD"}:
                if post >= 0 or abs(post) < floor_abs:
                    fails.append(f"post_bar_reverse {post:+.1f}% expected ≤ -{floor_abs:.1f}% (p90)")
            elif climax_type in {"SC", "Spring"}:
                if post <= 0 or abs(post) < floor_abs:
                    fails.append(f"post_bar_reverse {post:+.1f}% expected ≥ +{floor_abs:.1f}% (p90)")

    return fails


def _phase_in(phase: str, families: tuple) -> bool:
    return any(f in (phase or "") for f in families)


def _candidate_to_vc_shape(candidate: dict, climax_type: str, phase_context: dict | None) -> dict:
    """Convert a candidate dict to the legacy vph_conflict-shape dict so
    `_failures_for` (unchanged) can read its fields by the same names.
    Real numbers from OHLCV — never from LLM."""
    if not candidate:
        return {"exists": False}
    return {
        "exists": True,
        "climax_type": climax_type,
        "climax_date": candidate.get("date", ""),
        "extended_trend_20d_pct": (phase_context or {}).get("trend_20d_pct"),  # v7: pulled from ctx
        "climax_volume_ratio": candidate.get("vol_ratio"),
        "climax_range_vs_5d_avg_x": candidate.get("range_vs_5d_avg"),
        "climax_shadow_ratio": (
            candidate.get("upper_shadow")
            if climax_type in ("BC", "UTAD")
            else candidate.get("lower_shadow")
        ),
        "climax_close_position": candidate.get("close_position"),
        "post_bar_reverse_pct_3d": (
            candidate.get("post_bar_reverse_3d") * 100
            if candidate.get("post_bar_reverse_3d") is not None
            else None
        ),
    }


def _validate_vph_conflict_evidence(data: dict, phase_context: dict | None) -> None:
    """v7: validator looks up REAL OHLCV from the candidate pool, never from
    LLM-supplied numbers.

    Reads `data["selected_climax"]["candidate_id"]` and the candidates list
    from `phase_context["candidates"]`. Runs `_failures_for` on the looked-up
    candidate's real numerical features. LLM-emitted numbers in
    `data["vph_conflict"]` are explicitly ignored.

    On failure: phase demoted to family floor + verdict walked back per spec
    §2.4 atomic-revert specification.
    """
    data["vph_conflict_validated"] = False
    score = (phase_context or {}).get("vp_harmony_score", "neutral")
    if score not in ("bullish", "bearish"):
        return
    phase = data.get("phase", "") or ""
    family = _phase_family(phase)
    bearish_phase = family in {"distribution", "markdown"}
    bullish_phase = family in {"accumulation", "markup"}
    conflict = (
        (bearish_phase and score == "bullish")
        or (bullish_phase and score == "bearish")
    )
    if not conflict:
        return

    # v7: read selected_climax (LLM's choice from candidate pool)
    sc = data.get("selected_climax") or {}
    candidate_id = sc.get("candidate_id")
    climax_type = sc.get("climax_type")

    failures: list[str] = []
    matched_candidate: dict | None = None

    if not candidate_id:
        failures.append("selected_climax.candidate_id is null despite phase-vph conflict")
    else:
        candidates = (phase_context or {}).get("candidates", []) or []
        for c in candidates:
            if c.get("id") == candidate_id:
                matched_candidate = c
                break
        if matched_candidate is None:
            failures.append(f"selected_climax.candidate_id={candidate_id!r} not in candidate pool")

    if matched_candidate is not None:
        if not climax_type:
            failures.append("climax_type missing in selected_climax")
        elif climax_type not in _VALID_CLIMAX_TYPES:
            failures.append(
                f"climax_type {climax_type!r} not in valid set "
                f"(AR removed v5.1 — AR is a confirmation bar, not a climax)"
            )
        else:
            # Build a vc-shaped dict from the real OHLCV in the candidate
            vc_real = _candidate_to_vc_shape(matched_candidate, climax_type, phase_context)
            failures.extend(_failures_for(climax_type, vc_real, phase_context))

    # v7 §2.4: cross-family rule. If today's family differs from prior's
    # family AND the rule fires, additional checks beyond the basic
    # _failures_for must pass.
    prior_phase = data.get("prior_phase") or ""
    if prior_phase and not failures and matched_candidate:
        prior_family = _phase_family(prior_phase)
        valid_families = {"accumulation", "markup", "distribution", "markdown"}
        cross_family = (
            prior_family in valid_families
            and family in valid_families
            and prior_family != family
        )
        if cross_family:
            post = matched_candidate.get("post_bar_reverse_3d")
            post_obs = matched_candidate.get("post_bars_observed", 0)
            abspct_p90 = float((phase_context or {}).get("abspct_p90", 0.025) or 0.025)
            cross_failures: list[str] = []
            if post_obs < 3:
                cross_failures.append(
                    f"cross-family rule: post_bars_observed={post_obs} < 3, "
                    f"climax too recent for AR confirmation"
                )
            elif post is None:
                cross_failures.append(
                    "cross-family rule: post_bar_reverse_3d is null"
                )
            else:
                # Sign convention: BC/UTAD/SOW expect negative post; SC/Spring/SOS expect positive
                if climax_type in ("BC", "UTAD", "SOW"):
                    if post >= 0 or abs(post) < abspct_p90:
                        cross_failures.append(
                            f"cross-family rule: BC/UTAD/SOW expects post_bar_reverse "
                            f"≤ -{abspct_p90:.4f}, got {post:+.4f}"
                        )
                elif climax_type in ("SC", "Spring", "SOS"):
                    if post <= 0 or abs(post) < abspct_p90:
                        cross_failures.append(
                            f"cross-family rule: SC/Spring/SOS expects post_bar_reverse "
                            f"≥ +{abspct_p90:.4f}, got {post:+.4f}"
                        )

            if cross_failures:
                # Atomic revert per spec §2.4
                failures.extend(cross_failures)
                prior_verdict = data.get("prior_verdict", "中性") or "中性"
                data["phase"] = prior_phase
                data["direction"] = prior_verdict
                data["confirmed"] = False
                data["action_confirmed"] = False
                data["partial_confirmed"] = False
                data["confirmation_level"] = 1
                data["confirmation_tier"] = "pending"
                sc_field = data.get("selected_climax") or {}
                sc_field["candidate_id"] = None
                data["selected_climax"] = sc_field
                data["cross_family_blocked"] = True
                data["phase_guard_reason"] = (
                    f"cross-family revert: prior={prior_family} → "
                    f"today rejected to {family} due to: "
                    + cross_failures[0]
                )
                data["vph_conflict_validated"] = False
                data["vph_conflict"] = _candidate_to_vc_shape(
                    matched_candidate, climax_type, phase_context
                )
                data["vph_conflict_failures"] = failures
                return

    if not failures:
        data["vph_conflict_validated"] = True
        # Populate vph_conflict / vph_conflict_failures for backwards-compat readers
        # using REAL data from candidate, not LLM-supplied numbers
        data["vph_conflict"] = _candidate_to_vc_shape(matched_candidate, climax_type, phase_context)
        data["vph_conflict_failures"] = []
        return

    # Cross-family with failures: revert to prior instead of demoting to floor
    if prior_phase:
        prior_family = _phase_family(prior_phase)
        if prior_family in {"accumulation", "markup", "distribution", "markdown"} and prior_family != family:
            prior_verdict = data.get("prior_verdict", "中性") or "中性"
            data["phase"] = prior_phase
            data["direction"] = prior_verdict
            data["confirmed"] = False
            data["action_confirmed"] = False
            data["partial_confirmed"] = False
            data["confirmation_level"] = 1
            data["confirmation_tier"] = "pending"
            sc_field = data.get("selected_climax") or {}
            sc_field["candidate_id"] = None
            data["selected_climax"] = sc_field
            data["cross_family_blocked"] = True
            data["phase_guard_reason"] = (
                f"cross-family revert: prior={prior_family} → "
                f"today rejected to {family} (no valid climax). "
                + failures[0]
            )
            data["vph_conflict_validated"] = False
            data["vph_conflict"] = (
                _candidate_to_vc_shape(matched_candidate, climax_type, phase_context)
                if matched_candidate is not None
                else {"exists": True, "selected_candidate_id": candidate_id, "climax_type": climax_type}
            )
            data["vph_conflict_failures"] = failures
            return

    # Failure path: demote phase + walk verdict back
    data["vph_conflict_failures"] = failures
    data["vph_conflict"] = (
        _candidate_to_vc_shape(matched_candidate, climax_type, phase_context)
        if matched_candidate is not None
        else {"exists": True, "selected_candidate_id": candidate_id, "climax_type": climax_type}
    )
    if family == "markdown":
        data["phase"] = "下跌初期"
    elif family == "distribution":
        data["phase"] = "派发初期"
    elif family == "accumulation":
        data["phase"] = "吸筹初期"
    elif family == "markup":
        data["phase"] = "拉升初期"
    direction = data.get("direction", "中性") or "中性"
    data["direction"] = _VERDICT_DEMOTE_MAP.get(direction, direction)
    data["phase_guard_reason"] = (
        "v7 validator demote: " + "; ".join(failures[:2])
        + (" …" if len(failures) > 2 else "")
    )
    data["confirmed"] = False
    data["action_confirmed"] = False
    pc = dict(data.get("phase_change") or {})
    pc["confirmed"] = False
    if not pc.get("denial_level"):
        pc["denial_level"] = "v7_candidate_lookup_failed"
    data["phase_change"] = pc


def _validate_absorbed_test_signals(data: dict, phase_context: dict | None) -> None:
    """v10.2 / Phase 3 (Claude + GPT P1.9): deterministic absorbed_supply_test
    verification.

    LLM-emitted absorbed_supply_test signals must satisfy the v10.2 hard
    form (close_position > 0.6 AND up day) on the underlying bar. When the
    signal date matches a candidate in the pool we can read real OHLCV
    features and structurally enforce the rule. Otherwise (signal date not
    in candidate pool) we leave the signal alone — best-effort, since the
    candidate pool is curated for climax candidates and absorbed test bars
    may legitimately fall outside it.

    Failures: signal demoted to confirmed=false with explanation; failure
    text appended to vph_conflict_failures so the slim cache can record it.
    """
    signals = data.get("signals") or []
    if not isinstance(signals, list) or not signals:
        return
    candidates = (phase_context or {}).get("candidates") or []
    by_date: dict = {}
    for c in candidates:
        if isinstance(c, dict) and c.get("date"):
            by_date[str(c["date"])] = c

    failures: list[str] = []
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        name_raw = (sig.get("name") or "")
        name = name_raw.lower()
        # Match either English (absorbed/absorb) or Chinese (吸收/吸纳).
        if "absorbed" not in name and "absorb" not in name and "吸收" not in name_raw and "吸纳" not in name_raw:
            continue
        # Only police confirmed=true claims; pending candidates are fine.
        if not sig.get("confirmed"):
            continue
        sig_date = str(sig.get("date") or "").strip()
        if not sig_date:
            continue
        # Match against candidate pool: full YYYY-MM-DD or trailing MM-DD.
        cand = by_date.get(sig_date)
        if cand is None and len(sig_date) >= 5:
            short = sig_date[-5:]  # MM-DD trailing
            for d, c in by_date.items():
                if d.endswith(short):
                    cand = c
                    break
        if cand is None:
            # Not in pool — skip strict check (could be valid markup-internal absorbed test).
            continue

        # Check hard rule 1: close_position > 0.6 (strict per v10.2).
        close_pos = cand.get("close_position")
        try:
            close_pos_f = float(close_pos) if close_pos is not None else None
        except (TypeError, ValueError):
            close_pos_f = None
        if close_pos_f is None:
            continue  # missing data, can't enforce
        if close_pos_f <= 0.6:
            sig["confirmed"] = False
            sig["validator_demoted"] = True
            sig["demotion_reason"] = (
                f"v10.2 absorbed_supply_test 形态不合格: close_position="
                f"{close_pos_f:.2f} ≤ 0.6 硬门槛"
            )
            failures.append(
                f"absorbed_test @ {sig_date}: close_pos={close_pos_f:.2f} "
                f"violates v10.2 rule (must be > 0.6); demoted to confirmed=false"
            )
            continue

        # Check hard rule 2: up day (close > prev close). Use bar_type as proxy.
        bar_type = str(cand.get("bar_type") or "")
        if "阳" not in bar_type:
            sig["confirmed"] = False
            sig["validator_demoted"] = True
            sig["demotion_reason"] = (
                f"v10.2 absorbed_supply_test 必须是上涨日 (阳线); "
                f"实际 bar_type={bar_type!r}"
            )
            failures.append(
                f"absorbed_test @ {sig_date}: bar_type={bar_type!r} "
                f"is not 阳线 (v10.2 requires up day); demoted to confirmed=false"
            )

    if failures:
        existing = data.get("vph_conflict_failures") or []
        data["vph_conflict_failures"] = list(existing) + failures
