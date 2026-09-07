"""v7 narrative templater tests (spec §2.2.b)."""

import pandas as pd
import numpy as np
from alpha_agents.tools.vpa import _render_price_action_narrative, _compute_derived


FORBIDDEN_TOKENS = [
    "疑似", "潜在", "可能", "异常", "突破", "跌破", "走强", "走弱",
    "局内人", "主力", "机构", "测试", "确认", "信号", "派发", "吸筹",
    "拉升", "下跌", "卖压", "买盘", "动能", "反弹", "回落", "冲高",
]


def _make_df(n=20):
    rng = np.random.default_rng(seed=42)
    closes = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "code": ["300136"] * n,
        "name": ["信维通信"] * n,
        "date": [f"2025-12-{i+1:02d}" for i in range(n)],
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": [1_000_000] * n,
    })
    return _compute_derived(df, window=10)


def test_narrative_does_not_contain_forbidden_tokens():
    df = _make_df()
    text = _render_price_action_narrative(df, days=15)
    for token in FORBIDDEN_TOKENS:
        assert token not in text, (
            f"narrative contains forbidden interpretive token '{token}': "
            f"{text}"
        )


def test_narrative_includes_dates_and_basic_facts():
    df = _make_df()
    text = _render_price_action_narrative(df, days=10)
    # At least the most recent date should appear
    assert df["date"].iloc[-1] in text
    # Section header present
    assert "近期价格与成交量节奏" in text


def test_narrative_handles_empty_df():
    df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    text = _render_price_action_narrative(df, days=10)
    assert isinstance(text, str)


def test_narrative_emits_objective_descriptors_only():
    """Spot-check on a synthetic strong-up bar: narrative should describe
    the bar physically without inferring direction or actor."""
    df = _make_df()
    df.loc[15, "close"] = df.loc[14, "close"] * 1.05
    df.loc[15, "high"] = df.loc[15, "close"] * 1.005
    df.loc[15, "open"] = df.loc[14, "close"] * 1.001
    df.loc[15, "volume"] = 3_000_000
    df = _compute_derived(df, window=10)
    text = _render_price_action_narrative(df, days=10)
    # Narrative should mention numerical facts
    assert "%" in text or "量比" in text
    # Should NOT contain trajectory inference
    for forbidden in ["冲高回落", "突破", "强势"]:
        assert forbidden not in text
