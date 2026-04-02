"""Cheap-model news filtering and aggregation layer.

Pre-filters hundreds of raw news items from 6 sources (domestic finance,
international RSS, CLS telegraph, WallStreetCN, White House, PBOC) using
a cheap LLM (Haiku) before forwarding to the expensive Agent.

Deduplicates, merges related stories into events, scores importance,
and classifies market impact dimensions.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096

SYSTEM_PROMPT = """\
你是一个专业的金融新闻分析助手。你的任务是将多条原始新闻聚合、去重、评分，输出结构化的事件摘要。

规则：
1. 将相关的新闻合并为同一个"事件"（例如：3篇关于特朗普关税的文章 → 1个事件）
2. 只返回重要性 >= 3 的事件
3. 按重要性从高到低排序，重要性相同时按可信度排序（high > medium > low）
4. 用中文输出

对每个事件，输出以下JSON格式：
{
  "event": "事件标题",
  "summary": "综合多源信息的事件摘要",
  "importance": 4,
  "credibility": "high/medium/low",
  "sources": ["来源1", "来源2"],
  "market_impact": {
    "a_share": {"direction": "bullish/bearish/neutral", "sectors_bullish": [], "sectors_bearish": []},
    "hk_stock": {"direction": "bullish/bearish/neutral", "note": ""},
    "us_stock": {"direction": "bullish/bearish/neutral", "note": ""},
    "usd_cny": {"direction": "cny_strengthen/cny_weaken/neutral", "note": ""},
    "tariff_sensitive": {"direction": "positive/negative/neutral", "names": []},
    "domestic_substitution": {"direction": "positive/negative/neutral", "names": []}
  },
  "raw_titles": ["原始标题1", "原始标题2"]
}

只返回一个JSON数组，不要有其他文字。如果没有重要性>=3的事件，返回空数组 []。
"""


def _build_user_message(news_items: list[dict]) -> str:
    """Format raw news items into a user message for the LLM."""
    lines = []
    for i, item in enumerate(news_items, 1):
        title = item.get("title", "")
        summary = item.get("summary", "")
        time_ = item.get("time", "")
        source = item.get("source", "")
        lines.append(f"[{i}] 来源: {source} | 时间: {time_}\n标题: {title}\n摘要: {summary}")
    return "\n\n".join(lines)


def _parse_response(text: str) -> list[dict]:
    """Parse LLM response text into a list of event dicts.

    Handles cases where the model wraps JSON in markdown code fences.
    """
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (possibly ```json)
        first_newline = text.index("\n")
        text = text[first_newline + 1:]
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3].strip()

    events = json.loads(text)
    if not isinstance(events, list):
        logger.warning("LLM returned non-list JSON, wrapping: %s", type(events))
        events = [events]

    # Filter importance >= 3 (belt-and-suspenders, prompt already asks for this)
    events = [e for e in events if e.get("importance", 0) >= 3]

    # Sort by importance desc, then credibility (high > medium > low)
    cred_order = {"high": 0, "medium": 1, "low": 2}
    events.sort(
        key=lambda e: (-e.get("importance", 0), cred_order.get(e.get("credibility", "low"), 3))
    )

    return events


async def digest_news(news_items: list[dict]) -> list[dict]:
    """Filter, aggregate and score raw news using cheap LLM.

    Returns list of event dicts sorted by importance.
    """
    if not news_items:
        return []

    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set, cannot digest news")
        return []

    base_url = os.environ.get(ANTHROPIC_BASE_URL_ENV, DEFAULT_BASE_URL)
    url = f"{base_url}/v1/messages"

    user_message = _build_user_message(news_items)

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_message},
        ],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        # Extract text from Anthropic messages API response
        text = data["content"][0]["text"]
        return _parse_response(text)

    except httpx.HTTPStatusError as e:
        logger.error("Anthropic API HTTP error %d: %s", e.response.status_code, e.response.text)
        return []
    except (httpx.RequestError, KeyError, json.JSONDecodeError) as e:
        logger.error("News digest failed: %s: %s", type(e).__name__, e)
        return []
