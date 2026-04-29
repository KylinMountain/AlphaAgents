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

from alpha_agents.tools.vpa import _extract_verdict


def reparse(src: Path, dst: Path) -> dict:
    total = was_failed = recovered = unchanged = 0
    rows = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            res = r.get("result", {})
            total += 1
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
            r["result"] = {
                "ok": True,
                "llm_verdict": new.get("direction", "中性"),
                "llm_confidence": new.get("confidence", 0.5),
                "llm_phase": new.get("phase", ""),
                "llm_confirmed": bool(new.get("confirmed", False)),
                "llm_reason": new.get("reason", ""),
                "llm_report": report,
                "llm_target_low": new.get("target_low"),
                "llm_target_high": new.get("target_high"),
                "llm_scenarios": new.get("scenarios", []),
                "error": "",
            }
            if r["result"]["llm_phase"] or r["result"]["llm_verdict"] != "中性":
                recovered += 1
            rows.append(r)

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
    args = parser.parse_args()
    dst = args.out or args.src.with_suffix(".reparsed.jsonl")
    if dst.suffix != ".jsonl":
        dst = dst.with_suffix(".jsonl")

    t0 = time.time()
    stats = reparse(args.src, dst)
    print(f"src: {args.src}")
    print(f"dst: {dst}")
    print(f"total={stats['total']}  was_failed={stats['was_failed']}  "
          f"recovered={stats['recovered']}  unchanged={stats['unchanged']}  "
          f"elapsed={time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
