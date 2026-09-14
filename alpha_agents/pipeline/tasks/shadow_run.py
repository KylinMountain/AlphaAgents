"""Feed every open experiment the day's picks, then grade what matured.

Phase 4 delivered the shadow mechanism, and until 2026-09-14 nothing ran its two
daily actions: ``shadow.open_run`` / ``emit_for_date`` / ``run_gate`` had no
caller but the tests, so an experiment existed only if a person wrote Python.
This module is the scheduled half of that surface; the operator half is
``scripts/policy.py`` (``shadow-open`` / ``shadow-emit`` / ``shadow-score`` /
``gate``), and both go through the same functions so they cannot drift apart on
what an experiment means.

Once a trading day, after the close:

1. for every **open** run, write the challenger's forecasts for today. The panel
   is the champion's own picks for the day, so this runs *after* the champion has
   decided — 15:45, behind the 15:30 review. Emitting later adds no information
   to the challenger: its probability is a function of the champion's recorded
   confidence label and the version's mapping, never of prices, so a forecast
   written at 15:45 is the one the champion's morning signal implied.
2. grade the challenger's forecasts whose evidence window the market has closed.
   The three counts are reported apart — graded, window still open, unscorable —
   for the reason D10 was: a window the market has not traded shut is not a data
   gap, and folding the two into one number is what made an unripe forecast look
   censored.

What it deliberately does **not** do: ask the gate. A verdict is evidence for a
person's decision, and asking every day would write a near-identical
``insufficient`` row daily until the paired count fills — the shape of
governance without the substance, which is what the old
``run_gate("daily_playbook", today, today)`` call was removed for. The progress
number is reported instead, so the moment asking is worth it is visible.

Nothing here can fail the day. The shadow branch is graded and never traded, so
a broken experiment must not take the review with it: every step degrades to a
logged warning and the task still returns a report.
"""

import asyncio
import logging

from alpha_agents.data import clock

logger = logging.getLogger(__name__)


def _emit_line(run: dict, written: int | None) -> str:
    label = (f"run #{run['id']}（版本 #{run['policy_version_id']} / "
             f"{run['producer']} / {run['report_type']}）")
    if written is None:
        return f"• {label}：写入失败，见日志"
    if written == 0:
        return (f"• {label}：今天没有可配对的面板 —— 冠军这一天没有留下"
                "该报告类型的选股，所以这一天不进配对检验")
    return f"• {label}：写入 {written} 条预测"


async def run_shadow_run() -> str | None:
    """Feed and grade every open shadow experiment. Returns the report text.

    None when nothing is open, because a report saying "nothing happened" every
    trading day is noise in a feed a person reads — the log carries the reason
    instead.
    """
    from alpha_agents.evolution import shadow

    try:
        runs = await asyncio.to_thread(shadow.runs, status="open")
    except Exception as e:
        logger.warning("Shadow: runs unreadable: %s", e)
        return None

    if not runs:
        # Said out loud rather than at debug: "no experiment is running" is the
        # answer to "why is nothing being learned", and a silent no-op is
        # indistinguishable from a broken task.
        logger.info(
            "Shadow: no run is open, so there is nothing to feed or grade. "
            "Open one with `scripts/policy.py shadow-open`.")
        return None

    today = clock.today()
    written: dict[int, int | None] = {}
    for run in runs:
        try:
            ids = await asyncio.to_thread(shadow.emit_for_date, run["id"], today)
            written[run["id"]] = len(ids)
        except Exception as e:
            logger.warning("Shadow: emit failed for run #%s: %s", run["id"], e)
            written[run["id"]] = None

    try:
        graded = await asyncio.to_thread(shadow.score_due, as_of=today)
    except Exception as e:
        logger.warning("Shadow: grading failed: %s", e)
        graded = None

    lines = [f"【影子实验】{today}"]
    lines.extend(_emit_line(run, written[run["id"]]) for run in runs)
    if graded is None:
        lines.append("• 评分：失败，见日志")
    else:
        lines.append(f"• 评分：{graded['graded']} 已评 / "
                     f"{graded['deferred']} 窗口未收 / "
                     f"{graded['unscorable']} 已删失")
    try:
        for row in (await asyncio.to_thread(shadow.coverage))["runs"]:
            tail = (f"，还差 {row['remaining']} 个交易日"
                    if row["remaining"] else "，样本够了，可以问闸门了")
            lines.append(f"• run #{row['run_id']} 配对进度 "
                         f"{row['paired']}/{row['needed']}{tail}")
    except Exception as e:
        logger.warning("Shadow: coverage unreadable: %s", e)
    logger.info("Shadow run: %d open experiment(s), %d forecast(s) written",
                len(runs), sum(n or 0 for n in written.values()))
    return "\n".join(lines)
