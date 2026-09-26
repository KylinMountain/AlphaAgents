"""The agent's sell side, in a replay.

The first 20-day replay bought 11 positions and every one of them left
through its stop. That was not the model's choice: nothing asked it. The
runner called the agent once a day, about buying, and `_settle_exits` was
pure Python.

Two contracts matter here and both are about what the agent may *not* do:

* **The hard stop runs first.** A position that gapped through its stop is
  already closed when the agent is asked, so no amount of reasoning can
  argue a risk line away. This is the ordering in `run`, and it is the
  reason `decide_for_replay` must never be called before `_settle_exits`.
* **Every failure holds.** A timeout, an unreadable reply, a provider
  outage — none of them may sell. The mechanical stop is still underneath,
  so holding on error cannot run the book down.
"""

from __future__ import annotations

import asyncio

import pytest

from alpha_agents.pipeline.tasks import exit_decision as ED


POSITIONS = [
    {"id": 1, "code": "600001", "name": "甲", "shares": 100,
     "open_price": 10.0, "stop_loss": 9.0, "target_price": 12.0,
     "holding_days": 2, "theme": "半导体", "reason": "主线启动"},
]
PRICES = {"600001": 11.0}


class TestTheContextTellsTheAgentWhereItStands:
    def test_it_carries_cost_price_and_pnl(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[])
        assert "成本10.00" in ctx and "现价11.00" in ctx
        assert "+10.00%" in ctx

    def test_it_says_so_when_a_replay_has_no_news(self):
        """`None` means "this run supplied no news", which is not the same
        as "searched and found nothing" — a replay has no live index."""
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={})
        assert "未提供" in ctx
        assert "不等于没有消息" in ctx

    def test_a_live_caller_still_gets_the_live_wording(self, monkeypatch):
        monkeypatch.setattr(ED, "_news_for_theme", lambda t: [])
        ctx = ED.build_context(POSITIONS, PRICES, signals=[])
        assert "近期快讯: 无" in ctx
        assert "未提供" not in ctx

    def test_supplied_news_is_used_verbatim(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={"半导体": ["[09:10] 某快讯"]})
        assert "[09:10] 某快讯" in ctx


class TestEveryFailureHolds:
    def _run(self, monkeypatch, *, raises=None, reply=""):
        async def _fake_decide(context, trader=None, **kw):
            if raises:
                raise raises
            return ED.parse_decisions(reply)
        monkeypatch.setattr(ED, "decide", _fake_decide)
        return asyncio.run(ED.decide_for_replay(
            POSITIONS, PRICES, day="2026-01-05", news_by_theme={}))

    def test_a_timeout_holds(self, monkeypatch):
        assert self._run(monkeypatch, raises=asyncio.TimeoutError()) == []

    def test_a_provider_error_holds(self, monkeypatch):
        assert self._run(monkeypatch, raises=RuntimeError("boom")) == []

    def test_an_unreadable_reply_is_not_a_sell(self, monkeypatch):
        """Nothing means hold. An exit that fires because the model wrote
        broken JSON is worse than one that never fires."""
        assert self._run(monkeypatch, reply="我觉得该卖但我不写 JSON") == []

    def test_no_positions_makes_no_call(self, monkeypatch):
        called = []

        async def _fake_decide(*a, **kw):
            called.append(1)
            return []
        monkeypatch.setattr(ED, "decide", _fake_decide)
        out = asyncio.run(ED.decide_for_replay(
            [], PRICES, day="2026-01-05", news_by_theme={}))
        assert out == [] and called == []


class TestTheReplayPassesNoTools:
    def test_a_replay_model_and_no_tools_reach_decide(self, monkeypatch):
        """Every live tool answers from *now*. A replay that let
        `search_news` through would decide a 2026-08 day with 2026-09
        flashes. The buy side never had this problem because it passes no
        tools at all."""
        seen = {}

        async def _fake_decide(context, trader=None, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(ED, "decide", _fake_decide)
        asyncio.run(ED.decide_for_replay(
            POSITIONS, PRICES, day="2026-01-05", news_by_theme={},
            model=object()))
        assert seen.get("tools") == []
        assert seen.get("model") is not None


class TestParseDecisionsRefusesWhatItCannotGrade:
    def test_an_order_without_a_reason_is_ignored(self):
        """The whole point of moving the decision to the agent was to get a
        reason worth grading at review."""
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"sell",'
            '"reason":""}] -->')
        assert out == []

    def test_hold_needs_no_reason(self):
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"hold",'
            '"reason":""}] -->')
        assert [d["action"] for d in out] == ["hold"]

    def test_an_unknown_action_is_ignored(self):
        out = ED.parse_decisions(
            '<!-- DECISIONS: [{"code":"600001","action":"yolo",'
            '"reason":"x"}] -->')
        assert out == []

    def test_a_malformed_block_returns_nothing(self):
        assert ED.parse_decisions("no block here") == []


