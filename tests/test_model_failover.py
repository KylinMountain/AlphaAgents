"""An unavailable or unfunded model hands over to a sibling of its family."""

from __future__ import annotations

import asyncio

import pytest

from alpha_agents import model_factory as MF


class _Inner:
    def __init__(self, down):
        self.down = set(down)
        self.asked = []

    async def create(self, **kw):
        self.asked.append(kw["model"])
        if kw["model"] in self.down:
            raise RuntimeError("Error code: 503 - no_healthy_account")
        return f"answered by {kw['model']}"


class _Client:
    def __init__(self, inner):
        self.chat = type("C", (), {"completions": inner})()

    def with_options(self, **kw):
        return self


@pytest.fixture(autouse=True)
def siblings(monkeypatch):
    MF.SWITCHES.clear()
    monkeypatch.setattr(MF, "_discover", lambda url, key, model: [
        "global:deepseek-v4.1-flash", "cn:deepseek-v4.1-flash"])


def _client(down):
    inner = _Inner(down)
    return MF.with_failover(_Client(inner), "http://gw", "k",
                            "global:deepseek-v4.1-flash-sg"), inner


def test_a_down_model_hands_over_and_the_switch_sticks():
    c, inner = _client({"global:deepseek-v4.1-flash-sg"})
    ask = {"model": "global:deepseek-v4.1-flash-sg", "messages": []}
    assert asyncio.run(c.chat.completions.create(**ask)) == \
        "answered by global:deepseek-v4.1-flash"
    asyncio.run(c.with_options(max_retries=0).chat.completions.create(**ask))
    assert inner.asked == ["global:deepseek-v4.1-flash-sg",
                           "global:deepseek-v4.1-flash", "global:deepseek-v4.1-flash"]
    assert len(MF.SWITCHES) == 2


def test_all_down_stops_the_run():
    c, _ = _client({"global:deepseek-v4.1-flash-sg", "global:deepseek-v4.1-flash",
                    "cn:deepseek-v4.1-flash"})
    with pytest.raises(MF.ModelsUnavailable):
        asyncio.run(c.chat.completions.create(model="global:deepseek-v4.1-flash-sg"))


def test_an_ordinary_error_is_not_a_reason_to_switch():
    class _Bad(_Inner):
        async def create(self, **kw):
            self.asked.append(kw["model"])
            raise ValueError("400 bad request")
    inner = _Bad(())
    c = MF.with_failover(_Client(inner), "http://gw", "k", "global:deepseek-v4.1-flash-sg")
    with pytest.raises(ValueError):
        asyncio.run(c.chat.completions.create(model="global:deepseek-v4.1-flash-sg"))
    assert inner.asked == ["global:deepseek-v4.1-flash-sg"]


def test_the_family_is_the_name_without_route_and_region():
    assert MF.family("global:deepseek-v4.1-flash-sg") == "deepseek-v4.1-flash"
    assert MF.sibling_models("global:deepseek-v4.1-flash-sg", [
        "cn:deepseek-v4-flash", "cn:deepseek-v4.1-flash",
        "global:deepseek-v4.1-flash-sg"]) == ["cn:deepseek-v4.1-flash"]
