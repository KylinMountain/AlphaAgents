"""Every model exchange on disk — and a mode that answers from the disk.

Why this exists
---------------
Plan §6 draws a line between *archived* and *reproducible*. Saving the decision's
input snapshot tells you what the agent was shown; it does not let a second run
reach the same judgement. The model is sampled, so two runs of the same 2020
window disagree unless the second one is given the answers the first one got.

So the unit that gets recorded is the exchange: the request that went out, the
response that came back, and the fingerprints saying which configuration asked.
With those on disk, ``replay-recorded`` reproduces a run without touching a
provider — which is what makes the M2 ablation ("news filter on / off, same
window") a comparison of the filter rather than of two samples.

    ALPHAAGENTS_LLM_MODE=live             the default; nothing is written
    ALPHAAGENTS_LLM_MODE=record           call the provider, write down the exchange
    ALPHAAGENTS_LLM_MODE=replay-recorded  never call the provider; answer from the file
    ALPHAAGENTS_LLM_MODE=resume           answer from the file while it lasts, then
                                          call the provider and keep recording

A different axis from ``evolution.replay_mode``
-----------------------------------------------
``replay_mode`` answers *what data may I see* (an as-of instant). This answers
*am I allowed to ask the model*. A run needs both, independently: a recording
session may be made with the as-of unset, and a replay with it set.

Where the recording lives
-------------------------
``<DATA_DIR>/llm_journal/<run_id>.jsonl`` — one JSON object per line, in call
order. It follows ``DATA_DIR``, so the replay directory ``walk_bootstrap`` builds
carries its own journal and cannot read the live one.

What it deliberately does not do
--------------------------------
* **Streaming is refused, not half-recorded.** Nothing here streams
  (``Runner.run``, never ``run_streamed``); a journal that captured the first
  chunk and stopped would replay a truncated answer. In ``record`` and
  ``replay-recorded`` a streaming call raises; ``live`` is untouched.
* **``record`` does not resume.** Its first write replaces the run's journal,
  naming how many lines it dropped. Appending would let a second run of the same
  window serve the *first* run's answers — evidence that is wrong rather than
  absent, which is the one failure worth being illiberal about.
* **``resume`` is a replay that is allowed to run past the end.** A run that
  stopped — the provider ran out of credit, the machine went down — is re-run
  from its first session in a fresh sandbox: every call it already made is
  answered from the recording, *verified* request by request exactly as in
  ``replay-recorded``, so the rebuilt state is the state the run had. At the
  first call the recording cannot answer — its end, or a call that had failed —
  the tail is set aside and the run goes back to the provider, recording as it
  goes. A request that differs from the recording still raises: a resume that
  answered anyway would splice two different runs into one.
* **It covers the agent model only** (``model_factory.create_model``). The digest,
  embedding and VPA clients have their own call paths and are not journaled. They
  are named here rather than implied, so no one reads a recording as complete.
* **It is not a cache.** Serving is positional: the Nth call of the run gets the
  Nth recording. A request that differs raises instead of quietly matching,
  because a silent mismatch is how a replay turns into fiction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from openai.types.chat import ChatCompletion

from alpha_agents import config

logger = logging.getLogger(__name__)

__all__ = ["LIVE", "RECORD", "REPLAY", "RESUME", "MODES", "RECORDING_MODES", "MODE_ENV",
           "RUN_ID_ENV", "Journal", "LlmJournalError", "RecordedCallFailed",
           "ReplayDivergence", "ReplayExhausted", "StreamingNotRecorded",
           "digest", "journal", "journal_path", "journaled", "policy_binding",
           "reset_journal", "resolve_mode", "resolve_run_id"]

# ── the mode ────────────────────────────────────────────────────────────────

MODE_ENV = "ALPHAAGENTS_LLM_MODE"
RUN_ID_ENV = "ALPHAAGENTS_RUN_ID"

LIVE = "live"
RECORD = "record"
REPLAY = "replay-recorded"
RESUME = "resume"
MODES = (LIVE, RECORD, REPLAY, RESUME)

#: The modes a journal can exist in. ``live`` is not among them: a live run has
#: no journal, which is the whole content of the word. Refusing it here means an
#: object that could only ever be silently inert cannot be built.
RECORDING_MODES = (RECORD, REPLAY, RESUME)

DEFAULT_RUN_ID = "default"
JOURNAL_DIRNAME = "llm_journal"

#: A run id becomes a filename, so it is *checked* rather than sanitised: two
#: run ids that collapsed to the same file would silently share a recording.
_RUN_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class LlmJournalError(RuntimeError):
    """The journal cannot do what it was asked, and says why."""


class StreamingNotRecorded(LlmJournalError):
    """A streaming call arrived; the journal records whole responses only."""


class ReplayExhausted(LlmJournalError):
    """The run asked for a call the recording does not have."""


class ReplayDivergence(LlmJournalError):
    """The run asked something different from what was recorded."""


class RecordedCallFailed(LlmJournalError):
    """The recorded answer to this call was an exception, re-raised."""


def resolve_mode(environ: "os._Environ | dict | None" = None) -> str:
    """Read the mode from the environment, refusing to guess at an unknown one.

    Blank falls back to ``live``, the same way a blank ``ALPHAAGENTS_DATA_DIR``
    falls back to ``data/`` — an empty shell variable is an unset one.

    A function rather than a module constant so a test can ask what a given
    environment produces without reloading this module.
    """
    raw = ((environ if environ is not None else os.environ)
           .get(MODE_ENV) or "").strip()
    if not raw:
        return LIVE
    if raw not in MODES:
        raise LlmJournalError(
            f"{MODE_ENV}={raw!r} is not one of {', '.join(MODES)} — refusing to "
            "guess whether this run may call the provider")
    return raw


def resolve_run_id(environ: "os._Environ | dict | None" = None) -> str:
    """Read the run id, or ``default``. Checked, never rewritten."""
    raw = ((environ if environ is not None else os.environ)
           .get(RUN_ID_ENV) or "").strip()
    if not raw:
        return DEFAULT_RUN_ID
    if not _RUN_ID_OK.match(raw):
        raise LlmJournalError(
            f"{RUN_ID_ENV}={raw!r} is not a usable filename component; allowed "
            "characters are letters, digits, dot, dash and underscore")
    return raw


def journal_path(run_id: str, data_dir: Path | None = None) -> Path:
    """Where ``run_id``'s recording lives.

    Reads ``config.DATA_DIR`` at call time rather than importing the value, so
    that pointing ``ALPHAAGENTS_DATA_DIR`` at a replay directory — or a test
    redirecting the sandbox — moves the journal with everything else.
    """
    root = Path(data_dir) if data_dir is not None else Path(config.DATA_DIR)
    return root / JOURNAL_DIRNAME / f"{run_id}.jsonl"


# ── what goes in a record ───────────────────────────────────────────────────

def _canonical(value: Any) -> str:
    """One spelling per value, so equal payloads hash equally.

    Sorted keys, no incidental whitespace, unescaped non-ASCII (the prompts and
    the news are Chinese, and ``\\uXXXX`` would triple the file for nothing).
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def digest(value: Any) -> str:
    """A stable fingerprint of anything recordable, prefixed with its algorithm."""
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


