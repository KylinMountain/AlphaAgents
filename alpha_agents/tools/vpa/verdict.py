"""LLM-output parsing: VERDICT JSON extraction + phase keyword normalization.

Parses the structured VERDICT block from the Anna Coulling LLM report,
plus the phase-family taxonomy used by the validator and guard layers.
"""

import json


ACCUMULATION_KEYWORDS = (
    "吸筹", "accumulation", "spring", "lps", "selling climax", "sc",
    "恐慌抛售高潮", "卖出高潮", "底部吸收", "买入高峰",
)
MARKUP_KEYWORDS = ("拉升", "markup", "sos", "sign of strength", "主升", "上升")
DISTRIBUTION_KEYWORDS = (
    "派发", "distribution", "utad", "抛售高峰", "buying climax", "bc",
    "买入高潮", "买入高潮顶部", "顶部派发", "派发顶部",
)
MARKDOWN_KEYWORDS = ("下跌", "markdown", "sow", "sign of weakness", "破位")
NEUTRAL_KEYWORDS = ("震荡", "整理", "盘整", "中性", "未明", "ranging")

BULLISH_PHASE_FAMILIES = {"accumulation", "markup"}
BEARISH_PHASE_FAMILIES = {"distribution", "markdown"}

# Anna 两层框架：phase/verdict 是描述层，事件信号才是动作层。
# 这里定义需要“后续确认”的事件型 signals 关键词。
ACTION_SIGNAL_KEYWORDS = (
    "buying climax", "selling climax", "bc", "sc",
    "买入高潮顶部", "恐慌抛售高潮",
    "spring", "shakeout", "震仓", "假破位", "utad", "upthrust", "派发后上冲",
    "sos", "sow", "sign of strength", "sign of weakness", "强势确认", "弱势确认",
    "放量突破", "突破确认", "放量跌破", "破位", "跌破", "breakout", "breakdown",
    "锤头", "射击十字", "吊人", "吞没", "长腿十字", "反转",
    "需求测试", "供给测试", "demand test", "supply test",
    "false breakout", "fake breakout",
)

DECISIVE_CONFIRMATION_KEYWORDS = (
    "buying climax", "selling climax", "bc", "sc",
    "买入高潮顶部", "恐慌抛售高潮",
    "spring", "shakeout", "震仓", "假破位",
    "lps", "最后支撑",
    "utad", "upthrust", "派发后上冲",
    "sos", "sow", "sign of strength", "sign of weakness",
    "强势确认", "弱势确认", "放量突破", "放量跌破", "跌破区间", "跌破自动回落",
)

UNCONFIRMED_SIGNAL_MARKERS = (
    "候选", "雏形", "预警", "待确认", "未确认", "candidate", "pending",
)

PHASE_TRANSITION_REQUIREMENTS = {
    ("accumulation", "markup"):
        "吸筹→拉升必须有强势确认(SOS)/放量突破区间上沿并守住突破位",
    ("markup", "distribution"):
        "拉升→派发必须有已延伸涨势+买入高潮顶部(BC)后续自动回落，或多个初步供应(PSY)+bearish量价配合；单日上影只能预警",
    ("distribution", "markdown"):
        "派发→下跌必须有弱势确认(SOW)：放量跌破派发区间下沿并延伸>=3%",
    ("markdown", "accumulation"):
        "下跌→吸筹必须有恐慌抛售高潮/二次测试/春天/最后支撑点序列证据，单日锤头线不够",
}

ENTRY_PHASE_BY_FAMILY = {
    "accumulation": "吸筹初期",
    "markup": "拉升初期",
    "distribution": "派发初期",
    "markdown": "下跌初期",
}

PHASE_STAGE_RANK_KEYWORDS = (
    ("尾声", 3),
    ("末期", 3),
    ("完成", 3),
    ("中期", 2),
    ("初期", 1),
)


