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
from agents import function_tool
from alpha_agents.tools.registry import STOCK_TOOLS
from alpha_agents.data.portfolio import (
    get_open_positions_summary, get_pending_orders, get_open_positions,
    get_portfolio_stats, format_portfolio_stats,
    create_pending_order, close_position, parse_entry_zone, parse_stop_loss,
    get_available_capital, TOTAL_CAPITAL,
)
from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats,
)

logger = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = """你是 AlphaAgents 的交互式分析师，用户可以随时向你提问。

## 你的角色
你是用户的量化分析搭档。系统在后台自动运行（晨扫、盘中监控、复盘），你负责回答用户关于持仓、推荐、市场的任何问题。

## 你能做的事
1. **分析个股** — 用户问"能不能买 XXX"，调 get_institutional_position 分析后给出详细建议（资金流、机构动向、价格位置、操作建议）
2. **执行买入** — 用户确认要买时，调 place_buy_order 创建挂单（必须先分析再买，不能盲买）
3. **执行卖出** — 用户说"卖掉 XXX"，调 place_sell_order 平仓
4. **解释持仓** — 为什么买了某只票、当前逻辑是否还成立
5. **回顾推荐** — 之前推荐的票表现如何、为什么对/错
6. **讨论主线** — 当前有哪些活跃主线、强度如何、趋势判断
7. **风险评估** — 当前持仓整体风险、哪些该减仓
8. **市场观点** — 大盘情绪、板块轮动、资金流向

## 买入流程
用户说"买 XXX"时，你必须：
1. 先调 get_institutional_position 分析该股票
2. 告诉用户分析结果（资金流、机构共识、价格位置、风险）
3. 给出操作建议（介入区间、止损位）
4. 用户确认后，调 place_buy_order 下单
不要跳过分析直接下单。

## 卖出流程
用户说"卖 XXX"时：
1. 查看当前持仓和浮盈
2. 告诉用户当前盈亏情况
3. 用户确认后，调 place_sell_order 平仓

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


@function_tool
def place_buy_order(code: str, name: str, theme: str, entry_low: float = 0, entry_high: float = 0, stop_loss: float = 0, reason: str = "") -> str:
    """创建买入挂单。价格到达介入区间自动建仓。

    Args:
        code: 股票代码，如 "002384"
        name: 股票名称，如 "东山精密"
        theme: 关联主线，如 "铜缆高速连接"
        entry_low: 介入区间下限（突破型买入，价格涨到此处买入）。回调型填0
        entry_high: 介入区间上限（回调型买入，价格跌到此处买入）。突破型填0
        stop_loss: 止损价（必填）
        reason: 买入理由
    """
    import time
    today = time.strftime("%Y-%m-%d")

    if not stop_loss:
        return json.dumps({"error": "必须设置止损价"}, ensure_ascii=False)
    if not entry_low and not entry_high:
        return json.dumps({"error": "必须设置介入区间（entry_low 或 entry_high 至少填一个）"}, ensure_ascii=False)

    result = create_pending_order(
        code=code, name=name, theme=theme,
        order_date=today,
        entry_low=entry_low or None,
        entry_high=entry_high or None,
        stop_loss=stop_loss,
        source="chat",
        reason=reason[:100],
    )
    if result:
        available = get_available_capital()
        zone = f"{entry_low}-{entry_high}" if entry_low and entry_high else f"≤{entry_high}" if entry_high else f"≥{entry_low}"
        return json.dumps({
            "status": "挂单已创建",
            "code": code, "name": name,
            "entry_zone": zone,
            "stop_loss": stop_loss,
            "available_capital": f"{available:,.0f}元",
        }, ensure_ascii=False)
    else:
        return json.dumps({"status": "挂单创建失败（可能已有同名挂单或持仓）"}, ensure_ascii=False)


@function_tool
def place_sell_order(code: str) -> str:
    """卖出/平仓指定股票。

    Args:
        code: 要卖出的股票代码，如 "002384"
    """
    from alpha_agents.data.market_data import get_realtime_quotes

    positions = get_open_positions()
    target = None
    for p in positions:
        if p["code"] == code:
            target = p
            break

    if not target:
        return json.dumps({"error": f"未找到 {code} 的持仓"}, ensure_ascii=False)

    # Get realtime price for closing
    rt = get_realtime_quotes([code])
    if rt and code in rt:
        close_price = rt[code]["price"]
    else:
        return json.dumps({"error": f"无法获取 {code} 的实时价格"}, ensure_ascii=False)

    shares = target.get("shares", 0)
    open_price = target.get("open_price", 0)
    pnl = round((close_price - open_price) * shares, 2)
    pnl_pct = round((close_price - open_price) / open_price * 100, 2) if open_price else 0

    success = close_position(target["id"], close_price=close_price, close_reason="用户手动平仓")
    if success:
        return json.dumps({
            "status": "已平仓",
            "code": code,
            "name": target.get("name", ""),
            "shares": shares,
            "open_price": open_price,
            "close_price": close_price,
            "pnl": f"{pnl:+,.0f}元",
            "pnl_pct": f"{pnl_pct:+.2f}%",
        }, ensure_ascii=False)
    else:
        return json.dumps({"error": "平仓失败"}, ensure_ascii=False)


@function_tool
def show_portfolio() -> str:
    """查看当前持仓、挂单和资金状况。"""
    from alpha_agents.data.market_data import get_realtime_quotes

    positions = get_open_positions()
    pending = get_pending_orders()
    available = get_available_capital()

    lines = [f"总资金 {TOTAL_CAPITAL:,}元 | 已投 {TOTAL_CAPITAL - available:,.0f}元 | 可用 {available:,.0f}元"]

    if positions:
        codes = [p["code"] for p in positions]
        rt = get_realtime_quotes(codes) or {}
        lines.append(f"\n持仓 {len(positions)} 笔:")
        total_pnl = 0
        for p in positions:
            code = p["code"]
            shares = p.get("shares", 0)
            open_price = p.get("open_price", 0)
            real = rt.get(code, {})
            price = real.get("price", 0)
            if price and open_price:
                pnl = (price - open_price) * shares
                pnl_pct = (price - open_price) / open_price * 100
                total_pnl += pnl
                lines.append(
                    f"  {code} {p.get('name','')} {shares}股 @ {open_price:.2f} → {price:.2f} "
                    f"({pnl_pct:+.1f}%, {pnl:+,.0f}元) 止损{p.get('stop_loss') or '无'}"
                )
            else:
                lines.append(f"  {code} {p.get('name','')} {shares}股 @ {open_price:.2f} 止损{p.get('stop_loss') or '无'}")
        lines.append(f"  总浮盈: {total_pnl:+,.0f}元")

    if pending:
        lines.append(f"\n挂单 {len(pending)} 笔:")
        for p in pending:
            zone = ""
            if p.get("entry_high") and p.get("entry_low"):
                zone = f"{p['entry_low']:.2f}-{p['entry_high']:.2f}"
            elif p.get("entry_high"):
                zone = f"≤{p['entry_high']:.2f}"
            elif p.get("entry_low"):
                zone = f"≥{p['entry_low']:.2f}"
            lines.append(f"  {p['code']} {p.get('name','')} 介入{zone} 止损{p.get('stop_loss') or '无'}")

    if not positions and not pending:
        lines.append("无持仓/挂单")

    return "\n".join(lines)


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
        tools=STOCK_TOOLS + [place_buy_order, place_sell_order, show_portfolio],
    )


async def run_chat():
    """Interactive chat loop with conversation history and rich output."""
    from alpha_agents.agents.hooks import ToolEventHooks
    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    console = Console()
    hooks = ToolEventHooks(callback=None, agent_label="chat")

    # Redirect logging to file in chat mode so it doesn't pollute the UI
    _setup_chat_logging()

    console.print(Panel.fit(
        "[bold]AlphaAgents 交互模式[/bold]\n"
        "问任何关于持仓、推荐、市场的问题\n"
        "支持: 分析个股 / 买卖操作 / 查看持仓 / 讨论主线\n"
        "命令: [dim]quit[/dim] 退出 | [dim]refresh[/dim] 刷新状态 | [dim]portfolio[/dim] 查看持仓",
        title="AlphaAgents", border_style="blue",
    ))

    agent = _create_chat_agent()
    conversation_history = []  # Accumulated conversation for context

    # prompt_toolkit session with history (arrow up/down for previous inputs)
    session = PromptSession(history=InMemoryHistory())
    loop = asyncio.get_event_loop()

    while True:
        try:
            user_input = (await loop.run_in_executor(
                None, lambda: session.prompt("你: ")
            )).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            console.print("[dim]再见[/dim]")
            break
        if user_input.lower() == "refresh":
            agent = _create_chat_agent()
            conversation_history = []
            console.print("[dim]系统状态已刷新，对话历史已清空[/dim]")
            continue
        if user_input.lower() in ("portfolio", "持仓", "仓位"):
            # Quick shortcut: show portfolio without LLM call
            console.print(Panel(show_portfolio(), title="持仓概览", border_style="green"))
            continue

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"[{now}] {user_input}"

        console.print("[dim]分析中...[/dim]")

        try:
            result = await asyncio.wait_for(
                Runner.run(
                    agent, message,
                    hooks=hooks, max_turns=30,
                ),
                timeout=120,
            )

            output = result.final_output
            console.print(Panel(output, title="分析师", border_style="cyan"))

        except asyncio.TimeoutError:
            console.print("[red]回答超时，请重试[/red]")
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]")


def _setup_chat_logging():
    """In chat mode, redirect scheduler/pipeline logs to file instead of terminal."""
    import logging
    from alpha_agents.config import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = DATA_DIR / "scheduler.log"

    # Remove existing console handlers from root logger
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)

    # Add file handler
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Keep a minimal console handler for errors only
    ch = logging.StreamHandler()
    ch.setLevel(logging.ERROR)
    root.addHandler(ch)

    logger.info("Chat mode: logs redirected to %s", log_file)
