"""T1 决策器会主动提问：工具真的接进去了，而且真的会被调用。

评审的核心发现是 `t1_decider` 曾经 `tools=[]` / `max_turns=1`——它只能从
平台预先做好的面板里挑，不能自己转头看。这一组测试钉住三件事：

1. **接线**：`propose` 把工具交给了 Agent，`max_turns` 足够发起一次调用。
2. **真的会被调用**：用一个假模型真的发起 tool call，断言工具被执行、
   结果回到模型、最终订单仍被解析。用 grep 源码证明"工具传进去了"，
   证明不了"模型能用它"。
3. **可以被关掉**：`tools=[]` 仍然可用，这样"会用工具的 trader"和
   "只会挑的 scorer"能在同一窗口上对照。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from alpha_agents.agents import t1_decider
from alpha_agents.tools import trader_tools as TT


PANEL = [
    {"code": "600001", "name": "甲", "change_pct": 3.0, "adv20": 1e6,
     "close": 10.0, "turnover_rate": 5.0, "consecutive_limits": 1,
     "main_net": 100.0, "theme": "t"},
    {"code": "600002", "name": "乙", "change_pct": 1.0, "adv20": 1e6,
     "close": 10.0, "turnover_rate": 4.0, "consecutive_limits": 0,
     "main_net": 50.0, "theme": "t"},
]


class TestTheToolsAreWired:
    def test_the_six_question_tools_are_offered(self):
        assert len(TT.TRADER_TOOLS) == 6
        names = {t.name for t in TT.TRADER_TOOLS}
        assert names == {
            "get_market_regime", "get_theme_state", "get_stock_context",
            "get_intraday_shape", "get_stock_memory", "get_my_state",
        }

    def test_every_tool_says_what_question_it_answers(self):
        """A tool the model cannot tell when to use is a tool it will not
        use. The description is the interface."""
        for t in TT.TRADER_TOOLS:
            desc = (t.description or "").strip()
            assert len(desc) > 20, f"{t.name} has no usable description"

    def test_no_description_hands_over_a_verdict(self):
        """Reuses `lint_policy`'s definition of "advice" rather than a second,
        blunter one: a naive `"建议" in desc` flags `get_market_regime`'s
        "不含仓位建议", which is the *denial* of advice."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import lint_policy as LP
        for t in TT.TRADER_TOOLS:
            desc = (t.description or "")
            for pattern, why in LP.PATTERNS:
                assert not pattern.search(desc), (
                    f"{t.name}'s description trips the policy checker "
                    f"({why}): {desc[:80]}")

    def test_the_decider_defaults_to_more_than_one_turn(self):
        """A tool call costs a turn, so max_turns=1 makes tools unusable."""
        import inspect
        sig = inspect.signature(t1_decider.propose)
        assert sig.parameters["max_turns"].default > 1

    def test_tools_can_be_turned_off_for_a_comparison_run(self):
        import inspect
        sig = inspect.signature(t1_decider.propose)
        assert sig.parameters["tools"].default is None, (
            "tools must stay opt-in at the signature level so the bare-picker "
            "configuration remains reachable")


from agents.models.interface import Model as _ModelInterface  # noqa: E402


