"""Institutional position analysis — quantitative buy/sell signal computation.

Computes actionable trading signals from institutional behavior data:
1. Fund flow momentum — main force inflow/outflow trend and strength
2. Institutional cost basis — estimated cost from LHB/block trades/north flow
3. Relative strength — stock vs sector performance ranking
4. Turnover regime — volume/turnover analysis for entry timing

This is the core "机构思维" tool: positions are derived from who is
buying/selling and at what cost, not from chart patterns or indicators.
"""

import json
import logging
from datetime import datetime, timedelta

from alpha_agents.data.market_data import (
    get_stock_history, get_individual_fund_flow,
    get_lhb, get_block_trades, get_north_holdings,
    get_margin_detail, get_realtime_quotes,
)

logger = logging.getLogger(__name__)


def get_institutional_position_fn(code: str, market: str = "") -> str:
    """分析个股的机构持仓行为，输出量化买卖信号。

    综合资金流动量、机构成本区间、相对强弱、换手率状态，
    给出介入区间和止损位建议。基于机构行为数据，不使用技术指标。

    Args:
        code: 股票代码，如 "300394"
        market: "sh"上海/"sz"深圳，留空自动判断
    """
    try:
        return _do_institutional_position(code, market)
    except Exception as e:
        logger.warning("institutional_position(%s) failed: %s", code, e)
        return json.dumps({"code": code, "error": str(e)}, ensure_ascii=False)


def _do_institutional_position(code: str, market: str) -> str:
    """Internal implementation with all the analysis steps."""
    if not market:
        market = "sh" if code.startswith("6") else "sz"

    # Fetch realtime price once, pass to sub-analyzers
    realtime_price = None
    rt = get_realtime_quotes([code])
    if rt and code in rt and rt[code]["price"] > 0:
        realtime_price = rt[code]["price"]

    result = {
        "code": code,
        "realtime_price": realtime_price,
        "price_source": "realtime" if realtime_price else "historical",
        "fund_flow": None,
        "institutional_cost": None,
        "relative_strength": None,
        "turnover_regime": None,
        "signal_summary": None,
        "action": None,
    }

    # ── 1. Fund flow momentum (资金流动量) ──
    fund_flow = _analyze_fund_flow(code, market, realtime_price)
    result["fund_flow"] = fund_flow

    # ── 2. Institutional cost basis (机构成本区间) — skip LHB/block trade to save time ──
    inst_cost = _analyze_institutional_cost_lite(code)
    result["institutional_cost"] = inst_cost

    # ── 3. Price and relative strength (相对强弱) ──
    price_info = _analyze_price_position(code, realtime_price)
    result["relative_strength"] = price_info

    # ── 4. Turnover regime (换手率状态) ──
    turnover = _analyze_turnover(code)
    result["turnover_regime"] = turnover

    # ── 5. Synthesize signals → action ──
    result["signal_summary"], result["action"] = _synthesize(
        code, fund_flow, inst_cost, price_info, turnover
    )

    return json.dumps(result, ensure_ascii=False)



def _analyze_fund_flow(code: str, market: str, realtime_price: float | None = None) -> dict:
    """Analyze main force fund flow momentum over recent days."""
    try:
        df = get_individual_fund_flow(code, market)
        if df is None or df.empty:
            return {"error": "无资金流数据"}

        df = df.tail(5)
        records = []
        total_main_net = 0
        for _, row in df.iterrows():
            main_net = float(row.get("主力净流入-净额", 0) or 0)
            main_pct = float(row.get("主力净流入-净占比", 0) or 0)
            total_main_net += main_net
            records.append({
                "date": str(row.get("日期", "")),
                "close": float(row.get("收盘价", 0) or 0),
                "main_net_yi": round(main_net / 1e8, 2),
                "main_net_pct": main_pct,
            })

        # Consecutive inflow/outflow
        consecutive_in, consecutive_out = 0, 0
        for r in reversed(records):
            if r["main_net_yi"] > 0:
                consecutive_in += 1
            else:
                break
        for r in reversed(records):
            if r["main_net_yi"] < 0:
                consecutive_out += 1
            else:
                break

        # Momentum score: -5 to +5
        momentum = min(5, max(-5, consecutive_in - consecutive_out))
        if abs(total_main_net) > 5e8:
            momentum += 1 if total_main_net > 0 else -1
        momentum = min(5, max(-5, momentum))

        if consecutive_in >= 3:
            trend = "主力强势流入"
        elif consecutive_in >= 2:
            trend = "主力持续流入"
        elif consecutive_out >= 3:
            trend = "主力持续流出"
        elif consecutive_out >= 2:
            trend = "主力开始流出"
        else:
            trend = "资金方向不明"

        latest_close = records[-1]["close"] if records else None
        if realtime_price:
            latest_close = realtime_price

        return {
            "trend": trend,
            "momentum_score": momentum,
            "consecutive_inflow_days": consecutive_in,
            "consecutive_outflow_days": consecutive_out,
            "total_5d_net_yi": round(total_main_net / 1e8, 2),
            "latest_close": latest_close,
            "daily": records,
        }
    except Exception as e:
        return {"error": str(e)}


