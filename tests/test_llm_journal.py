"""The LLM journal: recording, replaying, and the ways it refuses.

Every refusal here is paired with the positive case, because "it raised" is also
what a wrapper that could never work would do. The two that matter most are the
``with_options`` trap — the SDK calls ``create`` on a *clone* of the client, so a
naive wrapper records nothing while the run looks fine — and the error path,
because a retried call that is not recorded misaligns every call after it.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from openai.types.chat import ChatCompletion

from alpha_agents import config, llm_journal
from alpha_agents.llm_journal import (
    LIVE, RECORD, REPLAY, Journal, LlmJournalError, RecordedCallFailed,
    ReplayDivergence, ReplayExhausted, StreamingNotRecorded, digest, journaled,
)

RUN_ID = "walk-test"


# ── stand-ins for a provider ────────────────────────────────────────────────

def completion(text: str = "ok", model: str = "qwen-plus",
               tool_calls: list | None = None) -> ChatCompletion:
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1758000000,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    })


class _Box:
    """What a fake provider answers with, and what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies) or [completion()]
        self.requests: list[dict] = []

    def next(self, kwargs):
        self.requests.append(kwargs)
        item = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeCompletions:
    def __init__(self, box: _Box):
        self._box = box

    async def create(self, **kwargs):
        return self._box.next(kwargs)


class _FakeChat:
    def __init__(self, box: _Box):
        self.completions = _FakeCompletions(box)


class _FakeClient:
    """Stands in for ``AsyncOpenAI``: base_url, chat.completions, with_options."""

    def __init__(self, box: _Box, base_url: str = "https://fake.example/v1/"):
        self._box = box
        self.base_url = base_url
        self.chat = _FakeChat(box)

    def with_options(self, **kwargs):
        clone = _FakeClient(self._box, self.base_url)
        clone.options = kwargs
        return clone


class _RefusingClient(_FakeClient):
    """A provider that fails the test if it is ever asked anything."""

    def __init__(self, base_url: str = "https://fake.example/v1/"):
        super().__init__(_Box(), base_url)

    def with_options(self, **kwargs):
        clone = _RefusingClient(self.base_url)
        clone.options = kwargs
        return clone


def _journal(tmp_path, mode, run_id=RUN_ID) -> Journal:
    return Journal(mode=mode, run_id=run_id,
                   path=tmp_path / f"{run_id}.jsonl")


def _records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