#: ``openai`` marks an unset parameter with a sentinel instance rather than
#: ``None``, and there are two of them (``omit is not NOT_GIVEN``). They are
#: detected by type name because importing ``openai._types`` would be reaching
#: into a private module to describe a value we only have to *recognise*.
_SENTINEL_NAMES = frozenset({"Omit", "NotGiven"})

#: Returned by the walk for a value that must not appear in the JSON at all.
_DROP = object()


class _Capture:
    """The JSON-safe form of what was handed to the provider.

    Two lists come out of every capture, because a lossy record has to say so:

    ``omitted``  a sentinel that was dropped — the provider fills that default,
                 so dropping it is the faithful thing, and naming the key keeps
                 the record honest about what it does not show.
    ``lossy``    a value with no JSON form, replaced by its type name.

    ``repr`` is never used for the second case. A repr carries a memory address,
    which would make the fingerprint of one conversation differ between two runs
    of it — the exact non-determinism the journal exists to eliminate.
    """

    def __init__(self) -> None:
        self.omitted: list[str] = []
        self.lossy: list[str] = []

    def take(self, value: Any, path: str = "$") -> Any:
        return _capture(value, path, self)


def _capture(value: Any, path: str, out: _Capture) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if type(value).__name__ in _SENTINEL_NAMES:
        out.omitted.append(path)
        return _DROP
    if isinstance(value, (list, tuple)):
        kept = [_capture(item, f"{path}[{i}]", out)
                for i, item in enumerate(value)]
        return [None if item is _DROP else item for item in kept]
    if isinstance(value, dict):
        captured = {str(key): _capture(item, f"{path}.{key}", out)
                    for key, item in value.items()}
        return {key: item for key, item in captured.items() if item is not _DROP}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _capture(dump(mode="json"), path, out)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _capture(value.value, path, out)
    named = f"{type(value).__module__}.{type(value).__qualname__}"
    out.lossy.append(f"{path} ({named})")
    return {"__unserializable__": named}


