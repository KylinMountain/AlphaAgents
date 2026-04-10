"""Interactive chat agent — discuss portfolio, ask questions, get analysis.

Runs alongside the autonomous system. Has full access to:
- All market data tools (same as intraday/morning agents)
- Portfolio state (positions, pending orders, P&L)
- Theme lines and memory
- Prediction history
"""

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
from alpha_agents.tools.registry import STOCK_TOOLS
from alpha_agents.data.portfolio import (
    get_open_positions_summary, get_pending_orders,
    get_portfolio_stats, format_portfolio_stats,
)
from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats,
)

logger = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = """你是 AlphaAgents 的交互式分析师，用户可以随时向你提问。

## 你的角色
你是用户的量化分析搭档。系统在后台自动运行（晨扫、盘中监控、复盘），你负责回答用户关于持仓、推荐、市场的任何问题。

## 你能做的事
1. **解释持仓** — 为什么买了某只票、当前逻辑是否还成立
2. **分析个股** — 用户问"能不能买 XXX"，你调工具分析后给出建议
3. **回顾推荐** — 之前推荐的票表现如何、为什么对/错
4. **讨论主线** — 当前有哪些活跃主线、强度如何、趋势判断
5. **风险评估** — 当前持仓整体风险、哪些该减仓
6. **市场观点** — 大盘情绪、板块轮动、资金流向

## 当前状态
{portfolio_summary}

{themes_summary}

{stats_summary}

## 工具
你有完整的市场数据工具，包括：实时行情、板块排名、龙虎榜、北向资金、融资融券、个股资金流、机构持仓分析等。
需要数据时直接调用工具，不要凭记忆编造数字。

## 重要原则
- **所有价格必须来自工具返回值**，绝不凭记忆编造
- 直接回答问题，不要废话
- 给出操作建议时要有数据支撑
- 如果不确定，说"我查一下"然后调工具
- 不使用emoji
"""


def _build_context() -> str:
    """Build current system context for the chat agent."""
    # Portfolio
    try:
        portfolio = get_open_positions_summary()
    except Exception:
        portfolio = "持仓数据不可用"

    # Pending orders
    try:
        pending = get_pending_orders()
        if pending:
            lines = [f"挂单 {len(pending)} 笔:"]
            for p in pending:
                zone = ""
                if p.get("entry_high") and p.get("entry_low"):
                    zone = f"{p['entry_low']:.2f}-{p['entry_high']:.2f}"
                elif p.get("entry_high"):
                    zone = f"≤{p['entry_high']:.2f}"
                elif p.get("entry_low"):
                    zone = f"≥{p['entry_low']:.2f}"
                lines.append(f"  {p['code']} {p.get('name','')} 介入{zone} 止损{p.get('stop_loss') or '无'}")
            portfolio += "\n" + "\n".join(lines)
    except Exception:
        pass

    # Themes
    try:
        themes = get_active_themes()
        theme_lines = []
        for t in themes:
            stocks = json.loads(t["core_stocks"]) if t.get("core_stocks") else []
            leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
            theme_lines.append(f"  {t['name']}（强度{t['strength']}/10, {t['status']}）龙头: {leader}")
        themes_text = f"活跃主线 {len(themes)} 条:\n" + "\n".join(theme_lines) if theme_lines else "无活跃主线"
    except Exception:
        themes_text = "主线数据不可用"

    # Stats
    try:
        stats = get_prediction_stats(days=7)
        perf = format_portfolio_stats(get_portfolio_stats(days=7))
        stats_text = f"近7天命中率: {stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{stats.get('total', 0)})\n{perf}"
    except Exception:
        stats_text = "统计数据不可用"

    return CHAT_SYSTEM_PROMPT.format(
        portfolio_summary=portfolio,
        themes_summary=themes_text,
        stats_summary=stats_text,
    )


def _create_chat_agent() -> Agent:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)

    return Agent(
        name="chat_analyst",
        instructions=_build_context(),
        model=model,
        tools=STOCK_TOOLS,
    )


async def run_chat():
    """Interactive chat loop."""
    from alpha_agents.agents.hooks import ToolEventHooks
    hooks = ToolEventHooks(callback=None, agent_label="chat")

    print("=" * 60)
    print("AlphaAgents 交互模式")
    print("你可以问任何关于持仓、推荐、市场的问题")
    print("输入 'quit' 或 'exit' 退出")
    print("输入 'refresh' 刷新系统状态")
    print("=" * 60)
    print()

    agent = _create_chat_agent()

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见")
            break
        if user_input.lower() == "refresh":
            agent = _create_chat_agent()
            print("[系统状态已刷新]")
            continue

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"[{now}] {user_input}"

        try:
            result = await asyncio.wait_for(
                Runner.run(agent, message, hooks=hooks, max_turns=30),
                timeout=120,
            )
            print(f"\n分析师: {result.final_output}\n")
        except asyncio.TimeoutError:
            print("\n[回答超时，请重试]\n")
        except Exception as e:
            print(f"\n[错误: {e}]\n")
