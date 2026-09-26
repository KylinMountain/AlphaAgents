"""The T+1 decider: what it may choose from, and what it does with a bad answer.

The decider is the one place in the replay where something outside the
repository — a sampled model — decides what gets bought. So the tests here are
about the two things that can go wrong silently:

* it names a security the decision was never shown, which is the selection-side
  universe leak (``TestOnlyThePanelIsBuyable``);
* its reply is unreadable, and an unreadable reply is counted as "it chose
  nothing" (``TestAnUnreadableReplyIsNotAnEmptyOne``).

Both are the same failure shape this repository keeps paying for: a wrong
answer that looks like a normal one.
"""

import asyncio

import pytest

from alpha_agents.agents import t1_decider as D
from alpha_agents.config import PROMPTS_DIR

PANEL = [
    {"code": "600001", "name": "甲", "close": 10.0, "change_pct": 3.0,
     "adv20": 1_000_000},
    {"code": "600002", "name": "乙", "close": 20.0, "change_pct": 1.0,
     "adv20": 500_000},
]
CODES = {row["code"] for row in PANEL}


class TestOnlyThePanelIsBuyable:
    """The panel is the whole universe rule, so it has to be enforced on the
    answer rather than merely stated in the prompt. A model asked about a 2020
    window has read 2026 and will name a security that had not listed yet."""

    def test_a_code_outside_the_panel_is_refused(self):
        verdict = D.parse_orders(
            '{"orders":[{"code":"300999","entry_low":9.0,"entry_high":9.5,'
            '"stop_loss":8.0,"reason":"突破"}]}', CODES)
        assert verdict["orders"] == []
        assert [r["why"] for r in verdict["refused"]] == ["outside_panel"]
        assert verdict["refused"][0]["code"] == "300999"

    def test_a_code_inside_the_panel_is_kept(self):
        """The pair for the test above: without this one, refusing everything
        would satisfy 'a code outside the panel is refused'."""
        verdict = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":9.8,"entry_high":10.2,'
            '"stop_loss":9.2,"reason":"回调到支撑"}]}', CODES)
        assert verdict["refused"] == []
        assert [o["code"] for o in verdict["orders"]] == ["600001"]
        assert verdict["orders"][0]["entry_low"] == 9.8

    def test_the_panel_is_what_the_model_was_shown(self):
        """The refused set and the rendered set are the same set. If they
        drifted, the model would be told one thing and judged by another."""
        rendered = D.format_panel(PANEL)
        for code in CODES:
            assert code in rendered
        assert "300999" not in rendered


class TestAMalformedOrderIsNamedNotDropped:
    """Each refusal carries a reason, because 'three orders came back and one
    was placed' is not a fact anyone can act on."""

    @pytest.mark.parametrize("body,why", [
        ('{"orders":[{"code":"600001","entry_low":10.2,"entry_high":9.8,'
         '"stop_loss":9.2}]}', "inverted_zone"),
        ('{"orders":[{"code":"600001","entry_low":10.0,"entry_high":11.0,'
         '"stop_loss":10.5}]}', "stop_not_below_entry"),
        ('{"orders":[{"code":"600001","entry_low":0,"entry_high":1.0,'
         '"stop_loss":-1.0}]}', "non_positive_price"),
        # entry_high is the one price an order cannot do without: it is the
        # most the trader will pay. (entry_low became optional on 2026-09-23.)
        ('{"orders":[{"code":"600001","entry_low":10.0,"stop_loss":9.2}]}',
         "bad_prices"),
        ('{"orders":[{"code":"600001","entry_low":null,"entry_high":10.0,'
         '"stop_loss":10.0}]}', "stop_not_below_entry"),
        ('{"orders":["600001"]}', "not_an_object"),
    ])
    def test_the_reason_is_returned(self, body, why):
        verdict = D.parse_orders(body, CODES)
        assert verdict["orders"] == []
        assert [r["why"] for r in verdict["refused"]] == [why]


