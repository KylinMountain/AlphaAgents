# Phase 3: Multi-Mode Analyst Agents

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single strategist agent with four specialized agents (morning scan, intraday, review, cross-validation), each with its own prompt, tool set, and thinking mode, sharing the memory system from Phase 1.

**Architecture:** Each agent gets a dedicated prompt file in `alpha_agents/prompts/` and an agent factory in `alpha_agents/agents/`. The existing task stubs in `pipeline/tasks/` are upgraded to create and run these agents, feeding them memory context as part of the prompt. The cross-validation agent is called by other agents before outputting recommendations (not scheduled independently).

**Tech Stack:** OpenAI Agents SDK (`agents.Agent`, `agents.Runner`), asyncio, existing STOCK_TOOLS (18 tools from Phase 2), memory_store.py from Phase 1.

---

## File Structure

```
alpha_agents/
├── prompts/
│   ├── morning_scan.md       # NEW — morning analyst prompt
│   ├── intraday.md           # NEW — intraday anomaly-tracing prompt
│   ├── review.md             # NEW — post-market review prompt
│   └── cross_validate.md     # NEW — 4-dimension validation prompt
├── agents/
│   ├── morning.py            # NEW — morning scan agent factory
│   ├── intraday.py           # NEW — intraday agent factory
│   ├── review_agent.py       # NEW — review agent factory (review.py taken by pipeline)
│   └── cross_validate.py     # NEW — cross-validation agent (called by others)
├── pipeline/tasks/
│   ├── morning_scan.py       # MODIFY — upgrade stub to use morning agent
│   ├── intraday_monitor.py   # MODIFY — upgrade stub to use intraday agent
│   └── review.py             # MODIFY — upgrade stub to use review agent
```

**Shared pattern:** Each agent module exports `async def run_X(context: str, hooks=None) -> str` which creates the agent, builds the prompt with memory context, runs it with timeout, and returns the output.

---

### Task 1: Create cross-validation agent (used by others)

This agent is called by the morning/intraday agents before outputting recommendations. It validates candidates across 4 dimensions.

**Files:**
- Create: `alpha_agents/prompts/cross_validate.md`
- Create: `alpha_agents/agents/cross_validate.py`

- [ ] **Step 1: Create the prompt**

Create `alpha_agents/prompts/cross_validate.md`:

```markdown
# 你是交叉验证分析师

你的职责是对推荐候选股进行4维验证，确定信心水平。你不做初始分析，只做验证。

## 4维验证清单

对每只推荐的股票，依次检查：

1. 资金面 — 调用 get_stock_fund_flow 查主力资金方向，调用 get_north_flow 查北向持仓
   - 通过：主力连续流入 或 北向增持
   - 不通过：主力连续流出 且 北向减持

2. 基本面 — 调用 get_financial_data 查 ROE/负债率/利润增速，调用 get_earnings_calendar 查业绩预告
   - 通过：ROE > 10% 且 无首亏/预减
   - 不通过：ROE < 5% 或 负债率 > 150% 或 业绩预告为首亏

3. 位置面 — 调用 get_stock_quotes 查当前价格和近期涨跌
   - 通过：近5日涨幅 < 15%（不追高）
   - 不通过：近5日涨幅 > 20%（追高风险太大）

4. 情绪面 — 调用 get_market_breadth 查大盘涨跌比
   - 通过：涨跌比 > 0.8（市场不是极度悲观）
   - 不通过：涨跌比 < 0.3（极度悲观市场不宜看多）

## 信心评级

- 4项通过 → 高信心
- 3项通过 → 高信心
- 2项通过 → 中信心（标注未通过维度作为风险提示）
- 1项或更少 → 仅列入观察，不推荐

## 输出格式

对每只候选股票输出一行：

```
{code} {name} | 资金:{通过/不通过} 基本面:{通过/不通过} 位置:{通过/不通过} 情绪:{通过/不通过} | 信心:{高/中/观察} | 风险:{具体风险}
```

最后汇总：
```
验证结果：X只高信心，Y只中信心，Z只仅观察
高信心推荐：{code1} {name1}, {code2} {name2}
```

## 重要提醒

- 每只股票的每个维度都必须调用工具获取数据，不得推断
- 非交易时段数据缺失时标注"无法验证"，不降低信心等级
- 最多验证10只股票，超过的跳过
```

- [ ] **Step 2: Create the agent module**

