"""A usage counter that undercounts is worse than none — it reads as calm.

The first version of this shipped a bug that is the whole reason these
tests exist: the async client's ``create`` is a plain method returning a
coroutine, so ``inspect.iscoroutinefunction`` says False. A sync wrapper
then read ``.usage`` off an un-awaited coroutine, found none, and dropped
the record. **The call still succeeded.** digest ran, billed, and counted
zero — exactly the silence the counter was built to end.

So the async path is pinned first, and the rest of the file pins the two
properties that make the counter trustworthy: it never raises into a
trading task, and it attributes to the right module.
"""

import asyncio

import pytest

from alpha_agents.data import token_usage as U


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "_DB", tmp_path / "usage.db", raising=False)
    monkeypatch.setattr(U._local, "conn", None, raising=False)
    yield
    conn = getattr(U._local, "conn", None)
    if conn is not None:
        conn.close()
    U._local.conn = None


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens, self.completion_tokens = p, c
        self.total_tokens = p + c


class _Resp:
    def __init__(self, p=100, c=20, model="test-model"):
        self.usage, self.model = _Usage(p, c), model


class _SyncHolder:
    def create(self, **kw):
        return _Resp()


class _AsyncHolder:
    """Shaped like the real async client: a plain method returning a
    coroutine, which is what defeated the first implementation."""
    async def create(self, **kw):
        return _Resp(200, 50)


class _Client:
    def __init__(self, holder):
        self.chat = type("C", (), {"completions": holder})()
        self.embeddings = holder


class TestAsyncCapture:
    def test_an_async_create_is_counted(self, store):
        """The regression. Not counting this was silent, which is the
        worst way for a cost counter to fail."""
        client = U.instrument(_Client(_AsyncHolder()), module="digest")
        asyncio.run(client.chat.completions.create())
        rows = U.recent(5)
        assert rows, "async 调用没有落库 —— 就是当初那个静默丢失的 bug"
        assert rows[0]["input_tokens"] == 200
        assert rows[0]["output_tokens"] == 50
        assert rows[0]["total_tokens"] == 250

    def test_the_caller_still_gets_the_real_response(self, store):
        """An instrumented client must behave identically in every respect
        except that the numbers land."""
        client = U.instrument(_Client(_AsyncHolder()))
        resp = asyncio.run(client.chat.completions.create())
        assert resp.usage.total_tokens == 250

    def test_a_sync_create_is_counted_too(self, store):
        client = U.instrument(_Client(_SyncHolder()), module="lessons")
        client.chat.completions.create()
        assert U.recent(1)[0]["total_tokens"] == 120


class TestAttribution:
    def test_an_explicit_module_wins_over_the_context(self, store):
        """digest inside a morning scan is still digest — that split is
        the entire point of the page this feeds."""
        client = U.instrument(_Client(_SyncHolder()), module="digest")
        with U.track("晨扫"):
            client.chat.completions.create()
        assert U.recent(1)[0]["module"] == "新闻筛选"

    def test_the_context_is_used_when_nothing_names_itself(self, store):
        client = U.instrument(_Client(_SyncHolder()))
        with U.track("晨扫"):
            client.chat.completions.create()
        assert U.recent(1)[0]["module"] == "晨扫"

    def test_agent_names_map_onto_modules(self):
        assert U._module_from_agent("morning_analyst:pullback") == "晨扫"
        assert U._module_from_agent("entry_pricer:breakout") == "盘中定价"

    def test_an_unknown_agent_keeps_its_own_name(self):
        """Bucketing it into "other" would hide a new spender behind a
        pile; showing it as itself makes it findable."""
        assert U._module_from_agent("brand_new_agent:x") == "brand_new_agent"

    def test_no_agent_is_not_a_module(self, store):
        assert U._module_from_agent("") == "unknown"


class TestItNeverBreaksTheCaller:
    def test_a_broken_database_does_not_raise(self, monkeypatch, store):
        monkeypatch.setattr(U, "_conn", lambda: (_ for _ in ()).throw(
            RuntimeError("db gone")))
        U.record("晨扫", "m", 10, 5)          # must not raise

    def test_a_response_without_usage_is_skipped_not_crashed(self, store):
        class NoUsage:
            def create(self, **kw):
                return object()
        client = U.instrument(_Client(NoUsage()))
        client.chat.completions.create()
        assert U.recent(5) == []

    def test_double_instrumentation_counts_once(self, store):
        client = _Client(_SyncHolder())
        U.instrument(U.instrument(client, module="digest"), module="digest")
        client.chat.completions.create()
        assert len(U.recent(5)) == 1

    def test_a_client_that_refuses_wrapping_is_returned_unchanged(self, store):
        class Locked:
            @property
            def chat(self):
                raise AttributeError("no chat here")
            @property
            def embeddings(self):
                raise AttributeError("no embeddings")
        obj = Locked()
        assert U.instrument(obj) is obj


class TestSummary:
    def test_three_horizons_are_reported(self, store):
        U.record("晨扫", "m", 100, 50)
        U.record("新闻筛选", "m", 20, 5, kind="chat")
        s = U.summary(7)
        assert s["today"]["total_tokens"] == 175
        assert s["window"]["total_tokens"] == 175
        assert s["alltime"]["total_tokens"] == 175
        assert {m["module"] for m in s["modules"]} == {"晨扫", "新闻筛选"}

    def test_chat_and_embedding_stay_apart(self, store):
        """They bill at completely different rates; one total would make
        the cheap half look like the expensive one."""
        U.record("晨扫", "m", 1000, 500)
        U.record("向量索引", "e", 40, 0, kind="embedding")
        kinds = {(m["module"], m["kind"]) for m in U.summary(7)["modules"]}
        assert ("向量索引", "embedding") in kinds
        assert ("晨扫", "chat") in kinds

    def test_an_empty_store_reports_zeros_not_an_error(self, store):
        s = U.summary(7)
        assert s["alltime"]["total_tokens"] == 0
        assert s["modules"] == []