class TestAnOrderIsTheMostItWillPay:
    """No forced band. The two-sided zone was a shape the system imposed, and
    it refused the cheapest fills: a gap *down* under the floor did not buy.
    "Not above X" is a whole order; a floor is the trader's choice."""

    @pytest.mark.parametrize("low", ['null', '""', None])
    def test_a_ceiling_alone_is_accepted(self, low):
        field = '' if low is None else f'"entry_low":{low},'
        body = ('{"orders":[{"code":"600001",' + field +
                '"entry_high":10.2,"stop_loss":9.2}]}')
        verdict = D.parse_orders(body, CODES)
        assert verdict["refused"] == []
        [order] = verdict["orders"]
        assert order["entry_low"] is None and order["entry_high"] == 10.2

    def test_a_gap_down_fills_a_ceiling_only_order(self):
        from alpha_agents.data.t1_execution import in_entry_zone
        assert in_entry_zone(9.5, None, 10.2)
        assert not in_entry_zone(10.3, None, 10.2)

    def test_a_floor_the_trader_chose_is_still_honoured(self):
        verdict = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.5}]}', CODES)
        assert verdict["orders"][0]["entry_low"] == 10.0


class TestAnUnreadableReplyIsNotAnEmptyOne:
    """An explained empty order list is an abstention. An unexplained one is
    incomplete: we do not know why it chose nothing. Collapsing the two is how a
    decider that never worked looks like a decider that is being careful."""

    def test_an_empty_order_list_parses_with_no_error(self):
        verdict = D.parse_orders('{"orders": [], "no_trade_reason": "No executable entry"}', CODES)
        assert verdict["orders"] == []
        assert verdict["parse_error"] is None

    def test_prose_is_a_parse_error(self):
        verdict = D.parse_orders("I would buy 600001 today.", CODES)
        assert verdict["parse_error"] is not None
        assert verdict["orders"] == []

    def test_an_empty_reply_is_a_parse_error(self):
        assert D.parse_orders("", CODES)["parse_error"] == "empty reply"

    def test_a_missing_orders_key_is_a_parse_error(self):
        verdict = D.parse_orders('{"picks": []}', CODES)
        assert verdict["parse_error"] is not None

    def test_a_fenced_reply_is_read(self):
        """A fence is formatting, not a different answer. Not stripping it
        would turn every fenced reply into the silent case above."""
        verdict = D.parse_orders('```json\n{"orders": [], "no_trade_reason": "No entry"}\n```', CODES)
        assert verdict["parse_error"] is None