def _first_difference(recorded: Any, actual: Any, path: str = "$") -> str:
    """Where two normalised requests part company, for the divergence message."""
    if type(recorded) is not type(actual):
        return (f"{path}: {type(recorded).__name__} recorded vs "
                f"{type(actual).__name__} now")
    if isinstance(recorded, dict):
        for key in sorted(set(recorded) | set(actual)):
            if key not in actual:
                return f"{path}.{key}: only in the recording"
            if key not in recorded:
                return f"{path}.{key}: only in this request"
            deeper = _first_difference(recorded[key], actual[key],
                                       f"{path}.{key}")
            if deeper:
                return deeper
        return ""
    if isinstance(recorded, list):
        if len(recorded) != len(actual):
            return f"{path}: {len(recorded)} entries recorded vs {len(actual)} now"
        for index, (left, right) in enumerate(zip(recorded, actual)):
            deeper = _first_difference(left, right, f"{path}[{index}]")
            if deeper:
                return deeper
        return ""
    if recorded == actual:
        return ""
    return f"{path}: {_short(recorded)} recorded vs {_short(actual)} now"


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _tool_calls(response: Any) -> list[dict]:
    """What the model asked the agent to run, read off the response."""
    if not isinstance(response, dict):
        return []
    choices = response.get("choices") or []
    message = (choices[0] or {}).get("message") if choices else None
    if not isinstance(message, dict):
        return []
    out = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        out.append({"id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments")})
    return out


def _tool_results(request: Any) -> list[dict]:
    """What the agent's tools answered, read off the *next* request.

    Derived rather than captured by a hook, and deliberately: the tool results
    that matter are the ones the model was shown, and those are the ``role:
    tool`` messages of the request that follows. Capturing them a second way
    would create two sources that could disagree about the same run.
    """
    if not isinstance(request, dict):
        return []
    out = []
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "tool":
            out.append({"tool_call_id": message.get("tool_call_id"),
                        "content": message.get("content")})
    return out


def _provider_of(base_url: Any) -> str | None:
    """The host the run actually talked to, not the one the config names."""
    if not base_url:
        return None
    text = str(base_url)
    without_scheme = text.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0] or None


# ── which configuration asked ───────────────────────────────────────────────

_binding: dict | None = None
_binding_lock = threading.Lock()


