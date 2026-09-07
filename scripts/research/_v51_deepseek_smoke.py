"""Smoke test: DeepSeek V4-Flash + v5.1 prompt on a single stock-day.

Purpose: verify (a) API connectivity, (b) prompt-cache behavior, (c) JSON
output format adherence, (d) v5.1 validator runs end-to-end on the
response. Picks a known interesting day (300136 on 2025-12-15 — where
qwen-plus v4 produced clean BC-candidate reasoning) so we can compare
against the v4 cache entry side-by-side.

Bypasses .env priority logic by constructing the OpenAI/DeepSeek client
explicitly. Reads the (commented-out) DEEPSEEK_API_KEY from .env via
grep.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _read_deepseek_key_from_env_file() -> str | None:
    env_path = REPO / ".env"
    if not env_path.exists():
        return None
    pattern = re.compile(r'^\s*#?\s*DEEPSEEK_API_KEY\s*=\s*(\S+)')
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None


def _load_v4_qwen_entry(date: str, code: str) -> dict | None:
    """Pull the existing qwen-plus v4 cache entry for the same stock-day,
    so we can compare verdict/phase/checklist outputs."""
    cache_path = REPO / "data/vpa_top20_baseline_v4_full_cache.jsonl"
    if not cache_path.exists():
        return None
    with open(cache_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("date") == date and r.get("code") == code:
                return r.get("result", {})
    return None


def main():
    api_key = _read_deepseek_key_from_env_file()
    if not api_key:
        print("ERROR: no DEEPSEEK_API_KEY found in .env (commented or missing).")
        sys.exit(1)

    base_url = "https://api.deepseek.com/v1"
    # V4-Flash explicit model name (April 2026 release).
    # "deepseek-chat" alias still points to V3.x — must use the explicit
    # name to get prompt caching at V4-Flash pricing.
    model = "deepseek-v4-flash"

    code = "300136"
    name = "信维通信"
    as_of = "2025-12-15"

    # Build the v5.1 input the same way compute_vpa_with_llm does.
    from alpha_agents.tools.vpa import (
        ANNA_COULLING_PROMPT,
        _load_ohlcv,
        _compute_derived,
        _detect_patterns,
        _format_text,
        _phase_guard_context_from_df,
        _extract_verdict,
        _apply_phase_state_guard,
    )

    df = _load_ohlcv(code, days=60, as_of=as_of)
    if df is None:
        print("ERROR: ohlcv load failed for", code)
        sys.exit(1)
    df = _compute_derived(df, window=20)
    patterns = _detect_patterns(df)
    text = _format_text(code, name, df, patterns, window=20)
    phase_context = _phase_guard_context_from_df(df, as_of=as_of)

    user_content = (
        f"以下是 {code} 的量价预计算数据，请按 Anna Coulling 理论做完整分析：\n\n{text}"
    )

    print(f"=== v5.1 smoke test ===")
    print(f"  model: {model} @ {base_url}")
    print(f"  stock: {code} {name}, as_of: {as_of}")
    print(f"  system prompt: {len(ANNA_COULLING_PROMPT):,} chars (~{int(len(ANNA_COULLING_PROMPT)*0.4):,} tok est)")
    print(f"  user content:  {len(user_content):,} chars (~{int(len(user_content)*0.4):,} tok est)")
    print(f"  phase_context: vph={phase_context['vp_harmony_score']}, "
          f"vol_p90={phase_context['vol_ratio_p90']}, abspct_p90={phase_context['abspct_p90']}")
    print()

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)

    # Disable Thinking mode for deterministic JSON output + lower latency.
    # V4-Flash defaults to Thinking on; for our structured task we want
    # Non-Thinking (BC checklist is rule-based, not reasoning-bound).
    print("Calling DeepSeek V4-Flash (non-thinking)...")
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": ANNA_COULLING_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=7000,
        timeout=120,
        extra_body={"enable_thinking": False},
    )
    elapsed = time.time() - t0
    content = resp.choices[0].message.content
    report = (content or "").strip()

    # Cache + token usage stats (DeepSeek exposes prompt_cache_hit_tokens etc.)
    usage = resp.usage if hasattr(resp, "usage") else None
    print(f"\nElapsed: {elapsed:.1f}s")
    if usage:
        print(f"Tokens: prompt={getattr(usage,'prompt_tokens','?')} "
              f"completion={getattr(usage,'completion_tokens','?')} "
              f"total={getattr(usage,'total_tokens','?')}")
        # DeepSeek-specific cache fields
        for attr in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            v = getattr(usage, attr, None)
            if v is not None:
                print(f"  {attr}: {v}")
        # Some SDKs put it under prompt_tokens_details
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            print(f"  prompt_tokens_details: {details}")
    print(f"Report: {len(report):,} chars")

    # Parse + validate end-to-end
    verdict_data = _extract_verdict(report)
    verdict_data = _apply_phase_state_guard(
        verdict_data, "", phase_context=phase_context
    )

    print(f"\n=== Parsed verdict (after v5.1 validator) ===")
    keys = ["direction", "phase", "raw_phase", "confirmation_level", "confirmation_tier",
            "confirmed", "vp_harmony_score", "vph_conflict_validated"]
    for k in keys:
        print(f"  {k}: {verdict_data.get(k)}")
    print(f"  reason: {verdict_data.get('reason','')[:120]}")

    vc = verdict_data.get("vph_conflict") or {}
    if isinstance(vc, dict) and vc.get("exists"):
        print(f"\nvph_conflict (LLM filled):")
        for k in ("climax_type", "climax_date", "extended_trend_20d_pct",
                  "climax_volume_ratio", "climax_range_vs_5d_avg_x",
                  "climax_shadow_ratio", "climax_close_position",
                  "post_bar_reverse_pct_3d"):
            print(f"  {k}: {vc.get(k)}")
        fails = verdict_data.get("vph_conflict_failures") or []
        if fails:
            print(f"  validator_failures: {fails}")
    elif phase_context.get("vp_harmony_score") in ("bullish", "bearish") and \
         verdict_data.get("phase"):
        print(f"\n(no vph_conflict — LLM thinks phase is consistent with vph)")

    # Side-by-side with qwen v4 cache entry
    qwen = _load_v4_qwen_entry(as_of, code)
    if qwen:
        print(f"\n=== Side-by-side with qwen v4 cache (same stock-day) ===")
        print(f"  {'field':<25}  {'qwen v4':<15}  {'ds v4-flash':<15}")
        for fld in ("llm_phase", "llm_verdict", "llm_confirmation_level"):
            base = fld.replace("llm_", "")
            v_qwen = qwen.get(fld, "-")
            v_ds = verdict_data.get(base, "-") if base != "verdict" else verdict_data.get("direction", "-")
            v_ds = verdict_data.get(base, "-")
            if base == "verdict":
                v_ds = verdict_data.get("direction", "-")
            print(f"  {fld:<25}  {v_qwen!s:<15}  {v_ds!s:<15}")

    # Save full report so we can inspect
    out = REPO / "data" / "v51_smoke_300136_2025-12-15_dsv4flash.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# v5.1 smoke test, DeepSeek V4-Flash\n")
        f.write(f"# stock: {code} {name}, as_of: {as_of}\n")
        f.write(f"# elapsed: {elapsed:.1f}s\n\n")
        f.write(report)
    print(f"\nFull report saved: {out}")


if __name__ == "__main__":
    main()