class TestThePromptCannotShipAHole:
    def test_a_doubled_brace_is_caught_after_rendering(self):
        """``{{x}}`` renders as a literal ``{x}``, so ``format`` succeeds and
        the model is shown braces. This is the case the post-render check is
        for; the missing-key case is caught by ``format`` itself (below)."""
        with pytest.raises(D.DeciderError, match="doubled brace"):
            D.build_message(day="d", prev_day="p", panel=PANEL, news=[],
                            book="", knowledge="", trader_note="", picks=1,
                            template="今天是 {{SOMETHING_ELSE}}")

    def test_the_real_template_renders(self):
        message = D.build_message(
            day="2025-07-01", prev_day="2025-06-30", panel=PANEL, news=[],
            book="", knowledge="", trader_note="", picks=2,
            template=D.load_prompt())
        assert "2025-07-01" in message and "2025-06-30" in message
        assert "600001" in message
        # The empty states are stated rather than left blank, so a reader of
        # the prompt can tell "nothing" from "we forgot to fill it in".
        assert "没有读到任何快讯" in message
        assert "空仓" in message

    def test_a_toolless_decision_does_not_advertise_unavailable_tools(self):
        message = D.build_message(
            day="2025-07-01", prev_day="2025-06-30", panel=PANEL, news=[],
            book="", knowledge="", trader_note="", picks=2,
            template=D.load_prompt(), tools_enabled=False)

        assert "本轮没有可调用工具" in message
        assert "get_market_regime" not in message

    def test_the_runner_timeout_reaches_the_decision_call(self, monkeypatch):
        seen = {}

        async def fake_run(frame, *, model, tools, budget, timeout=None,
                           attempts=None):
            seen["timeout"] = timeout
            return {"orders": [], "watch": [], "rejected": [],
                    "refused": [], "parse_error": None,
                    "no_trade_reason": "没有足够的入场证据"}

        monkeypatch.setattr(D, "_run_frame", fake_run)
        import asyncio
        from types import SimpleNamespace

        asyncio.run(D.propose(
            day="2025-07-01", prev_day="2025-06-30", panel=PANEL, news=[],
            book="", knowledge="", trader_note="", picks=2,
            template=D.load_prompt(), model=SimpleNamespace(model="stub"),
            timeout=17.5))

        assert seen["timeout"] == 17.5

    def test_the_news_actually_reaches_the_prompt(self):
        """The template claimed "news up to 09:00 today" and then did not
        contain the news: ``format_news`` was never called, so the model chose
        from the panel alone. Found by a 40-day run dying with
        ``KeyError: 'news'`` the moment the template gained a ``{news}``
        section — the run had been news-free for twelve days before that and
        nothing said so."""
        message = D.build_message(
            day="2025-07-01", prev_day="2025-06-30", panel=PANEL,
            news=[{"time": "2025-07-01 08:30", "source": "财联社",
                   "title": "工信部加快推进算力建设"}],
            book="", knowledge="", trader_note="", picks=2,
            template=D.load_prompt())
        assert "工信部加快推进算力建设" in message
        assert "财联社" in message
        assert "没有读到任何快讯" not in message

    def test_sector_trade_plan_receives_frozen_research_packet(self):
        packet = {
            "version": 1,
            "packet_hash": "abc",
            "stocks": [{
                "code": "600001",
                "selector_reason": "相对更强",
                "counterevidence": "位置偏高",
                "tool_facts": [{
                    "tool": "get_stock_context",
                    "payload": {"atr_pct": 3.2, "structure": "above_ma20"},
                }],
            }],
        }
        template = (PROMPTS_DIR / "sector_trade_plan.md").read_text(
            encoding="utf-8")
        message = D.build_message(
            day="2025-07-01", prev_day="2025-06-30", panel=PANEL, news=[],
            book="", knowledge="", trader_note="", picks=1,
            template=template, research_packet=packet)

        assert '"selector_reason": "相对更强"' in message
        assert '"atr_pct": 3.2' in message
        assert "上游冻结研究包" in message

    def test_a_key_the_renderer_does_not_supply_names_itself(self):
        """``str.format`` raises ``KeyError`` before any post-hoc check can
        run, so the guard has to wrap the call. The first version did not, and
        the error a reader saw was a bare ``KeyError`` from deep inside a
        template engine rather than the name of the placeholder."""
        with pytest.raises(D.DeciderError) as caught:
            D.build_message(day="d", prev_day="p", panel=PANEL, news=[],
                            book="", knowledge="", trader_note="", picks=1,
                            template="{a_field_nobody_supplies}")
        assert "a_field_nobody_supplies" in str(caught.value)
        assert "news" in str(caught.value), "and says what it did supply"