def policy_binding() -> dict:
    """A fingerprint of the policy and knowledge in force, resolved once.

    ``evolution.policy_sources.collect()`` is the single definition of what a
    policy version covers, so hashing its output is a complete fingerprint
    rather than a hand-picked subset that a future source could escape. Imported
    inside the function because ``policy_sources`` imports ``model_factory``,
    which imports this module — a top-level import here would be a cycle.

    A failure to read the policy is recorded as a **named absence** rather than
    raised: losing the fingerprint must not lose a live trading run, but it must
    not be silent either, or the record would claim a provenance it lacks. The
    one exception is a look-ahead fault, which is a system error and is left to
    propagate — the same rule the kernel applies to ``clock.LookAheadError``.
    """
    global _binding
    with _binding_lock:
        if _binding is not None:
            return _binding
        from alpha_agents.data.clock import LookAheadError
        try:
            from alpha_agents.evolution import policy_sources

            sources = policy_sources.collect()
            _binding = {
                "policy_hash": digest(sources),
                "knowledge_snapshot_id": sources["knowledge"]["snapshot_id"],
                "policy_error": None,
            }
        except Exception as exc:
            # Everything else is a named absence — except a look-ahead fault,
            # which is a system error and must reach the caller, exactly as the
            # kernel treats clock.LookAheadError. A catch-all that swallowed one
            # would hide a leak behind a null fingerprint.
            if isinstance(exc, LookAheadError):
                raise
            _binding = {"policy_hash": None, "knowledge_snapshot_id": None,
                        "policy_error": f"{type(exc).__name__}: {exc}"}
            logger.warning(
                "Policy fingerprint unavailable for the LLM journal (%s) — "
                "records will name the absence", exc)
        return _binding


# ── the journal ─────────────────────────────────────────────────────────────