Create `alpha_agents/agents/cross_validate.py`:

```python
"""Cross-validation agent — 4-dimension verification before recommending.

Called by morning/intraday agents to validate stock candidates.
Not scheduled independently.
"""

import asyncio
import logging

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    get_stock_quotes, get_financial_data, get_market_breadth,
    get_earnings_calendar, get_stock_fund_flow, get_north_flow,
)

logger = logging.getLogger(__name__)


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_validator() -> Agent:
    prompt = (PROMPTS_DIR / "cross_validate.md").read_text(encoding="utf-8")
    tools = [
        get_stock_quotes, get_financial_data, get_market_breadth,
        get_earnings_calendar, get_stock_fund_flow, get_north_flow,
    ]
    return Agent(
        name="cross_validator",
        instructions=prompt,
        model=_create_model(),
        tools=tools,
    )


async def run_cross_validation(candidates: str, hooks=None) -> str:
    """Validate stock candidates across 4 dimensions.

    Args:
        candidates: Text listing candidate stocks with codes and reasons.
        hooks: Optional agent hooks for event broadcasting.

    Returns:
        Validation result with confidence levels.
    """
    logger.info("Cross-validation starting...")
    agent = _create_validator()
    prompt = f"请对以下推荐候选股进行4维交叉验证：\n\n{candidates}"

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, prompt, hooks=hooks, max_turns=30),
            timeout=180,
        )
        logger.info("Cross-validation complete, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.warning("Cross-validation timed out after 180s")
        return "交叉验证超时，候选股票未经验证。"
    except Exception as e:
        logger.warning("Cross-validation failed: %s", e)
        return f"交叉验证失败: {e}"
```

- [ ] **Step 3: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.agents.cross_validate import _create_validator
agent = _create_validator()
print(f'Agent: {agent.name}, tools: {len(agent.tools)}')
assert agent.name == 'cross_validator'
assert len(agent.tools) == 6
print('TEST PASSED')
"
```

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/prompts/cross_validate.md alpha_agents/agents/cross_validate.py
git commit -m "feat: add cross-validation agent — 4-dimension stock verification"
```

---

### Task 2: Create morning scan agent

**Files:**
- Create: `alpha_agents/prompts/morning_scan.md`
- Create: `alpha_agents/agents/morning.py`
- Modify: `alpha_agents/pipeline/tasks/morning_scan.py`

- [ ] **Step 1: Create the prompt**

Create `alpha_agents/prompts/morning_scan.md`:

```markdown
# 你是AlphaAgents晨间分析师

你负责每天开盘前的晨间扫描，为投资者提供今日交易方向预判。

## 你的输入

你会收到以下信息：
1. 隔夜外盘动态（通过新闻获取）
2. 当前活跃主线及其状态
3. 近7天预测命中率
4. 各板块的市场认知

## 工作流程

1. 先调用 get_market_breadth 了解最新市场情绪
2. 调用 get_sector_ranking 查看行业资金流排名
3. 调用 get_anomaly_stocks 查看涨停板分布
4. 结合新闻事件和主线状态，预判今日哪些方向可能有动作
5. 对有看好方向的标的，调用 search_stocks 检索相关个股
6. 对检索到的个股，调用 filter_stocks 过滤
7. 输出晨报

## 输出格式（严格遵循，30秒可读完）

```
=== AlphaAgents 晨报 | {日期} ===

【隔夜外盘】
• 美股: {涨跌} | 原油: {价格} | 黄金: {价格}

【市场情绪】
• 涨跌比: {X} ({情绪}) | 涨停: {X}家 | 跌停: {X}家

【活跃主线预判】
1. {主线名}（强度 X/10）— {今日预判}
2. ...

【今日关注事件】
• {事件1}
• {事件2}

【推荐关注】
| 代码 | 名称 | 主线 | 理由 | 信心 |
|------|------|------|------|------|
| ... | ... | ... | ... | 高/中 |

【风险提示】
• {主要风险}
```

## 重要原则

- 晨报必须简洁，用户30秒内读完
- 推荐股票必须经过工具验证（search_stocks + filter_stocks）
- 每条主线最多推荐2只标的
- 标注预测命中率，让用户知道系统的可靠度
- 不使用emoji，不使用markdown标题(#)
```

- [ ] **Step 2: Create the agent module**

Create `alpha_agents/agents/morning.py`:

