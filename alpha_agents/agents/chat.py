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
    save_chat_memory, get_recent_chat_memories,
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

## 历史对话记忆
{chat_memories}

## 重要原则
- **所有价格必须来自工具返回值**，绝不凭记忆编造
- 直接回答问题，不要废话
- 给出操作建议时要有数据支撑
- 如果不确定，说"我查一下"然后调工具
- 用户提到之前讨论过的股票/操作，参考历史对话记忆
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


@function_tool
def show_market_overview() -> str:
    """查看大盘概览：主要指数、涨跌比、市场情绪。用于快速了解当前市场状态。"""
    from alpha_agents.tools.market_breadth import get_market_breadth_fn
    from alpha_agents.tools.global_market import get_global_overview_fn
    lines = []
    try:
        breadth = json.loads(get_market_breadth_fn())
        adv = breadth.get("advances", 0)
        dec = breadth.get("declines", 0)
        ratio = breadth.get("advance_decline_ratio", 1)
        sentiment = breadth.get("sentiment", "")
        lines.append(f"涨: {adv} | 跌: {dec} | 涨跌比: {ratio:.2f} ({sentiment})")
        lines.append(f"涨停: {breadth.get('limit_up', 0)} | 跌停: {breadth.get('limit_down', 0)}")
    except Exception as e:
        lines.append(f"市场情绪: 获取失败 ({e})")
    try:
        overview = json.loads(get_global_overview_fn())
        for idx in overview.get("us_indices", []):
            lines.append(f"{idx['name']}: {idx['close']} ({idx['change_pct']:+.2f}%)")
        bonds = overview.get("bond_yields", {})
        if bonds.get("cn_us_spread"):
            lines.append(f"中美利差: {bonds['cn_us_spread']}%")
    except Exception:
        pass
    return "\n".join(lines) if lines else "无数据"


@function_tool
def show_sector_ranking(top_n: int = 10) -> str:
    """查看今日板块资金排名。显示资金流入最多和流出最多的板块。"""
    from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
    data = json.loads(get_sector_ranking_fn(top_n=top_n))
    lines = ["资金流入 Top:"]
    for s in data.get("gainers", [])[:top_n]:
        lines.append(f"  {s['sector']} {s['change_pct']:+.2f}% 净流入{s['net_flow_yi']:.1f}亿")
    lines.append("资金流出 Top:")
    for s in data.get("losers", [])[:5]:
        lines.append(f"  {s['sector']} {s['change_pct']:+.2f}% 净流出{abs(s['net_flow_yi']):.1f}亿")
    return "\n".join(lines)


@function_tool
def show_stock_quote(code: str) -> str:
    """快速查看个股实时行情。输入6位股票代码。

    Args:
        code: 股票代码，如 "002384"
    """
    from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
    data = json.loads(get_stock_quotes_fn(code))
    quotes = data.get("quotes", [])
    if not quotes:
        return f"未找到 {code} 的行情数据"
    q = quotes[0]
    if q.get("error"):
        return f"{code}: {q['error']}"
    rt = "实时" if q.get("realtime") else "收盘"
    return (f"{code} {q.get('name', '')} | {q['price']:.2f}元 ({q['change_pct']:+.2f}%) [{rt}]\n"
            f"周涨跌: {q.get('week_change_pct', 0):+.2f}% | "
            f"周高: {q.get('week_high', 0):.2f} 周低: {q.get('week_low', 0):.2f}")


@function_tool
def show_lhb() -> str:
    """查看今日龙虎榜——哪些股票有机构大额买卖。"""
    from alpha_agents.tools.fund_flow import get_lhb_detail_fn
    data = json.loads(get_lhb_detail_fn())
    items = data.get("data", [])[:15]
    if not items:
        return "今日无龙虎榜数据"
    lines = [f"龙虎榜 ({data.get('count', 0)}只，机构买入{data.get('institutional_buys', 0)}只):"]
    for item in items:
        tag = "机构" if item.get("is_institutional") else "游资"
        lines.append(f"  {item['code']} {item['name']} {item['change_pct']:+.1f}% "
                      f"净买{item['net_buy_yi']:+.2f}亿 [{tag}]")
    return "\n".join(lines)


