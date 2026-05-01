"""Slash-command handlers and registry for chat mode.

Every handler is ``async def (arg: str, ctx: ChatContext) -> None``.
Synchronous logic just lives directly inside the async body — Python's
async machinery is only needed for handlers that call other awaitables
(notably the pipeline tasks).

Helper functions used by handlers (``show_portfolio``, ``show_market_overview``
etc.) are defined in ``alpha_agents.agents.chat`` and imported lazily inside
each handler to avoid an import-time cycle (chat.py ↔ chat_commands).
"""

from __future__ import annotations

import json
import logging
import re

from rich.markdown import Markdown
from rich.panel import Panel

from alpha_agents.agents.chat_commands.context import ChatContext, Command

logger = logging.getLogger(__name__)


# ── Parser ────────────────────────────────────────────────────────────

def parse_slash(user_input: str) -> tuple[str, str] | None:
    """Parse ``/cmd rest of args`` → ``("/cmd", "rest of args")``.

    Returns ``None`` if input doesn't start with ``/``. Command name is
    lower-cased for lookup; argument string is preserved verbatim (only
    leading/trailing whitespace stripped).
    """
    if not user_input.startswith("/"):
        return None
    parts = user_input.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


# ── Dispatcher ────────────────────────────────────────────────────────

async def dispatch(user_input: str, ctx: ChatContext) -> None:
    """Route a slash-command line to its handler. No-op for non-slash input."""
    parsed = parse_slash(user_input)
    if parsed is None:
        return
    cmd, arg = parsed
    command = _BY_NAME.get(cmd)
    if command is None:
        ctx.console.print(
            f"[red]未知命令: {cmd}[/red]  输入 [cyan]/help[/cyan] 查看全部"
        )
        return
    try:
        await command.handler(arg, ctx)
    except Exception as e:
        ctx.console.print(f"[red]{command.name} 执行失败: {e}[/red]")
        logger.exception("Slash command %s failed", command.name)


# ── System handlers ───────────────────────────────────────────────────

async def handle_quit(arg: str, ctx: ChatContext) -> None:
    ctx.console.print("[dim]再见[/dim]")
    ctx.should_exit = True


async def handle_help(arg: str, ctx: ChatContext) -> None:
    # Group commands in registration order, preserving group order of first
    # appearance so /help looks stable even as commands get reshuffled.
    groups: dict[str, list[Command]] = {}
    for cmd in REGISTRY:
        groups.setdefault(cmd.group, []).append(cmd)

    lines: list[str] = []
    for group_name, cmds in groups.items():
        lines.append(f"[bold]{group_name}:[/bold]")
        for cmd in cmds:
            names = cmd.usage or cmd.name
            if cmd.aliases:
                names = f"{names} ({', '.join(cmd.aliases)})"
            lines.append(f"  {names:38s} — {cmd.summary}")
        lines.append("")
    lines.append("[dim]非 /xxx 开头的输入会交给 LLM 分析师，自由提问即可。[/dim]")
    ctx.console.print(Panel("\n".join(lines).rstrip(), title="命令帮助", border_style="yellow"))


async def handle_refresh(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import _create_chat_agent
    ctx.agent = _create_chat_agent()
    ctx.conversation_history = []
    ctx.console.print("[dim]系统状态已刷新，对话历史已清空[/dim]")


async def handle_tasks(arg: str, ctx: ChatContext) -> None:
    from datetime import datetime as _dt
    from alpha_agents.config import DATA_DIR

    state_file = DATA_DIR / "scheduler_state.json"
    state = {}
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))

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
    ctx.console.print(Panel("\n".join(lines), title=f"定时任务 ({today})", border_style="blue"))


# ── Market/Quote handlers ─────────────────────────────────────────────