class TestTheHookActuallyFires:
    """The counter recorded a whole morning scan as "unknown".

    Not because attribution was wrong — because `main.py` set
    OPENAI_AGENTS_DISABLE_TRACING=1 to silence the SDK's export warning,
    which also silenced the spans the counter reads. install() now owns
    the processor list instead of appending to it, so there is no exporter
    to warn about and tracing stays on.
    """

    def test_install_takes_over_the_processor_list(self, monkeypatch):
        import os

        import alpha_agents.data.token_usage as T
        captured = {}
        monkeypatch.setattr(T, "_installed", False, raising=False)
        monkeypatch.setattr("agents.set_trace_processors",
                            lambda ps: captured.setdefault("procs", ps))
        monkeypatch.setattr("agents.set_tracing_disabled",
                            lambda v: captured.setdefault("disabled", v))
        monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")

        T.install()

        assert len(captured["procs"]) == 1, "必须替换而不是追加，否则默认导出器还在"
        assert captured["disabled"] is False, "tracing 关着钩子就不触发"
        assert os.environ.get("OPENAI_AGENTS_DISABLE_TRACING") is None

    def test_the_agent_client_is_not_double_wrapped(self):
        """Tracing counts agent generations; wrapping their client too
        would bill every turn twice."""
        import inspect

        from alpha_agents import model_factory
        src = inspect.getsource(model_factory.create_model)
        assert "instrument(" not in src


class TestAllTimeTableHasEveryColumn:
    def test_alltime_rows_carry_input_and_output(self, store):
        """The UI renders window and all-time with one component; a
        missing column showed as a dash in every row."""
        U.record("晨扫", "m", 100, 50)
        row = U.summary(7)["alltime_modules"][0]
        assert row["inp"] == 100 and row["outp"] == 50
        assert set(U.summary(7)["modules"][0]) >= set(row)


class TestTruncationIsVisible:
    """json_repair made a cut-off batch look like a finished one.

    A batch packed to fill the *input* window emits more events than the
    output ceiling holds; the array comes back half-written, json_repair
    salvages it into a shorter but perfectly valid list, and nothing
    anywhere says news was dropped. Three of three batches in a real
    morning scan were truncated and every one was accepted silently.
    """

    def test_a_truncated_batch_is_split_not_accepted(self):
        import asyncio
        import types

        from alpha_agents.pipeline import digest as D

        calls = []

        async def fake(client, batch):
            calls.append(len(batch))
            # Only the full batch overflows; the halves fit.
            return [{"event": f"e{len(batch)}"}], len(batch) > 4

        orig = D._digest_once
        D._digest_once = fake
        try:
            out = asyncio.run(D._digest_batch(None, list(range(8))))
        finally:
            D._digest_once = orig

        assert 8 in calls, "整批先试一次"
        assert 4 in calls, "顶到天花板必须拆开重试"
        assert len(out) == 2, "两半的事件都要收回来"

    def test_a_single_item_that_overflows_is_not_split_forever(self):
        import asyncio

        from alpha_agents.pipeline import digest as D

        async def always_truncated(client, batch):
            return [{"event": "e"}], True

        orig = D._digest_once
        D._digest_once = always_truncated
        try:
            out = asyncio.run(D._digest_batch(None, [1]))
        finally:
            D._digest_once = orig
        assert out == [{"event": "e"}], "拆不动了就收下，不能无限递归"


class TestTheMarketViewReachesTheThesis:
    """Three of eleven invalidation kinds could never fire.

    theme_rank_worse_than, theme_flow_negative and breadth_below all read
    fields nothing ever populated, so MarketView carried None and evaluate
    correctly declined to fire on data it did not have. Silent by
    construction: the vocabulary offered them, the agent wrote them into
    real theses, and the monitor could not check a single one.
    """

    def test_the_three_data_dependent_kinds_fire_when_given_data(self):
        from alpha_agents.data import thesis as T

        mv = T.MarketView(price=10, current_return_pct=-2,
                          theme_rank=323, theme_net_flow_yi=-45.8,
                          breadth_ratio=0.22)
        for kind, value in (("theme_rank_worse_than", 50),
                            ("theme_flow_negative", 10),
                            ("breadth_below", 0.8)):
            assert T.evaluate([T.Condition(kind, value)], mv), \
                f"{kind} 拿到数据后仍然不触发"

    def test_missing_data_still_means_no_fire(self):
        """The other half of the contract: "I could not measure it" must
        never read as "the thesis broke"."""
        from alpha_agents.data import thesis as T

        mv = T.MarketView(price=10, current_return_pct=-2)
        for kind, value in (("theme_rank_worse_than", 50),
                            ("theme_flow_negative", 10),
                            ("breadth_below", 0.8)):
            assert T.evaluate([T.Condition(kind, value)], mv) is None

    def test_the_monitor_is_handed_the_market_view(self):
        """Regression on the wiring, not the arithmetic: check_all took
        these three arguments and the one call site passed none of them."""
        import inspect

        from alpha_agents.pipeline.tasks import book_manager
        src = inspect.getsource(book_manager.manage_book)
        assert "sector_ranks" in src and "sector_flows" in src
        assert "breadth_ratio" in src
