"""LLM-based event relationship analysis.

Uses cheap model to identify causal/amplifying/mitigating relationships
between events within the same analysis cycle.
"""

import json
import logging

from openai import AsyncOpenAI

from alpha_agents.config import DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
你是一个事件关系分析专家。给定一组金融事件，判断它们之间是否存在因果、强化、对冲或关联关系。

关系类型定义：
- causes: A 直接导致 B 发生（如"美联储加息" causes "美元走强"）
- amplifies: A 强化了 B 的影响（如"关税升级" amplifies "国产替代加速"）
- mitigates: A 削弱了 B 的影响（如"降准" mitigates "流动性收紧"）
- relates_to: A 和 B 有关联但不是直接因果（如"油价上涨" relates_to "航空股承压"）

规则：
1. 只输出确实存在关系的事件对，不要强行关联
2. confidence 范围 0.1-1.0，越确定越高
3. 同一对事件只输出一个最重要的关系
4. 如果事件之间没有明显关系，返回空数组

输出JSON数组格式：
[
  {
    "source": 0,
    "target": 1,
    "relation": "causes/amplifies/mitigates/relates_to",
    "confidence": 0.8,
    "reason": "一句话解释为什么这两个事件有这种关系"
  }
]

只返回JSON数组，不要其他文字。没有关系就返回 []。
"""


async def analyze_event_links(events: list[dict]) -> list[dict]:
    """Analyze relationships between events using cheap LLM.

    Args:
        events: List of event dicts with 'event', 'category', 'summary' fields.
                Each event's index in the list is used as its identifier.

    Returns:
        List of link dicts: {source, target, relation, confidence, reason}
        source/target are indices into the input events list.
    """
    if len(events) < 2:
        return []

    if not DIGEST_API_KEY:
        logger.debug("DIGEST_API_KEY not set, skipping event linking")
        return []

    # Build user message with numbered events
    lines = []
    for i, e in enumerate(events):
        cat = e.get("category", "?")
        title = e.get("event", e.get("title", "?"))
        summary = e.get("summary", "")[:200]
        lines.append(f"[{i}] [{cat}] {title}\n    {summary}")

    user_msg = "请分析以下事件之间的关系：\n\n" + "\n\n".join(lines)

    try:
        client = AsyncOpenAI(api_key=DIGEST_API_KEY, base_url=DIGEST_BASE_URL)
        response = await client.chat.completions.create(
            model=DIGEST_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )

        text = (response.choices[0].message.content or "").strip()

        # Strip markdown fences
        if text.startswith("```"):
            text = text[text.index("\n") + 1:]
            if text.endswith("```"):
                text = text[:-3].strip()

        links = json.loads(text)
        if not isinstance(links, list):
            return []

        # Validate
        valid = []
        n = len(events)
        for link in links:
            src = link.get("source")
            tgt = link.get("target")
            rel = link.get("relation", "")
            conf = link.get("confidence", 0.5)
            reason = link.get("reason", "")

            if not isinstance(src, int) or not isinstance(tgt, int):
                continue
            if src < 0 or src >= n or tgt < 0 or tgt >= n or src == tgt:
                continue
            if rel not in ("causes", "amplifies", "mitigates", "relates_to"):
                continue

            conf = max(0.1, min(float(conf), 1.0))
            valid.append({
                "source": src,
                "target": tgt,
                "relation": rel,
                "confidence": conf,
                "reason": reason,
            })

        logger.info("Event linker found %d relationships among %d events", len(valid), n)
        return valid

    except Exception as e:
        logger.warning("Event linking failed: %s", e)
        return []
