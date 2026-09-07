"""User-message construction for the VPA LLM call.

Renders pre-computed indicators into the 4-5 section markdown the LLM
sees as its user content. v7's ``_format_user_message`` is the live
templater; ``_format_text`` is the v5.x legacy formatter retained for
backward-compat with cached reports / replay scripts.
"""

from datetime import datetime as _dt

import logging

import pandas as pd

logger = logging.getLogger(__name__)

from .data import _compute_5d_vph, _compute_context, _obv_trend, _volume_regime


def _render_price_action_narrative(df: pd.DataFrame, days: int = 30) -> str:
    """v7 §2.2(b): natural-language narrative of recent price action.

    Generates ONE-LINE-PER-BAR descriptors from raw OHLCV facts. Only
    objective physical descriptors are allowed:
      - 高开 / 低开 / 平开 (gap from prior close)
      - 阳线 / 阴线 / 十字星
      - 实体宽幅 / 实体窄幅 (binned by spread_pct20)
      - 缩量 / 放量 / 巨量 (binned by vr_pct60)
      - 上影 X.XX / 下影 X.XX (raw shadow ratios)
      - 收上半区 / 收下半区 / 收中位 (binned by close_position)
      - 量比 X.X (60日 pXX), 涨跌 X.X% (20日 pXX)

    NO interpretive language (forbidden list in test_vpa_v7_narrative_templater.py).
    """
    if df is None or len(df) == 0:
        return "### 近期价格与成交量节奏\n- (no data)"
    recent = df.tail(days)
    lines = ["### 近期价格与成交量节奏（最近 {} 个交易日）".format(min(days, len(recent)))]

    def _safe(value, default: float) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if pd.isna(v):
            return default
        return v

    prev_close = None
    for _, row in recent.iterrows():
        date_str = str(row.get("date", ""))
        opn = _safe(row.get("open"), 0.0)
        cls = _safe(row.get("close"), 0.0)
        bar_type = str(row.get("bar_type", ""))
        vr = _safe(row.get("volume_ratio"), 1.0)
        vr_pct = _safe(row.get("vr_pct60"), 0.5)
        spread_pct = _safe(row.get("spread_pct20"), 0.5)
        cp = _safe(row.get("close_position"), 0.5)
        upper = _safe(row.get("upper_shadow"), 0.0)
        lower = _safe(row.get("lower_shadow"), 0.0)
        pct = _safe(row.get("pct_change"), 0.0) * 100
        # v10.3 (Gemini): ATR-relative range. Helps LLM judge spread when
        # the 5d basis collapses after Spring / 吸筹尾声 narrow-range periods.
        range_atr = _safe(row.get("range_vs_atr"), 0.0)

        # Gap from prior
        gap_part = ""
        if prev_close is not None and prev_close > 0:
            gap_pct = (opn - prev_close) / prev_close * 100
            if abs(gap_pct) > 0.3:
                gap_part = "高开 " if gap_pct > 0 else "低开 "
                gap_part += f"{gap_pct:+.1f}%, "

        # Spread bin
        if spread_pct >= 0.80:
            spread_label = "实体宽幅"
        elif spread_pct <= 0.20:
            spread_label = "实体窄幅"
        else:
            spread_label = ""

        # Volume bin
        if vr_pct >= 0.90:
            vol_label = "巨量"
        elif vr_pct >= 0.60:
            vol_label = "放量"
        elif vr_pct <= 0.30:
            vol_label = "缩量"
        else:
            vol_label = ""

        # Close position bin
        if cp >= 0.70:
            cp_label = "收上半区"
        elif cp <= 0.30:
            cp_label = "收下半区"
        else:
            cp_label = "收中位"

        # Shadow note
        shadow_part = ""
        if upper >= 0.30:
            shadow_part = f", 上影 {upper:.2f}"
        if lower >= 0.30:
            shadow_part += f", 下影 {lower:.2f}"

        # ATR ratio shown when meaningfully off neutral (saves token noise on flat days).
        atr_part = f", range/ATR {range_atr:.2f}" if range_atr >= 0.5 else ""
        line = (
            f"- {date_str}: {gap_part}{bar_type} {pct:+.1f}%, "
            f"{spread_label}{(' ' if spread_label else '')}{cp_label}, "
            f"量比 {vr:.1f}（60日 p{int(vr_pct*100)}）"
            f"{(', ' + vol_label) if vol_label else ''}"
            f"{atr_part}"
            f"{shadow_part}"
        )
        lines.append(line)
        prev_close = cls
    return "\n".join(lines)


