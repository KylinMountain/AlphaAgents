"""Deterministic replay of v91 backtest with pluggable exit policies.

读取已有 LLM cache (/tmp/mimo_v91_cache.jsonl), 用同一批 LLM 输出 + 同一批
原始 entries (从 trades.csv) 重放策略层. 不调 LLM, 几秒出结果.

实现大佬 review 推荐的实验矩阵:
  A: legacy            (原 L>=3 SELL, 无风控 overlay)
  B: legacy + risk     (原 SELL + trailing/TP/abs-stop)
  C: phase_down        (strict phase_change.confirmed gate, 无风控)
  D: warning_persist   (warning 持续 N 天减仓, 无风控)
  E: full              (大佬 exit_decision 完整版 + 风控)

每个 policy 输出:
  - realized return / open PnL / total return
  - max position giveback (peak_pnl - exit_pnl)
  - alpha vs 真创业板指 (399006)
  - trade list with size_fraction
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DB_PATH = REPO / "data" / "market_history.db"
TX_COST = 0.0011  # one-way cost (bid-ask + commission)
BEAR_VERDICTS = {"看空", "偏空"}
BULL_VERDICTS = {"看多", "偏多"}

_PROMPT_VERSION_FALLBACK = os.environ.get("VPA_LLM_PROMPT_VERSION", "anna-vpa-v11")


def _current_prompt_version() -> str:
    try:
        from alpha_agents.tools.vpa.llm import PROMPT_VERSION as _pv
        return _pv
    except Exception:
        return _PROMPT_VERSION_FALLBACK


def _safe_select_provider() -> tuple:
    try:
        from alpha_agents.tools.vpa.llm import _select_provider as _sp
        return _sp()
    except Exception:
        return None, None, None, None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "y", "是"}:
            return True
        if v in {"false", "0", "no", "n", "否", ""}:
            return False
    return False


def _canonical_temperature_env() -> str:
    raw = os.environ.get("VPA_LLM_TEMPERATURE", "").strip()
    return raw if raw else "0"


def _current_cache_identity() -> dict:
    """Mirror backtest cache identity so replay compares apples-to-apples."""
    try:
        _api_key, base_url, model, provider = _safe_select_provider()
    except Exception:
        base_url = model = provider = None
    return {
        "prompt_version": _current_prompt_version(),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "temperature": _canonical_temperature_env(),
        "extra_body": os.environ.get("VPA_LLM_EXTRA_BODY", ""),
        "thinking": os.environ.get("VPA_LLM_THINKING", ""),
    }


def _cache_identity_matches(obj: dict, result: dict, identity: dict) -> bool:
    """Only replay rows generated under the same prompt/provider/runtime knobs."""
    row_identity = obj.get("cache_identity")
    if isinstance(row_identity, dict):
        return all(row_identity.get(k) == identity.get(k) for k in identity)

    row_key = obj.get("cache_key")
    if isinstance(row_key, list) and len(row_key) >= 9:
        return (
            row_key[2] == identity.get("prompt_version")
            and row_key[3] == identity.get("provider")
            and row_key[4] == identity.get("model")
            and row_key[5] == identity.get("base_url")
            and str(row_key[6] or "").strip() == str(identity.get("temperature") or "").strip()
            and (row_key[7] or "") == (identity.get("extra_body") or "")
            and (row_key[8] or "") == (identity.get("thinking") or "")
        )

    # Legacy fallback (no explicit identity/key): only allow under default
    # deterministic knobs and matching result metadata.
    return (
        result.get("llm_prompt_version") == identity.get("prompt_version")
        and result.get("llm_provider") == identity.get("provider")
        and result.get("llm_model") == identity.get("model")
        and str(identity.get("temperature", "")).strip() in {"", "0", "0.0"}
        and not identity.get("extra_body")
        and not identity.get("thinking")
    )


# ─── Position state ──────────────────────────────────────────

@dataclass
class Position:
    code: str
    entry_date: str
    entry_di: int
    entry_close: float
    remaining_size: float = 1.0
    partial_exits: list = field(default_factory=list)  # (size, exit_date, exit_price, gross_pct, reason)
    peak_pnl_pct: float = 0.0
    peak_close: float = 0.0
    warning_count: int = 0
    cur_pnl_pct: float = 0.0
    cur_close: float = 0.0
    tier1_sold_at: str | None = None
    tier2_sold_at: str | None = None
    trail_tighten_factor: float = 1.0  # multiplier on trailing stop pct (1.0 = no tighten)

    def update_marks(self, today_close: float):
        self.cur_close = today_close
        self.cur_pnl_pct = (today_close / self.entry_close - 1) * 100
        if self.cur_pnl_pct > self.peak_pnl_pct:
            self.peak_pnl_pct = self.cur_pnl_pct
        if today_close > self.peak_close:
            self.peak_close = today_close

    def position_value_per_unit(self, cur_close: float) -> float:
        """Total value (realized partials + remaining unrealized) / entry_close.
        1.0 = breakeven; 1.5 = +50% net."""
        realized = sum(sz * px for sz, _, px, _, _ in self.partial_exits)
        unrealized = self.remaining_size * cur_close
        return (realized + unrealized) / self.entry_close

    def total_pnl_pct(self, cur_close: float) -> float:
        """Net PnL% accounting for all partial exits (gross of cost)."""
        return (self.position_value_per_unit(cur_close) - 1) * 100


# ─── Exit policies ──────────────────────────────────────────

def policy_legacy(verdict: dict, pos: Position, args) -> tuple[str, float, str]:
    """A: 原 L>=3 SELL gate."""
    direction = verdict.get("llm_verdict", "")
    level = int(verdict.get("llm_confirmation_level", 0) or 0)
    phase = verdict.get("llm_phase", "")
    if direction in BEAR_VERDICTS and level >= 3:
        return "sell_all", pos.remaining_size, f"L3 {direction} {phase}"
    return "hold", 0.0, ""


_MARKDOWN_PHASE_PREFIXES = ("下跌",)
_DISTRIBUTION_LATE = ("派发尾声",)


def _strict_phase_exit_reason(verdict: dict) -> str:
    """Mirror backtest_anna_v91_dynamic.is_phase_based_exit exactly."""
    direction = str(verdict.get("llm_verdict", ""))
    pc = verdict.get("llm_phase_change") or {}
    pc_to = str(pc.get("to", "") or "")
    pc_confirmed = _as_bool(pc.get("confirmed"))

    if not pc_confirmed:
        return ""
    if pc_to.startswith(_MARKDOWN_PHASE_PREFIXES) and direction in BEAR_VERDICTS:
        return f"phase_change→{pc_to} confirmed (verdict={direction})"
    if pc_to.startswith(_DISTRIBUTION_LATE):
        return f"phase_change→{pc_to} confirmed (distribution_late)"
    return ""


def policy_phase_down(verdict: dict, pos: Position, args) -> tuple[str, float, str]:
    """C: strict phase-based exit (same semantics as backtest flag)."""
    reason = _strict_phase_exit_reason(verdict)
    if reason:
        return "sell_all", pos.remaining_size, reason
    return "hold", 0.0, ""


def policy_warning_persistence(verdict: dict, pos: Position, args) -> tuple[str, float, str]:
    """D: warning_phase 持续 N 次 + 浮盈大则减仓."""
    wp = verdict.get("llm_warning_phase", "") or ""
    is_warn = ("派发" in wp) or ("下跌" in wp)
    if is_warn:
        pos.warning_count += 1
    else:
        pos.warning_count = 0

    if pos.warning_count >= 2:
        if pos.cur_pnl_pct >= 80:
            return "sell_partial", min(0.5, pos.remaining_size), \
                f"persistent_warning ({pos.warning_count}x) +{pos.cur_pnl_pct:.0f}%"
        if pos.cur_pnl_pct >= 30:
            return "sell_partial", min(0.25, pos.remaining_size), \
                f"persistent_warning_profit ({pos.warning_count}x)"
        return "tighten", 0.75, f"persistent_warning ({pos.warning_count}x)"
    return "hold", 0.0, ""


def policy_full(verdict: dict, pos: Position, args) -> tuple[str, float, str]:
    """E: 大佬 exit_decision 完整版 (phase_down + warning persistence + tier).
    风控 overlay 由 apply_risk_overlay 单独处理, 永远优先于本函数."""
    # 1. structural markdown exit (phase_down 那套)
    res = policy_phase_down(verdict, pos, args)
    if res[0] != "hold":
        return res
    # 2. persistent warning
    res = policy_warning_persistence(verdict, pos, args)
    return res


POLICIES = {
    "legacy": policy_legacy,
    "phase_down": policy_phase_down,
    "warning_persistence": policy_warning_persistence,
    "full": policy_full,
}


# ─── Risk overlay (mirrors backtest_anna_v91_dynamic) ─────────

def apply_risk_overlay(pos: Position, today_close: float, args) -> tuple[str, float, str] | None:
    """Returns (action, size, reason) if any rule fires, else None."""
    cur_pnl = pos.cur_pnl_pct
    if pos.remaining_size <= 0:
        return None

    # 1. Abs stop loss
    if args.abs_stop_loss_pct > 0 and cur_pnl <= -args.abs_stop_loss_pct:
        return ("sell_all", pos.remaining_size,
                f"abs_stop pnl={cur_pnl:+.1f}% ≤ -{args.abs_stop_loss_pct}%")

    # 2. Trailing stop
    if args.trailing_stop_pct > 0 and pos.peak_pnl_pct >= 5.0:
        trail = args.trailing_stop_pct * pos.trail_tighten_factor
        if args.trailing_tighten_on_tier:
            if pos.tier2_sold_at:
                trail *= 0.5
            elif pos.tier1_sold_at:
                trail *= 0.75
        drawdown = pos.peak_pnl_pct - cur_pnl
        if drawdown >= trail:
            return ("sell_all", pos.remaining_size,
                    f"trailing drawdown={drawdown:.1f}pp peak={pos.peak_pnl_pct:.1f}% trail={trail:.1f}")

    # 3. TP tier 1
    if (args.tp_tier1_pct > 0 and cur_pnl >= args.tp_tier1_pct
        and pos.tier1_sold_at is None):
        size = min(args.tp_tier1_size, pos.remaining_size)
        return ("partial_sell", size,
                f"tp_tier1 pnl={cur_pnl:.1f}% size={size:.0%}")

    # 4. TP tier 2
    if (args.tp_tier2_pct > 0 and cur_pnl >= args.tp_tier2_pct
        and pos.tier2_sold_at is None):
        size = min(args.tp_tier2_size, pos.remaining_size)
        return ("partial_sell", size,
                f"tp_tier2 pnl={cur_pnl:.1f}% size={size:.0%}")

    return None


# ─── Replay engine ──────────────────────────────────────────

def load_cache(path: Path, identity: dict) -> tuple[dict, dict]:
    cache = {}
    kept = 0
    skipped_identity = 0
    malformed = 0
    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
                code = obj["code"]
                date = obj["date"]
                result = obj.get("result", {}) or {}
                if not _cache_identity_matches(obj, result, identity):
                    skipped_identity += 1
                    continue
                cache[(code, date)] = result
                kept += 1
            except Exception:
                malformed += 1
                continue
    stats = {
        "kept": kept,
        "skipped_identity": skipped_identity,
        "malformed": malformed,
    }
    return cache, stats


def load_prices(start: str, end: str, codes: set[str]) -> dict:
    """code → date → {open, close}."""
    conn = sqlite3.connect(str(DB_PATH))
    out: dict = defaultdict(dict)
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, date, open, close FROM daily_kline "
        f"WHERE code IN ({placeholders}) AND date BETWEEN ? AND ?",
        list(codes) + [start, end],
    ).fetchall()
    for code, date, op, cl in rows:
        out[code][date] = {"open": op, "close": cl}
    conn.close()
    return out


def get_eval_dates(start: str, end: str) -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def execute_exit(pos: Position, code: str, date: str, di: int,
                 eval_dates: list[str], prices: dict,
                 action: str, size: float, reason: str,
                 trades: list, positions: dict):
    """Find next-day open, record trade row, update position state."""
    exec_date, exec_price, exec_di = None, None, None
    for k in range(1, min(15, len(eval_dates) - di)):
        cand = eval_dates[di + k]
        p = prices.get(code, {}).get(cand, {}).get("open")
        if p:
            exec_date, exec_price, exec_di = cand, p, di + k
            break
    if exec_price is None:
        exec_price = pos.cur_close
        exec_date, exec_di = date, di

    gross = (exec_price - pos.entry_close) / pos.entry_close * 100
    net = gross - TX_COST * 100 * 2  # round-trip

    trades.append({
        "code": code, "entry_date": pos.entry_date, "exit_date": exec_date,
        "hold_days": exec_di - pos.entry_di,
        "size_fraction": size,
        "entry_close": pos.entry_close, "exit_close": exec_price,
        "gross_pct": gross, "net_pct": net,
        "exit_signal": reason[:80],
        "peak_pnl_at_exit": pos.peak_pnl_pct,
        "giveback_pp": pos.peak_pnl_pct - pos.cur_pnl_pct,
    })

    if action == "sell_all":
        pos.remaining_size = 0
    else:  # partial / tier
        pos.partial_exits.append((size, exec_date, exec_price, gross, reason))
        pos.remaining_size = max(0, pos.remaining_size - size)
        if "tp_tier1" in reason:
            pos.tier1_sold_at = date
        elif "tp_tier2" in reason:
            pos.tier2_sold_at = date


def replay(entries: list, cache: dict, prices: dict, eval_dates: list[str],
           policy_name: str, args) -> tuple[list, list]:
    """Run replay. Returns (trades, daily_marks)."""
    positions: dict[str, Position] = {}
    trades: list = []
    daily_marks: list = []
    policy_fn = POLICIES[policy_name]

    entries_by_date: dict[str, list] = defaultdict(list)
    for code, ent_date, price in entries:
        entries_by_date[ent_date].append((code, price))

    for di, date in enumerate(eval_dates):
        # 1. New entries on this date
        if date in entries_by_date:
            for code, price in entries_by_date[date]:
                # Find di of entry_date
                ent_di = di
                positions[code] = Position(
                    code=code, entry_date=date, entry_di=ent_di,
                    entry_close=price, peak_close=price,
                )

        # 2. Hold-monitor: apply policy + risk overlay
        for code in list(positions):
            pos = positions[code]
            today_close = prices.get(code, {}).get(date, {}).get("close")
            if today_close is None:
                continue
            pos.update_marks(today_close)

            # 2a. Risk overlay (always priority)
            risk = apply_risk_overlay(pos, today_close, args)
            if risk:
                action, size, reason = risk
                execute_exit(pos, code, date, di, eval_dates, prices,
                             action, size, "RISK:" + reason, trades, positions)
                if pos.remaining_size <= 0:
                    del positions[code]
                continue

            # 2b. LLM-driven exit policy
            verdict = cache.get((code, date))
            if not verdict or not verdict.get("ok", True):
                continue
            action, size, reason = policy_fn(verdict, pos, args)
            if action == "hold":
                continue
            if action == "tighten":
                # Tighten subsequent trailing stops
                pos.trail_tighten_factor *= float(size or 0.75)
                continue
            execute_exit(pos, code, date, di, eval_dates, prices,
                         action, size, "POLICY:" + reason, trades, positions)
            if pos.remaining_size <= 0:
                del positions[code]

        # 3. Daily mark
        pv = 0.0
        for code, pos in positions.items():
            cur = prices.get(code, {}).get(date, {}).get("close", pos.entry_close)
            pv += pos.position_value_per_unit(cur)
        daily_marks.append({"date": date, "n_held": len(positions), "pv_sum": pv})

    # 4. Forced final exit for remaining open positions (mark-to-final)
    final_date = eval_dates[-1]
    for code, pos in list(positions.items()):
        if pos.remaining_size <= 0:
            continue
        final_close = prices.get(code, {}).get(final_date, {}).get("close", pos.entry_close)
        gross = (final_close - pos.entry_close) / pos.entry_close * 100
        # No tx cost on forced final mark (it's mark-to-market, not actual sell)
        trades.append({
            "code": code, "entry_date": pos.entry_date, "exit_date": final_date,
            "hold_days": len(eval_dates) - pos.entry_di,
            "size_fraction": pos.remaining_size,
            "entry_close": pos.entry_close, "exit_close": final_close,
            "gross_pct": gross, "net_pct": gross,
            "exit_signal": "END_OF_PERIOD (mark-to-market)",
            "peak_pnl_at_exit": pos.peak_pnl_pct,
            "giveback_pp": pos.peak_pnl_pct - pos.cur_pnl_pct,
        })

    return trades, daily_marks


# ─── Metrics ──────────────────────────────────────────

def cyb_return(start: str, end: str) -> float | None:
    conn = sqlite3.connect(str(DB_PATH))
    s = conn.execute("SELECT close FROM daily_kline WHERE code='399006' AND date=?", (start,)).fetchone()
    e = conn.execute("SELECT close FROM daily_kline WHERE code='399006' AND date=?", (end,)).fetchone()
    conn.close()
    if not s or not e:
        return None
    return (float(e[0]) / float(s[0]) - 1) * 100


def summarize(trades: list, label: str, start_date: str, end_date: str,
              n_positions_concurrent: int = 3):
    if not trades:
        print(f"[{label}] no trades")
        return

    # Portfolio return = (sum of net × size_fraction) / n_concurrent_slots.
    # 3 positions equal-weighted: 003 (+4.27 + +51.9 + +91.7) / 3 = +49.3%
    # NOT sequential compound (which assumes capital reuse — wrong for concurrent).
    sum_weighted = sum(t["net_pct"] * t.get("size_fraction", 1.0) for t in trades)
    portfolio_return = sum_weighted / n_positions_concurrent

    realized_only = [t for t in trades if "END_OF_PERIOD" not in (t.get("exit_signal") or "")]
    open_only = [t for t in trades if "END_OF_PERIOD" in (t.get("exit_signal") or "")]
    realized_sum = sum(t["net_pct"] * t.get("size_fraction", 1.0) for t in realized_only)
    open_sum = sum(t["net_pct"] * t.get("size_fraction", 1.0) for t in open_only)
    realized_portfolio = realized_sum / n_positions_concurrent
    open_portfolio = open_sum / n_positions_concurrent

    # Position-level giveback (per code, max peak vs final exit)
    by_code = defaultdict(list)
    for t in trades:
        by_code[t["code"]].append(t)
    givebacks = []
    for code, ts in by_code.items():
        peak = max(t.get("peak_pnl_at_exit", 0) for t in ts)
        # final exit pnl (size-weighted exit pnl)
        final_exit_pnl = sum(t["gross_pct"] * (t.get("size_fraction", 1.0) or 1.0) for t in ts)
        gb = peak - final_exit_pnl
        givebacks.append((code, peak, final_exit_pnl, gb))

    cyb = cyb_return(start_date, end_date)
    alpha = portfolio_return - cyb if cyb is not None else None

    print(f"\n═══ [{label}] ═══")
    print(f"  portfolio return (avg /{n_positions_concurrent} slots): {portfolio_return:+.2f}%")
    print(f"    ├ realized exits:     {realized_portfolio:+.2f}% (n={len(realized_only)}; raw sum={realized_sum:+.2f})")
    print(f"    └ open mark-to-final: {open_portfolio:+.2f}% (n={len(open_only)}; raw sum={open_sum:+.2f})")
    if cyb is not None:
        print(f"  CYB (399006) {start_date}→{end_date}: {cyb:+.2f}%")
        print(f"  alpha: {alpha:+.2f}pp")
    print(f"  trades: {len(trades)} (incl. {len(open_only)} forced finals)")
    print(f"  giveback (per code, peak vs final exit):")
    for code, peak, final, gb in sorted(givebacks, key=lambda x: -x[3]):
        print(f"    {code}: peak +{peak:.1f}% → final +{final:.1f}% → giveback {gb:+.1f}pp")


# ─── Main ──────────────────────────────────────────

# Original entries from mimo backtest
ORIGINAL_ENTRIES = [
    ("603358", "2025-09-24", 42.95),
    ("002066", "2025-09-24", 15.36),
    ("000547", "2025-11-26", 14.39),
]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", default="/tmp/mimo_v91_cache.jsonl")
    p.add_argument("--start", default="2025-09-23")
    p.add_argument("--end", default="2026-05-08")
    p.add_argument("--policies", default="legacy,phase_down,warning_persistence,full",
                   help="Comma-separated subset of: legacy, phase_down, warning_persistence, full")
    # Risk overlay knobs (apply to all policies that aren't 'legacy')
    p.add_argument("--abs-stop-loss-pct", type=float, default=0.0)
    p.add_argument("--trailing-stop-pct", type=float, default=0.0)
    p.add_argument("--tp-tier1-pct", type=float, default=0.0)
    p.add_argument("--tp-tier1-size", type=float, default=0.3)
    p.add_argument("--tp-tier2-pct", type=float, default=0.0)
    p.add_argument("--tp-tier2-size", type=float, default=0.3)
    p.add_argument("--trailing-tighten-on-tier", action="store_true")
    p.add_argument("--out-prefix", default="/tmp/replay")
    args = p.parse_args()

    print(f"Loading cache from {args.cache}...")
    identity = _current_cache_identity()
    cache, cache_stats = load_cache(Path(args.cache), identity)
    print(f"  cache_identity: {identity}")
    print(
        "  cached rows: kept={kept}, skipped_identity={skipped_identity}, malformed={malformed}".format(
            **cache_stats
        )
    )
    print(f"  replay keys loaded: {len(cache)}")

    eval_dates = get_eval_dates(args.start, args.end)
    print(f"  {len(eval_dates)} eval days {eval_dates[0]} → {eval_dates[-1]}")

    codes_needed = {e[0] for e in ORIGINAL_ENTRIES}
    prices = load_prices(args.start, args.end, codes_needed)

    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    for policy in policies:
        if policy not in POLICIES:
            print(f"  unknown policy: {policy}, skip")
            continue
        # All policies (including 'legacy') use whatever risk-overlay args
        # the user passed. Risk overlay is independent of LLM policy by design
        # (it's the senior reviewer's "Layer 1: hard risk overlay always
        # priority"). Pass --abs-stop-loss-pct=0 etc. to disable.
        policy_args = args

        trades, marks = replay(ORIGINAL_ENTRIES, cache, prices, eval_dates,
                               policy, policy_args)
        out_csv = f"{args.out_prefix}_{policy}.csv"
        with open(out_csv, "w", newline="") as f:
            if trades:
                w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(trades)
        summarize(trades, policy, args.start, args.end)
        print(f"  [saved] {out_csv}")


if __name__ == "__main__":
    main()