#: One request shape for both halves of a test. Recording and replaying with the
#: same call keeps a test about exhaustion or a missing file from tripping the
#: divergence check instead — and the divergence check has its own test.
ASK = {"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]}


def _ask(journal, box, **overrides):
    """One round trip through the journal, wired the way ``create_model`` does."""
    client = journaled(_FakeClient(box), journal)
    return asyncio.run(client.chat.completions.create(**{**ASK, **overrides}))


def _replay(journal, **overrides):
    """Ask a replay, with a client that fails the test if it is ever called."""
    client = journaled(_RefusingClient(), journal)
    return asyncio.run(client.chat.completions.create(**{**ASK, **overrides}))


REQUIRED_FIELDS = ("kind", "run_id", "seq", "at", "mode", "model_provider",
                   "model_id", "response_model", "request_hash", "response_hash",
                   "request_json", "response_json", "omitted", "lossy",
                   "tool_calls", "tool_results", "policy_hash",
                   "knowledge_snapshot_id", "policy_error")


# ── the mode ────────────────────────────────────────────────────────────────

class TestModeSelection:
    def test_absent_and_blank_both_mean_live(self):
        assert llm_journal.resolve_mode({}) == LIVE
        assert llm_journal.resolve_mode(
            {llm_journal.MODE_ENV: "   "}) == LIVE

    def test_an_unknown_mode_is_refused_rather_than_guessed(self):
        """A near-miss spelling must not silently become live.

        ``replay`` is the obvious wrong guess for ``replay-recorded``, and if it
        were accepted as-is the run would call the provider while its operator
        believed it was reproducing a recording.
        """
        with pytest.raises(LlmJournalError, match="refusing to guess"):
            llm_journal.resolve_mode({llm_journal.MODE_ENV: "replay"})

    def test_a_run_id_becomes_a_filename_and_is_checked(self, tmp_path):
        assert llm_journal.resolve_run_id(
            {llm_journal.RUN_ID_ENV: "walk-2020"}) == "walk-2020"
        assert llm_journal.resolve_run_id({}) == llm_journal.DEFAULT_RUN_ID
        with pytest.raises(LlmJournalError, match="filename"):
            llm_journal.resolve_run_id({llm_journal.RUN_ID_ENV: "walk/2020"})

    def test_the_journal_follows_data_dir_when_it_is_asked(self, tmp_path,
                                                           monkeypatch):
        """Read at call time, not bound at import.

        The suite redirects ``config.DATA_DIR`` rather than reloading modules,
        so a module that captured the value at import would write into the real
        ``data/`` during tests — and, in production, would ignore
        ``ALPHAAGENTS_DATA_DIR`` and put a replay's recordings next to the live
        ones.
        """
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        assert llm_journal.journal_path("run").parent.parent == tmp_path
        assert llm_journal.journal_path("run").name == "run.jsonl"


# ── live is untouched ───────────────────────────────────────────────────────

class TestLiveModeWritesNothing:
    def test_the_client_comes_back_as_the_same_object(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv(llm_journal.MODE_ENV, LIVE)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        client = _FakeClient(_Box())
        assert journaled(client) is client

    def test_no_file_appears(self, tmp_path, monkeypatch):
        monkeypatch.setenv(llm_journal.MODE_ENV, LIVE)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        box = _Box()
        client = journaled(_FakeClient(box))
        asyncio.run(client.chat.completions.create(model="qwen-plus",
                                                  messages=[]))
        assert list(tmp_path.iterdir()) == []
        assert box.requests  # the provider was still called


# ── recording ───────────────────────────────────────────────────────────────

class TestRecording:
    def test_a_record_holds_everything_a_replay_needs(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        box = _Box(completion("first"))
        reply = _ask(journal, box)

        assert reply.choices[0].message.content == "first"
        (record,) = _records(journal.path)
        for field in REQUIRED_FIELDS:
            assert field in record, f"{field} missing from the record"
        assert record["kind"] == "llm_call"
        assert record["seq"] == 0
        assert record["run_id"] == RUN_ID
        assert record["model_id"] == "qwen-plus"
        assert record["response_model"] == "qwen-plus"
        assert record["model_provider"] == "fake.example"
        assert record["request_hash"] == digest(record["request_json"])
        assert record["response_hash"] == digest(record["response_json"])
        assert record["request_json"]["messages"][0]["content"] == "hi"
        # The fingerprint is either a real hash or a named absence — never
        # a hash-shaped placeholder.
        assert (record["policy_hash"] is not None
                ) != (record["policy_error"] is not None)

    def test_seq_counts_up_across_calls(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        for _ in range(3):
            _ask(journal, _Box())
        assert [r["seq"] for r in _records(journal.path)] == [0, 1, 2]

    def test_tool_calls_come_off_the_response(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box(completion(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "get_quote", "arguments": '{"code":"600000"}'},
        }])))
        (record,) = _records(journal.path)
        assert record["tool_calls"] == [{"id": "call_1", "name": "get_quote",
                                         "arguments": '{"code":"600000"}'}]

    def test_tool_results_are_derived_from_the_following_request(self, tmp_path):
        """One source of truth, and it is the one the model actually saw."""
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box())
        _ask(journal, _Box(), messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_quote", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "600000=12.3"},
        ])
        first, second = _records(journal.path)
        assert first["tool_results"] == []
        assert second["tool_results"] == [{"tool_call_id": "call_1",
                                          "content": "600000=12.3"}]

    def test_a_record_is_json_and_a_repeated_capture_hashes_the_same(
            self, tmp_path):
        """Including a payload with no JSON form.

        ``repr`` would embed a memory address here, so the same conversation
        would fingerprint differently on every run — the non-determinism the
        journal exists to remove.
        """
        journal = _journal(tmp_path, RECORD)
        for _ in range(2):
            _ask(journal, _Box(), unknown_object=object())
        payloads = [json.dumps(r["request_json"]) for r in _records(journal.path)]
        assert json.loads(payloads[0])
        assert payloads[0] == payloads[1]

    def test_an_unrepresentable_value_is_named_not_silently_dropped(self,
                                                                   tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box(), opaque=object())
        (record,) = _records(journal.path)
        assert record["lossy"] and "opaque" in record["lossy"][0]
        assert record["request_json"]["opaque"] == {
            "__unserializable__": "builtins.object"}

    def test_provider_sentinels_are_omitted_and_named(self, tmp_path):
        """``omit`` means "let the provider decide", so recording it as a value
        would be recording something that was never sent."""
        from openai import omit

        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box(), temperature=omit, top_p=0.9)
        (record,) = _records(journal.path)
        assert "temperature" not in record["request_json"]
        assert "top_p" in record["request_json"]
        assert "$.temperature" in record["omitted"]

    def test_the_first_write_replaces_a_previous_recording_and_says_so(
            self, tmp_path, caplog):
        """Appending would let a re-run answer from the earlier recording."""
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box())
        journal.close()

        with caplog.at_level(logging.WARNING):
            second = _journal(tmp_path, RECORD)
            _ask(second, _Box())

        (record,) = _records(second.path)
        assert record["seq"] == 0
        assert len(_records(second.path)) == 1
        assert any("replaces" in message for message in caplog.messages)