```python
"""Morning scan agent — pre-market analysis and daily briefing."""

import asyncio
import json
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    search_stocks, get_sector_data, filter_stocks,
    get_stock_quotes, get_market_breadth, get_sector_ranking,
    get_anomaly_stocks, web_search, get_pizzint,
)

logger = logging.getLogger(__name__)

# Morning agent gets analysis tools but NOT news tools (news is pre-fetched)
MORNING_TOOLS = [
    search_stocks, get_sector_data, filter_stocks,
    get_stock_quotes, get_market_breadth, get_sector_ranking,
    get_anomaly_stocks, web_search, get_pizzint,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_morning_agent() -> Agent:
    prompt = (PROMPTS_DIR / "morning_scan.md").read_text(encoding="utf-8")
    return Agent(
        name="morning_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=MORNING_TOOLS,
    )


async def run_morning_analysis(
    events_summary: str,
    themes_context: str,
    stats_context: str,
    hooks=None,
) -> str:
    """Run morning scan analysis with memory context.

    Args:
        events_summary: Pre-digested news events as text.
        themes_context: Active theme lines summary.
        stats_context: Prediction hit rate stats.
        hooks: Optional event hooks.

    Returns:
        Morning briefing report text.
    """
    agent = _create_morning_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【活跃主线】\n{themes_context}\n\n"
        f"【近期预测表现】\n{stats_context}\n\n"
        f"【隔夜新闻事件】\n{events_summary}\n\n"
        f"请生成今日晨报。"
    )

    logger.info("Morning agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=40),
            timeout=300,
        )
        logger.info("Morning agent finished, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.error("Morning agent timed out after 300s")
        return "[晨扫超时，未生成报告]"
    except Exception as e:
        logger.error("Morning agent failed: %s", e)
        return f"[晨扫失败: {e}]"
```

- [ ] **Step 3: Upgrade morning_scan.py task**

Replace the entire content of `alpha_agents/pipeline/tasks/morning_scan.py`:

```python
"""Morning scan task — runs at 06:30 before market open.

Fetches overnight news, reads memory context, and runs the morning
analyst agent to produce a daily briefing.
"""

import json
import logging
import time

from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
)
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.pipeline.monitor import NEWS_SOURCES
from alpha_agents.agents.morning import run_morning_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _format_themes(themes: list[dict]) -> str:
    """Format active themes into a readable context string."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        stock_names = ", ".join(s["name"] for s in stocks[:5])
        lines.append(
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} | 标的: {stock_names}\n"
            f"  催化: {t.get('catalyst', '无')}"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats into readable text."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    hit_rate = stats.get("hit_rate", 0)
    hits = stats.get("hits", 0)
    by_conf = stats.get("by_confidence", {})
    lines = [f"近7天命中率: {hit_rate:.1f}% ({hits}/{total})"]
    for conf, data in by_conf.items():
        lines.append(f"  {conf}信心: {data['hit_rate']:.1f}% ({data['hits']}/{data['total']})")
    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    """Format digested events into readable text."""
    if not events:
        return "无重要事件"
    lines = []
    for e in events:
        lines.append(
            f"- [{e.get('category', '?')}] {e.get('event', '?')} "
            f"(重要性 {e.get('importance', 0)}/5, {e.get('credibility', '?')})\n"
            f"  摘要: {e.get('summary', '')[:100]}"
        )
    return "\n".join(lines)


async def run_morning_scan() -> str | None:
    """Execute the morning scan task.

    1. Fetch overnight news from all sources
    2. Read active theme lines, stats, cognition from memory
    3. Digest news into events
    4. Run morning analyst agent with full context
    5. Push notification

    Returns the morning report text, or None if nothing significant.
    """
    import asyncio

    logger.info("Morning scan starting...")

    # 1. Fetch news from all sources
    news_items = []
    for source_id, name, fetch_fn_factory in NEWS_SOURCES:
        try:
            raw = await asyncio.to_thread(fetch_fn_factory)
            data = json.loads(raw)
            items = data.get("news", [])
            news_items.extend(items)
        except Exception as e:
            logger.debug("Morning scan: %s unavailable: %s", name, e)

    if not news_items:
        logger.info("Morning scan: no news items")
        return None

    # 2. Read memory
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    logger.info("Morning scan: %d news items, %d active themes", len(news_items), len(themes))

    # 3. Digest news
    events = await digest_news(news_items)
    if not events:
        logger.info("Morning scan: no significant events")
        return None

    # 4. Run morning agent with context
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)
    events_ctx = _format_events(events)

    report = await run_morning_analysis(events_ctx, themes_ctx, stats_ctx)

    # 5. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 晨报 | {time.strftime('%m-%d')}",
                report[:500],
            )
        except Exception as e:
            logger.debug("Morning notification failed: %s", e)

    print(report)
    return report
```

