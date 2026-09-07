"""跑 anna_screener top 20 候选股 在 5/7 + 5/8 上 v11 LLM 分析.

输出: 每只股票 × 每天 (5/7, 5/8) 的 verdict + phase + warning + signals.
按"有交易信号"程度排序: c2 / c3 / 派发预警 / 看空 / 看多 等都标出来.
"""
from __future__ import annotations
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

# Force deepseek path with thinking ON (production v4-flash)
os.environ.setdefault("VPA_LLM_PROVIDER", "deepseek")
os.environ.pop("AGENT_API_KEY", None)
os.environ.pop("SILICONFLOW_API_KEY", None)

from alpha_agents.tools.vpa import compute_vpa_with_llm
from alpha_agents.tools.vpa.llm import PROMPT_VERSION


def warmup_v8():
    """Pre-init mini-racer V8 in the main thread to avoid race on first
    multi-thread JS eval. akshare's THS endpoints decrypt JS at runtime."""
    try:
        import py_mini_racer
        ctx = py_mini_racer.MiniRacer()
        ctx.eval("1+1")
        print("  V8 warmup OK (py_mini_racer)", flush=True)
    except ImportError:
        try:
            import mini_racer
            ctx = mini_racer.MiniRacer()
            ctx.eval("1+1")
            print("  V8 warmup OK (mini_racer)", flush=True)
        except Exception as e:
            print(f"  V8 warmup skipped: {e}", flush=True)
    except Exception as e:
        print(f"  V8 warmup failed (non-fatal): {e}", flush=True)


def load_top_codes(picks_csv: Path, top: int) -> list[tuple[str, str]]:
    """Load (code, name) tuples from anna_screener output CSV. Name optional."""
    out: list[tuple[str, str]] = []
    with open(picks_csv) as f:
        header = f.readline().strip().split(",")
        ci = header.index("code") if "code" in header else 0
        ni = header.index("name") if "name" in header else None
        for line in f:
            parts = line.rstrip().split(",")
            if len(parts) <= ci:
                continue
            code = parts[ci].strip()
            name = parts[ni].strip() if ni is not None and ni < len(parts) else code
            out.append((code, name))
            if len(out) >= top:
                break
    return out


def analyze_one(code: str, name: str, as_of: str) -> dict:
    """Run v11 LLM analysis for one (code, as_of). compute_vpa_with_llm
    returns the api wrapper's `llm_*` prefixed fields, not raw _call_llm_vpa
    keys, so we read those."""
    t0 = time.time()
    try:
        r = compute_vpa_with_llm(code, as_of=as_of)
        dt = time.time() - t0
        if not r.get("ok", True):
            return {
                "code": code, "name": name, "as_of": as_of,
                "status": "error", "error": r.get("error", "unknown"),
                "elapsed_s": dt,
            }
        # Try to recover signals from llm_report — api wrapper doesn't surface
        # raw signals list, but the report contains them and the verdict block.
        sigs = []
        try:
            from alpha_agents.tools.vpa.verdict import _extract_verdict
            v = _extract_verdict(r.get("llm_report", "") or "")
            sigs = (v or {}).get("signals") or []
        except Exception:
            pass
        sig_summary = [
            f"{s.get('name','?')[:18]}{'✓' if s.get('confirmed') else '·'}"
            for s in sigs[:4]
        ]
        pc = r.get("llm_phase_change") or {}
        sc = r.get("selected_climax") or {}
        return {
            "code": code, "name": name, "as_of": as_of,
            "status": "ok",
            "direction": r.get("llm_verdict"),
            "phase": r.get("llm_phase", ""),
            "warning_phase": r.get("llm_warning_phase") or "",
            "phase_change_to": pc.get("to", ""),
            "phase_change_confirmed": pc.get("confirmed"),
            "confirmation_level": r.get("llm_confirmation_level", 0),
            "vp_harmony_score": r.get("vp_harmony_score", "neutral"),
            "selected_climax_id": sc.get("candidate_id"),
            "selected_climax_type": sc.get("climax_type"),
            "signals_count": len(sigs),
            "signals_summary": " ".join(sig_summary),
            "reason": (r.get("llm_reason") or "")[:140],
            "elapsed_s": dt,
        }
    except Exception as e:
        return {
            "code": code, "name": name, "as_of": as_of,
            "status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}",
            "elapsed_s": time.time() - t0,
        }


