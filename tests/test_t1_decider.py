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
        ('{"orders":[{"code":"600001","entry_high":10.2,"stop_loss":9.2}]}',
         "bad_prices"),
        ('{"orders":["600001"]}', "not_an_object"),
    ])
    def test_the_reason_is_returned(self, body, why):
        verdict = D.parse_orders(body, CODES)
        assert verdict["orders"] == []
        assert [r["why"] for r in verdict["refused"]] == [why]


class TestAnUnreadableReplyIsNotAnEmptyOne:
    """``{"orders": []}`` means the agent chose to do nothing. A reply we could
    not parse means we do not know what it chose. Collapsing the two is how a
    decider that never worked looks like a decider that is being careful."""

    def test_an_empty_order_list_parses_with_no_error(self):
        verdict = D.parse_orders('{"orders": []}', CODES)
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
        verdict = D.parse_orders('```json\n{"orders": []}\n```', CODES)
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
