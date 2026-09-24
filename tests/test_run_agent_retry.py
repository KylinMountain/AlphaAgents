"""Transient provider failures are retried; the rest are not."""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from alpha_agents import model_factory as MF


class _Agent:
    name = "t"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def sleep(_):
        return None
    monkeypatch.setattr(MF.asyncio, "sleep", sleep)


def _patch(monkeypatch, outcomes):
    import agents
    calls = []

    async def run(agent, message, max_turns=2, **kw):
        calls.append(1)
        o = outcomes[len(calls) - 1]
        if isinstance(o, BaseException):
            raise o
        return o
    monkeypatch.setattr(agents.Runner, "run", staticmethod(run))
    return calls


def _status(code):
    req = httpx.Request("POST", "http://x")
    return openai.APIStatusError("e", response=httpx.Response(code, request=req), body=None)


def test_a_timeout_then_an_answer(monkeypatch):
    calls = _patch(monkeypatch, [asyncio.TimeoutError(), "ok"])
    assert asyncio.run(MF.run_agent(_Agent(), "m", max_turns=2)) == "ok"
    assert len(calls) == 2


def test_a_502_is_retried_a_400_is_not(monkeypatch):
    calls = _patch(monkeypatch, [_status(502), "ok"])
    assert asyncio.run(MF.run_agent(_Agent(), "m", max_turns=2)) == "ok"
    calls = _patch(monkeypatch, [_status(400), "never"])
    with pytest.raises(openai.APIStatusError):
        asyncio.run(MF.run_agent(_Agent(), "m", max_turns=2))
    assert len(calls) == 1


def test_it_gives_up_after_the_attempts(monkeypatch):
    calls = _patch(monkeypatch, [asyncio.TimeoutError()] * 5)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(MF.run_agent(_Agent(), "m", max_turns=2, attempts=3))
    assert len(calls) == 3