def _format_user_message(
    code: str,
    name: str,
    df: pd.DataFrame,
    candidates: list[dict],
    prior_state: dict | None = None,
    window: int = 20,
    as_of: str | None = None,
) -> str:
    """v7 §2.2 + v9: 4 (or 5+1) section user message.

    Sections:
      (a) 结构性环境块 — concise stats (no phase_sequence heuristic)
      (a2) v9 多周期视角 — 周线 + 月线 大背景（"Always look at the higher
           timeframe before judging the lower" — Anna Coulling）
      (b) 价格行为流水 — narrative templater output
      (c) 5 日 vp_harmony aggregate — unchanged from v5.1
      (d) 候选 K 线 JSON
      (e) 上一次判断（仅当 prior_state 非空）
    """
    parts = []
    parts.append(f"## {code} {name} VPA 预计算数据（v9 multi-timeframe）\n")

    # (a) Structural environment
    ctx = _compute_context(df, window=window, code=code)
    parts.append("### 环境统计（日线）")
    if "resistance_20d" in ctx:
        parts.append(f"- 20日阻力 {ctx['resistance_20d']}, 支撑 {ctx['support_20d']}")
        parts.append(f"- 当前价格位置 {ctx['position_label']}（区间 {ctx['position_in_range_pct']:.0f}%分位）")
    if "trend_10d_pct" in ctx:
        parts.append(f"- 10日趋势 {ctx['trend_10d_pct']:+.1f}%, 20日趋势 {ctx.get('trend_20d_pct', 0):+.1f}%")
    if "volatility_10d" in ctx:
        parts.append(f"- 10日波动率 {ctx['volatility_10d']:.2f}%, 20日波动率 {ctx['volatility_20d']:.2f}%")
    if "consolidation_strength" in ctx:
        parts.append(f"- 整理: {ctx['consolidation_strength']}")
    if "consolidation_prior_trend_pct" in ctx:
        parts.append(f"- 整理前趋势 {ctx['consolidation_prior_trend_pct']:+.1f}%（{ctx.get('accumulation_type', '')}）")

    # (a1) Daily MA structure — deterministic trend guardrail. Anna VPA
    # itself doesn't use MAs, but the LLM needs an explicit MA snapshot
    # to anchor "daily phase must match daily price action": when MA5<
    # MA10<MA20 with negative slope, no amount of weekly markup narrative
    # should let it call the daily phase "拉升初期".
    try:
        from .regime import compute_ma_structure
        ma = compute_ma_structure(df)
        if ma.get("ma_stack") != "insufficient":
            parts.append("")
            parts.append("### 日线均线结构（trend guardrail，仅供约束 phase 判断使用）")
            stack_label = {"bullish": "多头排列 MA5>MA10>MA20",
                            "bearish": "空头排列 MA5<MA10<MA20",
                            "mixed":   "混合（非完整排列）"}.get(ma["ma_stack"], ma["ma_stack"])
            parts.append(
                f"- MA5={ma['ma5']}, MA10={ma['ma10']}, MA20={ma['ma20']}"
                + (f", MA60={ma['ma60']}" if ma.get("ma60") is not None else "")
            )
            parts.append(
                f"- **排列**: {stack_label}  |  **MA20 5日斜率**: {ma['ma20_slope_5d']:+.2f}%  "
                f"|  close/MA20: {ma['close_vs_ma20_pct']:+.2f}%"
                + (f"  |  close/MA60: {ma['close_vs_ma60_pct']:+.2f}%"
                   if ma.get("close_vs_ma60_pct") is not None else "")
            )
            # Editorial guardrail — explicit so the LLM cannot ignore it
            if ma["ma_stack"] == "bearish" and (ma.get("ma20_slope_5d") or 0) < 0:
                parts.append(
                    "  > ⚠ 日线 MA 空头 + MA20 下行 → phase 不应标记为 拉升* 系。"
                    "若周/月线 markup 仍然成立，最多标 `震荡（偏空）` 或 `下跌初期`。"
                )
            elif ma["ma_stack"] == "bullish" and (ma.get("ma20_slope_5d") or 0) > 0:
                parts.append(
                    "  > ✓ 日线 MA 多头 + MA20 上行 → 支持 拉升 / 吸筹尾声 系；"
                    "若标记 派发/下跌* 必须有强 VPA 证据。"
                )
    except Exception as e:
        logger.debug("MA structure section failed (non-fatal): %s", e)

    # (a2) Higher-timeframe BACKGROUND — strictly data, no editorial bias.
    #
    # Reviewer audit (2026-05-11): the prior "**持续上涨期** (markup ≥ 8 周,
    # 不要轻判派发)" prose was instructing the model to refuse downside
    # phase transitions whenever weekly was up — the cause of 301379
    # holding "拉升初期" through a multi-week daily downtrend and 300443
    # holding "下跌初期" through a +20% recovery. Per Anna: higher-TF is
    # background context, NOT a phase anchor. Pass the numbers; let the
    # system prompt enforce that daily phase must reflect daily action.
    try:
        from .data import _load_ohlcv_weekly, _load_ohlcv_monthly, _summarize_higher_timeframe
        df_w = _load_ohlcv_weekly(code, weeks=26, as_of=as_of)
        df_m = _load_ohlcv_monthly(code, months=6, as_of=as_of)
        if df_w is not None or df_m is not None:
            parts.append("")
            parts.append("### 高时间周期背景（仅作 bias 参考，不锁定日线 phase）")
            if df_w is not None:
                w = _summarize_higher_timeframe(df_w, periods=12, label="周")
                if w:
                    wsl = w.get('weeks_since_low', 0)
                    parts.append(
                        f"- **周线 12 周**: 趋势 {w['trend_pct']:+.1f}%, 区间宽度 {w['range_pct']:.0f}%, "
                        f"位置 {w['position_in_range']:.2f} 分位, 涨{w['up_count']}/跌{w['down_count']}周 (方向一致性 {w['direction_consistency']:+.2f})"
                    )
                    parts.append(
                        f"  - 周线区间: {w['low']} ~ {w['high']}, 当前 {w['current']}, 距低点 {wsl} 周"
                    )
            if df_m is not None:
                m = _summarize_higher_timeframe(df_m, periods=6, label="月")
                if m:
                    parts.append(
                        f"- **月线 6 月**: 趋势 {m['trend_pct']:+.1f}%, 区间宽度 {m['range_pct']:.0f}%, "
                        f"位置 {m['position_in_range']:.2f} 分位, 涨{m['up_count']}/跌{m['down_count']}月 (方向一致性 {m['direction_consistency']:+.2f})"
                    )
    except Exception as e:
        logger.debug("higher-tf background section failed (non-fatal): %s", e)

    # (b) Price action narrative (rule-based template, no labels)
    parts.append("")
    parts.append(_render_price_action_narrative(df, days=30))

    # (c) 5 日 vp_harmony — reuses _compute_5d_vph from v5.1
    vph_label, vph_dir, _, _ = _compute_5d_vph(df)
    parts.append("")
    parts.append(f"### 近5日量价配合（vp_harmony，第一层挑战层输入）: {vph_label}")
    if vph_dir == "bullish":
        parts.append("> 当前 vph 偏多；若最终 phase 处于派发/下跌家族，通常应能在候选中看到可验证的 BC/UTAD/SOW 证据。")
    elif vph_dir == "bearish":
        parts.append("> 当前 vph 偏空；若最终 phase 处于拉升/吸筹家族，通常应能在候选中看到可验证的 SC/Spring/SOS 证据。")

    # (d) Candidate JSON
    parts.append("")
    parts.append("### 高成交异常 K 线候选（代码预筛选, 无标签）")
    parts.append("从中选出符合 BC / SC / SOS / SOW / UTAD / Spring 的事件，"
                 "或说明无符合事件。**只输出候选的 id**，不要重复其数值。")
    if candidates:
        import json as _json
        parts.append("```json")
        parts.append(_json.dumps({"candidates": candidates}, ensure_ascii=False, indent=2))
        parts.append("```")
    else:
        parts.append("（窗口内无符合条件的候选）")

    # (e) Prior state (only if provided)
    if prior_state:
        parts.append("")
        parts.append(f"### 你的上一次判断（{prior_state.get('analysis_date', '')}）")
        parts.append(f"- phase: {prior_state.get('phase', '')}")
        parts.append(f"- verdict: {prior_state.get('verdict', '')}")
        parts.append(f"- selected_candidate_id: {prior_state.get('selected_candidate_id', 'null')}")
        if prior_state.get("rationale"):
            parts.append(f"- rationale: {prior_state['rationale'][:200]}")
        parts.append("今日判断后, 若与上次跨家族（拉升 ↔ 派发 / 吸筹 ↔ 下跌）改判, "
                     "必须从今日候选中选择 validated climactic event 作为依据。")

    return "\n".join(parts)