class TestTheAgentCanActuallyBeUnderstood:
    """The parser only accepted a form neither model emits.

    `parse_decisions` required `<!-- DECISIONS: [...] -->`. Asked for that
    marker, both mimo and Qwen answer with a ```json fenced array instead —
    measured: 17 exit calls in a 20-day replay, 17 unreadable, 0 decisions.
    Production has had `AGENT_EXIT_DECISIONS=1` set with not one
    `Exit decisions:` line in its logs, so the agent has never once acted on
    a position. It held everything and said nothing.

    That is the failure shape this repository keeps paying for: an answer
    that looks like a normal one. These tests pin every shape the fallbacks
    are for, and the ambiguity they must not fall into.
    """

    def _one(self, text):
        got = ED.parse_decisions(text)
        return got[0] if got else None

    def test_the_documented_marker_still_works(self):
        d = self._one('<!-- DECISIONS: [{"code":"600186","action":"hold",'
                      '"reason":"x"}] -->')
        assert d and d["code"] == "600186"

    def test_a_json_fenced_array_is_read(self):
        """The shape both models actually produce."""
        d = self._one('判断如下：\n```json\n[{"code":"600186",'
                      '"action":"sell","reason":"主线转弱"}]\n```')
        assert d and d["action"] == "sell"

    def test_an_unfenced_array_is_read(self):
        d = self._one('整体判断：\n[{"code":"600186","action":"hold",'
                      '"reason":"x"}]')
        assert d and d["code"] == "600186"

    def test_a_plain_fence_without_the_json_tag_is_read(self):
        d = self._one('```\n[{"code":"600186","action":"hold","reason":"x"}]\n```')
        assert d and d["code"] == "600186"

    def test_an_example_in_the_prose_does_not_win_over_the_answer(self):
        """A model that thinks out loud shows an example early and its real
        answer at the end; taking the first array would act on the example."""
        d = self._one(
            '例如 [{"code":"600000","action":"hold","reason":"示例"}]。\n'
            '我的判断：\n[{"code":"600186","action":"sell","reason":"真的"}]')
        assert d and d["code"] == "600186" and d["action"] == "sell"

    def test_the_real_shape_from_the_replay_parses(self):
        """Verbatim from the 20-day run that exposed this."""
        text = (
            "整体判断来看，莲花控股今日依然有资金流入，主线也继续保持强度，"
            "并没有出现主线归档或走弱的情况。\n\n"
            "```json\n[\n    {\n"
            '        "code": "600186",\n'
            '        "action": "hold",\n'
            '        "reason": "主线今日+1仍在流入",\n'
            '        "confidence": "high"\n    }\n]\n```')
        d = self._one(text)
        assert d and d["code"] == "600186" and d["action"] == "hold"
        assert d["confidence"] == "high"

    def test_prose_with_no_json_still_holds(self):
        assert ED.parse_decisions("我觉得该卖，但我不写 JSON") == []


