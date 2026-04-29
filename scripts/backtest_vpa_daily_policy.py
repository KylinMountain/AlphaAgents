"""Daily LLM VPA policy backtest.

This tests VPA as a *daily state machine*, not as a fixed T+5 forecast:

  - After each close D, run production ``compute_vpa_with_llm(..., as_of=D)``.
  - Convert the LLM verdict/phase into a policy action.
  - Execute that action at the next trading day's open.
  - While holding, re-check VPA after every close and monitor stop/target daily.

The output answers: if we used LLM VPA every day to enter/exit, what would
the realized, cost-adjusted trades look like?

Examples:
  uv run python scripts/backtest_vpa_daily_policy.py --stocks-per-day 20 --max-days 10
  uv run python scripts/backtest_vpa_daily_policy.py --stocks-per-day 100 --max-days 55 --workers 3
  uv run python scripts/backtest_vpa_daily_policy.py --require-bullish-phase
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd

from alpha_agents.data.market_history import _get_conn as _history_conn
from alpha_agents.data.portfolio import LOT_SIZE, _estimate_net_close_result
from alpha_agents.tools.vpa import compute_vpa_with_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vpa_daily_policy")

HORIZONS = [1, 2, 3, 4, 5]
MIN_HISTORY = 60

VERDICT_MAP = {
    "看多": "bullish",
    "偏多": "bullish",
    "看空": "bearish",
    "偏空": "bearish",
    "中性": "neutral",
}

BEARISH_PHASE_KEYWORDS = ("派发", "下跌", "抛售高峰")
BULLISH_PHASE_KEYWORDS = ("吸筹", "拉升", "买入高峰")


@dataclass
class Position:
    code: str
    entry_signal_date: str
    entry_date: str
    entry_price: float
    shares: int
    stop_price: float | None
    target_price: float | None
    entry_verdict: str
    entry_raw_verdict: str
    entry_phase: str
    entry_confidence: float
    entry_reason: str
    regime: str
    # Number of consecutive trading days the LLM verdict has been non-bullish
    # since entry. Drives --early-exit-on-verdict-decay.
    non_bullish_streak: int = 0
    # Notional size multiplier applied at entry (for --size-by-phase).
    size_factor: float = 1.0


def _norm_verdict(raw: str) -> str:
    return VERDICT_MAP.get(raw or "中性", "neutral")


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in (text or "") for k in keywords)


def _is_bearish_phase(phase: str) -> bool:
    return _has_keyword(phase, BEARISH_PHASE_KEYWORDS)


def _is_bullish_phase(phase: str) -> bool:
    return _has_keyword(phase, BULLISH_PHASE_KEYWORDS)


def _is_bare_accumulation(phase: str) -> bool:
    """True for plain 吸筹 without 初期 / 拉升 modifiers — the noisy slice
    where prior runs showed only ~39% win rate."""
    return ("吸筹" in phase) and ("初期" not in phase) and ("拉升" not in phase)


def _phase_size_factor(phase: str, args: argparse.Namespace) -> float:
    """Position-sizing multiplier when --size-by-phase is on.

    拉升 / 买入高峰 / 吸筹初期 → 1.0 (高质量 bullish phase)
    裸 吸筹                     → 0.5 (噪声大, 历史胜率偏低)
    其他                        → 1.0
    """
    if not args.size_by_phase or not phase:
        return 1.0
    if _is_bare_accumulation(phase):
        return 0.5
    return 1.0


def _is_entry_signal(
    vpa: dict[str, Any],
    args: argparse.Namespace,
    prev_vpa: dict[str, Any] | None = None,
) -> bool:
    verdict = _norm_verdict(vpa.get("llm_verdict", "中性"))
    phase = vpa.get("llm_phase", "") or ""
    conf = float(vpa.get("llm_confidence") or 0)
    if verdict != "bullish":
        return False
    if conf < args.buy_confidence:
        return False
    if _is_bearish_phase(phase):
        return False
    if args.require_bullish_phase and not _is_bullish_phase(phase):
        return False
    # Stability filter: bare 吸筹 single-day signals are noise. Require the
    # previous trading day's verdict to also be bullish so we only enter on
    # ≥ 2 days of agreement. (Doesn't apply to 拉升/吸筹初期 which are
    # already higher-quality entries.)
    if args.require_phase_stability and _is_bare_accumulation(phase):
        if prev_vpa is None or not prev_vpa.get("ok"):
            return False
        if _norm_verdict(prev_vpa.get("llm_verdict", "")) != "bullish":
            return False
    return True


def _exit_reason(
    vpa: dict[str, Any],
    args: argparse.Namespace,
    non_bullish_streak: int = 0,
) -> str | None:
    verdict = _norm_verdict(vpa.get("llm_verdict", "中性"))
    phase = vpa.get("llm_phase", "") or ""
    conf = float(vpa.get("llm_confidence") or 0)
    if _is_bearish_phase(phase):
        return f"vpa_phase:{phase}"
    if verdict == "bearish" and conf >= args.sell_confidence:
        return f"vpa_verdict:{vpa.get('llm_verdict', '')}"
    # Early exit: bullish entry has slipped to non-bullish for ≥ 2 consecutive
    # days (without yet hitting full 派发). Catches deterioration before the
    # phase fully turns and avoids the 0% win rate of late 下跌/抛售高峰 exits.
    if args.early_exit_on_verdict_decay and non_bullish_streak >= 2:
        return f"verdict_decay:{verdict}_streak{non_bullish_streak}"
    return None


def _calc_shares(price: float, notional: float) -> int:
    if price <= 0 or notional <= 0:
        return 0
    return int(notional / price) // LOT_SIZE * LOT_SIZE


def _load_all_histories(conn, max_stocks: int | None = None) -> dict[str, pd.DataFrame]:
    rows = conn.execute(
        "SELECT code, COUNT(*) n FROM daily_kline GROUP BY code HAVING n >= ? ORDER BY code",
        (MIN_HISTORY + max(HORIZONS) + 5,),
    ).fetchall()
    codes = [r["code"] for r in rows]
    if max_stocks:
        codes = codes[:max_stocks]

    histories: dict[str, pd.DataFrame] = {}
    for i, code in enumerate(codes, 1):
        if i % 500 == 0:
            logger.info("Loaded histories %d/%d", i, len(codes))
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM daily_kline "
            "WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
        if not rows:
            continue
        df = pd.DataFrame([dict(r) for r in rows])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
        if len(df) >= MIN_HISTORY + max(HORIZONS) + 5:
            df.attrs["date_to_idx"] = {d: idx for idx, d in enumerate(df["date"].tolist())}
            histories[code] = df
    return histories


def _build_regime_map(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT date, AVG(change_pct) AS avg_chg FROM daily_kline GROUP BY date ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    avg_chg = {r["date"]: float(r["avg_chg"] or 0) for r in rows}
    regime_map: dict[str, str] = {}
    for i, date in enumerate(dates):
        if i < 10:
            regime_map[date] = "unknown"
            continue
        vals = [avg_chg[d] for d in dates[i - 10:i]]
        mean = sum(vals) / len(vals)
        regime_map[date] = "rising" if mean > 0.3 else ("falling" if mean < -0.3 else "ranging")
    return regime_map


def _select_eval_dates(histories: dict[str, pd.DataFrame], args: argparse.Namespace) -> list[str]:
    all_dates = sorted(set(d for df in histories.values() for d in df["date"].tolist()))
    eval_dates = all_dates[MIN_HISTORY: len(all_dates) - 1]  # need next open for execution
    if args.start_date:
        eval_dates = [d for d in eval_dates if d >= args.start_date]
    if args.end_date:
        eval_dates = [d for d in eval_dates if d <= args.end_date]
    if args.max_days:
        eval_dates = eval_dates[:args.max_days]
    return eval_dates


def _select_stock_pool(
    histories: dict[str, pd.DataFrame],
    eval_dates: list[str],
    stocks_per_day: int,
    *,
    explicit_codes: list[str] | None = None,
) -> list[str]:
    if not eval_dates:
        return []
    required = set(eval_dates)
    eligible = []
    source_codes = explicit_codes if explicit_codes is not None else list(histories.keys())
    for code in source_codes:
        df = histories.get(code)
        if df is None:
            continue
        idx = df.attrs["date_to_idx"]
        if not required.issubset(idx):
            continue
        ok = True
        for date in eval_dates:
            i = idx[date]
            if i < MIN_HISTORY or i + 1 >= len(df):
                ok = False
                break
        if ok:
            eligible.append(code)
    if not eligible:
        return []
    if explicit_codes is not None:
        return eligible
    return random.sample(eligible, min(stocks_per_day, len(eligible)))


def _parse_explicit_codes(args: argparse.Namespace) -> list[str] | None:
    """Extract 6-digit stock codes from --codes (comma list) or --codes-file
    (CSV / one-per-line).

    Robust to varied CSV layouts: scans every comma-separated cell on each
    line and keeps the first one that is a 6-digit string. Skips lines where
    no cell qualifies (CSV headers, narrative text, etc.). The original
    "first column only" parser silently dropped formats like the Sina ETF PCF
    where column 0 is a date and the code is in column 1.
    """
    codes: list[str] = []
    if args.codes:
        codes.extend(c.strip() for c in args.codes.split(",") if c.strip())
    if args.codes_file:
        with open(args.codes_file, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                for cell in raw.split(","):
                    cell = cell.strip()
                    if cell.isdigit() and len(cell) == 6:
                        codes.append(cell)
                        break
    cleaned = []
    seen = set()
    for code in codes:
        if code.isdigit() and len(code) == 6 and code not in seen:
            cleaned.append(code)
            seen.add(code)
    return cleaned or None


def _load_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return cache
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                cache[(row["date"], row["code"])] = row["result"]
            except Exception:
                continue
    return cache


def _append_cache(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _call_vpa_for_code(payload: tuple[str, str, str]) -> tuple[str, dict[str, Any]]:
    """Run one production VPA call.

    This must stay top-level so ProcessPoolExecutor can pickle it. Process mode
    avoids sharing MiniRacer/V8 state across threads inside the data providers.
    """
    date, code, prev = payload
    result = compute_vpa_with_llm(
        code=code,
        name="",
        as_of=date,
        skip_save=True,
        previous_analysis_override=prev,
    )
    slim = {
        "ok": bool(result.get("ok")),
        "llm_verdict": result.get("llm_verdict", "中性"),
        "llm_confidence": result.get("llm_confidence", 0.5),
        "llm_phase": result.get("llm_phase", ""),
        "llm_confirmed": bool(result.get("llm_confirmed", False)),
        "llm_reason": result.get("llm_reason", ""),
        "llm_report": result.get("llm_report", ""),
        "error": result.get("error", ""),
    }
    return code, slim


def _run_vpa_for_day(
    date: str,
    stock_pool: list[str],
    prev_report_by_code: dict[str, str],
    cache: dict[tuple[str, str], dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    results: dict[str, dict[str, Any]] = {}
    cache_rows: list[dict[str, Any]] = []
    missing = [code for code in stock_pool if not args.refresh_cache and (date, code) not in cache]

    for code in stock_pool:
        cached = None if args.refresh_cache else cache.get((date, code))
        if cached is not None:
            results[code] = cached
            report = cached.get("llm_report", "")
            if report:
                prev_report_by_code[code] = report

    if not missing and not args.refresh_cache:
        return results, cache_rows

    call_codes = stock_pool if args.refresh_cache else missing

    executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    payloads = [(date, code, prev_report_by_code.get(code, "")) for code in call_codes]
    with executor_cls(max_workers=args.workers) as pool:
        futures = [pool.submit(_call_vpa_for_code, payload) for payload in payloads]
        for fut in as_completed(futures):
            try:
                code, result = fut.result()
            except Exception as e:
                logger.warning("LLM VPA failed on %s: %s", date, e)
                continue
            results[code] = result
            cache[(date, code)] = result
            cache_rows.append({"date": date, "code": code, "result": result})
            report = result.get("llm_report", "")
            if report:
                prev_report_by_code[code] = report

    return results, cache_rows


def _open_price(df: pd.DataFrame, date: str) -> float | None:
    idx = df.attrs["date_to_idx"].get(date)
    if idx is None:
        return None
    price = float(df.iloc[idx]["open"] or 0)
    return price if price > 0 else None


def _bar(df: pd.DataFrame, date: str) -> dict[str, float] | None:
    idx = df.attrs["date_to_idx"].get(date)
    if idx is None:
        return None
    row = df.iloc[idx]
    return {
        "open": float(row["open"] or 0),
        "high": float(row["high"] or 0),
        "low": float(row["low"] or 0),
        "close": float(row["close"] or 0),
    }


def _close_trade(
    pos: Position,
    exit_date: str,
    exit_price: float,
    exit_reason: str,
    exit_vpa: dict[str, Any] | None,
) -> dict[str, Any]:
    net = _estimate_net_close_result(pos.entry_price, exit_price, pos.shares)
    gross_return = (exit_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price else 0
    return {
        "code": pos.code,
        "entry_signal_date": pos.entry_signal_date,
        "entry_date": pos.entry_date,
        "entry_price": round(pos.entry_price, 3),
        "exit_signal_date": (exit_vpa or {}).get("date", ""),
        "exit_date": exit_date,
        "exit_price": round(exit_price, 3),
        "exit_reason": exit_reason,
        "holding_days": "",
        "shares": pos.shares,
        "gross_return_pct": round(gross_return, 3),
        "net_return_pct": net["return_pct"],
        "net_pnl": net["return_amount"],
        "estimated_costs": net["costs"],
        "entry_verdict": pos.entry_verdict,
        "entry_raw_verdict": pos.entry_raw_verdict,
        "entry_phase": pos.entry_phase,
        "entry_confidence": pos.entry_confidence,
        "entry_reason": pos.entry_reason,
        "exit_verdict": _norm_verdict((exit_vpa or {}).get("llm_verdict", "")),
        "exit_raw_verdict": (exit_vpa or {}).get("llm_verdict", ""),
        "exit_phase": (exit_vpa or {}).get("llm_phase", ""),
        "exit_confidence": (exit_vpa or {}).get("llm_confidence", ""),
        "exit_llm_reason": (exit_vpa or {}).get("llm_reason", ""),
        "entry_regime": pos.regime,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _summarize(trades: list[dict[str, Any]], initial_capital: float) -> None:
    if not trades:
        print("No closed trades.")
        return

    pnls = [float(t["net_pnl"]) for t in trades]
    rets = [float(t["net_return_pct"]) for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    total_pnl = sum(pnls)
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    print("\n" + "=" * 100)
    print("LLM VPA Daily Policy Backtest")
    print("=" * 100)
    print(f"Closed trades: {len(trades)}")
    print(f"Win rate: {len(wins) / len(trades) * 100:.1f}%")
    print(f"Avg net return/trade: {statistics.mean(rets):+.2f}%")
    print(f"Median net return/trade: {statistics.median(rets):+.2f}%")
    print(f"Avg win / avg loss: "
          f"{(statistics.mean(wins) if wins else 0):+.2f}% / "
          f"{(statistics.mean(losses) if losses else 0):+.2f}%")
    print(f"Total net PnL: {total_pnl:+,.2f}")
    print(f"Total return on initial capital: {total_pnl / initial_capital * 100:+.2f}%")
    print(f"Closed-trade equity max drawdown: {max_dd:.2f}%")

    print("\nExit reasons:")
    for reason, n in Counter(t["exit_reason"] for t in trades).most_common():
        print(f"  {reason}: {n}")

    print("\nEntry phases:")
    for phase, n in Counter(t["entry_phase"] or "(empty)" for t in trades).most_common(10):
        print(f"  {phase}: {n}")
    print("=" * 100 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks-per-day", type=int, default=20)
    parser.add_argument("--codes", type=str, default="",
                        help="Comma-separated fixed stock pool, e.g. 300750,300760")
    parser.add_argument("--codes-file", type=str, default="",
                        help="File with one code per line, or code as first CSV column")
    parser.add_argument("--max-days", type=int, default=10)
    parser.add_argument("--max-stocks", type=int, default=None,
                        help="Limit loaded histories before sampling; mostly for quick local tests")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--executor", choices=("process", "thread"), default="process",
                        help="process avoids MiniRacer/V8 thread-safety crashes in data providers")
    parser.add_argument("--initial-capital", type=float, default=100_000)
    parser.add_argument("--position-pct", type=float, default=0.10)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--buy-confidence", type=float, default=0.60)
    parser.add_argument("--sell-confidence", type=float, default=0.60)
    parser.add_argument("--require-bullish-phase", action="store_true",
                        help="Only buy when phase contains 吸筹/拉升/买入高峰")
    parser.add_argument("--size-by-phase", action="store_true",
                        help="Half-size position for bare 吸筹 entries; full size for 拉升/吸筹初期")
    parser.add_argument("--require-phase-stability", action="store_true",
                        help="Require prev-day bullish verdict before entering on bare 吸筹")
    parser.add_argument("--early-exit-on-verdict-decay", action="store_true",
                        help="Exit when LLM verdict has been non-bullish for ≥2 consecutive days")
    parser.add_argument("--stop-loss-pct", type=float, default=0.0,
                        help="Intraday stop-loss from entry price; <=0 disables")
    parser.add_argument("--take-profit-pct", type=float, default=0.0,
                        help="Intraday take-profit from entry price; <=0 disables")
    parser.add_argument("--max-holding-days", type=int, default=0,
                        help="Exit at next open after this many holding days; <=0 disables")
    parser.add_argument("--cache", type=str, default="data/vpa_llm_daily_policy_cache.jsonl")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--trades-out", type=str, default="data/vpa_llm_daily_policy_trades.csv")
    parser.add_argument("--daily-out", type=str, default="data/vpa_llm_daily_policy_observations.csv")
    parser.add_argument("--close-open-at-end", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    conn = _history_conn()
    regime_map = _build_regime_map(conn)
    histories = _load_all_histories(conn, max_stocks=args.max_stocks)
    eval_dates = _select_eval_dates(histories, args)
    explicit_codes = _parse_explicit_codes(args)
    stock_pool = _select_stock_pool(
        histories, eval_dates, args.stocks_per_day, explicit_codes=explicit_codes,
    )
    if not eval_dates or not stock_pool:
        raise SystemExit("No eligible dates/stocks. Check history DB or date filters.")

    logger.info("Eval dates: %d (%s -> %s)", len(eval_dates), eval_dates[0], eval_dates[-1])
    logger.info("Stock pool: %d codes = %d max LLM observations",
                len(stock_pool), len(stock_pool) * len(eval_dates))

    cache_path = Path(args.cache)
    cache = _load_cache(cache_path)
    logger.info("Loaded cache rows: %d from %s", len(cache), cache_path)

    prev_report_by_code: dict[str, str] = {}
    positions: dict[str, Position] = {}
    pending_actions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    # Previous trading day's full VPA dict per code, for --require-phase-stability.
    prev_vpa_by_code: dict[str, dict[str, Any]] = {}

    started = time.time()
    for di, date in enumerate(eval_dates, 1):
        logger.info("Day %d/%d %s", di, len(eval_dates), date)

        # 1) Execute yesterday's VPA decisions at today's open.
        for code, action in list(pending_actions.items()):
            df = histories[code]
            open_px = _open_price(df, date)
            if open_px is None:
                pending_actions.pop(code, None)
                continue
            if action["action"] == "buy" and code not in positions:
                if len(positions) >= args.max_positions:
                    pending_actions.pop(code, None)
                    continue
                vpa = action["vpa"]
                size_factor = _phase_size_factor(vpa.get("llm_phase", ""), args)
                notional = args.initial_capital * args.position_pct * size_factor
                shares = _calc_shares(open_px, notional)
                if shares <= 0:
                    pending_actions.pop(code, None)
                    continue
                stop = open_px * (1 - args.stop_loss_pct / 100) if args.stop_loss_pct > 0 else None
                target = open_px * (1 + args.take_profit_pct / 100) if args.take_profit_pct > 0 else None
                positions[code] = Position(
                    code=code,
                    entry_signal_date=action["signal_date"],
                    entry_date=date,
                    entry_price=open_px,
                    shares=shares,
                    stop_price=round(stop, 3) if stop else None,
                    target_price=round(target, 3) if target else None,
                    entry_verdict=_norm_verdict(vpa.get("llm_verdict", "")),
                    entry_raw_verdict=vpa.get("llm_verdict", ""),
                    entry_phase=vpa.get("llm_phase", ""),
                    entry_confidence=float(vpa.get("llm_confidence") or 0),
                    entry_reason=vpa.get("llm_reason", ""),
                    regime=regime_map.get(action["signal_date"], "unknown"),
                    size_factor=size_factor,
                )
            elif action["action"] == "sell" and code in positions:
                pos = positions.pop(code)
                trade = _close_trade(
                    pos,
                    exit_date=date,
                    exit_price=open_px,
                    exit_reason=action["reason"] + "@next_open",
                    exit_vpa={**action["vpa"], "date": action["signal_date"]},
                )
                trades.append(trade)
            pending_actions.pop(code, None)

        # 2) Check intraday stop/target for open positions using today's bar.
        for code, pos in list(positions.items()):
            bar = _bar(histories[code], date)
            if not bar:
                continue
            exit_price = None
            reason = ""
            if pos.stop_price and bar["low"] <= pos.stop_price:
                exit_price = pos.stop_price
                reason = "stop_loss"
            elif pos.target_price and bar["high"] >= pos.target_price:
                exit_price = pos.target_price
                reason = "take_profit"
            if exit_price is not None:
                positions.pop(code)
                trades.append(_close_trade(pos, date, exit_price, reason, None))

        # 3) Run after-close LLM VPA for every stock in the fixed pool.
        vpa_by_code, cache_rows = _run_vpa_for_day(
            date, stock_pool, prev_report_by_code, cache, args,
        )
        _append_cache(cache_path, cache_rows)

        # 4) Convert today's VPA state into tomorrow's orders.
        for code in stock_pool:
            vpa = vpa_by_code.get(code)
            if not vpa or not vpa.get("ok"):
                continue
            verdict = _norm_verdict(vpa.get("llm_verdict", ""))
            phase = vpa.get("llm_phase", "")
            action = "hold"
            reason = ""

            if code in positions:
                pos = positions[code]
                # Track verdict-decay streak for early-exit logic.
                if verdict == "bullish":
                    pos.non_bullish_streak = 0
                else:
                    pos.non_bullish_streak += 1
                holding_days = max(0, di - eval_dates.index(pos.entry_date))
                exit_r = _exit_reason(vpa, args, non_bullish_streak=pos.non_bullish_streak)
                if exit_r:
                    action = "sell"
                    reason = exit_r
                    pending_actions[code] = {
                        "action": "sell",
                        "signal_date": date,
                        "reason": reason,
                        "vpa": vpa,
                    }
                elif args.max_holding_days > 0 and holding_days >= args.max_holding_days:
                    action = "sell"
                    reason = f"max_holding:{holding_days}"
                    pending_actions[code] = {
                        "action": "sell",
                        "signal_date": date,
                        "reason": reason,
                        "vpa": vpa,
                    }
            else:
                if _is_entry_signal(vpa, args, prev_vpa=prev_vpa_by_code.get(code)):
                    action = "buy"
                    reason = "vpa_entry"
                    pending_actions[code] = {
                        "action": "buy",
                        "signal_date": date,
                        "reason": reason,
                        "vpa": vpa,
                    }

            observations.append({
                "date": date,
                "code": code,
                "regime": regime_map.get(date, "unknown"),
                "llm_verdict": verdict,
                "llm_raw_verdict": vpa.get("llm_verdict", ""),
                "llm_confidence": vpa.get("llm_confidence", ""),
                "llm_phase": phase,
                "llm_confirmed": vpa.get("llm_confirmed", False),
                "llm_reason": vpa.get("llm_reason", ""),
                "position_state": "open" if code in positions else "flat",
                "policy_action": action,
                "policy_reason": reason,
            })

        # Snapshot today's VPA into prev_vpa_by_code for tomorrow's stability check.
        for code in stock_pool:
            v = vpa_by_code.get(code)
            if v and v.get("ok"):
                prev_vpa_by_code[code] = v

        elapsed = time.time() - started
        logger.info("Progress: observations=%d trades=%d open=%d pending=%d elapsed=%.1fs",
                    len(observations), len(trades), len(positions), len(pending_actions), elapsed)

    if args.close_open_at_end and positions:
        last_date = eval_dates[-1]
        for code, pos in list(positions.items()):
            bar = _bar(histories[code], last_date)
            if not bar or bar["close"] <= 0:
                continue
            positions.pop(code)
            trades.append(_close_trade(pos, last_date, bar["close"], "end_of_test", None))

    # Fill holding_days after all trades are known.
    date_pos = {d: i for i, d in enumerate(sorted(set(d for df in histories.values() for d in df["date"].tolist())))}
    for t in trades:
        t["holding_days"] = max(0, date_pos.get(t["exit_date"], 0) - date_pos.get(t["entry_date"], 0))

    _write_csv(Path(args.trades_out), trades)
    _write_csv(Path(args.daily_out), observations)
    logger.info("Saved trades to %s", args.trades_out)
    logger.info("Saved observations to %s", args.daily_out)
    _summarize(trades, args.initial_capital)


if __name__ == "__main__":
    main()