def _format_text(code: str, name: str, df: pd.DataFrame,
                 patterns: list[dict], window: int = 20) -> str:
    """Build human-readable VPA data for LLM, with structural context."""
    output_days = min(15, len(df) - window)
    recent = df.tail(output_days) if output_days > 0 else df.tail(15)

    lines = [f"## {code} {name} VPA 预计算数据（基于 {window} 日均量基准）\n"]

    # ── Structural context (for Anna Coulling's three-step analysis) ──
    ctx = _compute_context(df, window=window, code=code)
    if ctx:
        lines.append("### 结构性上下文（全局视角）\n")
        if "resistance_20d" in ctx:
            lines.append(f"- **20日阻力位**: {ctx['resistance_20d']}  **20日支撑位**: {ctx['support_20d']}")
            lines.append(f"- **当前价格位置**: {ctx['position_label']}（20日区间内 {ctx['position_in_range_pct']:.0f}%）")
        if "trend_10d_pct" in ctx:
            trend_str = f"10日涨跌{ctx['trend_10d_pct']:+.2f}%"
            if "trend_20d_pct" in ctx:
                trend_str += f"，20日涨跌{ctx['trend_20d_pct']:+.2f}%"
            lines.append(f"- **趋势**: {ctx['trend_label']}（{trend_str}）")
        if "consolidation" in ctx:
            lines.append(f"- **整理区间**: {ctx['consolidation_note']}")
        # P0.1: explicit duration for cause-and-effect law
        if "consolidation_strength" in ctx:
            lines.append(f"- **整理时长**: {ctx['consolidation_strength']}")
        # #10: re-accumulation vs primary disambiguation
        if "accumulation_note" in ctx:
            lines.append(f"- **吸筹类型**: {ctx['accumulation_note']}")
        if "hl_trend" in ctx:
            lines.append(f"- **高低点趋势**: {ctx['hl_trend']}")
        if "high_5d" in ctx:
            lines.append(f"- **5日高点**: {ctx['high_5d']}  **5日低点**: {ctx['low_5d']}")
        # P1.2: relative strength on market down days
        if "relative_strength_label" in ctx:
            lines.append(f"- **相对强弱（价格）**: {ctx['relative_strength_label']}")
        # #8: volume relative strength on market down days
        if "volume_rel_strength_label" in ctx:
            lines.append(f"- **相对强弱（成交量）**: {ctx['volume_rel_strength_label']}")
        # P2.1: 20-day phase sequence (heuristic, LLM should refine)
        if "phase_sequence" in ctx:
            lines.append(f"- **20日 phase 序列（启发式，仅供参考）**: {ctx['phase_sequence']}")
        # 问题4: Wyckoff supply/demand test detection
        if "test_note" in ctx:
            lines.append(f"- **Wyckoff 测试**: {ctx['test_note']}")
        lines.append("")

    # ── OBV + volume regime ──
    lines.append(f"**OBV 趋势（10日）**: {_obv_trend(df)}")
    vr = _volume_regime(df)
    lines.append(f"**近5日量能状态**: {vr['regime']}（5日/20日均量 = {vr['ratio_5d_vs_20d']}）")

    # ── 5-day vp_harmony aggregate (Anna 第一层判据 — challenge gate input) ──
    # v5.1 #2: shared helper with _phase_guard_context_from_df so the LLM
    # input and the validator agree on settled-only vph (no realtime contamination).
    vph_label, vph_dir, _up_vol, _down_vol = _compute_5d_vph(df)
    lines.append(
        f"**近5日量价配合（vp_harmony，第一层挑战层输入）**: **{vph_label}**"
    )
    if vph_dir == "bullish":
        lines.append(
            "> 你若判定 phase 为 派发尾声/派发中期/下跌系列（与 vph 矛盾）"
            "→ 必须在 reason 中列出已满足的具体 BC 硬性条件，否则最多标'派发初期 confirmed=false'"
        )
    elif vph_dir == "bearish":
        lines.append(
            "> 你若判定 phase 为 拉升中期/拉升尾声/上涨系列（与 vph 矛盾）"
            "→ 必须在 reason 中列出已满足的具体 SC 硬性条件，否则最多标'拉升初期 confirmed=false'"
        )
    lines.append("")

    # ── Daily K-line table ──
    lines.append("### 逐日量价数据\n")
    lines.append("| 日期 | 类型 | 涨跌幅 | 实体 | 收盘位置 | 上影 | 下影 | 量比 | 量价关系 |")
    lines.append("|------|------|--------|------|----------|------|------|------|----------|")

    today_str = _dt.now().strftime("%Y-%m-%d")

    for _, row in recent.iterrows():
        raw_date = str(row.get("date", ""))
        dt = raw_date[-5:]
        # Mark today's realtime bar so LLM knows it's intraday (not settled)
        if raw_date == today_str:
            dt = f"{dt}(盘中实时)"
        pct = (row["pct_change"] * 100) if pd.notna(row["pct_change"]) else 0
        spread = row["bar_spread"]
        sp_pct = row.get("spread_pct20", 0.5)
        if pd.isna(sp_pct):
            sp_pct = 0.5
        spread_label = (
            "宽" if sp_pct >= 0.80 else ("窄" if sp_pct <= 0.20 else "中")
        )
        cp = row["close_position"]
        cp_label = "高位" if cp > 0.7 else ("低位" if cp < 0.3 else "中位")
        vol_r = row["volume_ratio"] if pd.notna(row["volume_ratio"]) else 0
        vr_pct = row.get("vr_pct60", 0.5)
        if pd.isna(vr_pct):
            vr_pct = 0.5
        # Bucket via percentile rank (each stock's own recent norm).
        if vr_pct >= 0.95:
            vr_tag = "极放量"
        elif vr_pct >= 0.80:
            vr_tag = "明显放量"
        elif vr_pct >= 0.60:
            vr_tag = "温和放量"
        elif vr_pct <= 0.05:
            vr_tag = "极缩量"
        elif vr_pct <= 0.20:
            vr_tag = "缩量"
        else:
            vr_tag = "中性"
        vr_label = f"{vol_r:.1f}({vr_tag},60日{vr_pct*100:.0f}分位)"

        lines.append(
            f"| {dt} | {row['bar_type']} | {pct:+.1f}% | {spread_label}({spread:.3f},20日{sp_pct*100:.0f}分位) "
            f"| {cp_label}({cp:.2f}) | {row['upper_shadow']:.2f} | {row['lower_shadow']:.2f} "
            f"| {vr_label} | {row['vp_harmony']} |"
        )

    lines.append("\n### 形态识别（中性物理描述，方向由 LLM 在威科夫上下文中判断）\n")
    if patterns:
        for p in patterns:
            lines.append(f"- **{p['label']}**（{p['pattern']}）: {p['detail']}")
    else:
        lines.append("- 近期无显著形态")

    return "\n".join(lines)