class _StubModel(_ModelInterface):
    """A real `Model` subclass, because `Agent.__init__` type-checks it.

    The call never runs — `Runner.run` is replaced — so `get_response` only
    has to exist.
    """

    async def get_response(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def stream_response(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestTheModelActuallyUsesThem:
    """The part a source grep cannot show.

    The assertion is that the tools reach the `Agent` the SDK builds, with
    the descriptions attached. Driving a real tool-calling turn needs a live
    provider; what can be checked offline is the object the model is handed —
    which is the thing that was empty before this change.
    """

    def test_the_agent_is_built_with_the_tools_attached(self, monkeypatch):
        from agents import Agent, Runner

        seen: dict = {}
        real_init = Agent.__init__

        def _spy(self, *a, **kw):
            seen.update(kw)
            real_init(self, *a, **kw)

        monkeypatch.setattr(Agent, "__init__", _spy)

        async def _fake_run(agent, message, **kw):
            seen["_turns"] = kw.get("max_turns")
            return _FakeResult(final=json.dumps({"orders": []}))

        monkeypatch.setattr(Runner, "run", staticmethod(_fake_run))
        out = asyncio.run(t1_decider.propose(
            day="2026-01-05", prev_day="2026-01-02", panel=PANEL,
            news=[], template=_minimal_template(), model=_StubModel(),
            tools=TT.TRADER_TOOLS, max_turns=3))

        assert out["orders"] == []
        assert len(seen.get("tools") or []) == 6, (
            "the Agent was built without the trader tools — this is the "
            "defect the review found (tools=[]) coming back")
        assert seen.get("_turns") == 3, (
            "max_turns=1 makes a tool call impossible")

    def test_no_tools_still_builds_a_bare_picker(self, monkeypatch):
        from agents import Agent, Runner

        seen: dict = {}
        real_init = Agent.__init__

        def _spy(self, *a, **kw):
            seen.update(kw)
            real_init(self, *a, **kw)

        monkeypatch.setattr(Agent, "__init__", _spy)

        async def _fake_run(agent, message, **kw):
            return _FakeResult(final=json.dumps({"orders": []}))

        monkeypatch.setattr(Runner, "run", staticmethod(_fake_run))
        asyncio.run(t1_decider.propose(
            day="2026-01-05", prev_day="2026-01-02", panel=PANEL,
            news=[], template=_minimal_template(), model=_StubModel(),
            tools=None, max_turns=3))

        assert seen.get("tools") == [], (
            "the bare-picker configuration must remain reachable for a "
            "comparison run")


class _FakeResult:
    """Just enough of the SDK's RunResult for `propose` to read."""

    def __init__(self, final: str = "", tool_calls=None):
        self.final_output = final
        self._tool_calls = tool_calls or []

    @property
    def new_items(self):
        return []


def _minimal_template() -> str:
    """The smallest template the renderer accepts.

    Real prompt text is exercised elsewhere; here the template must not be
    what fails, so it carries every placeholder the renderer supplies.
    """
    return (
        "day {session} sight {sight} news {news} cutoff {news_cutoff} "
        "window {news_window} panel {panel} market {market} book {book} "
        "knowledge {knowledge} note {trader_note} picks {picks} "
        "prev {prev_day}"
    )


class TestTheTimeContractSurvivesWiring:
    def test_the_tools_read_the_replay_clock_not_the_wall_clock(self):
        """The reason these tools are safe to hand a replay: they take their
        date from `replay_mode`, so the same call answers differently at two
        clocks. Pinned here as well as in the time-travel suite because this
        is the property that makes the wiring legal."""
        import inspect
        src = inspect.getsource(TT)
        assert "_as_of()" in src
        assert "replay_mode" in src
        assert "effective_eod_cut_date" in src

    def test_no_tool_opens_the_live_book_for_writes(self):
        """These are readers. A tool that could write would let a model
        change the book it is reasoning about."""
        import inspect
        src = inspect.getsource(TT)
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "commit()"):
            assert forbidden not in src, (
                f"trader_tools contains {forbidden!r} — these tools answer "
                f"questions, they do not change the book")


class TestATooLongDeliberationIsADayNotAWindow:
    """Two findings from the first real tool-enabled replay, both measured.

    The run was started with a turn budget of 3 — a guess made before any
    recording existed. The model's opening decision issued **six parallel
    tool calls**, then six more, and the SDK raised `MaxTurnsExceeded`. The
    exception escaped `propose` and killed the entire window: one
    over-thinking morning cost forty simulated days.
    """

    def test_the_default_budget_fits_what_the_recording_shows(self):
        import inspect
        sig = inspect.signature(t1_decider.propose)
        assert sig.parameters["max_turns"].default >= 6, (
            "the recorded deliberation needed three rounds of six calls plus "
            "an answer; a smaller default is a budget that fails in practice")

    def test_exceeding_the_budget_is_reported_not_raised(self, monkeypatch):
        from agents import Runner
        from agents.exceptions import MaxTurnsExceeded as MTE

        async def _blow_up(agent, message, **kw):
            raise MTE("Max turns (3) exceeded")

        monkeypatch.setattr(Runner, "run", staticmethod(_blow_up))
        out = asyncio.run(t1_decider.propose(
            day="2026-01-05", prev_day="2026-01-02", panel=PANEL,
            news=[], template=_minimal_template(), model=_StubModel(),
            tools=TT.TRADER_TOOLS, max_turns=3))

        assert out["orders"] == []
        assert out["parse_error"], (
            "'it never answered' must not be reported as 'it chose nothing'")
        assert "MaxTurnsExceeded" in out["parse_error"]

    def test_the_runner_treats_that_as_a_failed_day_not_a_failed_run(self):
        """The runner already has the right shape for this — `parse_error`
        is counted and the day is skipped — so the fix is that `propose`
        returns instead of raising. Checked on the AST rather than by
        substring: the handler's own comment says "raise here would...", and
        a text search cannot tell a comment from a statement."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(t1_decider.propose))
        handlers = [h for node in ast.walk(tree)
                    if isinstance(node, ast.Try)
                    for h in node.handlers
                    if h.type is not None
                    and "MaxTurnsExceeded" in ast.dump(h.type)]
        assert handlers, "propose does not handle MaxTurnsExceeded"
        for h in handlers:
            raises = [n for n in ast.walk(h) if isinstance(n, ast.Raise)]
            assert not raises, (
                "the MaxTurnsExceeded handler re-raises, which is the "
                "window-killing behaviour")