class TestTheCallingLoopOutlivesTheClient:
    """A client built once must be driven by one loop, and the runner broke
    that by calling ``asyncio.run`` per simulated day.

    ``Context.model`` is built once for a whole window, so a single
    ``AsyncOpenAI`` — and the httpx connection pool inside it — is shared by
    every day. httpx pools connections bound to the loop that opened them, so
    a fresh loop per day handed that day's first request a connection whose
    loop was already closed: a transport failure with no status at all, which
    the SDK's own retry then answered on the second attempt.

    Measured on two 120-day windows before the fix: 120 and 121 ``Retrying
    request`` lines, of which **119 in each** were this defect and the rest were
    provider stalls after the client timeout. 119 is ``120 days - 1`` — day one
    has nothing pooled to trip over — and the two windows agree on it
    independently. ``Context.loop`` is now one loop for the window.

    **The criterion is not "0 retries"**, and that was the first thing written
    down and the first thing to be wrong: ``--model-timeout 120`` exists because
    a throttled free tier sometimes queues a request instead of answering
    ``429``, so a stall-then-retry is a different cause that the fix does not and
    should not remove. The two separate by the gap to the preceding log line —
    a stale connection fails *instantly*, a stall takes the whole timeout. Across
    the three measured windows the stale class tops out at **3.098 s** (237 of
    its 238 members are <= 0.03 s) and the stall class bottoms out at
    **87.739 s**, so the criterion is **0 retries with a gap under 30 s** — a
    threshold placed in that measured 85-second empty band rather than picked for
    how it sounds. The first version said "sub-second", which would have counted
    one window's 119 stale retries as 118 and passed on the other: a threshold
    that fails on the evidence it was written for.

    What is pinned here is the contract: the loop handed in is the loop every
    call runs on, and it is still open at the end. The transport half of the
    claim — that a loop-bound connection really does break when the loop
    changes — is **not** covered by a test, because ``tests/conftest.py``
    installs an audit hook that refuses ``socket.bind`` and ``socket.connect``
    on ``AF_INET`` outright. It is measured instead by
    ``scripts/probe_loop_binding.py``, which runs the same client both ways
    against a loopback endpoint and prints both outcomes.
    """

    def test_every_call_runs_on_the_loop_it_was_given(self, monkeypatch):
        seen = []

        async def _fake(**_kwargs):
            seen.append(asyncio.get_running_loop())
            return {"orders": [], "refused": [], "parse_error": None}

        monkeypatch.setattr(D, "propose", _fake)
        loop = asyncio.new_event_loop()
        try:
            for _ in range(3):
                D.propose_sync(loop=loop)
            # Still open after three calls. A loop that were closed between
            # them would give the same ``id`` and still be the defect.
            assert not loop.is_closed()
        finally:
            loop.close()
        assert [id(one) for one in seen] == [id(loop)] * 3

    def test_without_a_loop_each_call_gets_its_own(self, monkeypatch):
        """The contrast, and the whole reason the parameter exists: this is
        the behaviour that was wrong — one client, three loops."""
        seen = []

        async def _fake(**_kwargs):
            seen.append(asyncio.get_running_loop())
            return {"orders": [], "refused": [], "parse_error": None}

        monkeypatch.setattr(D, "propose", _fake)
        for _ in range(3):
            D.propose_sync()
        assert len({id(one) for one in seen}) == 3

    def test_a_supplied_loop_is_not_the_default(self, monkeypatch):
        """Passing a loop changes the answer, so the two paths are not
        accidentally the same code. Without this, ``loop`` could be accepted
        and ignored and both tests above would still pass."""
        seen = []

        async def _fake(**_kwargs):
            seen.append(asyncio.get_running_loop())
            return {"orders": [], "refused": [], "parse_error": None}

        monkeypatch.setattr(D, "propose", _fake)
        loop = asyncio.new_event_loop()
        try:
            D.propose_sync(loop=loop)
            D.propose_sync()
        finally:
            loop.close()
        assert seen[0] is loop
        assert seen[1] is not loop


class TestATargetIsOptionalButChecked:
    """`target_price` is the difference between one exit and two.

    Every order in the first 20-day replay had a stop and no target, so the
    only way out of a position was the stop — and all 11 trades took it. The
    field was accepted by the order layer and dropped by the runner, so this
    is about the parser's half of that seam.
    """

    def test_a_target_above_the_entry_is_kept(self):
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"target_price":12.0,"reason":"主线"}]}', CODES)
        assert v["orders"][0]["target_price"] == 12.0

    def test_omitting_it_is_allowed_and_recorded_as_none(self):
        """Optional, and its absence is a fact worth carrying rather than an
        error: it says the position has exactly one exit."""
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"reason":"主线"}]}', CODES)
        assert v["orders"][0]["target_price"] is None

    def test_a_target_below_the_entry_ceiling_is_refused(self):
        """A target at or under the fill ceiling is not a target: the order
        would exit at a price it could have been filled at."""
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"target_price":10.2,"reason":"x"}]}', CODES)
        assert v["orders"] == []
        assert v["refused"][0]["why"] == "target_not_above_entry"

    def test_a_target_equal_to_the_ceiling_is_refused(self):
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"target_price":10.5,"reason":"x"}]}', CODES)
        assert v["refused"][0]["why"] == "target_not_above_entry"

    def test_an_unparseable_target_is_refused_by_name(self):
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"target_price":"涨一倍","reason":"x"}]}', CODES)
        assert v["refused"][0]["why"] == "bad_target"

    def test_an_empty_string_means_no_target(self):
        """Some models emit `""` for an absent optional field rather than
        omitting it; that is an omission, not a bad number."""
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":9.0,"target_price":"","reason":"x"}]}', CODES)
        assert v["orders"][0]["target_price"] is None

    def test_the_prompt_offers_both_levels_as_optional(self):
        """The template is part of the contract: a field the runner reads but
        the prompt never mentions is one the model will not emit. Both levels
        are the trader's choice, and the prompt must not promise a system line
        that does not exist."""
        text = D.load_prompt()
        assert "target_price" in text and "stop_loss" in text
        assert "可选" in text
        assert "系统没有任何止损或止盈线" in text


