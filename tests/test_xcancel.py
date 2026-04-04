"""Tests for xcancel X/Twitter source."""

import json
from unittest.mock import patch, MagicMock

from alpha_agents.sources.xcancel import (
    get_xcancel_fn,
    _parse_tweets,
    _clean_html,
    ACCOUNTS,
)

# Simulated xcancel HTML fragment
SAMPLE_HTML = """
<div class="timeline-item">
  <span class="tweet-date"><a href="/elonmusk/status/123456" title="Apr 3, 2026 · 10:43 PM UTC">Apr 3</a></span>
  <a class="tweet-link" href="/elonmusk/status/123456"></a>
  <div class="tweet-content media-body" dir="auto">Tesla cars, especially with FSD, are the safest in the world.</div>
</div>
<div class="timeline-item">
  <span class="tweet-date"><a href="/elonmusk/status/123457" title="Apr 3, 2026 · 9:27 PM UTC">Apr 3</a></span>
  <a class="tweet-link" href="/elonmusk/status/123457"></a>
  <div class="tweet-content media-body" dir="auto">New ads manager for 𝕏 <a href="https://ads.x.com">ads.x.com</a></div>
</div>
<div class="timeline-item">
  <span class="tweet-date"><a href="/elonmusk/status/123458" title="Apr 3, 2026 · 4:45 PM UTC">Apr 3</a></span>
  <a class="tweet-link" href="/elonmusk/status/123458"></a>
  <div class="tweet-content media-body" dir="auto"></div>
</div>
"""


def test_clean_html():
    assert _clean_html("<b>hello</b> &amp; world") == "hello & world"
    assert _clean_html("plain text") == "plain text"
    assert _clean_html("<a href='x'>link</a>") == "link"


def test_parse_tweets_basic():
    tweets = _parse_tweets(SAMPLE_HTML, "elonmusk", "Elon Musk")
    # Third tweet has empty content, should be skipped
    assert len(tweets) == 2
    assert tweets[0]["source"] == "X/@elonmusk"
    assert tweets[0]["author"] == "Elon Musk"
    assert "Tesla" in tweets[0]["summary"]
    assert tweets[0]["time"] == "Apr 3, 2026 · 10:43 PM UTC"
    assert tweets[0]["link"] == "https://x.com/elonmusk/status/123456"


def test_parse_tweets_strips_html():
    tweets = _parse_tweets(SAMPLE_HTML, "elonmusk", "Elon Musk")
    # Second tweet has <a> tag — should be stripped
    assert "<a" not in tweets[1]["summary"]
    assert "ads.x.com" in tweets[1]["summary"]


def test_parse_tweets_title_truncation():
    long_text = "A" * 200
    html = f'<div class="tweet-content media-body">{long_text}</div>'
    tweets = _parse_tweets(html, "test", "Test")
    assert len(tweets) == 1
    assert tweets[0]["title"].endswith("...")
    assert len(tweets[0]["title"]) < 100


def test_parse_tweets_empty_html():
    tweets = _parse_tweets("", "test", "Test")
    assert tweets == []
    tweets = _parse_tweets("<html><body>No tweets</body></html>", "test", "Test")
    assert tweets == []


def test_get_xcancel_fn_success():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML

    with patch("alpha_agents.sources.xcancel.fetch", return_value=mock_resp):
        result = json.loads(get_xcancel_fn(
            limit=10,
            accounts=[("Elon Musk", "elonmusk")],
        ))

    assert result["count"] == 2
    assert len(result["news"]) == 2
    assert result["news"][0]["source"] == "X/@elonmusk"


def test_get_xcancel_fn_limit():
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML

    with patch("alpha_agents.sources.xcancel.fetch", return_value=mock_resp):
        result = json.loads(get_xcancel_fn(
            limit=1,
            accounts=[("Elon Musk", "elonmusk")],
        ))

    assert result["count"] == 1


def test_get_xcancel_fn_fetch_failure():
    """Unavailable accounts are silently skipped."""
    with patch("alpha_agents.sources.xcancel.fetch", side_effect=Exception("503")):
        result = json.loads(get_xcancel_fn(
            accounts=[("Trump", "realDonaldTrump")],
        ))

    assert result["count"] == 0
    assert result["news"] == []


def test_get_xcancel_fn_mixed():
    """Some accounts succeed, some fail — should return partial results."""
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML

    def side_effect(url, **kwargs):
        if "elonmusk" in url:
            return mock_resp
        raise Exception("503")

    with patch("alpha_agents.sources.xcancel.fetch", side_effect=side_effect):
        result = json.loads(get_xcancel_fn(
            accounts=[("Trump", "realDonaldTrump"), ("Musk", "elonmusk")],
        ))

    assert result["count"] == 2


def test_accounts_list_not_empty():
    assert len(ACCOUNTS) > 0
    for name, handle in ACCOUNTS:
        assert name
        assert handle
        assert " " not in handle