def signal_strength(row: dict) -> int:
    """Score for sorting by signal interestingness. Higher = more notable."""
    if row.get("status") != "ok":
        return -1
    score = 0
    direction = row.get("direction") or ""
    if direction == "看多":
        score += 6
    elif direction == "看空":
        score += 5
    elif direction == "偏多":
        score += 3
    elif direction == "偏空":
        score += 4
    phase = row.get("phase") or ""
    if "派发" in phase:
        score += 5  # actionable bear signal
    if "拉升初期" in phase:
        score += 4  # actionable bull entry
    if "吸筹尾声" in phase:
        score += 3
    if row.get("phase_change_confirmed"):
        score += 3
    if row.get("warning_phase"):
        score += 1
    score += int(row.get("confirmation_level", 0)) * 2
    if row.get("selected_climax_id"):
        score += 2
    return score


def main():
    print("warming up V8 in main thread...", flush=True)
    warmup_v8()

    picks_csv = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not picks_csv or not picks_csv.exists():
        print("usage: python run_v11_top20_57_58.py PICKS_CSV [N=20] [WORKERS=5]")
        print(f"  expected screener output: code,name,...")
        sys.exit(1)
    n_top = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    n_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    codes = load_top_codes(picks_csv, n_top)
    print(f"\n═══ v11 LLM 分析 top {len(codes)} on 5/7 + 5/8 ═══")
    print(f"PROMPT_VERSION: {PROMPT_VERSION}")
    print(f"workers: {n_workers}")
    print(f"codes: {[c for c, _ in codes]}\n")

    # Days to analyze — pass via env var for flexibility
    days_str = os.environ.get("V11_DAYS", "2026-05-07,2026-05-08")
    days = [d.strip() for d in days_str.split(",") if d.strip()]
    print(f"days: {days}\n")

    tasks = []
    for c, n in codes:
        for d in days:
            tasks.append((c, n, d))

    results: list[dict] = []
    t_start = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(analyze_one, c, n, d): (c, d) for c, n, d in tasks}
        for fut in as_completed(futures):
            row = fut.result()
            results.append(row)
            completed += 1
            print(f"  [{completed}/{len(tasks)}] {row['code']} @ {row['as_of']}: "
                  f"{row.get('direction', '?')} {row.get('phase', '?')} "
                  f"warn={row.get('warning_phase') or '─'} "
                  f"({row.get('elapsed_s', 0):.0f}s)")

    total_time = time.time() - t_start
    print(f"\n[done in {total_time/60:.1f} min]\n")

    # Save raw
    with open("/tmp/v11_top20_57_58.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    # Sort and present
    print("═══ 按信号强度排序 ═══\n")
    print(f'{"code":7} {"name":10} {"date":12} {"direction":6} {"phase":12} {"warn":18} {"PC":18} {"signals":40}')
    print("─" * 130)

    # Group by code, then sort by max signal strength across the two days
    by_code: dict[str, list[dict]] = {}
    for r in results:
        by_code.setdefault(r["code"], []).append(r)

    code_scores = [(c, max(signal_strength(r) for r in rows)) for c, rows in by_code.items()]
    code_scores.sort(key=lambda x: -x[1])

    for code, score in code_scores:
        rows = sorted(by_code[code], key=lambda r: r["as_of"])
        for r in rows:
            if r.get("status") == "error":
                print(f'{r["code"]:7} {r["name"][:10]:10} {r["as_of"]:12} ERROR: {r.get("error", "?")}')
                continue
            pc_str = ""
            if r.get("phase_change_to"):
                pc_str = f"→{r['phase_change_to']}{'✓' if r.get('phase_change_confirmed') else '?'}"
            print(f'{r["code"]:7} {r["name"][:10]:10} {r["as_of"]:12} '
                  f'{(r.get("direction") or "?"):6} '
                  f'{(r.get("phase") or "?"):12} '
                  f'{(r.get("warning_phase") or "─"):18} '
                  f'{pc_str:18} '
                  f'{(r.get("signals_summary") or "")[:40]}')
        print()

    # Highlight noteworthy
    print("\n═══ 值得关注的信号 ═══")
    for code, score in code_scores[:10]:
        latest = max(by_code[code], key=lambda r: r["as_of"])
        if latest.get("status") != "ok":
            continue
        flags = []
        if "派发" in (latest.get("phase") or ""):
            flags.append("派发")
        if "拉升初期" in (latest.get("phase") or ""):
            flags.append("入场")
        if (latest.get("direction") or "") in ("看多", "偏多"):
            flags.append(latest["direction"])
        if (latest.get("direction") or "") in ("看空", "偏空"):
            flags.append(latest["direction"])
        if latest.get("warning_phase"):
            flags.append("warning")
        flag_str = " | ".join(flags) if flags else "无"
        print(f"  {latest['code']} {latest['name'][:10]:10} score={score}: {flag_str}")
        print(f"    reason: {latest.get('reason', '')[:120]}")


if __name__ == "__main__":
    main()
