"""Context window compression for long chat conversations.

Inspired by Hermes Agent's approach:
  - Protect head (system prompt) and tail (recent exchanges)
  - Summarize middle turns with structured LLM prompt
  - Iterative updates on subsequent compressions
  - Prune old tool outputs before summarizing

Uses the same model as the main agent (no separate cheap model needed).
"""

import logging
from typing import Any

from openai import OpenAI

from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate
_CHARS_PER_TOKEN = 4

# Compression triggers when estimated tokens exceed this
COMPRESS_THRESHOLD_TOKENS = 20_000

# How many messages to protect at head (system prompt + first exchange)
PROTECT_HEAD = 3

# Token budget for tail protection (most recent ~8K tokens kept intact)
TAIL_TOKEN_BUDGET = 8_000

# Placeholder for pruned tool outputs
_PRUNED = "[旧工具输出已清理]"

SUMMARY_TEMPLATE_FIRST = """请将以下对话历史压缩成结构化摘要。后续的AI助手将基于此摘要继续对话。

对话内容:
{content}

请用以下结构输出摘要:

## 用户目标
[用户想要做什么]

## 已完成的工作
[已经分析/操作了哪些股票，结论是什么]

## 关键决策
[做了哪些买卖决策，理由是什么]

## 重要数据
[提到的具体价格、止损位、持仓信息等关键数字]

## 当前讨论焦点
[最近在讨论什么话题]

要求: 保留具体的股票代码、价格、数量等关键数字。不要遗漏任何交易操作。"""

SUMMARY_TEMPLATE_UPDATE = """你之前的对话摘要如下。现在有新的对话内容需要合并进去。

上次摘要:
{previous_summary}

新增对话:
{content}

请更新摘要，保持相同结构。保留仍然有效的信息，添加新进展，移除已过时的信息。

## 用户目标
## 已完成的工作
## 关键决策
## 重要数据
## 当前讨论焦点

要求: 保留具体的股票代码、价格、数量等关键数字。"""

SUMMARY_PREFIX = (
    "[对话历史摘要] 以下是之前对话的压缩摘要。请基于此摘要和后续消息继续对话，"
    "不要重复已完成的工作:\n\n"
)


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate from message list."""
    total = 0
    for msg in messages:
        content = ""
        if isinstance(msg.get("content"), str):
            content = msg["content"]
        elif isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and "text" in part:
                    content += part["text"]
        total += len(content) // _CHARS_PER_TOKEN + 10
    return total


def _serialize_messages(messages: list[dict]) -> str:
    """Convert messages to text for summarization."""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = ""
        if isinstance(msg.get("content"), str):
            content = msg["content"]
        elif isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict):
                    if "text" in part:
                        content += part["text"]
                    elif part.get("type") == "tool_result":
                        content += f"[工具结果: {str(part.get('output', ''))[:200]}]"

        if not content:
            continue

        # Truncate very long messages
        if len(content) > 3000:
            content = content[:2000] + "\n...[截断]...\n" + content[-800:]

        if role == "user":
            parts.append(f"用户: {content}")
        elif role == "assistant":
            parts.append(f"分析师: {content}")
        else:
            parts.append(f"[{role}]: {content}")

    return "\n\n".join(parts)


def _prune_old_tool_outputs(messages: list[dict], protect_tail: int) -> list[dict]:
    """Replace old tool output content with placeholder (cheap pre-pass)."""
    if len(messages) <= protect_tail:
        return messages

    result = []
    boundary = len(messages) - protect_tail

    for i, msg in enumerate(messages):
        if i < boundary and isinstance(msg.get("content"), str) and len(msg["content"]) > 500:
            # Check if this looks like tool output (JSON-like or very long)
            content = msg["content"]
            if content.startswith("{") or content.startswith("[") or len(content) > 1000:
                result.append({**msg, "content": _PRUNED})
                continue
        result.append(msg)

    return result


class ContextCompressor:
    """Compresses conversation history when it gets too long."""

    def __init__(self):
        self._previous_summary: str | None = None
        self._compression_count = 0

    def should_compress(self, messages: list[dict]) -> bool:
        """Check if conversation needs compression."""
        return _estimate_tokens(messages) >= COMPRESS_THRESHOLD_TOKENS

    def compress(self, messages: list[dict]) -> list[dict]:
        """Compress conversation by summarizing middle turns.

        Algorithm:
        1. Prune old tool outputs (cheap, no LLM)
        2. Protect head messages (system prompt + first exchange)
        3. Protect tail messages (recent ~8K tokens)
        4. Summarize middle with LLM
        5. Reassemble: head + summary + tail
        """
        n = len(messages)
        if n <= PROTECT_HEAD + 5:
            return messages  # Too short to compress

        est_tokens = _estimate_tokens(messages)
        logger.info("Context compression triggered: ~%d tokens, %d messages",
                     est_tokens, n)

        # Phase 1: Prune old tool outputs
        messages = _prune_old_tool_outputs(messages, protect_tail=15)

        # Phase 2: Find boundaries
        head_end = PROTECT_HEAD

        # Phase 3: Find tail start by token budget
        tail_start = n
        accumulated = 0
        for i in range(n - 1, head_end, -1):
            msg = messages[i]
            content = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            msg_tokens = len(content) // _CHARS_PER_TOKEN + 10
            if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:
                tail_start = i + 1
                break
            accumulated += msg_tokens
            tail_start = i

        # Ensure we have something to compress
        if tail_start <= head_end + 1:
            tail_start = max(head_end + 2, n - 5)

        middle = messages[head_end:tail_start]
        if not middle:
            return messages

        logger.info("Compressing: head=%d, middle=%d (turns %d-%d), tail=%d",
                     head_end, len(middle), head_end, tail_start, n - tail_start)

        # Phase 4: Generate summary
        summary_text = self._generate_summary(middle)
        if not summary_text:
            # Fallback: just trim without summary
            logger.warning("Summary generation failed, using simple trim")
            return messages[:head_end] + messages[tail_start:]

        # Phase 5: Reassemble
        compressed = list(messages[:head_end])
        compressed.append({
            "role": "user",
            "content": SUMMARY_PREFIX + summary_text,
        })
        compressed.append({
            "role": "assistant",
            "content": "明白，我已了解之前的对话内容。请继续。",
        })
        compressed.extend(messages[tail_start:])

        self._compression_count += 1
        new_tokens = _estimate_tokens(compressed)
        logger.info("Compressed: %d → %d messages, ~%d → ~%d tokens (saved ~%d). "
                     "Compression #%d",
                     n, len(compressed), est_tokens, new_tokens,
                     est_tokens - new_tokens, self._compression_count)

        return compressed

    def _generate_summary(self, turns: list[dict]) -> str | None:
        """Summarize middle turns using LLM."""
        content = _serialize_messages(turns)
        if not content.strip():
            return None

        if self._previous_summary:
            prompt = SUMMARY_TEMPLATE_UPDATE.format(
                previous_summary=self._previous_summary,
                content=content,
            )
        else:
            prompt = SUMMARY_TEMPLATE_FIRST.format(content=content)

        try:
            client = OpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
            response = client.chat.completions.create(
                model=AGENT_MODEL or "qwen-plus",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.3,
            )
            summary = response.choices[0].message.content.strip()
            self._previous_summary = summary
            logger.info("Context summary generated (%d chars)", len(summary))
            return summary
        except Exception as e:
            logger.warning("Context summary failed: %s", e)
            return None
