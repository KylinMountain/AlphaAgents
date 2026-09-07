#!/usr/bin/env python3
"""Backfill deterministic VPA fields into an existing JSONL cache.

This script does not call LLM. It preserves the cached LLM result and adds
the deterministic envelope fields consumed by backtest/replay/chart tools:

    price_regime, trade_setup, supply_warnings, ma_structure

Usage:
    .venv/bin/python scripts/backfill_vpa_deterministic_fields.py \
      /tmp/cyb_v12_cache.jsonl \
      --out /tmp/cyb_v12_cache.backfilled.jsonl

    .venv/bin/python scripts/backfill_vpa_deterministic_fields.py \
      /tmp/cyb_v12_cache.jsonl --in-place
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from alpha_agents.tools.vpa.data import _compute_derived, _load_ohlcv  # noqa: E402
from alpha_agents.tools.vpa.regime import (  # noqa: E402
    classify_regime,
    compute_ma_structure,
    derive_trade_setup,
    detect_supply_warnings,
)


REQUIRED_FIELDS = (
    "price_regime",
    "price_regime_detail",
    "trade_setup",
    "trade_setup_score",
    "trade_setup_reason",
    "trade_setup_features",
    "supply_warnings",
    "supply_warnings_detail",
    "ma_structure",
)


def _clean_json(value: Any) -> Any:
    """Convert pandas/numpy scalars and NaN/Inf to strict JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if hasattr(value, "item"):
        try:
            return _clean_json(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json(v) for v in value]
    return str(value)


def _empty_fields(reason: str) -> dict[str, Any]:
    return {
        "price_regime": "insufficient_data",
        "price_regime_detail": {"regime": "insufficient_data", "reason": reason},
        "trade_setup": "none",
        "trade_setup_score": 0.0,
        "trade_setup_reason": reason,
        "trade_setup_features": {},
        "supply_warnings": [],
        "supply_warnings_detail": [],
        "ma_structure": {
            "as_of": None,
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "ma60": None,
            "ma_stack": "insufficient",
            "ma20_slope_5d": None,
            "close_vs_ma20_pct": None,
            "close_vs_ma60_pct": None,
        },
    }


def _needs_backfill(result: dict[str, Any], force: bool) -> bool:
    if force:
        return True
    if not isinstance(result, dict):
        return False
    for key in REQUIRED_FIELDS:
        if key not in result:
            return True
    if not result.get("price_regime"):
        return True
    if result.get("trade_setup") is None:
        return True
    if result.get("supply_warnings") is None:
        return True
    if not isinstance(result.get("trade_setup_features"), dict):
        return True
    if not isinstance(result.get("ma_structure"), dict) or not result.get("ma_structure"):
        return True
    return False


def _compute_fields(code: str, date: str, *, days: int, window: int) -> tuple[dict[str, Any], str]:
    if not code or not date:
        return _empty_fields("missing code/date"), "missing_key"

    df = _load_ohlcv(code, days=days, as_of=date)
    if df is None or len(df) < window + 5:
        return _empty_fields(f"data_unavailable through {date}"), "no_data"

    df = _compute_derived(df, window=window)
    regime = classify_regime(df)
    setup = derive_trade_setup(df)
    warnings = detect_supply_warnings(df)
    ma = compute_ma_structure(df)

    fields = {
        "price_regime": regime.get("regime", "unknown"),
        "price_regime_detail": regime,
        "trade_setup": setup.get("setup", "none"),
        "trade_setup_score": setup.get("score", 0.0),
        "trade_setup_reason": setup.get("reason", ""),
        "trade_setup_features": setup.get("features", {}),
        "supply_warnings": [w.get("pattern") for w in warnings if isinstance(w, dict)],
        "supply_warnings_detail": warnings,
        "ma_structure": ma,
    }
    return fields, "computed"


def _backup_path(path: Path) -> Path:
    candidate = path.with_suffix(path.suffix + ".bak")
    if not candidate.exists():
        return candidate
    for i in range(1, 1000):
        candidate = path.with_suffix(path.suffix + f".bak{i}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot allocate backup path for {path}")


def _default_out_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.backfilled{path.suffix}")


def _result_target(obj: dict[str, Any]) -> dict[str, Any] | None:
    result = obj.get("result")
    if isinstance(result, dict):
        return result
    # Support slim JSONL rows where the result fields are top-level.
    if "llm_verdict" in obj or "ok" in obj:
        return obj
    return None