class TestTheExitClassifierKnowsTheSettlementsWording:
    """`t1_execution` fills at the `lower` level (the stop) or the `upper`
    one (the target) and says so as "only the lower level X was touched".

    The first classifier looked for "stop"/"止损" only, so seven real exits
    in the 20-day replay landed in 其他 — it reported 止损 1 when the truth
    was 止损 6 / 止盈 2.
    """

    def _kinds(self, reasons):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        fills = [{"side": "sell", "reason": r} for r in reasons]
        return wf._exit_attribution({"fills": fills})[-1]

    def test_a_lower_level_touch_is_a_stop(self):
        assert "止损 1" in self._kinds(["only the lower level 11.0 was touched"])

    def test_an_upper_level_touch_is_a_target(self):
        assert "止盈 1" in self._kinds(["only the upper level 23.0 was touched"])

    def test_a_gap_through_the_stop_is_a_stop(self):
        assert "止损 1" in self._kinds(
            ["the open 12.47 gapped through the stop 12.49"])

    def test_an_agent_sell_is_not_attributed_to_a_rule_it_mentions(self):
        """An agent's reason is free text and may mention a stop without the
        stop having caused the exit."""
        assert "agent 清仓 1" in self._kinds(
            ["agent卖出: 还没来得及止损，但主线已经转弱"])

    def test_an_agent_trim_is_named_apart_from_a_full_exit(self):
        """Trims were invisible until the second agent-exits window: only
        the `agent_exit` alert became a fill row, so the report printed
        "卖出 5 笔" for a window with 18 real sell legs. A trim is the
        agent selling too, and "sold" vs "trimmed" is the difference
        between two kinds of management."""
        assert "agent 减仓 1" in self._kinds(
            ["agent减仓: 回撤达到2.9%，适当减仓降低风险"])
        kinds = self._kinds(["agent卖出: 逻辑证伪", "agent减仓: 回撤过大"])
        assert "agent 清仓 1" in kinds
        assert "agent 减仓 1" in kinds


class TestTheReplayFeedsTheAgentItsPeak:
    """峰值 / 回撤 / 持仓天数 were dead data in a replay.

    `position_monitor.check_positions` maintains `peak_return_pct` and
    `holding_days` in production, but a replay never runs that module —
    `build_context` rendered 峰值0% for every position on every day, and
    every drawdown figure in the agent's reasons was its own arithmetic on
    the current price rather than a fact the system had handed it. The
    helper `_fill_replay_peak_fields` computes them as-of the decision,
    from closes only.
    """

    @staticmethod
    def _fill_helper():
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        return wf

    def test_the_fill_days_own_close_counts(self):
        """A position filled at Tuesday's open has Tuesday's close behind
        it by any later 09:00 — T+1 is exactly why it could not have been
        sold first. Starting the series after the fill day made a position
        bought the previous session show a peak of zero on the first day
        the agent could act on it."""
        # open phase on 07-03: T-1's close is 07-02's 11.0 → peak +10%
        pos = {"code": "600001", "open_price": 10.0,
               "open_date": "2025-07-02"}
        wf = self._fill_helper()
        wf._fill_replay_peak_fields(
            _CtxStub(closes={"2025-07-01": 10.0, "2025-07-02": 11.0,
                             "2025-07-03": 9.5}),
            [pos], "2025-07-03", "open")
        assert pos["peak_return_pct"] == 10.0

    @staticmethod
    def _fill_helper():
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        return wf

    def test_the_open_phase_never_sees_todays_close(self):
        wf = self._fill_helper()
        pos = {"code": "600001", "open_price": 10.0,
               "open_date": "2025-07-01"}
        # close phase would see 11.0 (peak 10%), open phase must not
        wf._fill_replay_peak_fields(_CtxStub(closes={"2025-07-01": 10.0,
                                                     "2025-07-02": 11.0}),
                                    [pos], "2025-07-02", "open")
        assert pos["peak_return_pct"] == 0.0
        # ...but the close phase does
        pos2 = {"code": "600001", "open_price": 10.0,
                "open_date": "2025-07-01"}
        wf._fill_replay_peak_fields(_CtxStub(closes={"2025-07-01": 10.0,
                                                     "2025-07-02": 11.0}),
                                    [pos2], "2025-07-02", "close")
        assert pos2["peak_return_pct"] == 10.0

    def test_holding_days_is_calendar_days_to_the_decision(self):
        wf = self._fill_helper()
        pos = {"code": "600001", "open_price": 10.0,
               "open_date": "2025-07-01"}
        wf._fill_replay_peak_fields(_CtxStub(closes={"2025-07-01": 10.0,
                                                     "2025-07-04": 10.5}),
                                    [pos], "2025-07-04", "open")
        assert pos["holding_days"] == 3


def _CtxStub(closes: dict[str, float] | None = None):
    """A two-method stand-in for the runner's context.

    ``_fill_replay_peak_fields`` reads ``corpus.previous`` and
    ``corpus.bars``; a stub keeps the peak tests about the arithmetic
    rather than about the corpus loader.
    """
    closes = closes or {}

    class _Corpus:
        def __init__(self):
            self.days = sorted(closes)
            self.index = {d: i for i, d in enumerate(self.days)}
            self._bars = {d: {"600001": {"open": c, "high": c, "low": c,
                                         "close": c}}
                          for d, c in closes.items()}

        def bars(self, day):
            return self._bars.get(day, {})

        def previous(self, day):
            i = self.index.get(day)
            if i is None or i == 0:
                return None
            return self.days[i - 1]

    class _Ctx:
        corpus = _Corpus()

    return _Ctx()


