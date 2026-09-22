"""The record keeps the whole reason; only renderings may cut it.

A prediction's `reason`, an order's `reason`, a thesis `claim` and a
`close_reason` are the *evidence* the review scores. Cutting them at storage
time means the review grades the truncation and the attribution chain ends
mid-sentence.

Measured on live rows: the 2026-09-22 intraday pick for 002384 was archived as
"…现价204.06已破20日高200.6，但ma60=206.01是头上真" — 100 characters, stopping
before the noun the sentence was heading for. The first autonomous replay's
thesis for 002230 ended at "…也是我能确认的最近真实买盘区；", on a semicolon,
with the rest of the argument gone.

The distinction this file pins is: **is this the record, or a rendering of the
record?** Tool payloads shown back to an agent are renderings and stay capped,
because their width costs tokens in a prompt. Rows are not.
"""

import inspect
import re

import pytest

from alpha_agents.agents import chat, sector_selector, sector_stock_selector
from alpha_agents.agents import t1_decider
from alpha_agents.pipeline.tasks import exit_decision, intraday_monitor
from alpha_agents.pipeline.tasks import morning_scan

#: Each entry: the module, and the assignment that must not be cut.
_ARCHIVAL = [
    (t1_decider, r'"reason":\s*str\(raw\.get\("reason"\)[^)]*\)\.strip\(\)\[:'),
    (chat, r'reason=reason\[:'),
    (sector_stock_selector, r'"reason":\s*str\(raw\.get\("reason"\)[^)]*\)\.strip\(\)\[:'),
    (sector_selector, r'"thesis":\s*str\(raw\.get\("thesis"\)[^)]*\)\.strip\(\)\[:'),
    (morning_scan, r'reason=r\.get\("reason",\s*""\)\[:'),
    (intraday_monitor, r'reason=r\.get\("reason",\s*""\)\[:'),
]


@pytest.mark.parametrize("module,pattern", _ARCHIVAL,
                         ids=[m.__name__.split(".")[-1] for m, _ in _ARCHIVAL])
def test_the_stored_reason_is_not_sliced(module, pattern):
    source = inspect.getsource(module)
    hit = re.search(pattern, source)
    assert hit is None, (
        f"{module.__name__} slices the reason it stores: {hit.group(0)!r}. "
        "That field is evidence for attribution; cut it in the renderer.")


def test_close_reason_is_not_sliced():
    """close_reason is what the review scores — see the L1 exit design."""
    source = inspect.getsource(exit_decision)
    for verb in ("卖出", "减仓", "加仓"):
        assert f'agent{verb}: {{d[\'reason\']}}"[:' not in source, (
            f"agent{verb} close_reason is truncated before it is stored")


def test_a_long_reason_survives_the_order_parser():
    """The end-to-end check, not just the absence of a slice."""
    long_reason = "证据链：" + "主力净额连续三日为正，" * 40   # ~800 chars
    reply = (
        '{"orders": [{"code": "600519", "entry_low": 10.0, '
        '"entry_high": 10.2, "stop_loss": 9.5, "target_price": null, '
        f'"reason": "{long_reason}"}}]}}')
    out = t1_decider.parse_orders(reply, {"600519"})
    assert out["parse_error"] is None, out["parse_error"]
    stored = out["orders"][0]["reason"]
    assert stored == long_reason
    # Against the cap that used to apply, so this fails if it comes back.
    assert len(stored) > 200, f"still cut somewhere: {len(stored)} chars"


def test_tool_payloads_may_still_be_capped():
    """The other half of the rule: a rendering shown to an agent costs tokens.

    Asserted so that removing these caps later is a deliberate act rather than
    a tidy-up that quietly doubles every prompt.
    """
    from alpha_agents.tools import trader_tools
    source = inspect.getsource(trader_tools)
    assert '"buy_reason": (o["reason"] or "")[:120]' in source
    assert '"claim": (t["claim"] or "")[:200]' in source
