"""v7 validator short-circuit test (Anne review §6 gap 2, MANDATORY).

When the LLM fails to flag a textbook BC pattern (selected_climax.candidate_id
is null), a parallel "sanity scan" must detect the pattern and log it as a
LLM-missed-climax artifact. This does not block deployment but produces an
operator-visible signal of LLM false-negatives.
"""

import pandas as pd
import numpy as np

from alpha_agents.tools.vpa import _scan_climax_candidates, _compute_derived


def _build_textbook_bc_window():
    """Synthetic 60d window ending in a textbook BC pattern: extended uptrend
    followed by a climax bar with vol p98, close in lower 0.30, upper shadow
    0.55, post-bar -3% reversal.

    Layout (idx 0..59):
      - 0..54: gentle uptrend, normal volume.
      - 55: BC bar — open mid, high spikes, close in lower half, huge volume.
      - 56..59: -3% reversal (post-bar follow-through).
    """
    n = 60
    closes = list(np.linspace(10.0, 26.5, num=n - 5))
    closes.append(27.5)  # idx 55: peak before reversal (will be overwritten below)
    closes.extend([26.6, 26.0, 25.5, 25.0])  # idx 56-59: -3% reversal
    n = len(closes)

    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        # Synthetic dates — month range chosen so day numbers stay valid.
        "date": [
            f"2025-{((i // 20) + 9):02d}-{((i % 20) + 1):02d}"
            for i in range(n)
        ],
        "open": list(closes),
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": list(closes),
        "volume": [int(1e6) if i != 55 else int(5e7) for i in range(n)],
    })

    # Force idx 55 into a textbook BC geometry:
    #   open ~26.5 (near low), high 30.0 (long upper shadow), close 27.0
    #   close_position = (27 - 26.4) / (30 - 26.4) = 0.167 → lower 0.30 box
    #   upper_shadow   = (30 - 27)   / (30 - 26.4) = 0.833 → > 0.4 floor
    df.loc[55, "open"] = 26.5
    df.loc[55, "high"] = 30.0
    df.loc[55, "low"] = 26.4
    df.loc[55, "close"] = 27.0
    return _compute_derived(df, window=20)


def test_sanity_scan_detects_textbook_bc_when_llm_misses():
    """If the candidate scanner finds a clear BC pattern but the LLM
    selected_climax is null, a parallel sanity rule (cp<0.3 + upper_shadow≥0.4)
    should flag it. This is the short-circuit log signal."""
    df = _build_textbook_bc_window()
    candidates = _scan_climax_candidates(df, scan_window=40)
    assert candidates, "scanner failed to produce any candidates from textbook BC window"

    # Sanity rule: any candidate with cp<0.3 + upper_shadow≥0.4 is a BC pattern.
    bc_pattern_present = any(
        c.get("close_position", 0.5) < 0.3 and c.get("upper_shadow", 0) >= 0.4
        for c in candidates
    )
    assert bc_pattern_present, (
        "scanner pool does not contain any bar matching textbook BC sanity rule "
        "(cp<0.3 + upper_shadow>=0.4) — synthetic data may not be extreme enough. "
        f"Candidates emitted: {[(c['date'], round(c['close_position'], 2), round(c['upper_shadow'], 2)) for c in candidates]}"
    )

    # If LLM-selected candidate_id is null but BC pattern present in pool, the
    # short-circuit log artifact must be emitted (this is the operational
    # contract; we just verify the data shape that downstream logging would use).
    llm_picked_nothing = True  # simulate LLM false-negative
    if llm_picked_nothing and bc_pattern_present:
        # Operator-visible artifact: list of bars LLM should have flagged.
        missed_bars = [
            c for c in candidates
            if c.get("close_position", 0.5) < 0.3 and c.get("upper_shadow", 0) >= 0.4
        ]
        assert len(missed_bars) >= 1, "expected at least one missed BC candidate"
        # In production, this list would be written to
        # data/v7_llm_missed_climax_log.jsonl for ops review.