def _extract_json_object_with_key(text: str, needle: str) -> str | None:
    """Find a JSON object containing ``needle`` and return its full text.

    Bracket-balancing scan that respects string literals — handles nested
    arrays/objects which a regex cannot. Returns ``None`` if no balanced
    object containing the needle is found.
    """
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            return None
        open_idx = text.rfind('{', 0, idx)
        if open_idx == -1:
            return None
        depth = 0
        in_str = False
        escape = False
        for i in range(open_idx, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[open_idx:i + 1]
        # Unbalanced from this position — search past this needle occurrence
        pos = idx + len(needle)


def _normalize_phase(phase: object) -> str:
    """Normalize an LLM phase label for comparison without losing display text."""
    return str(phase or "").strip()


def _contains_phase_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    """Match phase keywords; short English abbreviations must be standalone."""
    import re

    for keyword in keywords:
        k = keyword.lower()
        if k.isascii() and len(k) <= 4:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text):
                return True
        elif k in text:
            return True
    return False


def _phase_family(phase: object) -> str:
    """Map flexible LLM phase text to a stable Wyckoff family."""
    p = _normalize_phase(phase).lower()
    if not p:
        return ""
    if _contains_phase_keyword(p, MARKDOWN_KEYWORDS):
        return "markdown"
    # Buying Climax/BC is a distribution-top concept. "买入高峰" is kept only
    # as a legacy bottoming synonym from older cached reports.
    if _contains_phase_keyword(p, DISTRIBUTION_KEYWORDS):
        return "distribution"
    if _contains_phase_keyword(p, MARKUP_KEYWORDS):
        return "markup"
    if _contains_phase_keyword(p, ACCUMULATION_KEYWORDS):
        return "accumulation"
    if _contains_phase_keyword(p, NEUTRAL_KEYWORDS):
        return "neutral"
    return "unknown"


def _coerce_confidence(value: object, default: float = 0.5) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, confidence))


