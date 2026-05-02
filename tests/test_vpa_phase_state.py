"""Tests for stateful Anna Coulling VPA phase handling."""

from alpha_agents.tools.vpa import (
    ANNA_COULLING_PROMPT,
    PHASE_TRANSITION_REQUIREMENTS,
    _apply_phase_state_guard,
    _extract_verdict,
    _format_previous_state_block,
    _phase_family,
)


PREV_MARKUP_REPORT = """
上一轮报告正文。
<!-- VERDICT: {"direction": "看多", "confidence": 0.82, "phase": "拉升", "reason": "HH/HL延续", "signals": [{"name": "SOS", "date": "11-18", "confirmed": true, "by": "放量突破区间上沿"}], "scenarios": [{"name": "主升延续", "phase": "拉升", "signal_names": ["SOS"], "confirmation": "回踩缩量不破突破位", "denial": "放量跌破20日线", "status": "confirmed"}]} -->
"""


PREV_ACCUMULATION_REPORT = """
<!-- VERDICT: {"direction": "偏多", "confidence": 0.68, "phase": "吸筹", "reason": "ST后缩量", "signals": [{"name": "ST", "date": "10-24", "confirmed": true, "by": "回测低点缩量"}], "scenarios": []} -->
"""


PREV_DISTRIBUTION_REPORT = """
<!-- VERDICT: {"direction": "偏空", "confidence": 0.72, "phase": "派发中期", "reason": "BC+AR+ST", "signals": [{"name": "BC", "date": "12-03", "confirmed": true, "by": "自动回落超过3%"}], "scenarios": []} -->
"""


def test_phase_family_maps_core_wyckoff_labels():
    assert _phase_family("吸筹尾声") == "accumulation"
    assert _phase_family("SC") == "accumulation"
    assert _phase_family("恐慌抛售高潮") == "accumulation"
    assert _phase_family("拉升初期") == "markup"
    assert _phase_family("派发初期") == "distribution"
    assert _phase_family("BC") == "distribution"
    assert _phase_family("买入高潮顶部") == "distribution"
    assert _phase_family("下跌初期") == "markdown"
    assert _phase_family("震荡整理") == "neutral"
    assert _phase_family("abc") == "unknown"
    assert _phase_family("misc") == "unknown"


def test_extract_verdict_keeps_phase_change_and_warning_phase():
    report = """
    <!-- VERDICT: {"direction": "偏空", "confidence": 0.54, "phase": "拉升", "warning_phase": "派发初期预警", "phase_change": {"from": "拉升", "to": "派发初期", "confirmed": false, "invalidated_by": "", "denial_level": "warning"}, "signals": []} -->
    """
    verdict = _extract_verdict(report)
    assert verdict["warning_phase"] == "派发初期预警"
    assert verdict["phase_change"]["to"] == "派发初期"
    assert verdict["phase_change"]["confirmed"] is False


def test_prompt_uses_consistent_schema_and_climax_thresholds():
    assert "cfm=False" not in ANNA_COULLING_PROMPT
    assert "20 日均量 **3 倍**" not in ANNA_COULLING_PROMPT
    assert "range > 近 5 日均 range × 2" not in ANNA_COULLING_PROMPT
    assert "拉升→派发必须停止创20日新高" not in PHASE_TRANSITION_REQUIREMENTS[
        ("markup", "distribution")
    ]
    assert "创新高本身不否定派发" in ANNA_COULLING_PROMPT


def test_previous_state_block_is_structured_not_raw_report():
    block = _format_previous_state_block(PREV_MARKUP_REPORT + "\n这段旧叙事不应继续传入")
    assert "previous_phase: 拉升" in block
    assert "previous_phase_family: markup" in block
    assert "阶段连续性参考" in block
    assert "不是必须延续的硬约束" in block
    assert "这段旧叙事不应继续传入" not in block


