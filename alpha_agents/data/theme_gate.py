"""Whether a theme can carry an order: admission, and cancellation.

Extracted from ``portfolio`` when the gate stopped being a single number.

It used to be ``strength >= MIN_THEME_STRENGTH`` (4), read at creation *and*
on every pending check "from one constant so the two cannot drift apart".
They did not drift — and that was the problem. The same day-old count of
confirmed sessions vetoed a candidate before the model ever saw it, then
re-vetoed the order every five minutes while it waited to fill. Of 117
orders, 106 were cancelled and ~100 of those on 主线走弱; in an earlier
count ~80 of 97 had died the same way, on themes already at strength 0–2
when the order was written. The system was creating orders it had already
decided it would not hold, cancelling them, and then counting those
cancellations as evidence about entry prices.

What replaced it is ``trend_score`` (``pipeline.theme_manager``): today's
theme strength normalised against the whole concept board, 0–1, refreshed
every cycle. Admission compares it to ``theme_gate.admit_score``;
cancellation compares it to the deliberately **lower**
``theme_gate.cancel_score``, and the band between the two is what stops a
pending order from being killed by daily-frequency noise at the boundary.
Both numbers travel with the policy pointer, so "admit at 0.5" versus
"admit at 0.35" is a difference between two frozen versions rather than a
code edit.

Layer: ``data``. Everything imported here is ``data`` as well, so this
module cannot introduce an upward dependency.
"""

import logging
import re

from alpha_agents.data.memory_store import get_theme_by_name

logger = logging.getLogger(__name__)


def resolve_theme(theme: str) -> str | None:
    """Canonical theme name for an order, or None when there is no thesis.

    ``check_pending_orders`` already refuses to hold a position whose theme
    is not in theme_lines — "no theme, no logical basis". That rule ran a
    cycle too late: an order was created from whatever concept string the
    agent wrote in its 关联概念 column, then cancelled the next cycle with
    "关联主线'磷化工'不存在". A capital slot spent on an idea the system had
    already decided it would not hold.

    Agents also write compound labels ("化肥/磷化工") for a theme tracked
    under one of its parts, so an exact match alone would reject ideas the
    system does hold.
    """
    if not theme:
        return None
    from alpha_agents.data.memory_store import get_active_themes
    try:
        known = [t["name"] for t in get_active_themes()]
    except Exception as e:
        # Without the theme list there is nothing to check against; let the
        # order through rather than dropping ideas on an unrelated failure.
        logger.warning("Theme lookup failed, accepting order theme %r: %s",
                       theme, e)
        return theme

    if theme in known:
        return theme
    parts = [p.strip() for p in re.split(r"[/、,，|]", theme) if p.strip()]
    for part in parts:
        if part in known:
            return part
    for name in known:
        if name and (name in theme or theme in name):
            return name
    return None