class TestTheAgentIsNotAskedAboutUnsellablePositions:
    """T+1: shares bought today cannot be sold today.

    Measured on a real run — two of four agent decisions were trims of a
    position bought the previous session, and both were refused by the exit
    path with "position #11 has only 0 settled shares today". The refusal is
    the correct backstop; the defect is that the question was asked at all.
    A model call whose only possible answer is "no" is a wasted call and a
    decision that can only be discarded.
    """

    def test_a_position_bought_today_is_not_offered(self):
        from alpha_agents.data import t1_settlement as S
        assert S.may_sell("2026-08-26", "2026-08-27") is True
        assert S.may_sell("2026-08-27", "2026-08-27") is False

    def test_the_filter_is_in_place_where_the_step_asks(self):
        """A grep rather than a run: driving the real step needs a corpus,
        a book and a model, and the property is that the guard is on the
        path at all."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._agent_exits)
        assert "may_sell" in src, (
            "the exit step does not filter by T+1, so it will ask about "
            "positions whose settled shares are zero")
        assert "agent_exit_t_plus_one" in src, (
            "the filter is silent; a run must report how many positions it "
            "held back")



class TestATrimIsALotOrItIsNotATrim:
    """`int(held * frac)` is not lot-aligned, and the exit path refuses a
    partial that is not a whole lot.

    Measured on a real run: a 7000-share position trimmed to 3500 fine, then
    the next trim asked for 1750 — refused, and the decision was silently
    dropped. The agent had decided; the arithmetic threw it away.
    """

    def _trim(self, monkeypatch, held, fraction=None):
        calls = {}

        def _fake_close(position_id, *, close_price, close_reason, shares=None):
            calls["shares"] = shares
            return True
        monkeypatch.setattr(ED, "close_position", _fake_close)
        pos = {"id": 1, "code": "600001", "name": "甲", "shares": held,
               "open_price": 10.0}
        d = {"code": "600001", "action": "trim", "reason": "减仓"}
        if fraction is not None:
            d["fraction"] = fraction
        out = ED._apply_trim(pos, 11.0, d)
        return calls.get("shares"), out

    def test_the_sale_is_lot_aligned_at_the_default_fraction(
            self, monkeypatch):
        """3500 * 0.5 = 1750, floored to the lot = 1700. The un-aligned
        value was 1750, which the exit path refuses."""
        shares, out = self._trim(monkeypatch, 3500)
        assert shares == 1700, f"expected 1700 (a whole lot), got {shares}"
        assert out is not None

    def test_a_seven_thousand_share_position_trims_to_thirty_five_hundred(
            self, monkeypatch):
        shares, _ = self._trim(monkeypatch, 7000)
        assert shares == 3500

    def test_the_remainder_is_always_a_whole_lot_too(self, monkeypatch):
        for held in (1000, 1500, 2900, 3500, 7000, 12300):
            shares, _ = self._trim(monkeypatch, held)
            assert shares % ED.LOT_SIZE == 0, (held, shares)
            assert (held - shares) % ED.LOT_SIZE == 0, (held, shares)

    def test_below_one_lot_it_holds_rather_than_selling_everything(
            self, monkeypatch):
        """A trim that cannot reach a lot is not a disguised full exit."""
        shares, out = self._trim(monkeypatch, 100)
        assert out is None

    def test_a_fraction_that_would_leave_nothing_is_not_a_trim(
            self, monkeypatch):
        """0.9 of 100 is 90 — below a lot. Selling the 100 instead would
        misreport what the agent decided."""
        shares, out = self._trim(monkeypatch, 100, fraction=0.9)
        assert out is None

    def test_the_headline_fraction_is_honoured_where_it_can_be(
            self, monkeypatch):
        shares, _ = self._trim(monkeypatch, 10000, fraction=0.3)
        assert shares == 3000


class TestTheAlertTypeIsCountedOnce:
    """The alert type already carries the `agent_` prefix.

    The first version prefixed it again, so the counter read
    `agent_agent_trim` — a key no reader would look for and the report never
    printed. A trim that succeeded therefore looked like no activity at all.
    """

    def test_the_types_the_executor_returns(self):
        import inspect
        src = inspect.getsource(ED._apply_trim)
        assert '"agent_trim"' in src
        src = inspect.getsource(ED._apply_sell)
        assert '"agent_exit"' in src

    def test_the_counter_does_not_double_the_prefix(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import walk_forward as wf
        src = inspect.getsource(wf._agent_exits)
        assert 'f"agent_{a.get' not in src, (
            "the counter prefixes a type that already has the prefix, so "
            "`agent_trim` is recorded as `agent_agent_trim`")
        assert "startswith(\"agent_\")" in src


class TestTheNoSafetyNetExperiment:
    """Disabling the mechanical exits asks one question: does the agent sell?

    The stop is the line that keeps a wrong call from becoming a blown
    account. Removing it is only safe in a replay, and the arm is only
    meaningful if two things hold:

    * the agent is told the truth — if it believes a stop sits underneath it
      will answer as though something else will save it, and the answer stops
      being a judgement about selling;
    * something can still exit — with no stop, no target and no agent, every
      fill stays open and the curve measures drift rather than decisions.
    """

    def test_the_context_says_there_is_no_safety_net(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={}, own_orders=False)
        assert "没有安全网" in ctx
        assert "卖不卖完全由你决定" in ctx

    def test_it_does_not_claim_a_hard_line_it_will_not_enforce(self):
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={}, own_orders=False)
        assert "系统强制平仓" not in ctx

    def test_the_default_context_says_only_own_levels_execute(self):
        """There is no system line any more, in either arm. The default says
        the agent's own stop/target execute and nothing else does."""
        ctx = ED.build_context(POSITIONS, PRICES, signals=[],
                               news_by_theme={})
        assert "没有系统止损" in ctx and "自己设的" in ctx
        assert "风控硬线" not in ctx and "系统强制平仓" not in ctx
        assert "自设价位" in ctx

    def test_the_flag_reaches_the_context(self, monkeypatch):
        seen = {}

        async def _fake_decide(context, trader=None, **kw):
            seen["context"] = context
            return []

        monkeypatch.setattr(ED, "decide", _fake_decide)
        asyncio.run(ED.decide_for_replay(
            POSITIONS, PRICES, day="2026-01-05", news_by_theme={},
            model=object(), own_orders=False))
        assert "没有安全网" in seen["context"]


