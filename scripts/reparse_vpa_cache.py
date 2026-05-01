"""Re-extract VERDICT JSON from existing VPA cache reports.

The pre-fix _extract_verdict() in alpha_agents/tools/vpa.py used a regex that
required `{"direction":` with no whitespace, so any pretty-printed JSON the
LLM emitted (inside ```json fences``` or otherwise) fell through to
verdict_parse_failed — losing direction/phase/confidence/confirmed for ~36%
of cached observations.

This utility re-runs the (now fixed) _extract_verdict on the saved
``llm_report`` string for every cache row and writes a new jsonl file. No
LLM calls — purely re-parsing what was already in the cache.

Usage:
    uv run python scripts/reparse_vpa_cache.py SRC.jsonl [--out DST.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alpha_agents.tools.vpa import (
    _apply_phase_state_guard,
    _extract_verdict,
    _replace_verdict_in_report,
)


def _apply_verdict_to_result(res: dict, verdict: dict, report: str) -> dict:
    out = dict(res)
    out.update({
        "ok": True,
        "llm_verdict": verdict.get("direction", "中性"),
        "llm_confidence": verdict.get("confidence", 0.5),
        "llm_phase": verdict.get("phase", ""),
        "llm_raw_phase": verdict.get("raw_phase", verdict.get("phase", "")),
        "llm_warning_phase": verdict.get("warning_phase", ""),
        "llm_phase_change": verdict.get("phase_change", {}),
        "llm_phase_state_changed": verdict.get("phase_state_changed", False),
        "llm_phase_guard_reason": verdict.get("phase_guard_reason", ""),
        "llm_previous_phase": verdict.get("previous_phase", ""),
        "llm_confirmed": bool(verdict.get("confirmed", False)),
        "llm_action_confirmed": bool(verdict.get("action_confirmed", verdict.get("confirmed", False))),
        "llm_partial_confirmed": bool(verdict.get("partial_confirmed", False)),
        "llm_confirmed_any_signal": bool(verdict.get("confirmed_any_signal", False)),
        "llm_confirmed_all_signals": bool(verdict.get("confirmed_all_signals", False)),
        "llm_action_signal_count": int(verdict.get("action_signal_count", 0) or 0),
        "llm_action_confirmed_signal_count": int(
            verdict.get("action_confirmed_signal_count", 0) or 0
        ),
        "llm_decisive_confirmed_signal_count": int(
            verdict.get("decisive_confirmed_signal_count", 0) or 0
        ),
        "llm_structural_phase_change_confirmed": bool(
            verdict.get("structural_phase_change_confirmed", False)
        ),
        "llm_phase_confidence": verdict.get("phase_confidence", verdict.get("confidence", 0.5)),
        "llm_confirmation_level": int(verdict.get("confirmation_level", 0) or 0),
        "llm_confirmation_tier": verdict.get("confirmation_tier", "none"),
        "llm_reason": verdict.get("reason", ""),
        "llm_report": report,
        "llm_target_low": verdict.get("target_low"),
        "llm_target_high": verdict.get("target_high"),
        "llm_scenarios": verdict.get("scenarios", []),
        "error": "",
    })
    return out


def reparse(src: Path, dst: Path, *, stateful_guard: bool = False) -> dict:
    total = was_failed = recovered = unchanged = 0
    rows = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            res = r.get("result", {})
            total += 1
            if stateful_guard:
                rows.append(r)
                continue
            original_failed = res.get("llm_reason") == "verdict_parse_failed"
            if not original_failed:
                rows.append(r)
                unchanged += 1
                continue
            was_failed += 1
            report = res.get("llm_report", "")
            if not report:
                rows.append(r)
                continue
            new = _extract_verdict(report)
            r["result"] = _apply_verdict_to_result(res, new, report)
            if r["result"]["llm_phase"] or r["result"]["llm_verdict"] != "中性":
                recovered += 1
            rows.append(r)

    if stateful_guard:
        rows.sort(key=lambda r: (r.get("code", ""), r.get("date", "")))
        prev_report_by_code: dict[str, str] = {}
        for r in rows:
            res = r.get("result", {})
            report = res.get("llm_report", "")
            if not report:
                unchanged += 1
                continue
            code = r.get("code", "")
            verdict = _extract_verdict(report)
            guarded = _apply_phase_state_guard(verdict, prev_report_by_code.get(code, ""))
            guarded_report = _replace_verdict_in_report(report, guarded)
            r["result"] = _apply_verdict_to_result(res, guarded, guarded_report)
            prev_report_by_code[code] = guarded_report
            if guarded.get("phase") != verdict.get("phase"):
                recovered += 1
            else:
                unchanged += 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {"total": total, "was_failed": was_failed,
            "recovered": recovered, "unchanged": unchanged}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path, help="source cache jsonl")
    parser.add_argument("--out", type=Path, default=None,
                        help="destination (default: <src>.reparsed.jsonl)")
    parser.add_argument("--stateful-guard", action="store_true",
                        help="replay each code in date order and persist guarded phase state")
    args = parser.parse_args()
    dst = args.out or args.src.with_suffix(".reparsed.jsonl")
    if dst.suffix != ".jsonl":
        dst = dst.with_suffix(".jsonl")

    t0 = time.time()
    stats = reparse(args.src, dst, stateful_guard=args.stateful_guard)
    print(f"src: {args.src}")
    print(f"dst: {dst}")
    print(f"total={stats['total']}  was_failed={stats['was_failed']}  "
          f"recovered={stats['recovered']}  unchanged={stats['unchanged']}  "
          f"elapsed={time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