def theme_gate(theme: str, kind: str = "admit") -> str | None:
    """Why this theme cannot carry an order of this ``kind``, or None if it can.

    ``kind`` is ``"admit"`` (may this candidate be priced?) or ``"cancel"`` (must
    this pending order be pulled?). **They read different thresholds on purpose.**
    Both used to compare ``strength`` against one number, so a theme oscillating
    around it killed its own orders on daily noise — 106 of 117 orders died that
    way. Admission asks a high bar of *today's* cross-sectionally normalised
    score; cancellation asks a lower one, and the band between them is the buffer.

    ``trend_score is None`` **passes**, and that is deliberate. It means the board
    could not name this line, not that the line is weak — measured on
    2026-09-14, the board simply has no CPO row, so the strongest theme in the
    book (strength 6, with a live pending order) is unmeasurable. Vetoing on a
    naming gap would be the old mistake with a new input. The exit is the
    lifecycle instead: ``theme_manager.mark_theme_unscored`` takes a point a day
    off a line nobody can measure, so it walks to ``declining`` and then
    ``archived`` by itself — slower, and honest about which of the two facts it
    was.

    Falls open on a lookup *failure* too — a broken theme read must not stop the
    system from trading, and the cancel path runs again a cycle later.
    """
    if not theme:
        return None
    try:
        row = get_theme_by_name(theme)
    except Exception as e:
        logger.debug("Theme gate unavailable for %r: %s", theme, e)
        return None
    if not row:
        return None
    status = row.get("status")
    if status in ("declining", "archived"):
        return f"主线已{status}({theme})"
    score = _score_as_of(theme, row)
    if score is None:
        logger.debug("Theme gate: '%s' has no score this cycle — passing it", theme)
        return None
    from alpha_agents.data import scoring
    gate = scoring.in_force_decision_params().get("theme_gate") or {}
    bar = gate.get("cancel_score" if kind == "cancel" else "admit_score")
    if bar is None:
        return None
    if float(score) < float(bar):
        label = "明显走弱" if kind == "cancel" else "偏弱"
        return f"主线{label}({theme}评分{float(score):.2f}<{bar})"
    return None


def _score_as_of(theme: str, row: dict) -> float | None:
    """This theme's score **at the moment being simulated**.

    Live: ``theme_lines.trend_score``, which is today's answer and the right
    one when today is now.

    Replay: the newest ``theme_score_history`` row at or before the replay
    as-of. That table exists because the live column cannot answer a
    historical question — it holds one day, so reading it under replay would
    let a 2026-09-14 decision use a score computed on 2026-09-18.

    A replay with no history row returns ``None``, which is the gate's
    documented "unmeasurable, therefore pass" branch. That is honest: the
    reconstruction covers only the days its source covers, so a window before
    2026-09-08 behaves exactly as it did before this function existed — the
    gate passes everything — and the theme gate is only *testable* from that
    date on.
    """
    from alpha_agents.evolution.replay_mode import get_replay_as_of

    as_of = get_replay_as_of()
    if not as_of:
        return row.get("trend_score")

    day = str(as_of)[:10]
    try:
        from alpha_agents.data.memory_store import _get_conn
        hit = _get_conn().execute(
            "SELECT score FROM theme_score_history "
            "WHERE theme = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
            (theme, day)).fetchone()
    except Exception as e:
        # No history table (a replay directory built before this existed) or a
        # read failure: fall through to "unmeasurable", never to the live
        # column — that column is the future under a replay.
        logger.debug("Theme history unavailable for %r: %s", theme, e)
        return None
    return float(hit["score"]) if hit else None


def theme_admits(theme: str) -> str | None:
    """Admission: may a new candidate on this theme be priced?

    The same call ``check_pending_orders`` makes through ``theme_gate``, one bar
    tighter. Callers reach for this by name so that the candidate filter and
    order creation cannot end up reading two different gates once more.
    """
    return theme_gate(theme, "admit")


def theme_score_note(theme: str) -> str:
    """The theme's score, phrased for the model — "" when there is no score.

    Carried on a candidate exactly like ``prior_view``, and for the same reason:
    the point of turning a veto into a number is that the number reaches the
    thing making the decision. A bar the model cannot see is what produced
    candidates dropped for a reason nothing downstream could inspect — 5 of 5
    candidates on 2026-09-14 died on one theme and only a log line said so.
    """
    try:
        row = get_theme_by_name(theme) or {}
    except Exception as e:
        logger.debug("Theme score note unavailable for %r: %s", theme, e)
        return ""
    score = row.get("trend_score")
    if score is None:
        return ""
    from alpha_agents.data import scoring
    gate = scoring.in_force_decision_params().get("theme_gate") or {}
    bar = gate.get("admit_score")
    floor = f"，低于 {float(bar):.2f} 不下单" if bar is not None else ""
    return f"今日主线强度 {float(score):.2f}/1.00（全市场板块分位合成{floor}）"