def _lines_in(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


class Journal:
    """One run's recordings, in call order.

    Built for a recording or a replay, never for a live run: see
    ``RECORDING_MODES`` for why ``live`` is refused rather than tolerated.
    """

    def __init__(self, *, mode: str, run_id: str, path: Path,
                 replace: bool = True) -> None:
        if mode not in RECORDING_MODES:
            raise LlmJournalError(
                f"a journal is {RECORD} or {REPLAY}, not {mode!r} — a "
                f"{LIVE} run records nothing, which is what the mode means. "
                f"Check {MODE_ENV} before building one.")
        self.mode = mode
        self.run_id = run_id
        self.path = Path(path)
        self._replace = replace and mode != RESUME
        #: In ``resume``: whether the recording has run out and calls now go
        #: to the provider.
        self.live = mode == RECORD
        self._lock = threading.Lock()
        self._handle = None
        self._seq = 0
        self._records: list[dict] | None = None
        self._cursor = 0

    # -- writing

    def record(self, request: dict, response: Any,
               provider: str | None = None) -> None:
        """Append one answered exchange."""
        capture = _Capture()
        payload = capture.take(request)
        body = _capture_response(response)
        self._append({
            "kind": "llm_call",
            "run_id": self.run_id,
            "seq": self._next_seq(),
            "at": datetime.now().isoformat(timespec="seconds"),
            "mode": self.mode,
            "model_provider": provider,
            "model_id": request.get("model"),
            "response_model": body.get("model") if isinstance(body, dict) else None,
            "request_hash": digest(payload),
            "response_hash": digest(body),
            "request_json": payload,
            "response_json": body,
            "omitted": capture.omitted,
            "lossy": capture.lossy,
            "tool_calls": _tool_calls(body),
            "tool_results": _tool_results(payload),
            **policy_binding(),
        })

    def record_failure(self, request: dict, exc: BaseException,
                       provider: str | None = None) -> None:
        """Append an exchange that raised.

        Recorded because the retry loop calls the model again: a run that failed
        twice and then succeeded has three exchanges, and a journal holding only
        the successful one would misalign every call after it on replay. The
        failure is part of the sequence, not an absence from it.
        """
        capture = _Capture()
        payload = capture.take(request)
        self._append({
            "kind": "llm_error",
            "run_id": self.run_id,
            "seq": self._next_seq(),
            "at": datetime.now().isoformat(timespec="seconds"),
            "mode": self.mode,
            "model_provider": provider,
            "model_id": request.get("model"),
            "request_hash": digest(payload),
            "request_json": payload,
            "omitted": capture.omitted,
            "lossy": capture.lossy,
            "error_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "error_message": str(exc),
            **policy_binding(),
        })

    def _append(self, record: dict) -> None:
        line = _canonical(record)
        with self._lock:
            handle = self._open()
            handle.write(line + "\n")
            handle.flush()
            # Flushed *and* synced: a recording that stops mid-run is replayable
            # up to the crash, and the truncated tail is caught on read.
            os.fsync(handle.fileno())

    def _open(self):
        if self._handle is not None:
            return self._handle
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._replace:
            existing = _lines_in(self.path)
            if existing:
                logger.warning(
                    "%s already held %d recorded call(s) and this run replaces "
                    "them — a recording describes one run, so the file is "
                    "rewritten rather than appended to", self.path, existing)
        self._handle = self.path.open("w" if self._replace else "a",
                                      encoding="utf-8")
        return self._handle

    def _next_seq(self) -> int:
        with self._lock:
            seq = self._seq
            self._seq += 1
            return seq

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    # -- replaying

    def replay(self, request: dict) -> ChatCompletion:
        """The recorded answer to this call, or a refusal that says why not."""
        capture = _Capture()
        payload = capture.take(request)
        if self._records is None:
            self._records = self._read()

        if self._cursor >= len(self._records):
            raise ReplayExhausted(
                f"{self.path} holds {len(self._records)} recorded call(s) for run "
                f"{self.run_id!r} and this run has asked for call "
                f"#{self._cursor + 1}; the run is not the one that was recorded. "
                f"A replay never falls through to the provider — re-record with "
                f"{MODE_ENV}={RECORD} if the run has legitimately changed")

        record = self._records[self._cursor]
        seq = record.get("seq", self._cursor)
        if record.get("kind") == "llm_error":
            # Consumed before re-raising, because the failure *is* the answer to
            # this call: the recording made the same call, got the same failure,
            # and the retry that followed is the next record. Leaving the cursor
            # here would replay the failure forever instead of the retry.
            self._cursor += 1
            raise RecordedCallFailed(
                f"the recorded answer to call #{seq} was "
                f"{record.get('error_type')}: {record.get('error_message')}")

        actual = digest(payload)
        if record.get("request_hash") != actual:
            # The first differing path names *where*; the request itself says
            # *what*. Saved beside the journal so the two can be diffed.
            try:
                self.path.with_name(f"{self.path.stem}.divergence-{seq}.json").write_text(
                    _canonical(payload), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not save the diverging request: %s", exc)
            raise ReplayDivergence(
                f"call #{seq} asks something the recording does not have.\n"
                f"    recorded {record.get('request_hash')}\n"
                f"    now      {actual}\n"
                f"    first    {_first_difference(record.get('request_json'),
                                                  payload) or '(only the hash)'}\n"
                "A replay that answers anyway would be evidence about a run that "
                "did not happen")

        self._cursor += 1
        return ChatCompletion.model_validate(record["response_json"])

    def resume(self, request: dict) -> ChatCompletion | None:
        """The recorded answer while the recording lasts; ``None`` once it has not.

        ``None`` means the caller must go to the provider — and by then the
        unanswerable tail (a failed call and everything after it) has been set
        aside and the journal is appending from this call on.
        """
        if self.live:
            return None
        if self._records is None:
            self._records = self._read() if self.path.exists() else []
        if (self._cursor < len(self._records)
                and self._records[self._cursor].get("kind") != "llm_error"):
            return self.replay(request)
        self._go_live()
        return None

    def _go_live(self) -> None:
        kept = self._cursor
        with self.path.open(encoding="utf-8") if self.path.exists() else _Empty() as h:
            lines = [ln for ln in h if ln.strip()]
        if len(lines) > kept:
            aside = self.path.with_name(
                f"{self.path.stem}.resume-tail-{datetime.now():%Y%m%d%H%M%S}.jsonl")
            aside.write_text("".join(lines[kept:]), encoding="utf-8")
            self.path.write_text("".join(lines[:kept]), encoding="utf-8")
            logger.warning("Resume: %d recorded call(s) answered, %d set aside in %s "
                           "(a failed call and what followed it); the provider "
                           "answers from call #%d on", kept, len(lines) - kept,
                           aside.name, kept)
        else:
            logger.warning("Resume: all %d recorded call(s) answered; the provider "
                           "answers from call #%d on", kept, kept)
        with self._lock:
            self._seq = kept
        self.live = True

    def _read(self) -> list[dict]:
        if not self.path.exists():
            raise LlmJournalError(
                f"{MODE_ENV}={REPLAY} but there is no recording at {self.path}; "
                f"run this window once with {MODE_ENV}={RECORD} first")
        out: list[dict] = []
        with self.path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise LlmJournalError(
                        f"{self.path}:{lineno} is not valid JSON — a truncated "
                        "journal is a broken recording, not a shorter one"
                    ) from exc
        return out

    def summary(self) -> dict:
        """What a startup line should say about this journal."""
        if self.mode == REPLAY:
            try:
                count = len(self._read())
            except LlmJournalError as exc:
                return {"mode": self.mode, "run_id": self.run_id,
                        "path": str(self.path), "recorded": None,
                        "problem": str(exc)}
            return {"mode": self.mode, "run_id": self.run_id,
                    "path": str(self.path), "recorded": count, "problem": None}
        return {"mode": self.mode, "run_id": self.run_id,
                "path": str(self.path), "recorded": None, "problem": None}