@function_tool
def show_north_flow() -> str:
    """查看今日北向资金流向——外资在买什么。"""
    from alpha_agents.tools.fund_flow import get_north_flow_fn
    data = json.loads(get_north_flow_fn("today"))
    items = data.get("data", [])[:10]
    if not items:
        return "无北向资金数据"
    lines = ["北向资金持仓 Top 10:"]
    for item in items:
        chg = item.get("change_value_wan", 0)
        direction = "增持" if chg > 0 else "减持" if chg < 0 else "持平"
        lines.append(f"  {item['code']} {item['name']} 持仓{item.get('pct_of_float', 0):.2f}% "
                      f"{direction}{abs(chg)/10000:.2f}亿")
    return "\n".join(lines)


@function_tool
def show_limit_up() -> str:
    """查看今日涨停板分布——哪些方向最强。"""
    from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
    data = json.loads(get_anomaly_stocks_fn())
    summary = data.get("summary", {})
    lines = [f"涨停: {summary.get('limit_up_count', 0)}家 | "
             f"跌停: {summary.get('limit_down_count', 0)}家 | "
             f"炸板: {summary.get('broken_limit_count', 0)}家"]
    if summary.get("top_sector"):
        lines.append(f"涨停集中: {summary['top_sector']}")
    consecutive = summary.get("consecutive_limit_stocks", [])
    if consecutive:
        lines.append("连板股:")
        for s in consecutive[:8]:
            lines.append(f"  {s['name']}({s['consecutive_limits']}板)")
    return "\n".join(lines)


@function_tool
def cancel_pending_order(code: str) -> str:
    """取消指定股票的挂单。

    Args:
        code: 要取消挂单的股票代码，如 "002384"
    """
    from alpha_agents.data.portfolio import get_pending_orders, _cancel_order
    orders = get_pending_orders()
    target = None
    for o in orders:
        if o["code"] == code:
            target = o
            break
    if not target:
        return f"未找到 {code} 的挂单"
    _cancel_order(target["id"], "用户手动取消")
    return f"已取消 {code} {target.get('name', '')} 的挂单"


@function_tool
def show_trade_history(days: int = 7) -> str:
    """查看最近的交易记录和盈亏。

    Args:
        days: 查看最近几天，默认7天
    """
    from alpha_agents.data.portfolio import get_portfolio_stats, format_portfolio_stats
    from alpha_agents.data.memory_store import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT code, name, shares, open_price, close_price, return_pct, "
        "return_amount, close_reason, open_date, close_date "
        "FROM virtual_portfolio WHERE status NOT IN ('open', 'pending', 'cancelled') "
        "ORDER BY close_date DESC LIMIT ?",
        (days * 5,),
    ).fetchall()
    if not rows:
        return "暂无交易记录"
    lines = ["最近交易:"]
    for r in rows:
        ret = r["return_pct"] or 0
        amt = r["return_amount"] or 0
        lines.append(
            f"  {r['code']} {r['name']} {r['shares']}股 "
            f"{r['open_price']:.2f}→{r['close_price']:.2f} "
            f"({ret:+.1f}%, {amt:+,.0f}元) {r['close_reason']} "
            f"[{r['open_date']}~{r['close_date']}]"
        )
    stats = get_portfolio_stats(days=days)
    lines.append(f"\n{format_portfolio_stats(stats)}")
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

    # Load cross-session memories
    try:
        memories = get_recent_chat_memories(days=7)
        if memories:
            memories_text = "\n---\n".join(memories[-3:])  # Last 3 session summaries
        else:
            memories_text = "无历史对话记忆"
    except Exception:
        memories_text = "无法加载历史记忆"

    return CHAT_SYSTEM_PROMPT.format(
        portfolio_summary=portfolio,
        themes_summary=themes_text,
        stats_summary=stats_text,
        chat_memories=memories_text,
    )