def test_guard_allows_cross_family_warning_as_description_phase():
    proposed = {
        "direction": "偏空",
        "confidence": 0.76,
        "phase": "派发初期",
        "reason": "单日放量上影",
        "signals": [
            {"name": "放量上影", "date": "11-24", "confirmed": False,
             "need": "跌破近5日低点", "deny": "继续创20日新高"}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(proposed, PREV_MARKUP_REPORT)
    assert guarded["phase"] == "派发初期"
    assert guarded["raw_phase"] == "派发初期"
    assert guarded["phase_state_changed"] is True
    assert guarded["confirmed"] is False
    assert guarded["confidence"] == 0.76
    assert guarded["phase_guard_reason"] == ""


def test_guard_allows_accumulation_to_markup_when_sos_confirmed():
    proposed = {
        "direction": "看多",
        "confidence": 0.83,
        "phase": "拉升初期",
        "reason": "SOS确认",
        "signals": [
            {"name": "SOS", "date": "11-18", "confirmed": True,
             "by": "放量突破吸筹区间上沿并守住突破位"}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(proposed, PREV_ACCUMULATION_REPORT)
    assert guarded["phase"] == "拉升初期"
    assert guarded["phase_state_changed"] is True
    assert guarded["phase_change"]["from"] == "吸筹"
    assert guarded["phase_change"]["to"] == "拉升初期"
    assert guarded["phase_change"]["confirmed"] is True
    assert guarded["confirmation_level"] == 3
    assert guarded["action_confirmed"] is True
    assert guarded["warning_phase"] == ""


def test_guard_allows_distribution_to_markdown_when_sow_confirmed():
    proposed = {
        "direction": "看空",
        "confidence": 0.81,
        "phase": "下跌初期",
        "reason": "SOW确认",
        "signals": [
            {"name": "SOW", "date": "12-18", "confirmed": True,
             "by": "放量跌破派发区间下沿并延伸3%以上"}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(proposed, PREV_DISTRIBUTION_REPORT)
    assert guarded["phase"] == "下跌初期"
    assert guarded["phase_state_changed"] is True
    assert guarded["phase_change"]["from"] == "派发中期"
    assert guarded["phase_change"]["to"] == "下跌初期"
    assert guarded["phase_change"]["confirmed"] is True
    assert guarded["confirmation_level"] == 3


def test_guard_allows_cross_family_phase_read_without_path_override():
    prev = """
    <!-- VERDICT: {"direction": "看空", "confidence": 0.78, "phase": "下跌初期", "signals": [{"name": "SOW", "confirmed": true, "by": "放量破位"}]} -->
    """
    proposed = {
        "direction": "看多",
        "confidence": 0.86,
        "phase": "拉升初期",
        "reason": "反弹阳线",
        "signals": [
            {"name": "SOS", "date": "11-28", "confirmed": True,
             "by": "反弹放量"}
        ],
        "phase_change": {"from": "下跌初期", "to": "拉升初期", "confirmed": True},
    }
    guarded = _apply_phase_state_guard(proposed, prev)
    assert guarded["phase"] == "拉升初期"
    assert guarded["raw_phase"] == "拉升初期"
    assert guarded["phase_state_changed"] is True
    assert guarded["phase_change"]["confirmed"] is True


def test_guard_requires_two_step_same_family_subphase_change():
    proposed = {
        "direction": "偏空",
        "confidence": 0.74,
        "phase": "派发尾声",
        "reason": "UTAD候选",
        "signals": [
            {"name": "派发后上冲（UTAD）候选", "date": "11-25", "confirmed": False}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(proposed, PREV_DISTRIBUTION_REPORT)
    assert guarded["phase"] == "派发中期"
    assert guarded["warning_phase"] == "派发尾声"
    assert guarded["phase_change"]["confirmed"] is False
    assert guarded["phase_change"]["denial_level"] == "subphase_unconfirmed"


def test_guard_can_upgrade_same_family_after_repeated_warning_and_decisive_signal():
    prev = """
    <!-- VERDICT: {"direction": "偏空", "confidence": 0.72, "phase": "派发中期", "warning_phase": "派发尾声", "signals": []} -->
    """
    proposed = {
        "direction": "看空",
        "confidence": 0.82,
        "phase": "派发尾声",
        "reason": "UTAD确认",
        "signals": [
            {"name": "派发后上冲（UTAD）", "date": "11-25", "confirmed": True,
             "by": "假突破后跌回区间"}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(proposed, prev)
    assert guarded["phase"] == "派发尾声"
    assert guarded["phase_state_changed"] is True


def test_guard_respects_llm_frame_when_signals_present_in_ranging_data():
    """Anna review item 5: if the LLM provides any Wyckoff signal — even an
    unconfirmed one — its phase frame is respected even on flat data. The
    ranging override is reserved for hallucinated phase claims with zero
    evidence."""
    proposed = {
        "direction": "偏多",
        "confidence": 0.72,
        "phase": "吸筹尾声",
        "reason": "卖压衰竭",
        "signals": [
            {"name": "卖压衰竭", "date": "12-10", "confirmed": True,
             "by": "缩量横盘"}
        ],
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(
        proposed,
        PREV_MARKUP_REPORT,
        phase_context={"trend_10d_pct": 1.2, "range_10d_pct": 7.5, "avg_volume_ratio_5d": 0.7},
    )
    assert guarded["phase"] == "吸筹尾声"
    assert guarded["phase_change"].get("denial_level") != "ranging"


def test_guard_overrides_to_ranging_only_when_zero_signals_and_truly_flat():
    """Strict ranging override fires only when the LLM emits no signal AND
    the window is tightly flat (|trend|<=2%, range<=8%, vol_ratio in
    [0.85,1.05])."""
    proposed = {
        "direction": "偏多",
        "confidence": 0.55,
        "phase": "吸筹初期",
        "reason": "horizontal range",
        "signals": [],  # no Wyckoff evidence supplied
        "scenarios": [],
    }
    guarded = _apply_phase_state_guard(
        proposed,
        PREV_MARKUP_REPORT,
        phase_context={"trend_10d_pct": 0.5, "range_10d_pct": 6.0, "avg_volume_ratio_5d": 0.95},
    )
    assert guarded["phase"] == "震荡"
    assert guarded["warning_phase"] == "吸筹初期"
    assert guarded["phase_change"]["denial_level"] == "ranging"


def test_guard_replays_cached_raw_phase_when_old_report_was_overridden():
    prev = """
    <!-- VERDICT: {"direction": "看空", "confidence": 0.78, "phase": "下跌初期", "signals": []} -->
    """
    proposed = {
        "direction": "看多",
        "confidence": 0.74,
        "phase": "下跌初期",
        "raw_phase": "拉升初期",
        "warning_phase": "拉升初期",
        "reason": "旧hard guard曾把phase锁在previous，但保留了raw_phase",
        "signals": [],
        "phase_change": {"from": "下跌初期", "to": "拉升初期", "confirmed": False},
    }
    guarded = _apply_phase_state_guard(proposed, prev)
    assert guarded["phase"] == "拉升初期"
    assert guarded["raw_phase"] == "拉升初期"
    assert guarded["phase_state_changed"] is True
