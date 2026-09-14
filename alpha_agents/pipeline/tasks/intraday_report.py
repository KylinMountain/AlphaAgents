"""Post-processing of the intraday report: real prices, cause analysis, fallback.

Split out of `intraday_monitor` when that file crossed its line ceiling. The
three functions here share one subject — they take the report the model wrote
and make it honest and complete before anything reads it:

  * `_fix_prices_in_report` — the model hallucinates prices, so the table rows
    are rewritten from live quotes, and anything already at limit-up is moved
    out of 可操作标的 (you cannot buy a stock that is already locked).
  * `_get_cause_analysis` — the LLM pass that traces *why* something moved
    (catalysts, 龙虎榜 seats, fund-flow direction) and judges persistence. It is
    the slowest step in the cycle (90s timeout) and never raises: a timeout
    returns the raw context, as it did on 2026-09-14.
  * `_auto_fill_actionable` — when the model only names limit-up stocks the
    actionable table comes back empty, so it is filled from sector beta picks.

None of them touch the theme gate, the portfolio or the scheduler, which is the
seam the split follows: this module is about *the report as text*.

**Two of the three have no caller.** `_get_cause_analysis` is called from
`run_intraday_monitor`; `_fix_prices_in_report` and `_auto_fill_actionable` are
not called from anywhere, in this revision or the one before it. So two safety
nets described in their own docstrings are inert:

  * hallucinated prices in the 可操作标的 table are **not** being replaced with
    live quotes, and limit-up names are **not** being moved to 信号确认;
  * an empty actionable table is **not** being filled from sector beta picks.

That is recorded rather than quietly wired up — turning them on would change
what the report says, which is a behaviour change with its own plan. What is
certain is that the report as shipped has neither net.
"""

import json
import logging
import re

from alpha_agents.data.market_data import get_realtime_quotes

logger = logging.getLogger(__name__)


def _fix_prices_in_report(report: str) -> str:
    """Replace hallucinated prices with real Sina data.

    Also moves stocks that are at limit-up (>=9.8%) from 可操作标的 to 信号确认,
    since you can't buy a stock that's already at the daily limit.
    """
    # Extract all stock codes mentioned in table rows
    codes_in_report = re.findall(r"\|\s*(\d{6})\s*\|", report)
    if not codes_in_report:
        return report

    rt = get_realtime_quotes(list(set(codes_in_report)))
    if not rt:
        return report

    # Collect stocks that are actually at limit-up but listed as actionable
    limit_up_moves = []

    lines = report.split("\n")
    fixed_lines = []
    in_actionable_table = False

    for line in lines:
        # Detect we're in the actionable table
        if "【可操作标的】" in line:
            in_actionable_table = True
            fixed_lines.append(line)
            continue
        if in_actionable_table and line.strip() and not line.strip().startswith("|"):
            in_actionable_table = False

        match = re.match(r"\|\s*(\d{6})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|", line)
        if match and in_actionable_table:
            code = match.group(1)
            try:
                name = match.group(2).strip()
                action = match.group(5).strip()

                if code in rt:
                    real = rt[code]
                    price = real.get("price", 0)
                    change_pct = real.get("change_pct", 0)
                    # If at limit-up (>=9.8%), remove from actionable, add to signals
                    if change_pct >= 9.8:
                        limit_up_moves.append(
                            f"• {code} {name} 涨停封板({change_pct:+.2f}%) "
                            f"— 实际已涨停，从可操作移至信号确认"
                        )
                        continue  # Skip this row from actionable table

                    fixed_lines.append(
                        f"| {code} | {name} | {price:.2f}元 | "
                        f"{change_pct:+.2f}% | {action} |"
                    )
                    continue
            except Exception as e:
                logger.warning("Failed to fix price for %s in report: %s", code, e)

        fixed_lines.append(line)

    # Append limit-up stocks to signal section
    if limit_up_moves:
        result = "\n".join(fixed_lines)
        insert_text = "\n".join(limit_up_moves)
        # Try to append after existing signal section
        if "【信号确认】" in result:
            # Find last line of signal section and append
            signal_idx = result.index("【信号确认】")
            # Find next section after signal
            next_section = None
            for marker in ["【可操作标的】", "【主线状态变化】"]:
                pos = result.find(marker, signal_idx + 10)
                if pos > 0:
                    next_section = pos
                    break
            if next_section:
                result = result[:next_section] + insert_text + "\n\n" + result[next_section:]
            else:
                result += "\n" + insert_text
        else:
            # No signal section exists, add one before actionable
            result = result.replace("【可操作标的】",
                                    "【信号确认】（已涨停，系统自动识别）\n" + insert_text + "\n\n【可操作标的】")
        return result

    return "\n".join(fixed_lines)