def _create_chat_agent() -> Agent:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)

    return Agent(
        name="chat_analyst",
        instructions=_build_context(),
        model=model,
        tools=STOCK_TOOLS + [
            place_buy_order, place_sell_order, show_portfolio,
            show_market_overview, show_sector_ranking, show_stock_quote,
            show_lhb, show_north_flow, show_limit_up,
            cancel_pending_order, show_trade_history,
        ],
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
        "支持: 分析个股 / 买卖操作 / 查看持仓 / 讨论主线 / 查看新闻 / 手动晨扫\n"
        "输入 [dim]help[/dim] 查看所有命令",
        title="AlphaAgents", border_style="blue",
    ))

    from alpha_agents.agents.context_compressor import ContextCompressor

    agent = _create_chat_agent()
    conversation_history = []  # Accumulated conversation for context
    compressor = ContextCompressor()

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
        if user_input.lower() in ("help", "帮助", "?"):
            console.print(Panel(
                "[bold]行情查看（秒出，不调LLM）:[/bold]\n"
                "  market / 大盘        — 大盘指数+涨跌比+情绪\n"
                "  sectors / 板块       — 板块资金排名 Top10\n"
                "  查 002384            — 快速查个股实时行情\n"
                "  lhb / 龙虎榜         — 今日龙虎榜机构动向\n"
                "  north / 北向         — 北向资金流向\n"
                "  limitup / 涨停       — 涨停板分布\n"
                "\n[bold]持仓管理:[/bold]\n"
                "  portfolio / 持仓     — 持仓+挂单+资金\n"
                "  trades / 历史        — 最近交易记录和盈亏\n"
                "  risk / 风险          — 当前风险评估\n"
                "  撤单 002384          — 取消某只挂单\n"
                "\n[bold]数据源:[/bold]\n"
                "  news / 新闻          — 最新新闻\n"
                "  themes / 主线        — 活跃主线状态\n"
                "\n[bold]手动运行任务:[/bold]\n"
                "  morning / 晨扫 | opening / 开盘 | intraday / 盘中\n"
                "  review / 复盘 | night / 夜扫 | weekly / 周报\n"
                "\n[bold]系统:[/bold]\n"
                "  tasks / 任务         — 查看定时任务运行状态\n"
                "  refresh | quit | help\n"
                "\n[bold]自然语言（调AI分析）:[/bold]\n"
                "  分析一下东山精密 / 帮我买002384止损124元\n"
                "  通鼎互联要不要卖？ / 今天哪些板块资金流入最多？",
                title="帮助", border_style="yellow",
            ))
            continue
        if user_input.lower() in ("morning", "晨扫"):
            console.print("[dim]正在运行晨扫...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.morning_scan import run_morning_scan
                report = await run_morning_scan()
                if report:
                    console.print(Panel(report, title="晨报", border_style="green"))
                else:
                    console.print("[dim]晨扫未产生报告[/dim]")
            except Exception as e:
                console.print(f"[red]晨扫失败: {e}[/red]")
            continue
        if user_input.lower() in ("news", "新闻"):
            console.print("[dim]获取最新新闻...[/dim]")
            try:
                from alpha_agents.tools.registry import get_news
                import json as _j
                raw = get_news(limit=20)
                news = _j.loads(raw)
                lines = []
                for item in news.get("news", [])[:15]:
                    lines.append(f"  {item.get('time', '')} {item.get('title', '')}")
                console.print(Panel("\n".join(lines) if lines else "无新闻", title="最新新闻", border_style="yellow"))
            except Exception as e:
                console.print(f"[red]获取新闻失败: {e}[/red]")
            continue
        if user_input.lower() in ("themes", "主线"):
            try:
                themes = get_active_themes()
                lines = []
                for t in themes:
                    stocks = json.loads(t["core_stocks"]) if t.get("core_stocks") else []
                    leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
                    lines.append(f"  {t['name']} (强度{t['strength']}/10, {t['status']}) 龙头: {leader}")
                console.print(Panel("\n".join(lines) if lines else "无活跃主线", title="活跃主线", border_style="magenta"))
            except Exception as e:
                console.print(f"[red]获取主线失败: {e}[/red]")
            continue
        if user_input.lower() in ("tasks", "任务", "定时"):
            try:
                from alpha_agents.config import DATA_DIR
                import json as _j2
                state_file = DATA_DIR / "scheduler_state.json"
                state = {}
                if state_file.exists():
                    state = _j2.loads(state_file.read_text(encoding="utf-8"))

                from datetime import datetime as _dt
                now = _dt.now()
                today = now.strftime("%Y-%m-%d")
                tasks_info = [
                    ("morning_scan", "06:30", "晨扫分析"),
                    ("opening_reminder", "09:15", "开盘提醒"),
                    ("intraday_monitor", "09:30-15:00", "盘中监控(每5分钟)"),
                    ("review", "15:30", "收盘复盘"),
                    ("night_scan", "20:00", "夜扫分析"),
                    ("weekly_report", "周六 10:00", "周报"),
                ]
                lines = []
                for name, schedule, desc in tasks_info:
                    last_run = state.get(name, "")
                    if last_run and last_run.startswith(today):
                        status = f"[green]已完成[/green] ({last_run[11:16]})"
                    elif last_run:
                        status = f"[dim]上次: {last_run[:16]}[/dim]"
                    else:
                        status = "[yellow]未运行[/yellow]"
                    lines.append(f"  {schedule:15s} {desc:18s} {status}")
                console.print(Panel("\n".join(lines), title=f"定时任务 ({today})", border_style="blue"))
            except Exception as e:
                console.print(f"[red]获取任务状态失败: {e}[/red]")
            continue
        if user_input.lower() in ("market", "大盘"):
            console.print(Panel(show_market_overview(), title="大盘概览", border_style="blue"))
            continue
        if user_input.lower() in ("sectors", "板块"):
            console.print(Panel(show_sector_ranking(top_n=10), title="板块排名", border_style="yellow"))
            continue
        if user_input.lower() in ("lhb", "龙虎榜"):
            console.print(Panel(show_lhb(), title="龙虎榜", border_style="red"))
            continue
        if user_input.lower() in ("north", "北向"):
            console.print(Panel(show_north_flow(), title="北向资金", border_style="green"))
            continue
        if user_input.lower() in ("limitup", "涨停"):
            console.print(Panel(show_limit_up(), title="涨停板", border_style="red"))
            continue
        if user_input.lower() in ("trades", "历史"):
            console.print(Panel(show_trade_history(), title="交易记录", border_style="cyan"))
            continue
        # Quick stock quote: "查 002384" or "quote 002384"
        import re as _re
        quote_match = _re.match(r"^(?:查|quote)\s+(\d{6})$", user_input.strip())
        if quote_match:
            console.print(Panel(show_stock_quote(code=quote_match.group(1)), title="个股行情", border_style="green"))
            continue
        # Cancel order: "撤单 002384" or "cancel 002384"
        cancel_match = _re.match(r"^(?:撤单|cancel)\s+(\d{6})$", user_input.strip())
        if cancel_match:
            console.print(Panel(cancel_pending_order(code=cancel_match.group(1)), title="撤单", border_style="yellow"))
            continue
        if user_input.lower() in ("risk", "风险"):
            # Quick risk: show portfolio with unrealized P&L
            console.print(Panel(show_portfolio(), title="持仓风险", border_style="red"))
            continue
        if user_input.lower() in ("opening", "开盘"):
            console.print("[dim]正在运行开盘提醒...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.opening_reminder import run_opening_reminder
                report = await run_opening_reminder()
                if report:
                    console.print(Panel(report, title="开盘提醒", border_style="green"))
                else:
                    console.print("[dim]无开盘提醒（可能无预测数据）[/dim]")
            except Exception as e:
                console.print(f"[red]开盘提醒失败: {e}[/red]")
            continue
        if user_input.lower() in ("intraday", "盘中", "异动"):
            console.print("[dim]正在检测盘中异动...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.intraday_monitor import run_intraday_monitor
                report = await run_intraday_monitor()
                if report:
                    console.print(Panel(report, title="盘中提醒", border_style="yellow"))
                else:
                    console.print("[dim]当前无异动[/dim]")
            except Exception as e:
                console.print(f"[red]盘中监控失败: {e}[/red]")
            continue
        if user_input.lower() in ("review", "复盘"):
            console.print("[dim]正在运行复盘分析...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.review import run_review
                report = await run_review()
                if report:
                    console.print(Panel(report, title="复盘报告", border_style="cyan"))
                else:
                    console.print("[dim]复盘未产生报告[/dim]")
            except Exception as e:
                console.print(f"[red]复盘失败: {e}[/red]")
            continue
        if user_input.lower() in ("night", "夜扫"):
            console.print("[dim]正在运行夜扫...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.night_scan import run_night_scan
                report = await run_night_scan()
                if report:
                    console.print(Panel(report, title="夜报", border_style="blue"))
                else:
                    console.print("[dim]夜扫未产生报告[/dim]")
            except Exception as e:
                console.print(f"[red]夜扫失败: {e}[/red]")
            continue
        if user_input.lower() in ("weekly", "周报"):
            console.print("[dim]正在生成周报...[/dim]")
            try:
                from alpha_agents.pipeline.tasks.weekly_report import run_weekly_report
                report = await run_weekly_report()
                if report:
                    console.print(Panel(report, title="周报", border_style="magenta"))
                else:
                    console.print("[dim]周报未产生报告[/dim]")
            except Exception as e:
                console.print(f"[red]周报失败: {e}[/red]")
            continue
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
            from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
            from openai.types.responses import ResponseTextDeltaEvent

            # Pass conversation history so agent remembers prior exchanges
            streamed = Runner.run_streamed(
                agent,
                input=conversation_history + [{"role": "user", "content": message}],
                hooks=hooks, max_turns=30,
            )

            full_output = ""
            streaming_text = False  # Track if we've started printing text

            async for event in streamed.stream_events():
                # Show tool calls in real-time
                if isinstance(event, RunItemStreamEvent):
                    item = event.item
                    if getattr(item, "type", "") == "tool_call_item":
                        # Extract tool name from raw_item
                        raw = getattr(item, "raw_item", None)
                        tool_name = ""
                        if raw:
                            # Could be dict or object
                            if isinstance(raw, dict):
                                tool_name = raw.get("name", "") or raw.get("function", {}).get("name", "")
                            else:
                                tool_name = getattr(raw, "name", "") or ""
                                if not tool_name:
                                    fn = getattr(raw, "function", None)
                                    if fn:
                                        tool_name = getattr(fn, "name", "") or ""
                        if tool_name:
                            if streaming_text:
                                print()
                                streaming_text = False
                            console.print(f"  [dim]🔧 {tool_name}...[/dim]")

                if isinstance(event, RawResponsesStreamEvent):
                    data = event.data
                    if isinstance(data, ResponseTextDeltaEvent):
                        chunk = data.delta
                        if chunk:
                            if not streaming_text:
                                console.print("[cyan]分析师:[/cyan] ", end="")
                                streaming_text = True
                            print(chunk, end="", flush=True)
                            full_output += chunk

            print()  # newline after streaming

            # Save conversation history and compress if needed
            try:
                conversation_history = streamed.to_input_list()
                if compressor.should_compress(conversation_history):
                    console.print("[dim]对话历史较长，正在压缩...[/dim]")
                    conversation_history = compressor.compress(conversation_history)
                    console.print("[dim]压缩完成[/dim]")
            except Exception as e:
                logger.warning("Failed to update conversation history: %s", e)

            # Fallback display if streaming didn't produce text
            if not full_output:
                try:
                    full_output = streamed.final_output or ""
                except Exception:
                    full_output = ""
                if full_output:
                    console.print(Panel(full_output, title="分析师", border_style="cyan"))

            print()

        except asyncio.TimeoutError:
            console.print("[red]回答超时，请重试[/red]")
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]")

    # ── Session end: save memory for next time ──
    if conversation_history and len(conversation_history) > 3:
        console.print("[dim]保存对话记忆...[/dim]")
        try:
            summary = compressor._generate_summary(conversation_history)
            if summary:
                save_chat_memory(summary)
                console.print("[dim]记忆已保存，下次对话可回忆[/dim]")
        except Exception as e:
            console.print(f"[dim]记忆保存失败: {e}[/dim]")


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