- [ ] **Step 4: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.agents.morning import _create_morning_agent
agent = _create_morning_agent()
print(f'Agent: {agent.name}, tools: {len(agent.tools)}')
assert agent.name == 'morning_analyst'
assert len(agent.tools) == 9

from alpha_agents.pipeline.tasks.morning_scan import _format_themes, _format_stats
themes_text = _format_themes([])
assert themes_text == '无活跃主线'
stats_text = _format_stats({'total': 10, 'hits': 7, 'hit_rate': 70.0, 'by_confidence': {'high': {'total': 5, 'hits': 4, 'hit_rate': 80.0}}})
assert '70.0%' in stats_text
print('TEST PASSED')
"
```

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/prompts/morning_scan.md alpha_agents/agents/morning.py alpha_agents/pipeline/tasks/morning_scan.py
git commit -m "feat: add morning scan agent with memory context and news digest"
```

---

### Task 3: Create intraday agent

**Files:**
- Create: `alpha_agents/prompts/intraday.md`
- Create: `alpha_agents/agents/intraday.py`
- Modify: `alpha_agents/pipeline/tasks/intraday_monitor.py`

- [ ] **Step 1: Create the prompt**

Create `alpha_agents/prompts/intraday.md`:

```markdown
# 你是AlphaAgents盘中监控分析师

你负责盘中异动检测和追因分析。只有检测到显著异动时才输出提醒。

## 你的输入

你会收到以下信息：
1. 当前活跃主线及核心标的
2. 板块资金流排名（哪些板块资金正在涌入/流出）
3. 涨停板分布（哪个方向涨停最多）

## 思维链路（异动追因）

1. 观察：哪个板块/个股出现异动？（突然放量、资金涌入、龙头涨停）
2. 追因：为什么？
   - 调用 web_search 搜索最新新闻
   - 调用 get_lhb_detail 或 get_north_flow 查资金来源
   - 查看关联板块是否联动
3. 判断：一日游还是持续行情？
   - 机构资金 + 明确催化 + 多板块联动 → 可能是新主线或主线加强
   - 游资席位 + 无催化 + 单一个股 → 一日游，不追
4. 决策：是否推荐？推荐什么？

## 异动判定标准

以下情况算"异动"，需要输出提醒：
- 某板块15分钟内涨幅 > 2%
- 活跃主线的龙头股涨停或跌停
- 某个方向连续出现3只以上涨停
- 板块资金流与主线预期严重背离

如果无异动，直接输出"无异动"三个字，不要输出其他内容。

## 输出格式（仅异动时输出）

```
=== 盘中提醒 | {时间} ===

【异动】{板块名} 突然放量拉升 +{X}%
• 龙头: {股票名} {涨跌情况}
• 资金: {板块资金净流入/流出}
• 催化: {新闻原因，如果有的话}
• 判断: {一日游/持续行情/新主线}
• 建议: {关注XX / 回避追高 / 等回调}

【主线状态变化】
• {主线名}: 强度 X→Y（{原因}）
```

## 重要原则

- 无异动时不要输出任何分析，直接输出"无异动"
- 不要为了输出而输出，宁缺毋滥
- 异动提醒必须简洁，交易员需要快速决策
- 不使用emoji
```

- [ ] **Step 2: Create the agent module**

Create `alpha_agents/agents/intraday.py`:

```python
"""Intraday monitoring agent — detects anomalies and traces causes."""

import asyncio
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    get_sector_data, get_sector_ranking, get_anomaly_stocks,
    get_stock_quotes, get_stock_fund_flow, get_north_flow,
    get_lhb_detail, web_search,
)

logger = logging.getLogger(__name__)

INTRADAY_TOOLS = [
    get_sector_data, get_sector_ranking, get_anomaly_stocks,
    get_stock_quotes, get_stock_fund_flow, get_north_flow,
    get_lhb_detail, web_search,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_intraday_agent() -> Agent:
    prompt = (PROMPTS_DIR / "intraday.md").read_text(encoding="utf-8")
    return Agent(
        name="intraday_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=INTRADAY_TOOLS,
    )


async def run_intraday_analysis(context: str, hooks=None) -> str:
    """Run intraday anomaly detection and cause-tracing.

    Args:
        context: Active themes and their core stocks as text.
        hooks: Optional event hooks.

    Returns:
        Alert text if anomaly found, "无异动" otherwise.
    """
    agent = _create_intraday_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【当前活跃主线和监控标的】\n{context}\n\n"
        f"请检查是否有异动。如果无异动，直接输出"无异动"。"
    )

    logger.info("Intraday agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=20),
            timeout=120,
        )
        output = result.final_output
        logger.info("Intraday agent finished, length=%d", len(output))
        return output
    except asyncio.TimeoutError:
        logger.warning("Intraday agent timed out")
        return "无异动"
    except Exception as e:
        logger.warning("Intraday agent failed: %s", e)
        return "无异动"
```

- [ ] **Step 3: Upgrade intraday_monitor.py task**

Replace the entire content of `alpha_agents/pipeline/tasks/intraday_monitor.py`:

```python
"""Intraday monitoring task — runs every 15 minutes during trading hours.

Detects anomalies in active theme stocks and sector fund flows,
then traces causes and generates alerts.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import get_active_themes
from alpha_agents.agents.intraday import run_intraday_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _format_themes_for_monitoring(themes: list[dict]) -> str:
    """Format themes into monitoring context."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        stock_list = ", ".join(f"{s['code']} {s['name']}" for s in stocks[:10])
        lines.append(
            f"主线: {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {t.get('leader_code', '无')}\n"
            f"  监控标的: {stock_list}"
        )
    return "\n".join(lines)


async def run_intraday_monitor() -> str | None:
    """Execute one intraday monitoring cycle.

    1. Get active theme lines and core stocks
    2. Run intraday agent to detect anomalies
    3. If anomaly found, push notification

    Returns alert text if anomaly found, None otherwise.
    """
    import asyncio

    themes = get_active_themes()
    if not themes:
        logger.debug("Intraday monitor: no active themes")
        return None

    context = _format_themes_for_monitoring(themes)
    logger.info("Intraday monitor: watching %d themes", len(themes))

    output = await run_intraday_analysis(context)

    if output and output.strip() != "无异动":
        logger.info("Intraday anomaly detected!")
        print(output)

        # Push notification
        try:
            now = datetime.now().strftime("%H:%M")
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 盘中提醒 | {now}",
                output[:500],
            )
        except Exception as e:
            logger.debug("Intraday notification failed: %s", e)

        return output

    logger.debug("Intraday monitor: no anomaly")
    return None
```

- [ ] **Step 4: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.agents.intraday import _create_intraday_agent
agent = _create_intraday_agent()
print(f'Agent: {agent.name}, tools: {len(agent.tools)}')
assert agent.name == 'intraday_analyst'
assert len(agent.tools) == 8

from alpha_agents.pipeline.tasks.intraday_monitor import _format_themes_for_monitoring
text = _format_themes_for_monitoring([])
assert text == '无活跃主线'
print('TEST PASSED')
"
```

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/prompts/intraday.md alpha_agents/agents/intraday.py alpha_agents/pipeline/tasks/intraday_monitor.py
git commit -m "feat: add intraday agent with anomaly detection and cause-tracing"
```

---

### Task 4: Create review agent

**Files:**
- Create: `alpha_agents/prompts/review.md`
- Create: `alpha_agents/agents/review_agent.py`
- Modify: `alpha_agents/pipeline/tasks/review.py`

- [ ] **Step 1: Create the prompt**

Create `alpha_agents/prompts/review.md`:

```markdown
# 你是AlphaAgents复盘分析师

你负责收盘后的复盘工作：验证预测、更新主线、总结经验。

## 你的输入

你会收到以下信息：
1. 今日待验证的预测列表（股票代码、推荐方向、推荐价格）
2. 当前活跃主线及其状态
3. 近7天预测命中率统计

## 工作流程

1. 验证预测：对每只待验证的股票调用 get_stock_quotes 获取今日收盘价，计算收益率
2. 评估主线：对每条主线调用 get_sector_data 和 get_sector_ranking 查资金流向，判断主线强弱变化
3. 查看龙虎榜：调用 get_lhb_detail 查看今日机构动向
4. 查看北向资金：调用 get_north_flow 查看外资方向
5. 生成复盘报告

## 预测验证规则

- 推荐方向为"看多"，且当日涨幅 > 0% → 命中
- 推荐方向为"看多"，且当日跌幅 > 2% → 未命中
- 推荐方向为"看多"，涨跌幅在 -2%~0% → 中性，不计入命中率
- 推荐方向为"看空"，反向判断

## 主线评估规则

对每条主线，根据今日数据给出信号评估：
- 板块跑赢大盘 + 资金净流入 → 加强
- 板块跑输大盘 + 资金净流出 → 减弱
- 龙头涨停 → 强信号加强
- 龙头跌停或破5日均线 → 强信号减弱

## 输出格式

```
=== AlphaAgents 复盘 | {日期} ===

【今日推荐验证】
| 代码 | 名称 | 方向 | 推荐价 | 收盘价 | 涨跌 | 结果 |
|------|------|------|-------|-------|------|------|
| ... | ... | ... | ... | ... | ... | 命中/未命中 |
命中率: X/Y (Z%)

【主线状态更新】
• {主线名}: 强度 X→Y（{原因}）
  信号: {加强/减弱/持平}

【龙虎榜关键信号】
• {股票}: {机构买入/卖出} {金额}亿 → {解读}

【北向资金方向】
• 今日净{买入/卖出} {X}亿
• 重点增持: {股票1}, {股票2}

【经验总结】
• 今日做对了什么: {经验}
• 今日做错了什么: {教训}
• 明日关注: {展望}
```

## 重要原则

- 验证预测时必须调用工具获取真实收盘价，不得推断
- 对每条主线的评估必须基于今日实际数据
- 经验总结要具体，不要泛泛而谈
- 不使用emoji
```

- [ ] **Step 2: Create the agent module**

Create `alpha_agents/agents/review_agent.py`:

```python
"""Post-market review agent — verifies predictions and updates theme lines."""

import asyncio
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    get_stock_quotes, get_sector_data, get_sector_ranking,
    get_lhb_detail, get_north_flow, get_market_breadth,
    get_stock_fund_flow, get_block_trade,
)

logger = logging.getLogger(__name__)

REVIEW_TOOLS = [
    get_stock_quotes, get_sector_data, get_sector_ranking,
    get_lhb_detail, get_north_flow, get_market_breadth,
    get_stock_fund_flow, get_block_trade,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_review_agent() -> Agent:
    prompt = (PROMPTS_DIR / "review.md").read_text(encoding="utf-8")
    return Agent(
        name="review_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=REVIEW_TOOLS,
    )


async def run_review_analysis(
    predictions_context: str,
    themes_context: str,
    stats_context: str,
    hooks=None,
) -> str:
    """Run post-market review analysis.

    Args:
        predictions_context: Today's predictions to verify.
        themes_context: Active theme lines to evaluate.
        stats_context: Recent prediction stats.
        hooks: Optional event hooks.

    Returns:
        Review report text.
    """
    agent = _create_review_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【待验证预测】\n{predictions_context}\n\n"
        f"【活跃主线】\n{themes_context}\n\n"
        f"【近期预测表现】\n{stats_context}\n\n"
        f"请执行收盘复盘分析。"
    )

    logger.info("Review agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=40),
            timeout=300,
        )
        logger.info("Review agent finished, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.error("Review agent timed out after 300s")
        return "[复盘超时，未生成报告]"
    except Exception as e:
        logger.error("Review agent failed: %s", e)
        return f"[复盘失败: {e}]"
```

- [ ] **Step 3: Upgrade review.py task**

Replace the entire content of `alpha_agents/pipeline/tasks/review.py`:

```python
"""Post-market review task — runs at 15:30 after market close.

Verifies today's predictions, updates theme line strengths,
updates market cognition, and generates review report.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition,
)
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength, retire_stale_themes,
)
from alpha_agents.agents.review_agent import run_review_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _format_predictions(predictions: list[dict]) -> str:
    """Format pending predictions for the review agent."""
    if not predictions:
        return "今日无待验证预测"
    lines = []
    for p in predictions:
        lines.append(
            f"- {p['code']} {p.get('name', '?')} | 方向: {p['direction']} | "
            f"信心: {p.get('confidence', '?')} | 推荐价: {p.get('entry_price', '?')} | "
            f"主线: {p.get('theme_line', '?')} | 理由: {p.get('reason', '')[:50]}"
        )
    return "\n".join(lines)


def _format_themes(themes: list[dict]) -> str:
    """Format active themes for the review agent."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        lines.append(
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} ({t.get('leader_code', '?')})"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    return f"近7天命中率: {stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{total})"


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Get pending predictions and format for agent
    2. Get active themes and format for agent
    3. Run review agent to verify and analyze
    4. Retire stale themes
    5. Push notification

    Returns the review report text.
    """
    import asyncio

    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("Review starting for %s...", today)

    # 1. Get data
    pending = get_pending_predictions(today)
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)

    logger.info("Review: %d predictions, %d themes", len(pending), len(themes))

    # 2. Format contexts
    pred_ctx = _format_predictions(pending)
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)

    # 3. Run review agent
    report = await run_review_analysis(pred_ctx, themes_ctx, stats_ctx)

    # 4. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 5. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 复盘 | {today}",
                report[:500],
            )
        except Exception as e:
            logger.debug("Review notification failed: %s", e)

    print(report)
    return report
```

- [ ] **Step 4: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.agents.review_agent import _create_review_agent
agent = _create_review_agent()
print(f'Agent: {agent.name}, tools: {len(agent.tools)}')
assert agent.name == 'review_analyst'
assert len(agent.tools) == 8

from alpha_agents.pipeline.tasks.review import _format_predictions, _format_themes
text = _format_predictions([])
assert text == '今日无待验证预测'
text2 = _format_themes([])
assert text2 == '无活跃主线'
print('TEST PASSED')
"
```

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/prompts/review.md alpha_agents/agents/review_agent.py alpha_agents/pipeline/tasks/review.py
git commit -m "feat: add review agent with prediction verification and theme updates"
```

---

### Task 5: Integration test — run full scheduler cycle

Test all agents together via the scheduler.

**Files:** (none created)

- [ ] **Step 1: Test morning scan end-to-end**

```bash
set -a && source .env && set +a && uv run python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from alpha_agents.data.memory_store import upsert_theme
from alpha_agents.pipeline.tasks.morning_scan import run_morning_scan

# Seed a theme so morning scan has context
upsert_theme('AI算力', status='active', strength=8,
             core_stocks=[{'code': '300308', 'name': '中际旭创', 'role': '龙头'}],
             leader_code='300308', catalyst='博通TPU合作')

async def test():
    report = await run_morning_scan()
    if report:
        print(f'Morning report ({len(report)} chars):')
        print(report[:800])
    else:
        print('No report (OK if no significant news)')
    print('MORNING TEST DONE')

asyncio.run(test())
" 2>&1 | grep -E "Morning|DONE|Agent|晨报|报告|主线|推荐" | head -15
```
Expected: Morning agent runs, produces a briefing with theme context.

- [ ] **Step 2: Test intraday monitor**

```bash
set -a && source .env && set +a && uv run python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from alpha_agents.pipeline.tasks.intraday_monitor import run_intraday_monitor

async def test():
    output = await run_intraday_monitor()
    print(f'Intraday output: {output[:200] if output else \"None (OK)\"}')
    print('INTRADAY TEST DONE')

asyncio.run(test())
" 2>&1 | grep -E "Intraday|DONE|agent|异动|无异动" | head -10
```
Expected: Either detects anomaly or outputs "无异动".

- [ ] **Step 3: Test review**

```bash
set -a && source .env && set +a && uv run python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from alpha_agents.data.memory_store import save_prediction
from alpha_agents.pipeline.tasks.review import run_review
from datetime import datetime

# Seed a prediction to review
save_prediction(datetime.now().strftime('%Y-%m-%d'), 'morning', '300308', '中际旭创', 'bullish', 'high', 'AI算力', 150.0, 'TPU概念龙头')

async def test():
    report = await run_review()
    if report:
        print(f'Review report ({len(report)} chars):')
        print(report[:800])
    print('REVIEW TEST DONE')

asyncio.run(test())
" 2>&1 | grep -E "Review|DONE|agent|复盘|命中|主线" | head -15
```
Expected: Review agent verifies the seeded prediction and produces a report.

- [ ] **Step 4: Commit if fixes needed**

```bash
git add -A && git commit -m "fix: phase 3 integration fixes" || echo "Nothing to fix"
```