def _capture_response(response: Any) -> Any:
    """A response as JSON, through the same loss-naming capture as the request."""
    capture = _Capture()
    body = capture.take(response)
    if capture.lossy:
        logger.debug("Journal response had unrepresentable values: %s",
                     ", ".join(capture.lossy))
    return body


# ── the client ──────────────────────────────────────────────────────────────

class _Empty:
    """A missing file read as no lines."""

    def __enter__(self):
        return iter(())

    def __exit__(self, *exc):
        return False


class _JournalCompletions:
    """``chat.completions`` with the journal in front of it."""

    def __init__(self, inner, journal: Journal, provider: str | None) -> None:
        self._inner = inner
        self._journal = journal
        self._provider = provider

    async def create(self, **kwargs):
        """Call the provider, or answer from the recording.

        Keyword-only, which is how the SDK calls it
        (``agents/models/openai_chatcompletions.py``); a positional caller would
        get a ``TypeError`` here rather than a request that silently never
        reached the journal.
        """
        if kwargs.get("stream"):
            raise StreamingNotRecorded(
                f"a streaming completion arrived while {MODE_ENV}="
                f"{self._journal.mode}; the journal records whole responses "
                "only. Streaming is not used by any caller in this repository — "
                f"set {MODE_ENV}={LIVE} if this one is deliberate")
        if self._journal.mode == REPLAY:
            return self._journal.replay(kwargs)
        if self._journal.mode == RESUME:
            recorded = self._journal.resume(kwargs)
            if recorded is not None:
                return recorded
        try:
            response = await self._inner.create(**kwargs)
        except BaseException as exc:
            self._journal.record_failure(kwargs, exc, self._provider)
            raise
        self._journal.record(kwargs, response, self._provider)
        return response


class _JournalChat:
    def __init__(self, inner, journal: Journal, provider: str | None) -> None:
        self._inner = inner
        self.completions = _JournalCompletions(inner.completions, journal,
                                               provider)

    def __getattr__(self, name):
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


