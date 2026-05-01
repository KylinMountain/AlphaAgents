"""Reprocess an existing VPA cache through the current state machine guard.

When `_apply_phase_state_guard` logic changes, the existing cache's final
fields (llm_phase / llm_phase_change / llm_confirmation_level / etc.) become
stale. This script re-applies the current guard to the saved llm_report
without calling the LLM, regenerating all guard-derived fields in place.

Inputs preserved from cache: llm_report (the source of truth from LLM).
Inputs recomputed from OHLCV: phase_context (trend_10d, range_10d, vol_ratio).
Inputs derived from prior day's cache entry: previous_analysis (its report).

Usage:
    python scripts/reprocess_vpa_guard.py SRC.jsonl --out DST.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.tools.vpa import (
    _apply_phase_state_guard,
    _compute_derived,
    _extract_verdict,
    _load_ohlcv,
    _phase_guard_context_from_df,
)


def reprocess(src: Path, dst: Path) -> dict:
    # Load all entries, group by code
    entries_by_code: dict[str, list] = defaultdict(list)
    with open(src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            entries_by_code[r["code"]].append(r)

    # Sort each code's entries by date (so previous_analysis comes from prior day)
    for code in entries_by_code:
        entries_by_code[code].sort(key=lambda r: r["date"])

    # Cache OHLCV + derived per-code (we slice to as_of for each entry)
    ohlcv_by_code: dict[str, "pd.DataFrame"] = {}
    for code in entries_by_code:
        try:
            df = _load_ohlcv(code, days=120, include_realtime=False, as_of=None)
            if df is not None and len(df) >= 25:
                df = _compute_derived(df, window=20)
                ohlcv_by_code[code] = df
        except Exception as e:
            print(f"  warn: ohlcv load failed for {code}: {e}")

    stats = {"total": 0, "ok": 0, "phase_changed": 0,
             "raw_eq_final_old_neq": 0, "errors": 0}
    out_rows = []

    for code, entries in entries_by_code.items():
        df_full = ohlcv_by_code.get(code)
        prev_report = ""
        for entry in entries:
            stats["total"] += 1
            res = entry.get("result", {})
            if not res.get("ok"):
                out_rows.append(entry)
                continue
            report = res.get("llm_report", "") or ""
            if not report:
                out_rows.append(entry)
                continue

            # Slice OHLCV up to entry date for phase_context
            phase_context = {}
            if df_full is not None:
                df_slice = df_full[df_full["date"] <= entry["date"]]
                if len(df_slice) >= 10:
                    phase_context = _phase_guard_context_from_df(df_slice)

            try:
                verdict_data = _extract_verdict(report)
                verdict_data = _apply_phase_state_guard(
                    verdict_data, prev_report, phase_context=phase_context
                )

                old_phase = res.get("llm_phase", "")
                new_phase = verdict_data.get("phase", "") or ""
                new_raw = verdict_data.get("raw_phase", "") or ""

                # Update cache entry with new guard outputs
                res["llm_phase"] = new_phase
                res["llm_raw_phase"] = new_raw
                res["llm_warning_phase"] = verdict_data.get("warning_phase", "")
                res["llm_phase_change"] = verdict_data.get("phase_change", {})
                res["llm_phase_state_changed"] = verdict_data.get("phase_state_changed", False)
                res["llm_phase_guard_reason"] = verdict_data.get("phase_guard_reason", "")
                res["llm_previous_phase"] = verdict_data.get("previous_phase", "")
                res["llm_confirmed"] = verdict_data.get("confirmed", False)
                res["llm_action_confirmed"] = verdict_data.get("action_confirmed", False)
                res["llm_partial_confirmed"] = verdict_data.get("partial_confirmed", False)
                res["llm_confirmed_any_signal"] = verdict_data.get("confirmed_any_signal", False)
                res["llm_confirmed_all_signals"] = verdict_data.get("confirmed_all_signals", False)
                res["llm_action_signal_count"] = verdict_data.get("action_signal_count", 0)
                res["llm_action_confirmed_signal_count"] = verdict_data.get("action_confirmed_signal_count", 0)
                res["llm_decisive_confirmed_signal_count"] = verdict_data.get("decisive_confirmed_signal_count", 0)
                res["llm_structural_phase_change_confirmed"] = verdict_data.get("structural_phase_change_confirmed", False)
                res["llm_phase_confidence"] = verdict_data.get("phase_confidence", 0.5)
                res["llm_confirmation_level"] = verdict_data.get("confirmation_level", 0)
                res["llm_confirmation_tier"] = verdict_data.get("confirmation_tier", "none")
                res["llm_reason"] = verdict_data.get("reason", "")
                res["llm_verdict"] = verdict_data.get("direction", "中性")
                res["llm_confidence"] = verdict_data.get("confidence", 0.5)

                if old_phase != new_phase:
                    stats["phase_changed"] += 1
                stats["ok"] += 1

                # next iteration's previous_analysis = this entry's report
                prev_report = report
            except Exception as e:
                stats["errors"] += 1
                print(f"  warn: guard failed for {code} {entry['date']}: {e}")

            out_rows.append(entry)

    # Write output
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("src", type=Path)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    dst = args.out or args.src.with_suffix(".reguarded.jsonl")
    stats = reprocess(args.src, dst)
    print(f"\nsrc: {args.src}")
    print(f"dst: {dst}")
    print(f"total entries: {stats['total']}")
    print(f"  reprocessed ok:  {stats['ok']}")
    print(f"  phase changed:   {stats['phase_changed']}")
    print(f"  errors:          {stats['errors']}")


if __name__ == "__main__":
    main()
