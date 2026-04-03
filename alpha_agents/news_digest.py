"""Cheap-model news filtering and aggregation layer.

Pre-filters hundreds of raw news items from 11 sources using a cheap LLM
before forwarding to the expensive strategist Agent.

Uses OpenAI-compatible SDK so any provider works:
- SiliconFlow (default, free tier Qwen)
- OpenAI, Anthropic, DeepSeek, Ollama, vLLM, etc.

Configure via env vars: DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL
"""

import json
import logging
import re

from openai import AsyncOpenAI

from alpha_agents.config import DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096

SYSTEM_PROMPT = """\
你是一个专业的金融新闻分析助手。你的任务是将多条原始新闻聚合、去重、评分，输出结构化的事件摘要。

规则：
1. 将相关的新闻合并为同一个"事件"（例如：3篇关于特朗普关税的文章 → 1个事件）
2. 只返回重要性 >= 3 的事件
3. 按重要性从高到低排序，重要性相同时按可信度排序（high > medium > low）
4. 用中文输出

事件分类(category)定义：
- "政策" — 央行货币政策、财政政策、监管政策、产业政策等政府行为
- "地缘" — 国际关系、战争冲突、制裁、关税博弈、外交事件
- "宏观" — 经济数据、就业、通胀、GDP、PMI等宏观指标
- "行业" — 具体行业/公司层面的重大事件、技术突破、并购重组
- "市场" — 市场异动、资金流向、流动性事件、黑天鹅

对每个事件，输出以下JSON格式：
{
  "event": "事件标题",
  "category": "政策/地缘/宏观/行业/市场",
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


BATCH_SIZE = 30  # Process this many news items per LLM call


def _build_user_message(news_items: list[dict]) -> str:
    """Format raw news items into a user message for the LLM."""
    lines = []
    for i, item in enumerate(news_items, 1):
        title = item.get("title", "")
        summary = item.get("summary", "")[:150]
        time_ = item.get("time", "")
        source = item.get("source", "")
        lines.append(f"[{i}] 来源: {source} | 时间: {time_}\n标题: {title}\n摘要: {summary}")
    return "\n\n".join(lines)


def _clean_json(text: str) -> str:
    """Clean up non-standard JSON produced by some LLMs.

    Handles:
    - Markdown code fences (```json ... ``` or ``` ... ```)
    - Trailing commas before ] or } (invalid JSON, valid in JS)
    - Leading/trailing whitespace
    """
    text = text.strip()

    # Strip markdown code fences: ```json\n...\n``` or ```\n...\n```
    if text.startswith("```"):
        # Remove opening fence line (e.g. "```json")
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        # Remove closing fence
        if text.endswith("```"):
            text = text[:-3].rstrip()

    # Remove trailing commas before closing brackets/braces
    # e.g.  { "a": 1, }  →  { "a": 1 }
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text.strip()


def _parse_response(text: str) -> list[dict]:
    """Parse LLM response text into a list of event dicts.

    Handles non-standard LLM output:
    - Markdown code fences around JSON
    - Trailing commas in objects/arrays
    - Single-line or multi-line JSON arrays
    """
    cleaned = _clean_json(text)

    try:
        events = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse LLM JSON response (%s). Raw text (first 200 chars): %s",
            exc, text[:200],
        )
        return []
    if not isinstance(events, list):
        logger.warning("LLM returned non-list JSON, wrapping: %s", type(events))
        events = [events]

    # Validate and normalize each event
    valid = []
    for e in events:
        if not isinstance(e, dict):
            continue
        # Ensure required fields with defaults
        e.setdefault("event", "未知事件")
        e.setdefault("category", "未知")
        e.setdefault("summary", "")
        e.setdefault("sources", [])
        e.setdefault("credibility", "low")
        e.setdefault("raw_titles", [])
        e.setdefault("market_impact", {})
        # Coerce importance to int, clamp 1-5
        try:
            imp = int(e.get("importance", 0))
        except (ValueError, TypeError):
            imp = 0
        e["importance"] = max(0, min(imp, 5))
        # Filter importance >= 3
        if e["importance"] >= 3:
            valid.append(e)

    # Sort by importance desc, then credibility (high > medium > low)
    cred_order = {"high": 0, "medium": 1, "low": 2}
    valid.sort(
        key=lambda e: (-e["importance"], cred_order.get(e.get("credibility", "low"), 3))
    )

    # Cap at 20 events max
    return valid[:20]


def _get_client() -> AsyncOpenAI:
    """Create an OpenAI-compatible async client."""
    return AsyncOpenAI(
        api_key=DIGEST_API_KEY,
        base_url=DIGEST_BASE_URL,
    )


async def _digest_batch(client: AsyncOpenAI, batch: list[dict]) -> list[dict]:
    """Digest a single batch of news items."""
    user_message = _build_user_message(batch)
    response = await client.chat.completions.create(
        model=DIGEST_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    text = response.choices[0].message.content or ""
    return _parse_response(text)


async def digest_news(news_items: list[dict]) -> list[dict]:
    """Filter, aggregate and score raw news using cheap LLM.

    Splits large news lists into batches of BATCH_SIZE, processes each
    batch with a separate LLM call, then merges and deduplicates results.

    Returns list of event dicts sorted by importance.
    """
    if not news_items:
        return []

    if not DIGEST_API_KEY:
        logger.error(
            "DIGEST_API_KEY not set. Set it to use any OpenAI-compatible provider. "
            "Default: SiliconFlow free tier (https://siliconflow.cn)"
        )
        return []

    client = _get_client()
    all_events = []

    # Split into batches
    for i in range(0, len(news_items), BATCH_SIZE):
        batch = news_items[i : i + BATCH_SIZE]
        try:
            events = await _digest_batch(client, batch)
            all_events.extend(events)
        except Exception as e:
            logger.error("News digest batch %d failed: %s: %s", i // BATCH_SIZE, type(e).__name__, e)

    # Deduplicate by event title (keep highest importance)
    seen = {}
    for ev in all_events:
        title = ev.get("event", "")
        if title not in seen or ev.get("importance", 0) > seen[title].get("importance", 0):
            seen[title] = ev

    # Sort by importance desc
    result = sorted(seen.values(), key=lambda x: x.get("importance", 0), reverse=True)
    return result[:20]