# ── the trap the SDK sets ───────────────────────────────────────────────────

class TestTheClientCloneTrap:
    def test_with_options_keeps_journaling(self, tmp_path):
        """``with_options`` builds a new client with new resource objects.

        Plain delegation would therefore hand the call to an unwrapped client.
        This asserts the clone records, using the clone the way the SDK does.
        """
        journal = _journal(tmp_path, RECORD)
        client = journaled(_FakeClient(_Box()), journal)
        clone = client.with_options(max_retries=0)
        assert clone is not client
        asyncio.run(clone.chat.completions.create(model="qwen-plus",
                                                 messages=[]))
        assert len(_records(journal.path)) == 1

    def test_the_sdk_retry_path_still_records(self, tmp_path):
        """The clone is not hypothetical — the retry loop really makes one.

        ``_get_client`` returns ``with_options(max_retries=0)`` whenever
        provider retries are disabled, and that is the object ``create`` is
        called on. Whatever ``_get_client`` returns, it must journal.
        """
        from agents import OpenAIChatCompletionsModel
        from agents.models._retry_runtime import provider_managed_retries_disabled

        journal = _journal(tmp_path, RECORD)
        model = OpenAIChatCompletionsModel(
            model="qwen-plus",
            openai_client=journaled(_FakeClient(_Box()), journal))

        with provider_managed_retries_disabled(True):
            effective = model._get_client()

        asyncio.run(effective.chat.completions.create(model="qwen-plus",
                                                     messages=[]))
        assert len(_records(journal.path)) == 1, (
            "the client the SDK actually calls must still reach the journal")


# ── replaying ───────────────────────────────────────────────────────────────

