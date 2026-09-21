"""The trader's own instructions must reach the replay's prompts.

``_trader_note`` called ``load_traders().get(ctx.trader)``. ``load_traders``
returns a list, so that raised ``AttributeError`` on every single run; the
broad except turned it into "trader note unavailable: 'list' object has no
attribute 'get'", which reads like a configuration problem and not like a call
that could never work. The consequence is invisible in a report: the replay
ran without the style file it is built to honour — for ``pullback`` that is
the instruction to find real support with ``get_price_levels``, to size the
entry band by ATR rather than a flat percentage, and to put a theme condition
in every invalidation list.

A count assertion is deliberate. "It returned a string" was also true when the
string was always empty.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.data import trader as trader_mod  # noqa: E402


class _Ctx:
    def __init__(self, trader_id):
        self.trader = trader_id


def _fake_traders():
    return [
        trader_mod.Trader(id="breakout", name="突破派",
                          extra_prompt="追强势。"),
        trader_mod.Trader(id="pullback", name="回调派",
                          extra_prompt="你等价格。用 get_price_levels 找支撑。"),
    ]


def test_the_note_of_the_named_trader_is_returned(monkeypatch):
    monkeypatch.setattr(trader_mod, "load_traders", _fake_traders)
    assert wf._trader_note(_Ctx("pullback")) == (
        "你等价格。用 get_price_levels 找支撑。")


def test_an_unknown_trader_id_yields_an_empty_note(monkeypatch):
    monkeypatch.setattr(trader_mod, "load_traders", _fake_traders)
    assert wf._trader_note(_Ctx("nobody")) == ""


def test_a_broken_traders_directory_costs_the_note_not_the_run(monkeypatch):
    def _boom():
        raise OSError("traders/ unreadable")
    monkeypatch.setattr(trader_mod, "load_traders", _boom)
    assert wf._trader_note(_Ctx("pullback")) == ""


def test_the_shipped_pullback_trader_actually_carries_a_note():
    """Guards the real file, not only the lookup.

    The lookup can be correct while the config it reads is empty, and an empty
    note is exactly the state this bug produced for months.
    """
    note = wf._trader_note(_Ctx("pullback"))
    assert len(note) > 200, f"pullback extra_prompt is {len(note)} chars"
    assert "get_price_levels" in note
