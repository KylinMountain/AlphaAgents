"""Tests for the news digest cheap-model filtering layer."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from alpha_agents.news_digest import (
    _build_user_message,
    _parse_response,
    digest_news,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_NEWS = [
    {
        "title": "特朗普宣布对华加征25%关税",
        "summary": "美国总统特朗普宣布将对中国商品加征25%关税",
        "time": "2026-04-02 08:00",
        "source": "华尔街见闻",
    },
    {
        "title": "Trump announces 25% tariffs on China",
        "summary": "President Trump announced new 25% tariffs on Chinese goods",
        "time": "2026-04-02 08:05",
        "source": "BBC",
    },
    {
        "title": "央行宣布降准0.5个百分点",
        "summary": "中国人民银行决定下调存款准备金率0.5个百分点",
        "time": "2026-04-02 07:30",
        "source": "PBOC",
    },
]

SAMPLE_LLM_RESPONSE = [
    {
        "event": "特朗普对华加征25%关税",
        "summary": "美国总统特朗普宣布将对中国商品加征25%关税，引发市场担忧。",
        "importance": 5,
        "credibility": "high",
        "sources": ["华尔街见闻", "BBC"],
        "market_impact": {
            "a_share": {
                "direction": "bearish",
                "sectors_bullish": ["国产替代", "军工"],
                "sectors_bearish": ["出口", "消费电子"],
            },
            "hk_stock": {"direction": "bearish", "note": "港股科技股承压"},
            "us_stock": {"direction": "neutral", "note": "美股影响有限"},
            "usd_cny": {"direction": "cny_weaken", "note": "贸易战预期推升美元"},
            "tariff_sensitive": {
                "direction": "negative",
                "names": ["苹果产业链", "纺织出口"],
            },
            "domestic_substitution": {
                "direction": "positive",
                "names": ["半导体设备", "EDA"],
            },
        },
        "raw_titles": [
            "特朗普宣布对华加征25%关税",
            "Trump announces 25% tariffs on China",
        ],
    },
    {
        "event": "央行降准0.5个百分点",
        "summary": "中国人民银行决定下调存款准备金率0.5个百分点，释放流动性。",
        "importance": 4,
        "credibility": "high",
        "sources": ["PBOC"],
        "market_impact": {
            "a_share": {
                "direction": "bullish",
                "sectors_bullish": ["银行", "地产"],
                "sectors_bearish": [],
            },
            "hk_stock": {"direction": "bullish", "note": "利好港股内房股"},
            "us_stock": {"direction": "neutral", "note": ""},
            "usd_cny": {"direction": "cny_weaken", "note": "宽松预期压人民币"},
            "tariff_sensitive": {"direction": "neutral", "names": []},
            "domestic_substitution": {"direction": "neutral", "names": []},
        },
        "raw_titles": ["央行宣布降准0.5个百分点"],
    },
]


def _make_anthropic_response(events: list[dict]) -> dict:
    """Build a mock Anthropic messages API response."""
    return {
        "content": [{"type": "text", "text": json.dumps(events, ensure_ascii=False)}],
        "model": "claude-haiku-4-5-20251001",
        "role": "assistant",
    }


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    def test_includes_all_items(self):
        msg = _build_user_message(SAMPLE_NEWS)
        for item in SAMPLE_NEWS:
            assert item["title"] in msg
            assert item["source"] in msg
            assert item["time"] in msg

    def test_empty_list(self):
        msg = _build_user_message([])
        assert msg == ""


class TestParseResponse:
    def test_parses_json_array(self):
        text = json.dumps(SAMPLE_LLM_RESPONSE, ensure_ascii=False)
        result = _parse_response(text)
        assert len(result) == 2
        assert result[0]["importance"] >= result[1]["importance"]

    def test_strips_markdown_fences(self):
        text = "```json\n" + json.dumps(SAMPLE_LLM_RESPONSE, ensure_ascii=False) + "\n```"
        result = _parse_response(text)
        assert len(result) == 2

    def test_filters_low_importance(self):
        events = [
            {**SAMPLE_LLM_RESPONSE[0], "importance": 5},
            {"event": "低重要性事件", "importance": 2, "credibility": "low"},
        ]
        result = _parse_response(json.dumps(events, ensure_ascii=False))
        assert len(result) == 1
        assert result[0]["importance"] == 5

    def test_sorts_by_importance_then_credibility(self):
        events = [
            {"event": "B", "importance": 4, "credibility": "low"},
            {"event": "A", "importance": 5, "credibility": "high"},
            {"event": "C", "importance": 4, "credibility": "high"},
        ]
        result = _parse_response(json.dumps(events))
        assert result[0]["event"] == "A"
        assert result[1]["event"] == "C"  # same importance, higher cred
        assert result[2]["event"] == "B"

    def test_wraps_single_object(self):
        event = SAMPLE_LLM_RESPONSE[0]
        result = _parse_response(json.dumps(event, ensure_ascii=False))
        assert len(result) == 1

    def test_empty_array(self):
        result = _parse_response("[]")
        assert result == []


# ---------------------------------------------------------------------------
# Async integration tests (mocked API)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_news_calls_api_with_all_items():
    """Verify the prompt sent to the API includes all news items."""
    mock_response = httpx.Response(
        200,
        json=_make_anthropic_response(SAMPLE_LLM_RESPONSE),
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    captured_kwargs = {}

    async def mock_post(self, url, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_response

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    # Check all titles appear in the prompt
    payload = captured_kwargs["json"]
    user_content = payload["messages"][0]["content"]
    for item in SAMPLE_NEWS:
        assert item["title"] in user_content

    assert payload["model"] == "claude-haiku-4-5-20251001"
    assert payload["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_digest_news_parses_response():
    """Verify the function correctly parses the LLM JSON response."""
    mock_response = httpx.Response(
        200,
        json=_make_anthropic_response(SAMPLE_LLM_RESPONSE),
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    async def mock_post(self, url, **kwargs):
        return mock_response

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    assert len(result) == 2
    assert result[0]["importance"] == 5
    assert result[0]["event"] == "特朗普对华加征25%关税"
    assert result[1]["importance"] == 4
    assert "market_impact" in result[0]
    assert result[0]["market_impact"]["a_share"]["direction"] == "bearish"


@pytest.mark.asyncio
async def test_digest_news_handles_api_http_error():
    """Verify graceful handling of HTTP errors (returns empty list)."""
    mock_response = httpx.Response(
        500,
        text="Internal Server Error",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    async def mock_post(self, url, **kwargs):
        return mock_response

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    assert result == []


@pytest.mark.asyncio
async def test_digest_news_handles_connection_error():
    """Verify graceful handling of connection errors (returns empty list)."""

    async def mock_post(self, url, **kwargs):
        raise httpx.ConnectError("Connection refused")

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    assert result == []


@pytest.mark.asyncio
async def test_digest_news_handles_malformed_json():
    """Verify graceful handling of malformed LLM output."""
    mock_response = httpx.Response(
        200,
        json={"content": [{"type": "text", "text": "not valid json [[["}]},
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    async def mock_post(self, url, **kwargs):
        return mock_response

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    assert result == []


@pytest.mark.asyncio
async def test_digest_news_empty_input():
    """Verify empty input returns empty list without API call."""
    result = await digest_news([])
    assert result == []


@pytest.mark.asyncio
async def test_digest_news_missing_api_key():
    """Verify missing API key returns empty list without API call."""
    with patch.dict("os.environ", {}, clear=True):
        result = await digest_news(SAMPLE_NEWS)
    assert result == []


@pytest.mark.asyncio
async def test_digest_news_custom_base_url():
    """Verify custom base URL is used when set."""
    mock_response = httpx.Response(
        200,
        json=_make_anthropic_response([]),
        request=httpx.Request("POST", "https://custom.api.com/v1/messages"),
    )

    captured_url = None

    async def mock_post(self, url, **kwargs):
        nonlocal captured_url
        captured_url = url
        return mock_response

    with (
        patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": "https://custom.api.com"},
        ),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        await digest_news(SAMPLE_NEWS)

    assert captured_url == "https://custom.api.com/v1/messages"


@pytest.mark.asyncio
async def test_digest_news_importance_sorting():
    """Verify results are sorted by importance desc, then credibility."""
    events = [
        {"event": "低重要性", "importance": 3, "credibility": "low"},
        {"event": "高重要性", "importance": 5, "credibility": "high"},
        {"event": "中重要性高可信", "importance": 4, "credibility": "high"},
        {"event": "中重要性低可信", "importance": 4, "credibility": "medium"},
    ]
    mock_response = httpx.Response(
        200,
        json=_make_anthropic_response(events),
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    async def mock_post(self, url, **kwargs):
        return mock_response

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.object(httpx.AsyncClient, "post", mock_post),
    ):
        result = await digest_news(SAMPLE_NEWS)

    assert [e["importance"] for e in result] == [5, 4, 4, 3]
    assert result[1]["credibility"] == "high"
    assert result[2]["credibility"] == "medium"