class TestReplay:
    def test_a_replay_answers_without_a_provider(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        recorded = _ask(journal, _Box(completion("the recorded answer")))
        journal.close()

        replay = _journal(tmp_path, REPLAY)
        reply = _replay(replay)
        assert reply.choices[0].message.content == "the recorded answer"
        assert reply.model_dump() == recorded.model_dump()
        # The SDK branches on this: anything else and it takes a converter path
        # written for a different response shape.
        assert isinstance(reply, ChatCompletion)

    def test_two_replays_of_one_recording_are_identical(self, tmp_path):
        """What M1's "run it twice, same fills line for line" rests on."""
        journal = _journal(tmp_path, RECORD)
        for _ in range(3):
            _ask(journal, _Box())
        journal.close()

        seen = []
        for _ in range(2):
            replay = _journal(tmp_path, REPLAY)
            seen.append([_replay(replay).model_dump() for _ in range(3)])
        assert seen[0] == seen[1]

    def test_asking_for_more_than_was_recorded_raises(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box())
        journal.close()

        replay = _journal(tmp_path, REPLAY)
        _replay(replay)
        with pytest.raises(ReplayExhausted, match="never falls through"):
            _replay(replay)

    def test_a_missing_recording_is_named_before_anything_is_asked(self, tmp_path):
        replay = _journal(tmp_path, REPLAY)
        with pytest.raises(LlmJournalError, match="no recording at"):
            _replay(replay)

    def test_a_different_question_raises_and_names_the_first_difference(self,
                                                                       tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box(), temperature=0.2)
        journal.close()

        replay = _journal(tmp_path, REPLAY)
        with pytest.raises(ReplayDivergence) as caught:
            _replay(replay, temperature=0.9)
        message = str(caught.value)
        assert "$.temperature" in message
        assert "0.2 recorded" in message and "0.9 now" in message

    def test_a_truncated_journal_is_a_broken_recording_not_a_shorter_one(
            self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box())
        journal.close()
        with journal.path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "llm_call", "seq": 1')

        replay = _journal(tmp_path, REPLAY)
        with pytest.raises(LlmJournalError, match=r":2 is not valid JSON"):
            _replay(replay)


# ── the failure path has to stay in the sequence ────────────────────────────

class TestFailuresStayInSequence:
    def test_a_failed_call_is_recorded(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        box = _Box(RuntimeError("502 from upstream"), completion("second try"))

        with pytest.raises(RuntimeError):
            _ask(journal, box)
        _ask(journal, box)

        first, second = _records(journal.path)
        assert first["kind"] == "llm_error"
        assert first["error_type"] == "builtins.RuntimeError"
        assert "502" in first["error_message"]
        assert second["kind"] == "llm_call"
        assert [first["seq"], second["seq"]] == [0, 1]

    def test_replaying_a_recorded_failure_raises_it_again(self, tmp_path):
        """A journal holding only the success would misalign everything after.

        Two attempts, one failure: a replay that skipped the failure record
        would ask for call #1 and be handed attempt #2's answer.
        """
        journal = _journal(tmp_path, RECORD)
        box = _Box(ValueError("first attempt died"), completion("second try"))
        with pytest.raises(ValueError):
            _ask(journal, box)
        _ask(journal, box)
        journal.close()

        replay = _journal(tmp_path, REPLAY)
        with pytest.raises(RecordedCallFailed, match="ValueError: first attempt"):
            _replay(replay)
        assert _replay(replay).choices[0].message.content == "second try"


# ── refusals with no silent alternative ─────────────────────────────────────

class TestRefusals:
    def test_streaming_is_refused_in_record_mode(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        with pytest.raises(StreamingNotRecorded, match="whole responses only"):
            _ask(journal, _Box(), stream=True)
        assert not journal.path.exists()

    def test_streaming_is_refused_in_replay_mode(self, tmp_path):
        journal = _journal(tmp_path, RECORD)
        _ask(journal, _Box())
        journal.close()
        with pytest.raises(StreamingNotRecorded):
            _replay(_journal(tmp_path, REPLAY), stream=True)

    def test_a_journal_cannot_be_built_for_an_unknown_mode(self, tmp_path):
        with pytest.raises(LlmJournalError):
            Journal(mode="halfway", run_id=RUN_ID, path=tmp_path / "x.jsonl")

    def test_a_live_journal_cannot_be_built_at_all(self, tmp_path):
        """``live`` means no journal; an object that could only be inert is a
        mistake with a shape, so it is refused rather than tolerated."""
        with pytest.raises(LlmJournalError, match="records nothing"):
            Journal(mode=LIVE, run_id=RUN_ID, path=tmp_path / "x.jsonl")


class TestPolicyBinding:
    def test_a_failure_to_read_the_policy_is_a_named_absence(self, tmp_path,
                                                             monkeypatch):
        from alpha_agents.evolution import policy_sources

        def explode():
            raise RuntimeError("no pointer")

        monkeypatch.setattr(policy_sources, "collect", explode)
        llm_journal.reset_journal()
        binding = llm_journal.policy_binding()
        assert binding["policy_hash"] is None
        assert binding["knowledge_snapshot_id"] is None
        assert "RuntimeError: no pointer" == binding["policy_error"]

    def test_a_look_ahead_fault_is_not_swallowed(self, monkeypatch):
        """The journal must not become the catch-all the kernel warns about.

        ``clock.LookAheadError`` is a system fault, not a missing fingerprint,
        so an ``except Exception`` here has to let it through.
        """
        from alpha_agents.data.clock import LookAheadError
        from alpha_agents.evolution import policy_sources

        def leak():
            raise LookAheadError("dated after the clock")

        monkeypatch.setattr(policy_sources, "collect", leak)
        llm_journal.reset_journal()
        with pytest.raises(LookAheadError):
            llm_journal.policy_binding()

    def test_the_binding_is_resolved_once(self, monkeypatch):
        from alpha_agents.evolution import policy_sources

        calls = []
        original = policy_sources.collect

        def counted():
            calls.append(1)
            return original()

        monkeypatch.setattr(policy_sources, "collect", counted)
        llm_journal.reset_journal()
        llm_journal.policy_binding()
        llm_journal.policy_binding()
        assert len(calls) == 1


class TestProcessJournal:
    def test_every_model_shares_one_journal(self, tmp_path, monkeypatch):
        """Ten call sites build ten models; one sequence has to span them.

        A journal per model would restart at zero for each of them, so the
        recordings would collide and the replay order would depend on which
        agent happened to be constructed first.
        """
        monkeypatch.setenv(llm_journal.MODE_ENV, RECORD)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        llm_journal.reset_journal()
        try:
            first = journaled(_FakeClient(_Box()))
            second = journaled(_FakeClient(_Box()))
            assert first._journal is second._journal
            asyncio.run(first.chat.completions.create(model="qwen-plus",
                                                      messages=[]))
            asyncio.run(second.chat.completions.create(model="qwen-plus",
                                                       messages=[]))
            assert [r["seq"] for r in
                    _records(first._journal.path)] == [0, 1]
        finally:
            llm_journal.reset_journal()