def _coerce_bool(value: object, default: bool = False) -> bool:
    """Safe bool coercion for LLM JSON fields.

    Python's ``bool("false")`` is True; VERDICT strings must not be upgraded
    into confirmed signals or phase changes.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "y", "是"}:
            return True
        if v in {"false", "0", "no", "n", "否", ""}:
            return False
    return default


def _normalize_phase_change(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "from": value.get("from", "") or "",
        "to": value.get("to", "") or "",
        "confirmed": _coerce_bool(value.get("confirmed", False)),
        "invalidated_by": value.get("invalidated_by", "") or "",
        "denial_level": value.get("denial_level", "") or "",
    }


def _normalize_signal(signal: object) -> dict | None:
    if not isinstance(signal, dict):
        return None
    name = str(signal.get("name", "") or "").strip()
    if not name:
        return None
    out = {
        "name": name,
        "date": str(signal.get("date", "") or ""),
        "confirmed": _coerce_bool(signal.get("confirmed", False)),
    }
    by = signal.get("by")
    need = signal.get("need")
    deny = signal.get("deny")
    if isinstance(by, str) and by.strip():
        out["by"] = by.strip()
    if isinstance(need, str) and need.strip():
        out["need"] = need.strip()
    if isinstance(deny, str) and deny.strip():
        out["deny"] = deny.strip()
    return out


def _signal_text(signal: dict) -> str:
    parts = [
        signal.get("name", ""),
        signal.get("by", ""),
        signal.get("need", ""),
        signal.get("deny", ""),
    ]
    return " ".join(str(p) for p in parts if p)


def _signal_requires_action_confirmation(signal: dict) -> bool:
    text = _signal_text(signal).lower()
    if not text:
        return False
    return _contains_phase_keyword(text, ACTION_SIGNAL_KEYWORDS)


def _is_confirmed_decisive_signal(signal: dict) -> bool:
    if not _coerce_bool(signal.get("confirmed", False)):
        return False
    text = _signal_text(signal).lower()
    if not text:
        return False
    if _contains_phase_keyword(text, UNCONFIRMED_SIGNAL_MARKERS):
        return False
    if _contains_phase_keyword(text, ("sos", "sign of strength", "强势确认")):
        return "放量" in text and ("突破" in text or "站稳" in text)
    if _contains_phase_keyword(text, ("sow", "sign of weakness", "弱势确认")):
        return "放量" in text and ("跌破" in text or "破位" in text)
    if _contains_phase_keyword(text, ("spring", "春天", "震仓")):
        return (
            ("假破" in text or "跌破" in text)
            and ("收复" in text or "收回" in text or "站回" in text)
        )
    if _contains_phase_keyword(text, ("lps", "最后支撑")):
        return "缩量" in text and ("企稳" in text or "守住" in text or "回踩" in text)
    if _contains_phase_keyword(text, ("utad", "upthrust", "派发后上冲")):
        return "假突破" in text and ("跌回" in text or "收回" in text or "回落" in text)
    if _contains_phase_keyword(text, ("buying climax", "bc", "买入高潮顶部")):
        return "自动回落" in text or "ar" in text or "回落" in text or "跌破" in text
    if _contains_phase_keyword(text, ("selling climax", "sc", "恐慌抛售高潮")):
        return "自动反弹" in text or "ar" in text or "反弹" in text or "收复" in text
    if "放量突破" in text or "放量跌破" in text:
        return True
    return False


def _is_structural_phase_change(phase_change: dict) -> bool:
    if not _coerce_bool(phase_change.get("confirmed", False)):
        return False
    from_family = _phase_family(phase_change.get("from"))
    to_family = _phase_family(phase_change.get("to"))
    if not from_family or not to_family or from_family == to_family:
        return False
    return (from_family, to_family) in PHASE_TRANSITION_REQUIREMENTS


def _derive_confirmation_level(
    *,
    action_signal_count: int,
    decisive_confirmed_signal_count: int,
    structural_phase_change_confirmed: bool,
) -> tuple[int, str]:
    """Map confirmation evidence into a 0-3 tier.

    Level definition (Anna two-layer compatible):
      0: no action-type signal.
      1: setup/background evidence only, or action signals are still pending.
      2: at least one decisive VPA event is confirmed, but structure has not switched.
      3: a decisive event confirms a valid Wyckoff phase transition.
    """
    if action_signal_count <= 0:
        return 0, "none"
    if decisive_confirmed_signal_count <= 0:
        return 1, "pending"
    if structural_phase_change_confirmed:
        return 3, "strong"
    return 2, "partial"


def _extract_verdict_json_text(report: str) -> str | None:
    """Return raw VERDICT JSON text from a report, if present."""
    import re

    match = re.search(r'<!--\s*VERDICT:\s*(\{.*\})\s*-->', report, re.DOTALL)
    if match:
        return match.group(1)
    raw = _extract_json_object_with_key(report, '"direction"')
    if raw:
        return raw
    # Last fallback for any single-line tightly-packed JSON.
    m2 = re.search(r'\{"direction":\s*"[^"]+?".*\}', report, re.DOTALL)
    if m2:
        return m2.group(0)
    return None


def _parse_verdict_json(raw: str | None) -> dict:
    """Parse a VERDICT JSON object without adding defaults or coercing fields."""
    if not raw:
        return {}
    try:
        from json_repair import repair_json
        data = repair_json(raw.strip(), return_objects=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            data = json.loads(raw.strip())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnboundLocalError, AttributeError):
            return {}


def _extract_raw_verdict(report: str) -> dict:
    """Extract raw machine VERDICT exactly as the model emitted it.

    This is the schema-validation input. Do not call ``setdefault`` or coerce
    bools here, otherwise missing fields and string booleans are hidden before
    the validator sees them.
    """
    return _parse_verdict_json(_extract_verdict_json_text(report))


def _normalize_verdict_data(raw_data: dict) -> dict:
    """Normalize a schema-valid raw VERDICT into the internal enriched shape."""
    data = dict(raw_data or {})
    if not data:
        return {}

    data.setdefault("direction", "中性")
    data.setdefault("confidence", 0.5)
    data.setdefault("phase", "")
    data.setdefault("warning_phase", "")
    data.setdefault("phase_change", {})
    data.setdefault("reason", "")
    data.setdefault("signals", [])
    data.setdefault("scenarios", [])
    data["confidence"] = _coerce_confidence(data.get("confidence"), default=0.5)
    data["phase_change"] = _normalize_phase_change(data.get("phase_change"))

    # P0.2 Target zone — optional. Coerce to float or None.
    for k in ("target_low", "target_high"):
        v = data.get(k)
        if v is None or v == "":
            data[k] = None
        else:
            try:
                data[k] = float(v)
            except (ValueError, TypeError):
                data[k] = None

    # Problem 7 scenarios: defensive normalization. Each must have name
    # and at least one signal_name — otherwise it's just a stray fragment
    # from the LLM and we drop it.
    scenarios = data.get("scenarios", [])
    if not isinstance(scenarios, list):
        scenarios = []
    normalized_scenarios = []
    for sc in scenarios:
        if not isinstance(sc, dict):
            continue
        name = sc.get("name", "").strip() if isinstance(sc.get("name"), str) else ""
        if not name:
            continue
        signal_names = sc.get("signal_names", []) or []
        if not isinstance(signal_names, list):
            signal_names = [str(signal_names)]
        normalized_scenarios.append({
            "name": name,
            "phase": sc.get("phase", "") or "",
            "signal_names": [str(s) for s in signal_names],
            "confirmation": sc.get("confirmation", "") or "",
            "denial": sc.get("denial", "") or "",
            "status": sc.get("status", "pending") or "pending",
        })
    data["scenarios"] = normalized_scenarios

    raw_signals = data.get("signals", [])
    if not isinstance(raw_signals, list):
        raw_signals = []
    signals = []
    for sig in raw_signals:
        n_sig = _normalize_signal(sig)
        if n_sig is not None:
            signals.append(n_sig)
    data["signals"] = signals

    confirmed_any_signal = any(_coerce_bool(s.get("confirmed", False)) for s in signals)
    confirmed_all_signals = bool(signals) and all(
        _coerce_bool(s.get("confirmed", False)) for s in signals
    )
    action_signals = [s for s in signals if _signal_requires_action_confirmation(s)]
    action_confirmed_signal_count = sum(
        1 for s in action_signals if _coerce_bool(s.get("confirmed", False))
    )
    decisive_confirmed_signals = [
        s for s in action_signals if _is_confirmed_decisive_signal(s)
    ]
    structural_phase_change_confirmed = _is_structural_phase_change(data["phase_change"])
    level, tier = _derive_confirmation_level(
        action_signal_count=len(action_signals),
        decisive_confirmed_signal_count=len(decisive_confirmed_signals),
        structural_phase_change_confirmed=structural_phase_change_confirmed,
    )
    partial_confirmed = level >= 2
    action_confirmed = level >= 3

    # 兼容字段：
    # - confirmed: 严格结构确认（C3，执行口径）
    # - partial_confirmed: 局部事件确认（C2+，观察/诊断口径）
    # - confirmed_all_signals: 旧口径（所有 signal 都 confirmed）
    data["confirmed_any_signal"] = confirmed_any_signal
    data["confirmed_all_signals"] = confirmed_all_signals
    data["action_signal_count"] = len(action_signals)
    data["action_confirmed_signal_count"] = action_confirmed_signal_count
    data["decisive_confirmed_signal_count"] = len(decisive_confirmed_signals)
    data["structural_phase_change_confirmed"] = structural_phase_change_confirmed
    data["partial_confirmed"] = partial_confirmed
    data["action_confirmed"] = action_confirmed
    data["confirmation_level"] = level
    data["confirmation_tier"] = tier
    data["phase_confidence"] = data["confidence"]
    data["confirmed"] = action_confirmed
    return data


def _failed_verdict() -> dict:
    return {"direction": "中性", "confidence": 0.0, "phase": "", "confirmed": False,
            "signals": [], "reason": "verdict_parse_failed",
            "target_low": None, "target_high": None, "scenarios": [],
            "warning_phase": "", "phase_change": {},
            "confirmed_any_signal": False, "confirmed_all_signals": False,
            "action_signal_count": 0, "action_confirmed_signal_count": 0,
            "partial_confirmed": False, "action_confirmed": False, "phase_confidence": 0.0,
            "confirmation_level": 0, "confirmation_tier": "none",
            "decisive_confirmed_signal_count": 0,
            "structural_phase_change_confirmed": False}


def _extract_verdict(report: str) -> dict:
    """Extract structured verdict from LLM report.

    Three lookup paths, in order of preference:
      1. ``<!-- VERDICT: {...} -->`` HTML-comment block (the prompt's
         requested format).
      2. Bracket-balanced JSON object containing ``"direction"`` — handles
         pretty-printed JSON inside ```json fences``` etc.
      3. Last-ditch single-line regex (kept for backward compatibility).

    Pre-fix, path 2 was a too-strict regex that only matched ``{"direction":``
    with no whitespace between ``{`` and the key — so any pretty-printed JSON
    fell through to ``verdict_parse_failed`` despite being valid. That cost
    36% of cached observations in the cybetf top20 backtest.
    """
    data = _normalize_verdict_data(_extract_raw_verdict(report))
    return data if data else _failed_verdict()


def _extract_selected_climax(report: str) -> dict:
    """Extract the v7 selected_climax field from a VERDICT JSON in a report.

    Returns a dict with always-present keys:
      - candidate_id: str | None
      - climax_type: str | None  (one of BC/SC/SOS/SOW/UTAD/Spring or None)
      - rationale: str (may be empty)

    Falls back gracefully on missing field, malformed JSON, etc.
    """
    default = {"candidate_id": None, "climax_type": None, "rationale": ""}
    if not report:
        return default
    try:
        verdict_data = _extract_verdict(report)
    except Exception:
        return default
    sc = verdict_data.get("selected_climax")
    if not isinstance(sc, dict):
        return default
    return {
        "candidate_id": sc.get("candidate_id") if sc.get("candidate_id") not in ("", "null") else None,
        "climax_type": sc.get("climax_type") if sc.get("climax_type") not in ("", "null") else None,
        "rationale": sc.get("rationale", "") or "",
    }


def _replace_verdict_in_report(report: str, verdict_data: dict) -> str:
    """Persist the guarded machine verdict so next-day replay sees stable state.

    Phase 3 (Claude + GPT P1.15): when the guard rewrote phase / blocked
    cross-family / demoted signals, append a visible 【校验器修正】 footer to
    the human-readable narrative so report body and machine VERDICT don't
    silently disagree.
    """
    import re

    payload = json.dumps(verdict_data, ensure_ascii=False, default=str)
    verdict_block = f"<!-- VERDICT: {payload} -->"

    # Build optional narrative footer when guard altered the LLM judgment.
    guard_reason = (verdict_data.get("phase_guard_reason") or "").strip()
    state_changed = bool(verdict_data.get("phase_state_changed"))
    cross_blocked = bool(verdict_data.get("cross_family_blocked"))
    failures = verdict_data.get("vph_conflict_failures") or []
    footer_lines: list[str] = []
    if guard_reason and (state_changed or cross_blocked):
        footer_lines.append(f"**校验器修正**: {guard_reason}")
    if failures:
        # Surface up to 2 validator failures so reader can see why.
        for f in failures[:2]:
            footer_lines.append(f"- 校验失败: {f}")
        if len(failures) > 2:
            footer_lines.append(f"- (... 另有 {len(failures) - 2} 条 validator 失败)")
    if footer_lines:
        footer_lines.append(
            f"最终 phase 以 VERDICT JSON 为准 "
            f"(`{verdict_data.get('phase', '') or '?'}`); "
            f"上文 narrative 是 LLM 原始判断, 可能与最终结论不同."
        )
    footer = ("\n\n---\n\n【校验器修正】\n" + "\n".join(footer_lines) + "\n") if footer_lines else ""

    if re.search(r'<!--\s*VERDICT:\s*\{.*\}\s*-->', report, re.DOTALL):
        new_report = re.sub(
            r'<!--\s*VERDICT:\s*\{.*\}\s*-->',
            verdict_block,
            report,
            count=1,
            flags=re.DOTALL,
        )
        if footer and "【校验器修正】" not in new_report:
            new_report = new_report.replace(
                verdict_block, footer + verdict_block
            )
        return new_report
    return f"{report.rstrip()}{footer}\n\n{verdict_block}"


def _extract_previous_state(previous_analysis: str) -> dict:
    """Compress a previous full report into point-in-time state.

    Passing the full old report back to the LLM tends to create narrative
    anchoring and can bury the latest OHLCV facts. For VPA continuity we only
    need the stable state and unresolved scenarios/signals.
    """
    if not previous_analysis:
        return {}
    verdict = _extract_verdict(previous_analysis)
    if verdict.get("reason") == "verdict_parse_failed" and not verdict.get("phase"):
        return {}

    scenarios = verdict.get("scenarios", [])
    if not isinstance(scenarios, list):
        scenarios = []
    compact_scenarios = []
    for sc in scenarios[:3]:
        if not isinstance(sc, dict):
            continue
        compact_scenarios.append({
            "name": sc.get("name", "") or "",
            "phase": sc.get("phase", "") or "",
            "confirmation": sc.get("confirmation", "") or "",
            "denial": sc.get("denial", "") or "",
            "status": sc.get("status", "pending") or "pending",
        })

    return {
        "direction": verdict.get("direction", "中性"),
        "confidence": _coerce_confidence(verdict.get("confidence"), default=0.5),
        "phase": verdict.get("phase", "") or "",
        "phase_family": _phase_family(verdict.get("phase", "")),
        "warning_phase": verdict.get("warning_phase", "") or "",
        "confirmed": _coerce_bool(verdict.get("confirmed", False)),
        "reason": verdict.get("reason", "") or "",
        "scenarios": compact_scenarios,
    }


def _format_previous_state_block(previous_analysis: str) -> str:
    """Render previous state as strict continuity guidance for the LLM."""
    state = _extract_previous_state(previous_analysis)
    if not state:
        return ""

    lines = [
        "【上次结构化VPA状态】",
        f"- previous_phase: {state.get('phase') or '未知'}",
        f"- previous_phase_family: {state.get('phase_family') or 'unknown'}",
        f"- previous_direction: {state.get('direction') or '中性'}",
        f"- previous_confirmed: {state.get('confirmed')}",
        f"- previous_reason: {state.get('reason') or '无'}",
    ]
    if state.get("warning_phase"):
        lines.append(f"- previous_warning_phase: {state.get('warning_phase')}")

    scenarios = state.get("scenarios", [])
    if scenarios:
        lines.append("- previous_scenarios:")
        for sc in scenarios:
            lines.append(
                "  "
                f"* {sc.get('name') or '未命名'} / {sc.get('phase') or '未知'} "
                f"/ {sc.get('status') or 'pending'}; "
                f"confirm={sc.get('confirmation') or '未写'}; "
                f"deny={sc.get('denial') or '未写'}"
            )

    lines.extend([
        "【阶段连续性参考】",
        "- previous_phase 只是上下文参考，不是必须延续的硬约束。",
        "- phase 是对当前整段量价结构的描述；如果最新量价结构已经改变，可以直接切换主阶段。",
        "- 单日异常量价优先写 warning_phase；只有当整段结构支持时才改 phase。",
        "- confirmed/confirmation_level 用于行动信号强度，不用于阻止 description 层的 phase 读取。",
    ])
    return "\n".join(lines)