class TestAStopIsTheTradersChoice:
    """The system no longer demands a stop: a demanded stop is the system's
    exit written in the trader's hand."""

    def test_an_order_without_a_stop_is_accepted(self):
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"reason":"主线"}]}', CODES)
        assert v["refused"] == []
        assert v["orders"][0]["stop_loss"] is None

    def test_null_and_empty_mean_no_stop(self):
        for raw in ('null', '""'):
            v = D.parse_orders(
                '{"orders":[{"code":"600001","entry_high":10.5,'
                f'"stop_loss":{raw},"reason":"x"}}]}}', CODES)
            assert v["orders"][0]["stop_loss"] is None, raw

    def test_a_stop_it_does_write_is_still_checked(self):
        v = D.parse_orders(
            '{"orders":[{"code":"600001","entry_low":10.0,"entry_high":10.5,'
            '"stop_loss":10.2,"reason":"x"}]}', CODES)
        assert v["refused"][0]["why"] == "stop_not_below_entry"


class TestTheBookShowsWhetherAPositionIsUp:
    """`inject_portfolio` renders `现价未提供` unless a price map is passed.

    The replay's agent was asked to decide holds and sells while unable to see
    its own P&L. This pins the seam, not the formatting."""

    def test_the_summary_says_so_when_no_price_is_supplied(self):
        from alpha_agents.data import portfolio_report as PR
        line = PR._position_line(
            {"code": "600001", "name": "甲", "shares": 100,
             "open_price": 10.0, "stop_loss": 9.0, "holding_days": 1},
            None)
        assert "现价未提供" in line
        assert "成本1,000元" in line
        assert "市值" not in line.split("现价未提供")[0]

    def test_the_cost_label_is_not_called_market_value(self):
        """The defect: the line read `(市值1,000元)` while computing
        `open_price * shares`. A field named 市值 that reports cost is worse
        than no field, because it reads as a fact about the market."""
        from alpha_agents.data import portfolio_report as PR
        line = PR._position_line(
            {"code": "600001", "name": "甲", "shares": 100,
             "open_price": 10.0, "stop_loss": 9.0, "holding_days": 1},
            None)
        assert "成本1,000元" in line

    def test_a_price_shows_the_mark_and_the_pnl(self):
        from alpha_agents.data import portfolio_report as PR
        line = PR._position_line(
            {"code": "600001", "name": "甲", "shares": 100,
             "open_price": 10.0, "stop_loss": 9.0, "holding_days": 1},
            {"600001": 11.0})
        assert "现价11.00" in line and "市值1,100元" in line
        assert "+100元(+10.00%)" in line

    def test_a_loss_is_signed(self):
        from alpha_agents.data import portfolio_report as PR
        line = PR._position_line(
            {"code": "600001", "name": "甲", "shares": 100,
             "open_price": 10.0, "stop_loss": 9.0, "holding_days": 1},
            {"600001": 9.5})
        assert "-50元(-5.00%)" in line

    def test_the_target_is_rendered_when_present(self):
        from alpha_agents.data import portfolio_report as PR
        line = PR._position_line(
            {"code": "600001", "name": "甲", "shares": 100,
             "open_price": 10.0, "stop_loss": 9.0, "target_price": 12.0,
             "holding_days": 1}, None)
        assert "目标12.0" in line


