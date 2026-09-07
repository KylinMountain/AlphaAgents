"""What was knowable when a recommendation was made.

A prediction on its own cannot be re-examined: we know the pick and the
outcome, but not what the system could see at the time, so any later
question — was the call reasonable given the evidence, did a change in
the playbook actually change decisions — is unanswerable. That gap is
what docs/strategy_evaluation_2026-09.md flagged as the largest single
one on the entry side, and G2's held-out gate depends on closing it.

So every prediction records the *decision point*: the timestamp, the
information window that fed it, the features that drove it, and the
benchmark it is measured against. Enough to replay "what was visible →
what was recommended" for any historical day.

Deliberately excluded: the news bodies themselves. They already live in
snapshot_store.news_items keyed by published_at, so the window bounds are
sufficient to recover them, and copying text into every prediction row
would bloat the table for no gain.
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Schema version for the recorded context. Bump when the shape changes so
# a replay can tell what it is looking at rather than guessing.
CONTEXT_VERSION = 1


def build_decision_context(
    *,
    task: str,
    news_window_hours: float | None = None,
    news_count: int | None = None,
    themes: list[dict] | None = None,
    market_regime: str | None = None,
    sentiment_phase: str | None = None,
    extra: dict | None = None,
    decided_at: datetime | None = None,
) -> dict:
    """Describe the decision point behind a recommendation.

    Args:
        task: which scheduled task produced it (morning_scan / intraday …).
        news_window_hours: how far back the news window reached.
        news_count: how many items were actually in that window.
        themes: active themes at decision time — name and strength only.
        market_regime: strong / neutral / weak, from exit_signals.
        sentiment_phase: the sentiment-cycle label.
        extra: task-specific fields.
        decided_at: defaults to now.
    """
    decided_at = decided_at or datetime.now()
    ctx: dict = {
        "v": CONTEXT_VERSION,
        "task": task,
        "decided_at": decided_at.strftime("%Y-%m-%d %H:%M:%S"),
    }

    if news_window_hours is not None:
        start = decided_at - timedelta(hours=news_window_hours)
        ctx["news_window"] = {
            "since": start.strftime("%Y-%m-%d %H:%M:%S"),
            "until": ctx["decided_at"],
            "hours": round(news_window_hours, 2),
            "count": news_count,
        }

    if themes:
        # Names and strengths only — the full theme rows are in
        # memory_store and would make this blob unwieldy.
        ctx["themes"] = [
            {"name": t.get("name", ""), "strength": t.get("strength", 0)}
            for t in themes[:8]
        ]

    if market_regime:
        ctx["market_regime"] = market_regime
    if sentiment_phase:
        ctx["sentiment_phase"] = sentiment_phase
    if extra:
        ctx.update(extra)

    return ctx



def merge_features(features: dict | None, context: dict | None) -> dict:
    """Combine per-stock features with the shared decision context.

    Kept under a "_ctx" key so the flat feature namespace that playbook
    clustering reads is not polluted with context fields.
    """
    merged = dict(features or {})
    if context:
        merged["_ctx"] = context
    return merged


def replay_day(date: str, report_type: str | None = None) -> dict:
    """Reconstruct what was visible and what was recommended on `date`.

    Returns the recommendations with their decision context, plus the
    news window each one drew on, read back out of the store. This is the
    G6 acceptance test in function form.
    """
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.data.snapshot_store import read_news

    conn = _get_conn()
    q = ["SELECT * FROM predictions WHERE date = ?"]
    params: list = [date]
    if report_type:
        q.append("AND report_type = ?")
        params.append(report_type)
    q.append("ORDER BY id")
    rows = [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]

    picks, windows = [], {}
    for r in rows:
        try:
            feats = json.loads(r.get("features_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            feats = {}
        ctx = feats.pop("_ctx", {}) if isinstance(feats, dict) else {}

        picks.append({
            "code": r["code"],
            "name": r["name"],
            "prob": r.get("prob"),
            "confidence": r.get("confidence"),
            "theme": r.get("theme_line"),
            "reason": r.get("reason"),
            "features": feats,
            "context": ctx,
            # Outcome, present once the horizon elapsed.
            "hit": r.get("hit"),
            "brier": r.get("brier"),
            "excess_return": r.get("excess_return"),
            "residual_alpha": r.get("residual_alpha"),
        })

        window = ctx.get("news_window")
        if window and window.get("since"):
            key = (window["since"], window["until"])
            if key not in windows:
                windows[key] = read_news(
                    sources=None, as_of=window["until"],
                    since=window["since"], limit=500,
                )

    visible_news = []
    for items in windows.values():
        visible_news.extend(items)

    return {
        "date": date,
        "picks": picks,
        "visible_news_count": len(visible_news),
        "visible_news": visible_news[:200],
        "replayable": bool(picks) and any(p["context"] for p in picks),
    }
