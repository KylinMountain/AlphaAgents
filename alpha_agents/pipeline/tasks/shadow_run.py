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

What it does **not** do daily: ask the gate. It asks exactly once, at the point
the sample was declared sufficient — the day an experiment's paired count first
reaches what the gate requires. Two reasons, and they are the same reason:

* §12 forbids repeated inspection until something passes. Asking every day is
  optional stopping: the same experiment gets many looks, and "one of them said
  promote" stops being evidence. The preregistered stopping rule is the count,
  so the question is asked when the count is reached and not before.
* The alternative — asking daily from the start — also writes a near-identical
  `insufficient` row every day until then, which is the shape of governance
  without the substance. That is what the old
  ``run_gate("daily_playbook", today, today)`` call was removed for.

So the rule is mechanical: ask when ``paired >= needed`` and the newest verdict
on that version is still short of the **same** bar. Both sides of that
comparison are paired samples — ``needed`` is
``holdout_gate.MIN_VALIDATION_SAMPLES`` and the verdict's ``n`` is the number of
pairs it compared. Until 2026-09-15 the second side was ``validation_days``, so
an experiment passed the sample bar long before the day bar and the gate was
asked every day in between (D16). A person looking early (via
``scripts/policy.py gate``) does not suppress the scheduled question: their
verdict's ``n`` is short of the bar too, and ``gate`` shares the unit.

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


def _position_line(row: dict, verdict: dict | None) -> str:
    """One line for the report, and nothing else — no question is asked here.

    The unit is a paired sample, not a day. ``row['paired']`` is the number of
    ``(date, code)`` pairs both sides scored; ``row['scored_days']`` is how many
    distinct dates those came from. This line used to append 交易日 to the first
    one, so one morning of twenty picks read as twenty trading days of evidence
    — in the report a person actually reads to decide whether to act.

    The bar a *recorded verdict* is compared against is the same unit: the
    verdict's own ``n`` (paired samples) against ``needed``. Until 2026-09-15
    this compared ``validation_days`` instead, so a verdict resting on twenty
    pairs from one morning looked short of a twenty-*day* bar and the experiment
    kept asking (D16).
    """
    if verdict is not None and (verdict.get("n") or 0) >= row["needed"]:
        return (f"• run #{row['run_id']} 已裁决：{verdict['outcome']}"
                f"（n={verdict['n']} 个配对样本 / "
                f"{verdict.get('validation_days')} 个验证日）"
                "—— 下一步是人工 approve")
    if row["remaining"]:
        return (f"• run #{row['run_id']} 配对进度 {row['paired']}/{row['needed']}"
                f" 个配对样本（覆盖 {row['scored_days']} 个交易日），"
                f"还差 {row['remaining']} 个样本")
    return (f"• run #{row['run_id']} 配对 {row['paired']}/{row['needed']}，"
            "样本够了")


async def _ask_the_gate(row: dict, verdict: dict | None, today: str) -> str:
    """The one question, at the preregistered point, or a line saying why not.

    Returns the report line. A refusal is reported rather than raised: the
    gate's own refusals (an ambiguous run, a window that cannot hold evidence)
    are answers about the experiment, and the trading day continues either way.
    """
    from alpha_agents.evolution import holdout_gate

    # Both sides of this comparison are now **paired samples**. Until
    # 2026-09-15 the second was ``validation_days``: an experiment passed the
    # sample bar long before the day bar, so the gate was asked *every day* in
    # between, writing a near-identical ``insufficient`` row each time — exactly
    # the "governance in shape only" this module's docstring says it was written
    # to avoid. The tests could not see it, because the stubbed verdict gave
    # ``n`` and ``validation_days`` the same value as ``needed`` and the two
    # units coincided. Fixed with D15 (2026-09-15), whose answer settled the
    # unit: the preregistered sample is 20 *paired samples*, so pairs compare
    # with pairs and nothing else does.
    asked_already = (verdict is not None
                     and (verdict.get("n") or 0) >= row["needed"])
    if asked_already or row["remaining"]:
        return _position_line(row, verdict)
    try:
        decision = await asyncio.to_thread(
            holdout_gate.run_gate, row["policy_version_id"],
            report_type=row["report_type"], today=today)
    except Exception as e:
        logger.warning("Shadow: the gate refused to answer for version #%s: %s",
                       row["policy_version_id"], e)
        return (f"• run #{row['run_id']} 样本够了，但闸门拒绝回答：{e}")
    logger.info("Shadow: gate verdict on version #%s: %s",
                row["policy_version_id"], decision["outcome"])
    return (f"• run #{row['run_id']} 样本够了，裁决已记录：{decision['outcome']}"
            f"（{decision['validation_days']} 个验证日，"
            f"scope {decision['evidence_scope']}）—— 下一步是人工 approve")


async def run_shadow_run() -> str | None:
    """Feed and grade every open shadow experiment. Returns the report text.

    None when nothing is open, because a report saying "nothing happened" every
    trading day is noise in a feed a person reads — the log carries the reason
    instead.
    """
    from alpha_agents.evolution import holdout_gate, shadow

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

    try:
        coverage = (await asyncio.to_thread(shadow.coverage))["runs"]
    except Exception as e:
        logger.warning("Shadow: coverage unreadable: %s", e)
        coverage = []
    try:
        newest: dict[int, dict] = {}
        for row in await asyncio.to_thread(holdout_gate.get_gate_decisions):
            # Newest first, so the first row seen for a version is its latest.
            newest.setdefault(row["policy_version_id"], row)
    except Exception as e:
        logger.warning("Shadow: verdicts unreadable: %s", e)
        newest = {}

    lines = [f"【影子实验】{today}"]
    lines.extend(_emit_line(run, written[run["id"]]) for run in runs)
    if graded is None:
        lines.append("• 评分：失败，见日志")
    else:
        lines.append(f"• 评分：{graded['graded']} 已评 / "
                     f"{graded['deferred']} 窗口未收 / "
                     f"{graded['unscorable']} 已删失")
    for row in coverage:
        lines.append(await _ask_the_gate(row, newest.get(row["policy_version_id"]),
                                         today))
    logger.info("Shadow run: %d open experiment(s), %d forecast(s) written",
                len(runs), sum(n or 0 for n in written.values()))
    return "\n".join(lines)