def _format_60min_section(df_60min: pd.DataFrame, patterns_60min: list[dict]) -> str:
    """Format 60-min VPA data as a supplemental section for the LLM.

    Daily bars tell us TREND (problem 5 from Anna Coulling review). 60-min
    bars tell us ENTRY TIMING and intraday accumulation/distribution. A-share
    ±10% caps compress daily extremes, so hourly volume-price matters more
    than in unrestricted markets.

    Returns a markdown section, or empty string if no useful data.
    """
    if df_60min is None or len(df_60min) < 10:
        return ""

    lines = ["\n### 60分钟级别量价上下文（用于入场时机判断，日线负责趋势）\n"]

    # Show last 12 60-min bars (≈3 trading days of intraday action)
    recent = df_60min.tail(12).reset_index(drop=True)
    lines.append("| 时间 | 类型 | 涨跌幅 | 收盘位置 | 量比 | 量价关系 |")
    lines.append("|------|------|--------|----------|------|----------|")
    for _, row in recent.iterrows():
        dt = str(row.get("date", ""))[-8:-3] if len(str(row.get("date", ""))) >= 8 else str(row.get("date", ""))
        # Need change_pct — compute if missing
        pct = (row.get("pct_change", 0) * 100) if pd.notna(row.get("pct_change", 0)) else 0
        spread = row.get("bar_spread", 0)
        cp = row.get("close_position", 0.5) or 0.5
        cp_label = "高" if cp > 0.7 else ("低" if cp < 0.3 else "中")
        vol_r = row.get("volume_ratio", 1) or 1
        vr_label = f"{vol_r:.1f}"
        if vol_r > 1.8:
            vr_label += "(放量)"
        elif vol_r < 0.6:
            vr_label += "(缩量)"
        bar_type = row.get("bar_type", "")
        vp_harmony = row.get("vp_harmony", "")
        lines.append(
            f"| {dt} | {bar_type} | {pct:+.2f}% | {cp_label}({cp:.2f}) | {vr_label} | {vp_harmony} |"
        )

    # 60-min patterns
    lines.append("\n**60分钟形态**（中性物理描述）:")
    if patterns_60min:
        for p in patterns_60min:
            lines.append(f"- {p['label']}（{p['pattern']}）: {p['detail']}")
    else:
        lines.append("- 近12根60分钟bar无显著形态")

    lines.append("\n> 使用指南：60分钟 bar 揭示当日盘中资金行为，与日线 bar 一同放入威科夫框架解读。")

    return "\n".join(lines)