class TestWaitIsAFirstClassDecision:
    def test_wait_parses_without_global_no_trade_reason(self):
        verdict = D.parse_orders(
            '{"orders":[],"watch":[{"code":"600001","reason":"等回踩",'
            '"confidence":0.7,"next_check":[{"metric":"price","op":"<=",'
            '"value":9.8,"subject":"600001"}],"invalidations":[]}]}',
            CODES,
        )
        assert verdict["parse_error"] is None
        assert verdict["decision_status"] == "waiting"
        assert verdict["orders"] == []
        assert verdict["watch"] == [{
            "code": "600001",
            "reason": "等回踩",
            "confidence": 0.7,
            "next_check": [{
                "metric": "price", "op": "<=", "value": 9.8,
                "subject": "600001",
            }],
            "invalidations": [],
        }]

    def test_wait_requires_a_machine_checkable_trigger(self):
        verdict = D.parse_orders(
            '{"orders":[],"watch":[{"code":"600001","reason":"再等等",'
            '"next_check":[],"invalidations":[]}]}',
            CODES,
        )
        assert verdict["decision_status"] == "incomplete"
        assert verdict["refused"][0]["why"] == "invalid_watch"

    def test_wait_condition_operator_is_validated(self):
        verdict = D.parse_orders(
            '{"orders":[],"watch":[{"code":"600001","reason":"等",'
            '"next_check":[{"metric":"price","op":"around","value":10}],'
            '"invalidations":[]}]}',
            CODES,
        )
        assert verdict["decision_status"] == "incomplete"
        assert verdict["refused"] == [{
            "code": "600001", "why": "invalid_watch",
            "detail": "invalid condition or confidence",
        }]

    def test_wait_cannot_name_a_stock_outside_the_panel(self):
        verdict = D.parse_orders(
            '{"orders":[],"watch":[{"code":"300999","reason":"等",'
            '"next_check":[{"metric":"price","op":"<=","value":10}],'
            '"invalidations":[]}]}',
            CODES,
        )
        assert verdict["decision_status"] == "incomplete"
        assert verdict["refused"][0]["why"] == "invalid_watch"

    def test_invalid_wait_does_not_discard_a_valid_buy(self):
        verdict = D.parse_orders(
            '{"orders":[{"code":"600001","entry_high":10.2,'
            '"stop_loss":9.2,"reason":"买"}],'
            '"watch":[{"code":"600002","reason":"等",'
            '"next_check":[{"metric":"price","op":"<=","value":19}],'
            '"cancel_if":[{"metric":"net_flow","op":"<","value":0}]}]}',
            CODES,
        )
        assert verdict["parse_error"] is None
        assert [order["code"] for order in verdict["orders"]] == ["600001"]
        assert verdict["watch"] == []
        assert verdict["refused"] == [{
            "code": "600002", "why": "invalid_watch",
            "detail": "invalid condition or confidence",
        }]

    def test_invalid_rejection_does_not_discard_other_valid_actions(self):
        verdict = D.parse_orders(
            '{"orders":[{"code":"600001","entry_high":10.2,'
            '"stop_loss":9.2,"reason":"买"}],'
            '"watch":[{"code":"600002","reason":"等",'
            '"next_check":[{"metric":"price","op":"<=","value":19}],'
            '"cancel_if":[]}],'
            '"rejected":[{"code":"300999","reason":"面板外","rule_ids":[]}]}',
            CODES,
        )
        assert verdict["parse_error"] is None
        assert [order["code"] for order in verdict["orders"]] == ["600001"]
        assert [item["code"] for item in verdict["watch"]] == ["600002"]
        assert verdict["rejected"] == []
        assert verdict["refused"] == [{
            "code": "300999", "why": "invalid_rejected",
            "detail": "rejected needs a unique panel code, reason and rule_ids list",
        }]

    @pytest.mark.parametrize("other", ["orders", "rejected"])
    def test_same_code_cannot_be_wait_and_another_action(self, other):
        if other == "orders":
            body = (
                '{"orders":[{"code":"600001","entry_high":10.2,'
                '"stop_loss":9.2,"reason":"买"}],'
                '"watch":[{"code":"600001","reason":"等",'
                '"next_check":[{"metric":"price","op":"<=","value":9.8}],'
                '"invalidations":[]}]}'
            )
        else:
            body = (
                '{"orders":[],"no_trade_reason":"不立即买",'
                '"watch":[{"code":"600001","reason":"等",'
                '"next_check":[{"metric":"price","op":"<=","value":9.8}],'
                '"invalidations":[]}],'
                '"rejected":[{"code":"600001","reason":"同时拒绝","rule_ids":[]}]}'
            )
        verdict = D.parse_orders(body, CODES)
        assert verdict["decision_status"] == "incomplete"
        assert "simultaneously" in verdict["parse_error"]

    def test_plain_empty_orders_still_need_a_reason(self):
        verdict = D.parse_orders('{"orders":[]}', CODES)
        assert verdict["decision_status"] == "incomplete"
        assert "no_trade_reason" in verdict["parse_error"]

    def test_prompt_teaches_wait_as_distinct_from_reject(self):
        text = D.load_prompt()
        assert '"watch"' in text
        assert "WAIT" in text
        assert "next_check" in text
        assert "orders / watch / rejected" in text
        assert "\"net_flow\"" not in text


