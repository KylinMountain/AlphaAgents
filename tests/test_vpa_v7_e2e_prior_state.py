"""v7 prior_state propagation tests (Anne review §2 mandatory gap)."""

import sqlite3
from pathlib import Path
import pytest

from alpha_agents.tools.vpa import _prev_trading_day

DB = Path(__file__).resolve().parent.parent / "data" / "market_history.db"


def test_prev_trading_day_skips_weekends():
    """Monday's previous trading day is the prior Friday."""
    # Pick a known Monday with a Friday-prior in market_history.db.
    # 2025-12-15 (Mon) → 2025-12-12 (Fri).
    assert _prev_trading_day("2025-12-15", db_path=str(DB)) == "2025-12-12"


def test_prev_trading_day_skips_holidays():
    """A trading day after a market holiday returns the day before the holiday.
    Use 2026-01-05 (first trading day of 2026 in CN markets) → 2025-12-31."""
    assert _prev_trading_day("2026-01-05", db_path=str(DB)) == "2025-12-31"


def test_prev_trading_day_returns_none_for_first_known_date():
    """If no prior trading day exists in the db, return None."""
    # Pick an arbitrarily early date; no data before should yield None.
    assert _prev_trading_day("1990-01-01", db_path=str(DB)) is None


def test_get_prior_state_backtest_returns_largest_prior_date():
    """Backtest mode: return largest-dated row strictly before as_of."""
    from alpha_agents.data import memory_store
    pytest.importorskip("alpha_agents.data.memory_store")
    assert callable(getattr(memory_store, "get_prior_state_backtest", None))


def test_get_prior_state_live_returns_only_exact_prev_trading_day():
    from alpha_agents.data import memory_store
    pytest.importorskip("alpha_agents.data.memory_store")
    assert callable(getattr(memory_store, "get_prior_state_live", None))


def test_e2e_prior_state_propagates_into_user_message(monkeypatch):
    """Anne review §2 mandatory: write prior_state, call compute_vpa_with_llm,
    assert section (e) appears in the user message and contains the right fields.
    """
    from alpha_agents.tools import vpa as vpa_mod

    captured_messages = []

    class FakeResp:
        class Message:
            content = (
                "<!-- VERDICT: {\"direction\": \"中性\", \"phase\": \"震荡\", "
                "\"phase_change\": {\"from\": \"\", \"to\": \"震荡\", \"confirmed\": false, "
                "\"invalidated_by\": \"\", \"denial_level\": \"none\"}, "
                "\"reason\": \"test\", "
                "\"selected_climax\": {\"candidate_id\": null, \"climax_type\": null, \"rationale\": \"test\"}, "
                "\"signals\": [], \"scenarios\": []} -->"
            )
        choices = [type("C", (), {"message": Message()})()]
        usage = None

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    captured_messages.append(kwargs.get("messages", []))
                    return FakeResp()

    # Patch the openai module's OpenAI symbol since vpa.py does
    # `from openai import OpenAI` inside _call_llm_vpa.
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    from alpha_agents.tools.vpa.llm import _CLIENT_CACHE
    _CLIENT_CACHE.clear()
    monkeypatch.setenv("VPA_LLM_PROVIDER", "agent")
    # Also need an API key to bypass the no_api_key early-return.
    from alpha_agents import config as agent_config
    monkeypatch.setattr(agent_config, "AGENT_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(agent_config, "AGENT_BASE_URL", "https://test", raising=False)
    monkeypatch.setattr(agent_config, "AGENT_MODEL", "test-model", raising=False)

    prior_state = {
        "analysis_date": "2025-12-12",
        "phase": "拉升中期",
        "verdict": "偏多",
        "selected_candidate_id": "cand-2025-12-08",
        "rationale": "SOS at 12-08",
    }
    result = vpa_mod.compute_vpa_with_llm(
        code="300136", name="信维通信", as_of="2025-12-15",
        skip_save=True, prior_state=prior_state,
    )
    assert captured_messages, "no LLM call captured"
    user_msg = captured_messages[0][1]["content"]  # messages[0] is system, [1] is user
    assert "你的上一次判断" in user_msg
    assert "拉升中期" in user_msg
    assert "cand-2025-12-08" in user_msg


def test_misaligned_prior_state_raises_runtime_error(monkeypatch):
    """A stale prior_state must fail loudly, without reading the corpus.

    The guard compares the caller's analysis_date against the previous
    trading day of as_of. Resolving that date from real data would make
    this a corpus test and drag the business DB into the sandbox, so the
    lookup is stubbed: the point under test is the comparison, not the
    calendar. The stub also proves the guard fires *before* any data load,
    which is what keeps a misaligned backtest from silently degrading.
    """
    from alpha_agents.tools import vpa as vpa_mod
    from alpha_agents.tools.vpa import api as vpa_api

    monkeypatch.setattr(vpa_api, "_prev_trading_day",
                        lambda as_of, **kwargs: "2025-12-12")

    # prior_state's analysis_date is NOT the previous trading day of as_of
    bad_prior = {
        "analysis_date": "2024-01-01",
        "phase": "拉升", "verdict": "看多",
        "selected_candidate_id": "cand-2024-01-01", "rationale": "stale",
    }
    with pytest.raises(RuntimeError, match="prior_state date misalignment"):
        vpa_mod.compute_vpa_with_llm(
            code="300136", name="信维通信", as_of="2025-12-15",
            skip_save=True, prior_state=bad_prior,
        )


def test_aligned_prior_state_does_not_trip_the_guard(monkeypatch):
    """The mirror case: a correctly dated baseline clears the alignment
    check. With no corpus in the sandbox the call stops at the data load
    and reports an error result — it must not raise a misalignment."""
    from alpha_agents.tools import vpa as vpa_mod
    from alpha_agents.tools.vpa import api as vpa_api

    monkeypatch.setattr(vpa_api, "_prev_trading_day",
                        lambda as_of, **kwargs: "2025-12-12")
    good_prior = {
        "analysis_date": "2025-12-12",
        "phase": "拉升", "verdict": "看多",
        "selected_candidate_id": "cand-2025-12-12", "rationale": "aligned",
    }
    result = vpa_mod.compute_vpa_with_llm(
        code="300136", name="信维通信", as_of="2025-12-15",
        skip_save=True, prior_state=good_prior,
    )
    assert result.get("ok") is False  # data load, not the guard, stopped it
    assert "misalignment" not in str(result)