class TestAWindowWithNoExitIsRefused:
    """`--no-mechanical-exits` without `--agent-exits` leaves nothing able to
    close a position: every fill stays open and the equity curve measures the
    window's drift rather than any decision. That is a missing feature, not
    an experiment, so it is refused rather than run and explained after."""

    def _args(self, **over):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        base = {"mechanical_stop": False, "mechanical_target": False,
                "agent_exits": False}
        base.update(over)
        return wf, type("A", (), base)()

    def test_it_refuses_without_an_agent(self):
        wf, args = self._args()
        with pytest.raises(SystemExit, match="nothing can close a position"):
            wf._assert_an_exit_exists(args)

    def test_it_allows_the_experiment_arm(self):
        wf, args = self._args(mechanical_stop=False,
                              mechanical_target=False, agent_exits=True)
        wf._assert_an_exit_exists(args)

    def test_the_default_is_unaffected(self):
        wf, args = self._args(mechanical_stop=True, mechanical_target=True,
                              agent_exits=False)
        wf._assert_an_exit_exists(args)

    def test_hiding_the_levels_is_what_removes_the_exit(self):
        """`exit_verdict` reads the *position's* stop, so passing None is
        exactly the mechanical exit and leaves T+1, price limits, suspension
        and gap handling intact — a constant would not."""
        from alpha_agents.data import market_rules, t1_settlement as S
        rule = market_rules.market_rules("600001", "2026-01-05", name="甲")
        # A quiet session: -2% open, so no price limit is involved and the
        # only thing that can close the position is its own stop level.
        bar = S.DayBar(date="2026-01-05", open=9.8, high=10.2, low=9.4,
                       close=9.9)
        with_levels = S.exit_verdict(bar=bar, prev_close=10.0, rule=rule,
                                     stop_loss=9.5, target_price=None)
        without = S.exit_verdict(bar=bar, prev_close=10.0, rule=rule,
                                 stop_loss=None, target_price=None)
        assert with_levels.status == S.FILLED_AT_OPEN
        assert without.status == S.NO_FILL
        assert "no stop and no target" in without.reason