class TestRuntimeDecisionTranslation:
    def _context(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from alpha_agents.trader import (
            DecisionContext, DecisionHorizon, EvidenceScope, Session, Timeframe,
        )
        return DecisionContext(
            mode="live",
            observation_resolution=Timeframe.DAILY,
            decision_horizon=DecisionHorizon.SWING,
            session=Session.PRE_OPEN,
            information_cutoff=datetime(
                2026, 9, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            evidence_scope=EvidenceScope.LIVE_DAILY,
        )

    def test_buy_wait_reject_map_to_distinct_runtime_actions(self):
        from alpha_agents.trader import Action
        verdict = D.parse_orders(
            '{"orders":[{"code":"600001","entry_high":10.2,'
            '"stop_loss":9.2,"reason":"现在买","conviction":0.8}],'
            '"watch":[{"code":"600002","reason":"等回踩","confidence":0.7,'
            '"next_check":[{"metric":"price","op":"<=","value":19.0}],'
            '"invalidations":[]}],'
            '"rejected":[]}',
            CODES,
        )
        verdict["decision_id"] = "capture-1"
        decisions = D.to_runtime_decisions(verdict, self._context())
        assert [item.action for item in decisions] == [Action.BUY, Action.WAIT]
        assert decisions[0].code == "600001"
        assert decisions[0].confidence == 0.8
        assert decisions[1].code == "600002"
        assert decisions[1].next_check[0].metric == "price"

    def test_pure_abstention_becomes_global_hold(self):
        from alpha_agents.trader import Action
        verdict = D.parse_orders(
            '{"orders":[],"watch":[],"no_trade_reason":"市场太弱",'
            '"rejected":[]}',
            CODES,
        )
        verdict["decision_id"] = "capture-2"
        [decision] = D.to_runtime_decisions(verdict, self._context())
        assert decision.action == Action.HOLD
        assert decision.code is None
        assert decision.reasoning == "市场太弱"

    def test_unreadable_reply_cannot_enter_runtime(self):
        verdict = D.parse_orders("not json", CODES)
        verdict["decision_id"] = "capture-3"
        with pytest.raises(D.DeciderError, match="unreadable"):
            D.to_runtime_decisions(verdict, self._context())


class TestExactDecisionCutoff:
    def test_preopen_cutoff_is_rendered_into_the_prompt(self):
        text = D.build_message(
            day="2026-09-25", prev_day="2026-09-24",
            panel=PANEL, news=[], book="", knowledge="",
            trader_note="", picks=1, template=D.load_prompt(),
            phase="open", decision_time="2026-09-25 09:05:00")
        assert "2026-09-25 09:05:00" in text

    def test_open_cutoff_at_or_after_0930_is_refused(self):
        with pytest.raises(D.DeciderError, match="before 09:30"):
            D._validate_information_cutoff(
                "2026-09-25", "open", "2026-09-25 09:30:00")

    def test_cutoff_cannot_belong_to_another_day(self):
        with pytest.raises(D.DeciderError, match="decision day"):
            D._validate_information_cutoff(
                "2026-09-25", "open", "2026-09-24 09:05:00")


def test_wait_rejects_a_metric_the_runtime_cannot_observe_yet():
    verdict = D.parse_orders(
        '{"orders":[],"watch":[{"code":"600001","reason":"等量",'
        '"next_check":[{"metric":"volume_ratio","op":">=","value":1.5}],'
        '"cancel_if":[]}]}',
        CODES)
    assert verdict["decision_status"] == "incomplete"
    assert verdict["refused"][0]["why"] == "invalid_watch"