async def handle_market(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_market_overview
    ctx.console.print(Panel(show_market_overview._fn(), title="大盘概览", border_style="blue"))


async def handle_sectors(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_sector_ranking
    ctx.console.print(Panel(show_sector_ranking._fn(top_n=10), title="板块排名", border_style="yellow"))


async def handle_quote(arg: str, ctx: ChatContext) -> None:
    m = re.match(r"^(\d{6})$", arg)
    if not m:
        ctx.console.print("[red]用法: /quote <6位代码>[/red]  例: /quote 002384")
        return
    from alpha_agents.agents.chat import show_stock_quote
    ctx.console.print(Panel(show_stock_quote._fn(code=m.group(1)), title="个股行情", border_style="green"))


async def handle_best(arg: str, ctx: ChatContext) -> None:
    if not arg:
        ctx.console.print("[red]用法: /best <板块名>[/red]  例: /best 电池")
        return
    from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
    ctx.console.print(Panel(get_sector_best_stocks_fn(arg), title="板块选股", border_style="cyan"))


async def handle_vpa(arg: str, ctx: ChatContext) -> None:
    m = re.match(r"^(\d{6})(?:\s+(.+))?$", arg)
    if not m:
        ctx.console.print("[red]用法: /vpa <6位代码> [名称][/red]  例: /vpa 002364 中恒电气")
        return
    code = m.group(1)
    name = (m.group(2) or "").strip()
    ctx.console.print(f"[dim]正在进行 Anna Coulling 量价分析 {code} {name}...[/dim]")
    from alpha_agents.tools.vpa import compute_vpa_with_llm
    r = compute_vpa_with_llm(code, name=name)
    if not r.get("ok"):
        ctx.console.print(f"[red]VPA 分析失败: {r.get('error', '未知错误')}[/red]")
        return
    verdict = r.get("llm_verdict", "中性")
    phase = r.get("llm_phase", "?")
    confirmed = r.get("llm_confirmed", False)
    reason = r.get("llm_reason", "")
    report = r.get("llm_report", "")

    color_map = {"看多": "green", "偏多": "green", "看空": "red", "偏空": "red", "中性": "yellow"}
    border = color_map.get(verdict, "cyan")
    confirm_str = "已确认" if confirmed else "待确认"
    title = f"VPA 量价分析 — {code} {name} [{verdict} {phase} {confirm_str}]"
    ctx.console.print(Panel(Markdown(report or reason), title=title, border_style=border))


async def handle_lhb(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_lhb
    ctx.console.print(Panel(show_lhb._fn(), title="龙虎榜", border_style="red"))


async def handle_north(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_north_flow
    ctx.console.print(Panel(show_north_flow._fn(), title="北向资金", border_style="green"))


async def handle_limitup(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_limit_up
    ctx.console.print(Panel(show_limit_up._fn(), title="涨停板", border_style="red"))


async def handle_sentiment(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.data.sentiment_cycle import get_sentiment_cycle, format_sentiment_cycle
    cycle = get_sentiment_cycle()
    ctx.console.print(Panel(
        format_sentiment_cycle(cycle),
        title=f"情绪周期: {cycle['phase']}",
        border_style="magenta",
    ))


# ── Portfolio handlers ────────────────────────────────────────────────

async def handle_portfolio(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_portfolio
    ctx.console.print(Panel(show_portfolio._fn(), title="持仓概览", border_style="green"))


async def handle_trades(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_trade_history
    ctx.console.print(Panel(show_trade_history._fn(), title="交易记录", border_style="cyan"))


async def handle_risk(arg: str, ctx: ChatContext) -> None:
    # Quick risk = portfolio with unrealized P&L (same as current chat behavior).
    from alpha_agents.agents.chat import show_portfolio
    ctx.console.print(Panel(show_portfolio._fn(), title="持仓风险", border_style="red"))


async def handle_cancel(arg: str, ctx: ChatContext) -> None:
    m = re.match(r"^(\d{6})$", arg)
    if not m:
        ctx.console.print("[red]用法: /cancel <6位代码>[/red]  例: /cancel 002384")
        return
    from alpha_agents.agents.chat import cancel_pending_order
    ctx.console.print(Panel(cancel_pending_order._fn(code=m.group(1)), title="撤单", border_style="yellow"))


# ── Data handlers ─────────────────────────────────────────────────────

async def handle_news(arg: str, ctx: ChatContext) -> None:
    ctx.console.print("[dim]获取最新新闻...[/dim]")
    from alpha_agents.sources.eastmoney import get_news_fn
    raw = get_news_fn(limit=20)
    news = json.loads(raw)
    lines = [f"  {item.get('time', '')} {item.get('title', '')}"
             for item in news.get("news", [])[:15]]
    ctx.console.print(Panel(
        "\n".join(lines) if lines else "无新闻",
        title="最新新闻",
        border_style="yellow",
    ))


async def handle_themes(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.data.memory_store import get_active_themes
    themes = get_active_themes()
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t.get("core_stocks") else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        lines.append(f"  {t['name']} (强度{t['strength']}/10, {t['status']}) 龙头: {leader}")
    ctx.console.print(Panel(
        "\n".join(lines) if lines else "无活跃主线",
        title="活跃主线",
        border_style="magenta",
    ))


# ── Manual-task handlers ──────────────────────────────────────────────

async def _run_pipeline_task(ctx: ChatContext, *, label: str, title: str, border: str,
                             runner_import: str, empty_msg: str) -> None:
    """Shared skeleton for /morning, /opening, /intraday, /review, /night, /weekly."""
    from alpha_agents.agents.chat import _SuppressPrint
    module_path, func_name = runner_import.rsplit(".", 1)
    mod = __import__(module_path, fromlist=[func_name])
    runner = getattr(mod, func_name)

    ctx.console.print(f"[dim]正在运行{label}...[/dim]")
    with _SuppressPrint():
        report = await runner()
    if report:
        ctx.console.print(Panel(Markdown(report), title=title, border_style=border))
    else:
        ctx.console.print(f"[dim]{empty_msg}[/dim]")


async def handle_morning(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="晨扫", title="晨报", border="green",
        runner_import="alpha_agents.pipeline.tasks.morning_scan.run_morning_scan",
        empty_msg="晨扫未产生报告",
    )


async def handle_opening(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="开盘提醒", title="开盘提醒", border="green",
        runner_import="alpha_agents.pipeline.tasks.opening_reminder.run_opening_reminder",
        empty_msg="无开盘提醒（可能无预测数据）",
    )


async def handle_intraday(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="盘中异动检测", title="盘中提醒", border="yellow",
        runner_import="alpha_agents.pipeline.tasks.intraday_monitor.run_intraday_monitor",
        empty_msg="当前无异动",
    )


async def handle_review(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="复盘分析", title="复盘报告", border="cyan",
        runner_import="alpha_agents.pipeline.tasks.review.run_review",
        empty_msg="复盘未产生报告",
    )


async def handle_night(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="夜扫", title="夜报", border="blue",
        runner_import="alpha_agents.pipeline.tasks.night_scan.run_night_scan",
        empty_msg="夜扫未产生报告",
    )


async def handle_weekly(arg: str, ctx: ChatContext) -> None:
    await _run_pipeline_task(
        ctx, label="周报生成", title="周报", border="magenta",
        runner_import="alpha_agents.pipeline.tasks.weekly_report.run_weekly_report",
        empty_msg="周报未产生报告",
    )


# ── Evolution/Playbook handlers ───────────────────────────────────────

async def handle_evolution(arg: str, ctx: ChatContext) -> None:
    """Show evolution system health — 30-day metric trend."""
    from alpha_agents.evolution import get_evolution_metrics_trend, format_metrics_trend
    rows = get_evolution_metrics_trend(days=30)
    text = format_metrics_trend(rows)
    ctx.console.print(Panel(text, title="进化系统自评（30天）", border_style="cyan"))


async def handle_playbook(arg: str, ctx: ChatContext) -> None:
    """List all playbooks with status / weight / hit rate."""
    from alpha_agents.data.memory_store import get_all_playbooks
    rows = get_all_playbooks()
    if not rows:
        ctx.console.print("[yellow]尚无 Playbook。至少积累 2 周 intraday 数据后由复盘自动发现。[/yellow]")
        return
    lines = [f"共 {len(rows)} 条 Playbook:"]
    for pb in rows:
        hr = pb.get("hit_rate", 0) or 0
        status = pb["status"]
        color = {"active": "green", "degraded": "yellow", "deprecated": "dim"}.get(status, "white")
        lines.append(
            f"[{color}]• {pb['name']} ({status}, weight={pb.get('weight', 1.0):.1f}) "
            f"— 胜率{hr*100:.0f}% ({pb.get('wins',0)}/{pb.get('total_trades',0)})[/]"
        )
        if pb.get("annotation"):
            lines.append(f"    [dim]注：{pb['annotation']}[/dim]")
    ctx.console.print(Panel("\n".join(lines), title="Playbook 列表", border_style="cyan"))


# ── Automation handlers ───────────────────────────────────────────────

async def handle_alerts(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import show_price_alerts
    ctx.console.print(Panel(show_price_alerts._fn(), title="价格提醒", border_style="yellow"))


async def handle_scheduled(arg: str, ctx: ChatContext) -> None:
    from alpha_agents.agents.chat import list_scheduled_tasks
    ctx.console.print(Panel(list_scheduled_tasks._fn(), title="自定义定时任务", border_style="blue"))


# ── Registry ──────────────────────────────────────────────────────────
# Order here determines /help rendering order.

REGISTRY: tuple[Command, ...] = (
    # 行情
    Command("/market", "行情", "大盘指数+涨跌比+情绪", handle_market),
    Command("/sectors", "行情", "板块资金排名 Top10", handle_sectors),
    Command("/quote", "行情", "快速查个股实时行情", handle_quote,
            aliases=("/查",), usage="/quote <6位代码>"),
    Command("/best", "行情", "板块内多因子选股", handle_best,
            aliases=("/选股",), usage="/best <板块名>"),
    Command("/vpa", "行情", "量价分析（Wyckoff/Anna Coulling）", handle_vpa,
            aliases=("/量价",), usage="/vpa <6位代码> [名称]"),
    Command("/lhb", "行情", "今日龙虎榜机构动向", handle_lhb),
    Command("/north", "行情", "北向资金流向", handle_north),
    Command("/limitup", "行情", "涨停板分布", handle_limitup),
    Command("/sentiment", "行情", "当前情绪周期和策略建议", handle_sentiment),

    # 持仓
    Command("/portfolio", "持仓", "持仓+挂单+资金", handle_portfolio),
    Command("/trades", "持仓", "最近交易记录和盈亏", handle_trades),
    Command("/risk", "持仓", "当前风险评估", handle_risk),
    Command("/cancel", "持仓", "取消某只挂单", handle_cancel,
            aliases=("/撤单",), usage="/cancel <6位代码>"),

    # 数据
    Command("/news", "数据", "最新新闻", handle_news),
    Command("/themes", "数据", "活跃主线状态", handle_themes),
    Command("/playbook", "数据", "查看 Playbook 列表及胜率", handle_playbook),
    Command("/evolution", "数据", "查看进化系统自评指标", handle_evolution),

    # 任务
    Command("/morning", "任务", "手动运行晨扫", handle_morning),
    Command("/opening", "任务", "手动运行开盘提醒", handle_opening),
    Command("/intraday", "任务", "手动检测盘中异动", handle_intraday),
    Command("/review", "任务", "手动运行复盘", handle_review),
    Command("/night", "任务", "手动运行夜扫", handle_night),
    Command("/weekly", "任务", "手动生成周报", handle_weekly),

    # 自动化
    Command("/alerts", "自动化", "查看价格提醒", handle_alerts),
    Command("/scheduled", "自动化", "查看自定义定时任务", handle_scheduled),

    # 系统
    Command("/tasks", "系统", "查看系统定时任务状态", handle_tasks),
    Command("/refresh", "系统", "刷新系统状态 & 清空对话历史", handle_refresh),
    Command("/help", "系统", "显示本帮助", handle_help,
            aliases=("/?",)),
    Command("/quit", "系统", "退出交互模式", handle_quit,
            aliases=("/exit", "/q")),
)


def _build_name_map(registry: tuple[Command, ...]) -> dict[str, Command]:
    """Flatten name + aliases into a single lookup table. Fail fast on dup."""
    by_name: dict[str, Command] = {}
    for cmd in registry:
        for key in (cmd.name, *cmd.aliases):
            key_lower = key.lower()
            if key_lower in by_name:
                raise ValueError(f"Duplicate slash command: {key}")
            by_name[key_lower] = cmd
    return by_name


_BY_NAME: dict[str, Command] = _build_name_map(REGISTRY)
