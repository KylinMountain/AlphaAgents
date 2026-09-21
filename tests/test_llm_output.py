"""An empty model response must not pass for an answer.

Measured 2026-09-21 against ``cn:deepseek-v4.1-flash``: an analytical prompt
capped at ``max_tokens=800`` returned HTTP 200, ``finish_reason="length"``,
800 tokens of reasoning and ``content == ""``. Every call site in this
repository did ``(content or "").strip()`` and carried on, so the failure was
invisible — ``playbook.annotate_degraded`` returned ``""`` rather than its own
fallback, because nothing raised for its except to catch.

These tests pin the two halves of the fix: the shared check reports an empty
body as a failure, and the call site that had the tightest budget now falls
back instead of storing nothing.
"""

from types import SimpleNamespace

from alpha_agents.llm_output import content_or_none, reasoning_tokens


def _response(content, finish_reason="stop", reasoning=None):
    details = (SimpleNamespace(reasoning_tokens=reasoning)
               if reasoning is not None else None)
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason)],
        usage=SimpleNamespace(completion_tokens_details=details),
    )


def test_ordinary_text_comes_back_stripped():
    assert content_or_none(_response("  结论：不追高  "), where="t") == "结论：不追高"


def test_empty_content_is_none_not_an_empty_string():
    """The distinction the old code lost: "" is falsy but still a str."""
    assert content_or_none(_response(""), where="t") is None
    assert content_or_none(_response(None), where="t") is None


def test_the_reasoning_starvation_case_is_named(caplog):
    """200 + finish_reason=length + empty body is the measured failure."""
    with caplog.at_level("ERROR"):
        assert content_or_none(
            _response("", finish_reason="length"), where="lessons") is None
    assert "lessons" in caplog.text
    assert "reasoning" in caplog.text


def test_a_truncated_but_non_empty_answer_is_kept_and_flagged(caplog):
    """json_repair would silently salvage this; the log must not be silent."""
    with caplog.at_level("WARNING"):
        out = content_or_none(_response('{"a": 1', finish_reason="length"),
                              where="digest")
    assert out == '{"a": 1'
    assert "token ceiling" in caplog.text


def test_no_choices_at_all():
    assert content_or_none(SimpleNamespace(choices=[]), where="t") is None


def test_reasoning_tokens_absent_reads_as_none_not_zero():
    assert reasoning_tokens(_response("x")) is None
    assert reasoning_tokens(_response("x", reasoning=0)) == 0
    assert reasoning_tokens(_response("x", reasoning=5995)) == 5995


def test_annotate_degraded_falls_back_when_the_model_says_nothing(monkeypatch):
    """The call site that used max_tokens=80 keeps its own fallback."""
    from alpha_agents.evolution import playbook

    class _Client:
        def __init__(self, *a, **k):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: _response("", finish_reason="length")))

    monkeypatch.setattr("openai.OpenAI", _Client)
    out = playbook.annotate_degraded(
        {"name": "缺口回补", "pattern_json": "{}", "hit_rate": 0.32,
         "total_trades": 25, "avg_return": -1.8})
    assert out == "近期胜率下滑"
