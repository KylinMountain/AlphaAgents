"""Two-phase backtest accelerator: batch-prefetch LLM cache for all
``(date, code)`` pairs in scan_market's top-N for each eval day, so the
v91 sequential simulator runs purely on cache hits afterward.

Sequential per-day LLM is the bottleneck because each day waits for all
20 candidates to return before the next day starts. But scan_market is
deterministic on price/volume — every day's candidate set can be
enumerated up front. With LLM calls flattened across days, we can run
~50 in parallel and finish ~146×20=2920 calls in ~60 min instead of
6-8h.

Usage:
    python scripts/prefetch_v91_cache.py \\
        --start 2025-09-23 --end 2026-05-08 \\
        --cache /tmp/anna_v11_v5_cache.jsonl \\
        --workers 50 --top-k 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

from alpha_agents.tools.vpa import compute_vpa_with_llm
from alpha_agents.tools.vpa.llm import PROMPT_VERSION, _select_provider
import sys as _sys
_sys.path.insert(0, str(REPO / "scripts"))
from anna_screener_v5 import scan_market  # noqa: E402

import sqlite3

DB = REPO / "data/market_history.db"


def warmup_v8() -> None:
    """Pre-init V8 in the parent before forking workers; matches v91's
    own warmup. py_mini_racer is used inside akshare/ddgs to decrypt
    Chinese market API responses, and parallel first-time init across
    threads crashes with ``Check failed: !IsConfigurablePoolInitialized()``.
    """
    try:
        import py_mini_racer
        ctx = py_mini_racer.MiniRacer()
        ctx.eval("1+1")
        print("  V8 warmup OK (py_mini_racer)", flush=True)
    except Exception as e:
        print(f"  V8 warmup skipped: {e}", flush=True)


def _llm_one_worker(args: tuple) -> tuple[str, str, dict]:
    """ProcessPool entry point — must be top-level to pickle. Each
    worker process gets its own V8 instance, sidestepping the
    cross-thread race that crashed the ThreadPool variant."""
    date, code = args
    try:
        from alpha_agents.tools.vpa import compute_vpa_with_llm
        result = compute_vpa_with_llm(code, days=120, as_of=date, skip_save=True)
        return date, code, result
    except Exception as e:
        return date, code, {"ok": False, "error": str(e)}


def get_eval_dates(start: str, end: str) -> list[str]:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def get_cache_identity() -> dict:
    """Match the identity that backtest_anna_v91_dynamic uses so its
    cache reuse picks up these rows."""
    import os
    try:
        _api_key, base_url, model, provider = _select_provider()
    except Exception:
        base_url = model = provider = None
    return {
        "prompt_version": PROMPT_VERSION,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "temperature": os.environ.get("VPA_LLM_TEMPERATURE", ""),
        "extra_body": os.environ.get("VPA_LLM_EXTRA_BODY", ""),
        "thinking": os.environ.get("VPA_LLM_THINKING", ""),
    }


def cache_key(code: str, date: str, identity: dict) -> tuple:
    return (
        code, date,
        identity.get("prompt_version"),
        identity.get("provider"),
        identity.get("model"),
        identity.get("base_url"),
        identity.get("temperature"),
        identity.get("extra_body"),
        identity.get("thinking"),
    )


def load_cache(path: Path) -> set:
    """Return set of (code, date, ...) keys already present so we don't
    re-LLM what's done. Matches both ``cache_identity``-stamped rows
    and legacy rows that carry identity fields inline."""
    keys: set = set()
    if not path.exists():
        return keys
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ident = r.get("cache_identity") or {}
            if not ident:
                # legacy row: pull identity off the result envelope
                res = r.get("result", {})
                ident = {
                    "prompt_version": res.get("llm_prompt_version"),
                    "provider": res.get("llm_provider"),
                    "model": res.get("llm_model"),
                    "base_url": "", "temperature": "",
                    "extra_body": "", "thinking": "",
                }
            keys.add(cache_key(r["code"], r["date"], ident))
    return keys


def _scan_one_day_worker(args: tuple) -> tuple[str, list[tuple[str, float]]]:
    """ProcessPool entry point — runs scan_market for ONE date and
    returns top-K (code, score) pairs. Top-level for picklability.
    Each process gets its own SQLite handle + V8 instance, fully
    independent across days."""
    date, boards, top_k, universe_file, min_amount = args
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from anna_screener_v5 import scan_market as _scan
    try:
        cands = _scan(boards, as_of=date, min_amount=min_amount,
                      universe_file=universe_file)
    except Exception as e:
        return date, []  # caller logs
    cands.sort(key=lambda x: -x.get("score", 0))
    return date, [(c["code"], c.get("score", 0.0)) for c in cands[:top_k]]


def enumerate_targets(eval_dates: list[str], boards: list[str],
                      top_k: int, universe_file: str | None,
                      min_amount: float, scan_workers: int) -> list[tuple[str, str, float]]:
    """For every eval date, run scan_market and emit the top-K
    candidates as (date, code, score) targets. scan_market for each
    day is independent (deterministic, reads same DB read-only), so
    we fan out across N processes — turns the serial ~60s/day grind
    into ~N-way speedup."""
    targets: list = []
    work_args = [(d, boards, top_k, universe_file, min_amount) for d in eval_dates]
    done = 0
    if scan_workers <= 1:
        # Sequential fallback
        for arg in work_args:
            d, picks = _scan_one_day_worker(arg)
            for code, score in picks:
                targets.append((d, code, score))
            done += 1
            if done % 20 == 0 or done == len(eval_dates):
                print(f"  scan_market {done}/{len(eval_dates)} ({d}): "
                      f"+{len(picks)} targets (total {len(targets)})", flush=True)
        return targets

    with ProcessPoolExecutor(max_workers=scan_workers) as ex:
        futs = {ex.submit(_scan_one_day_worker, arg): arg[0] for arg in work_args}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                _d, picks = fut.result()
            except Exception as e:
                print(f"  [scan_market WARN] {d}: {e}", flush=True)
                continue
            for code, score in picks:
                targets.append((d, code, score))
            done += 1
            if done % 20 == 0 or done == len(eval_dates):
                print(f"  scan_market {done}/{len(eval_dates)} ({d}): "
                      f"+{len(picks)} targets (total {len(targets)})", flush=True)
    return targets


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--cache", type=Path, required=True,
                   help="Append-mode cache file (compatible with backtest_anna_v91_dynamic).")
    p.add_argument("--top-k", type=int, default=20,
                   help="Per-day top-K from scan_market to prefetch.")
    p.add_argument("--workers", type=int, default=50,
                   help="Concurrent LLM workers. mimo handles 50+ comfortably.")
    p.add_argument("--min-amount", type=float, default=1.0)
    p.add_argument("--boards", default="000,001,002,003,300,301,600,601,603,605")
    p.add_argument("--universe-file", default=None)
    p.add_argument("--max-llm-calls", type=int, default=10000,
                   help="Hard cap to prevent runaway spend.")
    p.add_argument("--executor", choices=("process", "thread"), default="process",
                   help="process: each worker gets its own V8 (safe, default). "
                        "thread: shared V8 — only works after main-thread warmup "
                        "AND limited concurrency (≤20). Use thread for short windows.")
    p.add_argument("--scan-workers", type=int, default=8,
                   help="Concurrent scan_market processes (Phase 1). Each scans "
                        "all ~5000 stocks for one day; days are independent so "
                        "N-way speedup is near-linear. Default 8.")
    args = p.parse_args()
    print("Warming up V8 in parent process...")
    warmup_v8()

    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    eval_dates = get_eval_dates(args.start, args.end)
    print(f"Eval dates: {len(eval_dates)} ({eval_dates[0]} → {eval_dates[-1]})")

    identity = get_cache_identity()
    print(f"Cache identity: prompt={identity['prompt_version']} "
          f"provider={identity['provider']} model={identity['model']} "
          f"temp={identity['temperature']!r}")

    existing = load_cache(args.cache)
    print(f"Existing cache rows for this identity: {len(existing)}")

    print(f"\nPhase 1: enumerating targets via scan_market "
          f"on each day ({args.scan_workers} concurrent days)...")
    t0 = time.time()
    targets = enumerate_targets(eval_dates, boards, args.top_k,
                                  args.universe_file, args.min_amount,
                                  scan_workers=args.scan_workers)
    print(f"Total raw targets: {len(targets)} (took {time.time()-t0:.0f}s)")

    # Deduplicate (a stock often appears in multiple consecutive days)
    seen: set = set()
    unique: list = []
    for d, code, score in targets:
        key = (d, code)
        if key in seen:
            continue
        seen.add(key)
        unique.append((d, code, score))
    print(f"After dedup by (date, code): {len(unique)} unique calls")

    # Filter out already-cached
    todo: list = []
    for d, code, score in unique:
        if cache_key(code, d, identity) in existing:
            continue
        todo.append((d, code, score))
    print(f"After cache filter: {len(todo)} new LLM calls needed")

    if len(todo) > args.max_llm_calls:
        print(f"[STOP] {len(todo)} > --max-llm-calls={args.max_llm_calls}, "
              f"reduce --top-k or expand cap.")
        return

    if not todo:
        print("Nothing to prefetch — cache is complete for the requested window.")
        return

    print(f"\nPhase 2: launching {len(todo)} LLM calls with {args.workers} workers...")
    t0 = time.time()
    n_done = 0
    n_err = 0

    cache_writer = open(args.cache, "a")
    Executor = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    print(f"Using {Executor.__name__} with {args.workers} workers", flush=True)

    try:
        with Executor(max_workers=args.workers) as ex:
            futs = {ex.submit(_llm_one_worker, (d, c)): (d, c) for (d, c, _s) in todo}
            for fut in as_completed(futs):
                d, code = futs[fut]
                try:
                    _d, _c, result = fut.result()
                except Exception as e:
                    print(f"  [ERR] {code} {d}: {e}", flush=True)
                    n_err += 1
                    continue
                cache_writer.write(json.dumps({
                    "code": code, "date": d, "result": result,
                    "cache_identity": identity,
                }, ensure_ascii=False) + "\n")
                cache_writer.flush()
                n_done += 1
                if n_done % 50 == 0:
                    elapsed = time.time() - t0
                    eta = elapsed / n_done * (len(todo) - n_done)
                    print(f"  {n_done}/{len(todo)} done "
                          f"({elapsed:.0f}s elapsed, ETA {eta:.0f}s, errs={n_err})",
                          flush=True)
    finally:
        cache_writer.close()

    print(f"\nPrefetch complete: {n_done}/{len(todo)} success, {n_err} errors, "
          f"total {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