class _JournalClient:
    """An ``AsyncOpenAI`` whose chat completions go through the journal.

    Delegation is the whole surface the agents SDK touches: ``base_url`` (read
    for the tracing span and by ``is_official_openai_client``) and
    ``chat.completions.create``. Everything else falls through to the client it
    was given.

    ``with_options`` is overridden, and that is load-bearing rather than
    decorative. The SDK's retry loop calls ``self._client.with_options(
    max_retries=0)`` around every attempt (``agents/run_internal/model_retry.py``
    with the flag from ``models/_retry_runtime.py``) and calls ``create`` on the
    *result*. ``with_options`` builds a new client with new resource objects — it
    is not the same object, and neither is its ``chat.completions`` — so plain
    delegation would hand the call to an unwrapped client and leave the journal
    empty while every line of the run looked fine. Pinned by a test that drives
    that exact path.
    """

    def __init__(self, inner, journal: Journal) -> None:
        self._inner = inner
        self._journal = journal
        self._provider = _provider_of(getattr(inner, "base_url", None))
        # Named ``chat``, not ``_chat``: the SDK reaches for ``client.chat``, so
        # anything else would be shadowed out of ``__dict__`` and ``__getattr__``
        # would hand back the inner client's resource, unwrapped.
        self.chat = _JournalChat(inner.chat, journal, self._provider)

    def __getattr__(self, name):
        if name in ("_inner", "_journal", "_provider", "chat"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def with_options(self, **kwargs):
        return _JournalClient(self._inner.with_options(**kwargs), self._journal)


def journaled(client, journal_: Journal | None = None):
    """Return a client whose completions are journaled, or the client itself.

    ``live`` hands back the very object it was given, so the production path is
    unchanged in every respect including identity — no proxy to reason about, no
    file touched, nothing to go wrong.

    A journal passed in **is** the decision: its mode is used and the environment
    is not consulted. That is what a replay runner and a test want — they hold a
    journal that already knows whether it is recording or answering, and asking
    the environment again would let an unset variable quietly turn their
    recording into a live call.

    The default is the process journal, which is what
    ``model_factory.create_model`` needs: see :func:`journal` for why it is one
    object and not one per model.
    """
    if journal_ is not None:
        return _JournalClient(client, journal_)
    if resolve_mode() == LIVE:
        return client
    return _JournalClient(client, journal())


# ── the process journal ─────────────────────────────────────────────────────

_journal: Journal | None = None
_journal_lock = threading.Lock()


def journal() -> Journal:
    """The process's journal, built once.

    Every agent builds its own model through ``create_model`` — ten call sites,
    and one more each time an agent is added. A journal per model would restart
    the sequence at zero for each of them, so the recordings would collide and
    the replay order would depend on registration. One object per process, with
    the sequence inside it, is what makes "the Nth call" mean anything.

    Raises in ``live`` mode, because there is no journal to build; callers that
    might be live check ``resolve_mode()`` first, and ``journaled`` does.
    """
    global _journal
    with _journal_lock:
        if _journal is None:
            mode = resolve_mode()
            run_id = resolve_run_id()
            _journal = Journal(mode=mode, run_id=run_id,
                               path=journal_path(run_id))
            summary = _journal.summary()
            recorded = summary["recorded"]
            count = "" if recorded is None else f" recorded={recorded}"
            logger.info("LLM journal: mode=%s run_id=%s path=%s%s",
                        summary["mode"], summary["run_id"], summary["path"],
                        count)
            if summary["problem"]:
                logger.warning("LLM journal: %s", summary["problem"])
        return _journal


def reset_journal() -> None:
    """Drop the process journal so the next call builds a new one.

    For tests, and for a process that deliberately begins a second run.
    """
    global _journal, _binding
    with _journal_lock:
        if _journal is not None:
            _journal.close()
        _journal = None
    with _binding_lock:
        _binding = None