def backfill_cache(
    cache_path: Path,
    out_path: Path,
    *,
    days: int,
    window: int,
    force: bool,
    progress_every: int,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "rows": 0,
        "updated": 0,
        "unchanged": 0,
        "parse_errors": 0,
        "no_result": 0,
        "no_data": 0,
        "compute_errors": 0,
        "missing_key": 0,
    }
    memo: dict[tuple[str, str], dict[str, Any]] = {}
    examples: list[str] = []

    with cache_path.open("r", encoding="utf-8") as src, out_path.open("w", encoding="utf-8") as dst:
        for line_no, line in enumerate(src, start=1):
            if not line.strip():
                continue
            stats["rows"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                stats["parse_errors"] += 1
                if len(examples) < 5:
                    examples.append(f"line {line_no}: JSONDecodeError {exc}")
                dst.write(line)
                continue

            result = _result_target(obj)
            if result is None:
                stats["no_result"] += 1
                dst.write(json.dumps(_clean_json(obj), ensure_ascii=False) + "\n")
                continue

            if _needs_backfill(result, force):
                code = str(obj.get("code") or result.get("code") or "").strip()
                date = str(obj.get("date") or result.get("date") or result.get("analysis_date") or "").strip()[:10]
                key = (code, date)
                try:
                    if key not in memo:
                        fields, status = _compute_fields(code, date, days=days, window=window)
                        memo[key] = fields
                        if status != "computed":
                            stats[status] = stats.get(status, 0) + 1
                    result.update(memo[key])
                    stats["updated"] += 1
                except Exception as exc:
                    stats["compute_errors"] += 1
                    if len(examples) < 5:
                        examples.append(f"line {line_no} {code} {date}: {type(exc).__name__}: {exc}")
                    result.update(_empty_fields(f"deterministic_error: {type(exc).__name__}: {exc}"))
                    stats["updated"] += 1
            else:
                stats["unchanged"] += 1

            dst.write(json.dumps(_clean_json(obj), ensure_ascii=False, separators=(",", ":")) + "\n")

            if progress_every > 0 and stats["rows"] % progress_every == 0:
                print(
                    f"  {stats['rows']} rows | updated {stats['updated']} | "
                    f"unchanged {stats['unchanged']} | no_data {stats['no_data']}",
                    flush=True,
                )

    stats["examples"] = examples
    stats["unique_computed_keys"] = len(memo)
    return stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cache", type=Path, help="Input JSONL cache path")
    p.add_argument("--out", type=Path, default=None, help="Output JSONL path")
    p.add_argument("--in-place", action="store_true", help="Rewrite input file in place and create .bak")
    p.add_argument("--force", action="store_true", help="Recompute fields even if already present")
    p.add_argument("--days", type=int, default=120, help="OHLCV lookback days for deterministic fields")
    p.add_argument("--window", type=int, default=20, help="Rolling window for VPA derived metrics")
    p.add_argument("--progress-every", type=int, default=200, help="Print progress every N rows; 0 disables")
    args = p.parse_args()

    if args.in_place and args.out is not None:
        p.error("--in-place and --out cannot be used together")
    if not args.cache.exists():
        p.error(f"cache file does not exist: {args.cache}")
    return args


def main() -> None:
    args = parse_args()
    cache_path = args.cache.resolve()

    if args.in_place:
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
            dir=str(cache_path.parent),
        )
        os.close(tmp_fd)
        out_path = Path(tmp_name)
    else:
        out_path = (args.out or _default_out_path(cache_path)).resolve()
        if out_path == cache_path:
            raise SystemExit("refusing to write --out to the same path; use --in-place instead")
        out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Backfilling deterministic VPA fields: {cache_path}")
    print(f"  output: {out_path}")
    print(f"  force={args.force} days={args.days} window={args.window}")

    stats = backfill_cache(
        cache_path,
        out_path,
        days=args.days,
        window=args.window,
        force=args.force,
        progress_every=args.progress_every,
    )

    if args.in_place:
        backup = _backup_path(cache_path)
        shutil.copy2(cache_path, backup)
        os.replace(out_path, cache_path)
        print(f"  backup: {backup}")
        print(f"  replaced: {cache_path}")

    print("Done.")
    for key in (
        "rows",
        "updated",
        "unchanged",
        "unique_computed_keys",
        "no_data",
        "missing_key",
        "no_result",
        "parse_errors",
        "compute_errors",
    ):
        print(f"  {key}: {stats.get(key, 0)}")
    if stats.get("examples"):
        print("  examples:")
        for ex in stats["examples"]:
            print(f"    - {ex}")


if __name__ == "__main__":
    main()