def _analyze_institutional_cost_lite(code: str) -> dict:
    """Lightweight version — only check north flow and margin (fast).
    Skip LHB and block trades which require multiple date lookups."""
    cost_refs = []
    bullish, bearish = 0, 0
    sources_ok = 0

    # North flow
    try:
        df = get_north_holdings()
        if df is not None and not df.empty:
            sources_ok += 1
            match = df[df["代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                pct = float(row.get("今日持股-占流通股比", 0) or 0)
                change_pct = float(row.get("今日增持估计-市值增幅", 0) or 0)
                change = "增持" if change_pct > 0 else "减持" if change_pct < 0 else "持平"
                cost_refs.append({"source": "北向资金", "hold_pct": pct, "change_today": change})
                if change == "增持":
                    bullish += 1
                elif change == "减持":
                    bearish += 1
    except Exception as e:
        logger.warning("institutional_cost_lite north flow failed for %s: %s", code, e)

    # Margin
    try:
        df = get_margin_detail()
        if df is not None and not df.empty:
            sources_ok += 1
            match = df[df["标的证券代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                buy = float(row.get("融资买入额", 0) or 0)
                repay = float(row.get("融资偿还额", 0) or 0)
                net = buy - repay
                signal = "加杠杆" if net > 0 else "去杠杆"
                cost_refs.append({"source": "融资融券", "signal": signal})
                if net > 0:
                    bullish += 1
                else:
                    bearish += 1
    except Exception as e:
        logger.warning("institutional_cost_lite margin failed for %s: %s", code, e)

    if sources_ok == 0:
        return {"error": "所有数据源失败", "institutional_bullish": 0, "institutional_bearish": 0, "consensus": "数据缺失", "details": []}

    return {
        "institutional_bullish": bullish,
        "institutional_bearish": bearish,
        "consensus": "机构看多" if bullish > bearish else "机构看空" if bearish > bullish else "机构分歧",
        "details": cost_refs,
    }


def _analyze_institutional_cost(code: str) -> dict:
    """Estimate institutional cost basis from LHB, block trades, north flow."""
    cost_refs = []
    sources_ok = 0

    # LHB: institutional buy price ≈ close price on LHB day
    try:
        date = datetime.now().strftime("%Y%m%d")
        for attempt in range(3):
            df = get_lhb(date)
            if df is not None and not df.empty:
                sources_ok += 1
                match = df[df["代码"] == code]
                if not match.empty:
                    row = match.iloc[0]
                    net_buy = float(row.get("龙虎榜净买额", 0) or 0)
                    interpretation = str(row.get("解读", ""))
                    if net_buy > 0 and "机构" in interpretation:
                        # Approximate cost = close on that day
                        cost_refs.append({
                            "source": "龙虎榜机构买入",
                            "date": date,
                            "net_buy_yi": round(net_buy / 1e8, 2),
                            "note": interpretation[:50],
                        })
                break
            date = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except Exception as e:
        logger.warning("institutional_cost LHB failed for %s: %s", code, e)

    # Block trades: actual trade price (more precise than close)
    try:
        date = datetime.now().strftime("%Y%m%d")
        for attempt in range(3):
            df = get_block_trades(date)
            if df is not None and not df.empty:
                sources_ok += 1
                match = df[df["证券代码"] == code]
                if not match.empty:
                    row = match.iloc[0]
                    trade_price = float(row.get("成交价", 0) or 0)
                    discount = float(row.get("折溢率", 0) or 0)
                    amount = float(row.get("成交总额", 0) or 0)
                    if trade_price > 0:
                        cost_refs.append({
                            "source": "大宗交易",
                            "date": date,
                            "price": trade_price,
                            "discount_rate": discount,
                            "amount_wan": round(amount, 1),
                            "signal": "溢价接盘(看多)" if discount > 0 else "折价出货(看空)" if discount < -3 else "平价",
                        })
                break
            date = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except Exception as e:
        logger.warning("institutional_cost block trades failed for %s: %s", code, e)

    # North flow: position value / shares → implied cost
    try:
        df = get_north_holdings()
        if df is not None and not df.empty:
            sources_ok += 1
            match = df[df["代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                shares = float(row.get("今日持股-股数", 0) or 0)
                value = float(row.get("今日持股-市值", 0) or 0)
                pct = float(row.get("今日持股-占流通股比", 0) or 0)
                change_pct = float(row.get("今日增持估计-市值增幅", 0) or 0)
                if shares > 0:
                    cost_refs.append({
                        "source": "北向资金",
                        "hold_pct": pct,
                        "change_today": "增持" if change_pct > 0 else "减持" if change_pct < 0 else "持平",
                        "change_pct": change_pct,
                    })
    except Exception as e:
        logger.warning("institutional_cost north flow failed for %s: %s", code, e)

    # Margin: leverage direction
    try:
        df = get_margin_detail()
        if df is not None and not df.empty:
            sources_ok += 1
            match = df[df["标的证券代码"] == code]
            if not match.empty:
                row = match.iloc[0]
                balance = float(row.get("融资余额", 0) or 0)
                buy = float(row.get("融资买入额", 0) or 0)
                repay = float(row.get("融资偿还额", 0) or 0)
                net = buy - repay
                cost_refs.append({
                    "source": "融资融券",
                    "balance_yi": round(balance / 1e8, 2),
                    "net_buy_wan": round(net / 1e4, 1),
                    "signal": "加杠杆" if net > 0 else "去杠杆",
                })
    except Exception as e:
        logger.warning("institutional_cost margin failed for %s: %s", code, e)

    if sources_ok == 0:
        return {"error": "所有数据源失败", "institutional_bullish": 0, "institutional_bearish": 0, "consensus": "数据缺失", "details": []}

    # Count bullish/bearish institutional signals
    bullish = 0
    bearish = 0
    for ref in cost_refs:
        src = ref.get("source", "")
        if src == "龙虎榜机构买入":
            bullish += 1
        elif src == "大宗交易":
            if ref.get("discount_rate", 0) > 0:
                bullish += 1
            elif ref.get("discount_rate", 0) < -3:
                bearish += 1
        elif src == "北向资金":
            if ref.get("change_today") == "增持":
                bullish += 1
            elif ref.get("change_today") == "减持":
                bearish += 1
        elif src == "融资融券":
            if ref.get("signal") == "加杠杆":
                bullish += 1
            else:
                bearish += 1

    return {
        "institutional_bullish": bullish,
        "institutional_bearish": bearish,
        "consensus": "机构看多" if bullish > bearish else "机构看空" if bearish > bullish else "机构分歧",
        "details": cost_refs,
    }


def _analyze_price_position(code: str, realtime_price: float | None = None) -> dict:
    """Analyze price position using recent history — no technical indicators."""
    try:
        history = get_stock_history(code, days=20)
        if not history or len(history) < 5:
            return {"error": "历史数据不足"}

        closes = [d["close"] for d in history]
        latest = closes[-1]

        # Override with realtime price during trading hours
        if realtime_price:
            latest = realtime_price

        # 5-day and 20-day price range
        high_5d = max(closes[-5:])
        low_5d = min(closes[-5:])
        high_20d = max(closes)
        low_20d = min(closes)

        # Position within range (0% = at low, 100% = at high)
        range_20d = high_20d - low_20d
        position_pct = ((latest - low_20d) / range_20d * 100) if range_20d > 0 else 50

        # Recent returns (use latest which may be realtime price)
        ret_1d = (latest / closes[-2] - 1) * 100 if len(closes) >= 2 else 0
        ret_5d = (latest / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
        ret_10d = (latest / closes[-10] - 1) * 100 if len(closes) >= 10 else 0

        # Volume-weighted average price (VWAP) as cost proxy
        if all("volume" in d for d in history[-5:]):
            total_vol = sum(d["volume"] for d in history[-5:])
            if total_vol > 0:
                vwap_5d = sum(d["close"] * d["volume"] for d in history[-5:]) / total_vol
            else:
                vwap_5d = latest
        else:
            vwap_5d = sum(closes[-5:]) / 5

        # Determine position label
        if position_pct > 85:
            position = "高位"
        elif position_pct > 60:
            position = "中高位"
        elif position_pct > 40:
            position = "中位"
        elif position_pct > 15:
            position = "中低位"
        else:
            position = "低位"

        # Chase-high warning
        chasing_risk = ret_5d > 15 or ret_10d > 25

        return {
            "latest_price": latest,
            "position": position,
            "position_pct": round(position_pct, 1),
            "range_20d": {"high": high_20d, "low": low_20d},
            "range_5d": {"high": high_5d, "low": low_5d},
            "vwap_5d": round(vwap_5d, 2),
            "returns": {
                "1d": round(ret_1d, 2),
                "5d": round(ret_5d, 2),
                "10d": round(ret_10d, 2),
            },
            "chasing_risk": chasing_risk,
            "chasing_note": f"近5日涨{ret_5d:.1f}%，追高风险大" if chasing_risk else None,
        }
    except Exception as e:
        return {"error": str(e)}


def _analyze_turnover(code: str) -> dict:
    """Analyze turnover rate regime for entry timing."""
    try:
        history = get_stock_history(code, days=10)
        if not history or len(history) < 3:
            return {"error": "数据不足"}

        turnovers = [d.get("turnover_rate", 0) for d in history]
        latest_turnover = turnovers[-1]
        avg_turnover = sum(turnovers) / len(turnovers)

        # Volume ratio (量比 approximation): latest vs average
        volume_ratio = latest_turnover / avg_turnover if avg_turnover > 0 else 1

        # Turnover trend: expanding or contracting?
        recent_3 = turnovers[-3:]
        expanding = all(recent_3[i] > recent_3[i - 1] for i in range(1, len(recent_3)))
        contracting = all(recent_3[i] < recent_3[i - 1] for i in range(1, len(recent_3)))

        # Price-volume divergence
        closes = [d["close"] for d in history]
        price_up = closes[-1] > closes[-3] if len(closes) >= 3 else False
        vol_down = contracting

        if price_up and vol_down:
            regime = "缩量上涨(量价背离，需警惕)"
        elif price_up and expanding:
            regime = "放量上涨(量价配合)"
        elif not price_up and expanding:
            regime = "放量下跌(恐慌抛售)"
        elif not price_up and vol_down:
            regime = "缩量下跌(洗盘可能)"
        else:
            regime = "正常换手"

        return {
            "latest_turnover": round(latest_turnover, 2),
            "avg_turnover_10d": round(avg_turnover, 2),
            "volume_ratio": round(volume_ratio, 2),
            "regime": regime,
            "expanding": expanding,
            "contracting": contracting,
        }
    except Exception as e:
        return {"error": str(e)}


def _synthesize(
    code: str,
    fund_flow: dict,
    inst_cost: dict,
    price_info: dict,
    turnover: dict,
) -> tuple[dict, dict]:
    """Synthesize all signals into a summary and actionable recommendation."""

    bullish_signals = []
    bearish_signals = []
    confidence_penalty = 0

    # Check if sub-analyzers returned errors
    if fund_flow.get("error"):
        bearish_signals.append(f"资金流数据缺失: {fund_flow['error']}")
        confidence_penalty += 2
    if inst_cost.get("error"):
        bearish_signals.append(f"机构持仓数据缺失: {inst_cost['error']}")
        confidence_penalty += 2

    # Fund flow signals
    ff_momentum = fund_flow.get("momentum_score", 0)
    if ff_momentum >= 2:
        bullish_signals.append(f"资金动量强({fund_flow.get('trend', '')}，连续{fund_flow.get('consecutive_inflow_days', 0)}天流入)")
    elif ff_momentum <= -2:
        bearish_signals.append(f"资金动量弱({fund_flow.get('trend', '')})")

    # Institutional consensus
    consensus = inst_cost.get("consensus", "")
    if consensus == "机构看多":
        bullish_signals.append(f"机构看多({inst_cost.get('institutional_bullish', 0)}个机构信号)")
    elif consensus == "机构看空":
        bearish_signals.append(f"机构看空({inst_cost.get('institutional_bearish', 0)}个机构信号)")

    # Position risk
    chasing = price_info.get("chasing_risk", False)
    if chasing:
        bearish_signals.append(price_info.get("chasing_note", "追高风险"))

    position = price_info.get("position", "")
    if position in ("低位", "中低位"):
        bullish_signals.append(f"价格{position}，安全边际较大")
    elif position == "高位":
        bearish_signals.append("价格高位，回调风险大")

    # Turnover regime
    regime = turnover.get("regime", "")
    if regime == "放量上涨(量价配合)":
        bullish_signals.append("量价配合良好")
    elif regime == "缩量下跌(洗盘可能)":
        bullish_signals.append("缩量调整，可能是洗盘")
    elif regime == "放量下跌(恐慌抛售)":
        bearish_signals.append("放量下跌，抛压沉重")
    elif regime == "缩量上涨(量价背离，需警惕)":
        bearish_signals.append("缩量上涨，量价背离")

    # Overall score: -10 to +10
    score = len(bullish_signals) * 2 - len(bearish_signals) * 2
    score += ff_momentum
    score -= confidence_penalty
    score = min(10, max(-10, score))

    summary = {
        "score": score,
        "bullish_signals": bullish_signals,
        "bearish_signals": bearish_signals,
        "verdict": "强烈看多" if score >= 6 else "看多" if score >= 2 else "中性" if score > -2 else "看空" if score > -6 else "强烈看空",
    }
    if confidence_penalty > 0:
        summary["data_incomplete"] = True
        summary["verdict"] += "（数据不完整，置信度降低）"

    # Generate action
    latest_price = price_info.get("latest_price")
    vwap = price_info.get("vwap_5d")
    low_5d = price_info.get("range_5d", {}).get("low")
    low_20d = price_info.get("range_20d", {}).get("low")

    if not latest_price:
        return summary, {"recommendation": "数据不足，无法给出操作建议"}

    if score >= 4 and not chasing:
        # Strong bullish: give entry zone
        support = vwap if vwap and vwap < latest_price else low_5d
        stop_loss = low_5d * 0.97 if low_5d else latest_price * 0.93
        action = {
            "recommendation": "可介入",
            "entry_zone": f"{support:.2f} - {latest_price:.2f}" if support else f"当前价{latest_price:.2f}附近",
            "stop_loss": f"{stop_loss:.2f}",
            "stop_loss_note": "跌破5日低点3%止损" if low_5d else "跌破买入价7%止损",
            "reason": "；".join(bullish_signals),
        }
    elif score >= 2 and not chasing:
        support = vwap if vwap and vwap < latest_price else low_5d
        action = {
            "recommendation": "轻仓试探",
            "entry_zone": f"回调至{support:.2f}附近可试" if support else f"回调至{latest_price * 0.95:.2f}附近",
            "stop_loss": f"{low_5d * 0.97:.2f}" if low_5d else f"{latest_price * 0.93:.2f}",
            "reason": "；".join(bullish_signals) + "（但信号不够强，控制仓位）",
        }
    elif score <= -4:
        action = {
            "recommendation": "回避",
            "reason": "；".join(bearish_signals),
        }
    elif chasing:
        action = {
            "recommendation": "等回调",
            "entry_zone": f"回调至{vwap:.2f}附近再考虑" if vwap else "等充分回调",
            "reason": "短期涨幅过大，追高风险高。" + ("；".join(bullish_signals) if bullish_signals else ""),
        }
    else:
        action = {
            "recommendation": "观望",
            "reason": "多空信号交织，等待方向明确",
        }

    return summary, action
