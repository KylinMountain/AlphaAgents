"""Generate the three v7 deliverable reports per spec §9.1.

Reads a v7 cache and produces:
  - data/v7_scanner_coverage_report.csv
  - data/v7_defense_activation_log.csv
  - data/v7_vs_v5_divergence.csv (when --v5-cache is provided)

Usage:
    python scripts/v7_generate_reports.py data/vpa_v7_300136_smoke_cache.jsonl \
        [--v5-cache data/vpa_v51_300136_dsv4f_cache.jsonl] \
        --out-dir data/
"""

import argparse
import csv
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("v7_cache", type=Path)
    p.add_argument("--v5-cache", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=Path("data"))
    args = p.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(l) for l in args.v7_cache.read_text().splitlines() if l.strip()]

    # Report 1: scanner coverage
    coverage_path = args.out_dir / "v7_scanner_coverage_report.csv"
    with open(coverage_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "candidates_count", "max_vr_pct60",
                    "selected_candidate_id", "selected_pct_in_pool"])
        for r in rows:
            res = r["result"]
            cands = res.get("candidates_snapshot", []) or []
            selected = res.get("selected_climax", {}).get("candidate_id")
            max_vr = max((c.get("vol_ratio_pct", 0) for c in cands), default=0)
            sel_rank = ""
            if selected:
                ranked = sorted(cands, key=lambda c: -c.get("vol_ratio_pct", 0))
                ids = [c["id"] for c in ranked]
                if selected in ids:
                    sel_rank = f"{ids.index(selected)+1}/{len(ids)}"
            w.writerow([r["date"], r["code"], len(cands), f"{max_vr:.3f}",
                        selected or "", sel_rank])

    # Report 2: defense activations
    defense_path = args.out_dir / "v7_defense_activation_log.csv"
    with open(defense_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "prior_phase", "attempted_phase",
                    "candidate_id", "post_bars_observed", "failure_reason"])
        for r in rows:
            res = r["result"]
            if res.get("cross_family_blocked"):
                cand_id = res.get("selected_climax", {}).get("candidate_id", "")
                post_obs = ""
                cands = res.get("candidates_snapshot", []) or []
                if cand_id:
                    for c in cands:
                        if c.get("id") == cand_id:
                            post_obs = c.get("post_bars_observed", "")
                            break
                w.writerow([
                    r["date"], r["code"],
                    res.get("prior_phase", ""),
                    res.get("llm_phase", ""),
                    cand_id,
                    post_obs,
                    res.get("phase_guard_reason", ""),
                ])

    # Report 3: divergence (if v5 cache provided)
    div_path = args.out_dir / "v7_vs_v5_divergence.csv"
    div_count = 0
    if args.v5_cache and args.v5_cache.exists():
        v5_rows = {(r["date"], r["code"]): r["result"]
                   for r in (json.loads(l) for l in args.v5_cache.read_text().splitlines() if l.strip())}
        with open(div_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "code",
                        "v5_phase", "v7_phase",
                        "v5_verdict", "v7_verdict",
                        "v5_lvl", "v7_lvl",
                        "v7_candidate_id", "category"])
            for r in rows:
                key = (r["date"], r["code"])
                v5 = v5_rows.get(key, {})
                v7 = r["result"]
                if (v5.get("llm_phase") != v7.get("llm_phase")
                        or v5.get("llm_verdict") != v7.get("llm_verdict")):
                    if v7.get("cross_family_blocked"):
                        cat = "v7 blocked cross-family revision"
                    elif v7.get("selected_climax", {}).get("candidate_id"):
                        cat = "different climax pick"
                    else:
                        cat = "no obvious driver"
                    w.writerow([
                        r["date"], r["code"],
                        v5.get("llm_phase", ""), v7.get("llm_phase", ""),
                        v5.get("llm_verdict", ""), v7.get("llm_verdict", ""),
                        v5.get("llm_confirmation_level", ""),
                        v7.get("llm_confirmation_level", ""),
                        v7.get("selected_climax", {}).get("candidate_id", ""),
                        cat,
                    ])
                    div_count += 1

    print(f"Coverage report: {coverage_path}")
    print(f"Defense log:     {defense_path}")
    print(f"Divergence:      {div_path}  ({div_count} divergent rows)")


if __name__ == "__main__":
    main()