async def _get_cause_analysis(context: str) -> str:
    """V2 anomaly tracing: observe → trace cause → judge persistence.

    Restored from V2 spec design. LLM has tools to search for catalysts
    and check fund flow sources. Code handles stock selection and pricing.

    Tools given:
      - web_search: find news catalysts (policy, earnings, events)
      - get_lhb_detail: check institutional vs hot money seats
      - get_stock_fund_flow: check main force vs retail flow direction
      - get_sector_data: check related sector linkage
    """
    import asyncio
    from agents import Agent, Runner
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL
    from alpha_agents.tools.registry import (
        web_search, get_lhb_detail, get_stock_fund_flow, get_sector_data,
        get_cls_telegraph, get_news,
    )

    # Agent client — counted by the tracing hook, not wrapped here.
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)
    agent = Agent(
        name="cause_analyst",
        instructions=(
            "你是盘中异动追因分析师。检测到市场异动后，你需要追溯原因并判断持续性。\n\n"
            "## 思维链路（严格按步骤执行）\n\n"
            "1. **观察**：阅读传入的异动数据，识别核心异动（哪个板块、什么类型的资金异动）\n"
            "2. **追因**：\n"
            "   - **优先**调用 get_cls_telegraph（财联社电报）搜索最新快讯，关键词用板块名或龙头股名\n"
            "   - 如果财联社没有，调用 get_news（东方财富新闻）搜索\n"
            "   - 如果国内源都没有，再调用 web_search 搜索（注意：web_search 对中文新闻效果有限）\n"
            "   - 调用 get_lhb_detail 查看龙虎榜，判断资金来源是机构还是游资\n"
            "   - 如果有明确的龙头股，调用 get_stock_fund_flow 查看主力资金方向\n"
            "   - 调用 get_sector_data 查看关联板块是否联动\n"
            "3. **判断**：基于追因结果判断——\n"
            "   - 机构资金 + 明确政策/产业催化 + 多板块联动 → **持续行情**\n"
            "   - 游资席位 + 无明确催化 + 单一个股 → **一日游，不追**\n"
            "   - 资金异动但无新闻催化 → **主力提前布局，密切关注**\n\n"
            "## 输出格式\n\n"
            "对每个异动板块/方向输出：\n"
            "【异动】{板块名} {异动类型}\n"
            "• 催化: {找到的新闻原因，没找到就写'未发现明确催化，可能是资金先行'}\n"
            "• 资金来源: {机构/游资/主力/不明}\n"
            "• 关联板块: {是否有联动}\n"
            "• 判断: {一日游/持续行情/主力提前布局}\n"
            "• 失效条件: {什么情况下判断不成立}\n\n"
            "## 重要原则\n"
            "- 不要推荐股票，不要给操作建议，不要生成表格\n"
            "- 资金行为优先于新闻叙事——如果资金在流入但没有新闻，不要说'没有异动'\n"
            "- 没找到新闻催化不代表没有原因，可能是主力提前知道了什么\n"
            "- 每个工具最多调用一次，不要重复调用"
        ),
        model=model,
        tools=[get_cls_telegraph, get_news, web_search, get_lhb_detail, get_stock_fund_flow, get_sector_data],
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, f"以下是刚检测到的市场异动，请按思维链路追因分析：\n\n{context}",
                       max_turns=30),
            timeout=90,
        )
        output = result.final_output or ""

        # Clean up LLM tool call tags that leak into output (LongCat compatibility)
        import re as _re_clean
        output = _re_clean.sub(r'</?longcat_tool_call>', '', output)
        output = _re_clean.sub(r'</?tool_call>', '', output)
        output = _re_clean.sub(r'\{"name":\s*"[^"]+",\s*"arguments":\s*\{[^}]*\}\}', '', output)
        output = output.strip()

        if not output or len(output) < 20:
            logger.warning("Cause analysis returned empty/garbage, using fallback")
            return context

        return output
    except asyncio.TimeoutError:
        logger.warning("Cause analysis timed out (90s)")
        return context
    except Exception as e:
        logger.warning("Cause analysis failed: %s", e)
        return context


def _auto_fill_actionable(report: str, sectors: list[str]) -> str:
    """If the actionable table is empty, auto-fill it from sector beta picks.

    LLM sometimes only recommends limit-up stocks. This fallback ensures
    the report always has non-limit-up candidates.
    """
    # Check if actionable table is empty (only header/separator, no data rows)
    lines = report.split("\n")
    in_table = False
    has_data_rows = False
    for line in lines:
        if "【可操作标的】" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("|") and "代码" not in line and "---" not in line:
            # Found a data row
            has_data_rows = True
            break
        if in_table and line.strip() and not line.strip().startswith("|"):
            break  # End of table section

    if has_data_rows:
        return report  # Table already has data, don't override

    # Fetch beta picks and build table rows
    try:
        from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
        from alpha_agents.data.market_data import get_realtime_quotes

        all_picks = []
        for sector in sectors:
            result = json.loads(get_sector_best_stocks_fn(sector, top_n=3))
            for s in result.get("top", []):
                if s.get("today_change_pct", 0) < 9.8:  # Not limit-up
                    all_picks.append(s)

        if not all_picks:
            return report

        # Build table rows
        table_rows = []
        for s in all_picks[:5]:
            code = s["code"]
            name = s["name"]
            price = s.get("price", 0)
            chg = s.get("today_change_pct", 0)
            beta = s.get("beta_weighted", 0)
            note = s.get("note", "")
            table_rows.append(
                f"| {code} | {name} | {price:.2f}元 | {chg:+.2f}% | "
                f"高beta={beta:.2f}, {note} (系统预选) |"
            )

        if table_rows:
            # Insert after the table header
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and "可操作标的" in line:
                    # Skip to after header/separator
                    pass
                if not inserted and line.strip().startswith("|---"):
                    new_lines.extend(table_rows)
                    inserted = True
            if inserted:
                report = "\n".join(new_lines)
                logger.info("Auto-filled %d actionable stocks from sector beta picks", len(table_rows))
    except Exception as e:
        logger.debug("Auto-fill actionable failed: %s", e)

    return report
