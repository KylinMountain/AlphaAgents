"""A position is held because of a thesis, and the thesis is the unit.

The previous design asked the agent "what do you think now" every five
minutes. That is not how anyone holds a position. It has no memory of its
own reasoning, so it can reverse itself between two cycles without
noticing; it costs a full round of LLM reasoning 48 times a session to
answer "hold" 46 of those times; and it cannot ever say the sentence that
matters — "I said I would exit if X, and X just happened."

A trader holds a *plan*: I am in this until the theme's inflow turns
negative or it takes out 18.20. So that is what gets stored. The agent
writes the plan once, in structured form, and code checks it every cycle
for free.

The vocabulary below is the load-bearing part. If invalidation conditions
were free text the whole design collapses back into asking the model
every cycle, so every ``kind`` here is something the monitor already has
the data to evaluate — price, peak drawdown, theme strength and today's
score, sector rank, elapsed time, market breadth. The agent picks from
this list.

``narrative`` is the deliberate escape hatch: a condition that genuinely
cannot be mechanised ("if the tariff exemption is not renewed"). It never
fires on its own — it marks the thesis for an LLM re-read at a fixed slot
instead. Pretending everything is mechanisable would just push the
judgement back into a free-text field that nothing ever reads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)

# Status lifecycle:
#   active ──▶ invalidated   a listed condition fired
#          ├─▶ validated     horizon reached, thesis played out
#          ├─▶ expired       horizon reached, nothing happened either way
#          └─▶ blind_spot    closed by the hard floor with nothing fired
#
# blind_spot is the only status that says the *reasoning* failed rather
# than the trade. It is the one worth learning from, and it exists as a
# distinct status so the review can count it instead of inferring it.
ACTIVE = "active"
INVALIDATED = "invalidated"
VALIDATED = "validated"
EXPIRED = "expired"
BLIND_SPOT = "blind_spot"

CLOSED_STATUSES = (INVALIDATED, VALIDATED, EXPIRED, BLIND_SPOT)


@dataclass
class MarketView:
    """Everything a condition can be evaluated against, gathered once.

    Assembled by the caller from data the intraday cycle already fetched,
    so evaluating a thesis costs no I/O and no model call. Optional fields
    are None when that source failed this cycle — a condition that needs a
    missing field does not fire, because "I could not check" must never be
    read as "the thesis broke".
    """
    price: float
    current_return_pct: float
    peak_return_pct: float = 0.0
    holding_days: int = 0
    theme_strength: int | None = None
    theme_daily_score: int | None = None
    theme_status: str | None = None
    theme_rank: int | None = None
    theme_net_flow_yi: float | None = None
    breadth_ratio: float | None = None


@dataclass
class Condition:
    kind: str
    value: float | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "note": self.note}


# Each entry: (needs, unit, template, fired)
#   needs    — the MarketView attribute this reads; None when it reads
#              several, or one always present
#   unit     — shown to the agent so it writes 5 rather than 0.05 for 5%
#   template — how the condition reads on the dashboard and in a report;
#              a plain format string rather than a lambda so
#              prompt_vocabulary can render it with a placeholder
#   hint     — extra explanation for the agent only. Kept out of template
#              because the display already appends the agent's own note,
#              and two parentheticals in a row read as a bug:
#              "主线今日分低于 -1（当天转弱）（主线当日资金转为净流出）"
#   fired    — the predicate
#
# Keeping them in one table rather than an if-chain means the agent-facing
# documentation, the validator and the evaluator cannot drift apart: the
# prompt is generated from these same keys.
_CONDITIONS: dict[str, dict] = {
    "price_below": {
        "needs": None,
        "unit": "元",
        "template": "股价跌破 {v}",
        "fired": lambda mv, v: mv.price < v,
    },
    "price_above": {
        "needs": None,
        "unit": "元",
        "template": "股价升破 {v}",
        "hint": "目标达成",
        "fired": lambda mv, v: mv.price > v,
    },
    "drawdown_from_peak": {
        "needs": None,
        "unit": "%",
        "template": "从峰值回撤超过 {v}%",
        "fired": lambda mv, v: (mv.peak_return_pct > 0
                                and mv.peak_return_pct - mv.current_return_pct >= v),
    },
    "loss_exceeds": {
        "needs": None,
        "unit": "%",
        "template": "浮亏超过 {v}%",
        "fired": lambda mv, v: mv.current_return_pct <= -abs(v),
    },
    "theme_strength_below": {
        "needs": "theme_strength",
        "unit": "",
        "template": "主线累计强度跌到 {v} 以下",
        "fired": lambda mv, v: mv.theme_strength < v,
    },
    "theme_daily_score_below": {
        "needs": "theme_daily_score",
        "unit": "",
        "template": "主线今日分低于 {v}",
        "hint": "当天转弱，比连续走弱更早",
        "fired": lambda mv, v: mv.theme_daily_score < v,
    },
    "theme_flow_negative": {
        "needs": "theme_net_flow_yi",
        "unit": "亿",
        "template": "主线资金净流出超过 {v}亿",
        "fired": lambda mv, v: mv.theme_net_flow_yi <= -abs(v),
    },
    "theme_rank_worse_than": {
        "needs": "theme_rank",
        "unit": "名",
        "template": "主线掉出板块排名前 {v}",
        "fired": lambda mv, v: mv.theme_rank > v,
    },
    "breadth_below": {
        "needs": "breadth_ratio",
        "unit": "",
        "template": "市场涨跌比跌破 {v}",
        "hint": "大盘转弱",
        "fired": lambda mv, v: mv.breadth_ratio < v,
    },
    "no_progress_by_day": {
        "needs": None,
        "unit": "天",
        "template": "持仓 {v} 天仍未走出方向",
        "hint": "浮动在 ±1% 内算没走出方向",
        "fired": lambda mv, v: (mv.holding_days >= v
                                and abs(mv.current_return_pct) < 1.0),
    },
    "narrative": {
        # Never fires mechanically — see the module docstring.
        "needs": None,
        "unit": "",
        "template": "需要模型复核的叙事条件",
        "fired": lambda mv, v: False,
    },
}

VALID_KINDS = tuple(_CONDITIONS)

# Kinds where a *larger* value is a looser condition, used only to sanity
# check obviously inverted input (an agent asking to exit when the theme
# strength drops below 11 has written a condition that is always true).
_SANITY = {
    "theme_strength_below": (0, 10),
    "theme_daily_score_below": (-4, 5),
    "drawdown_from_peak": (0.5, 50),
    "loss_exceeds": (0.5, 50),
    "theme_rank_worse_than": (1, 100),
    "no_progress_by_day": (1, 60),
}


def describe(cond: Condition) -> str:
    spec = _CONDITIONS.get(cond.kind)
    if not spec:
        return f"未知条件 {cond.kind}"
    value = cond.value
    if cond.kind in ("theme_flow_negative", "loss_exceeds") and value is not None:
        value = abs(value)
    text = spec["template"].format(v=value)
    return f"{text}（{cond.note}）" if cond.note else text


def validate_condition(raw: dict) -> Condition | None:
    """Turn one agent-written condition into something evaluable, or drop it.

    Dropping is deliberate. A condition the evaluator cannot read is worse
    than no condition at all: it looks like the risk was considered while
    nothing will ever check it.
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip()
    if kind not in _CONDITIONS:
        logger.warning("Unknown invalidation kind %r — dropped", kind)
        return None

    note = str(raw.get("note", "")).strip()[:120]
    if kind == "narrative":
        if not note:
            logger.warning("narrative condition with no text — dropped")
            return None
        return Condition(kind=kind, value=None, note=note)

    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        logger.warning("Condition %s has non-numeric value %r — dropped",
                       kind, raw.get("value"))
        return None

    lo, hi = _SANITY.get(kind, (None, None))
    if lo is not None and not (lo <= abs(value) <= hi):
        logger.warning("Condition %s value %s outside [%s, %s] — dropped",
                       kind, value, lo, hi)
        return None

    return Condition(kind=kind, value=value, note=note)


def evaluate(conditions: list[Condition], mv: MarketView) -> Condition | None:
    """The first condition that has fired, or None.

    A condition whose data is missing this cycle does not fire. The whole
    point of moving these checks into code is that they run identically
    every time; letting a failed sector fetch close a position would make
    them run differently depending on the weather.
    """
    for cond in conditions:
        spec = _CONDITIONS.get(cond.kind)
        if not spec or cond.value is None and cond.kind != "narrative":
            continue
        needs = spec["needs"]
        if needs and getattr(mv, needs, None) is None:
            continue
        try:
            if spec["fired"](mv, cond.value):
                return cond
        except (TypeError, ValueError) as e:
            logger.debug("Condition %s could not be evaluated: %s", cond.kind, e)
    return None


def needs_narrative_review(conditions: list[Condition]) -> bool:
    return any(c.kind == "narrative" for c in conditions)


# ── Persistence ─────────────────────────────────────────────

@dataclass
class Thesis:
    code: str
    name: str = ""
    theme: str = ""
    claim: str = ""
    horizon_days: int = 5
    prob: float = 0.5
    conviction: float = 0.5
    conditions: list[Condition] = field(default_factory=list)
    id: int | None = None
    status: str = ACTIVE
    position_id: int | None = None
    created_by: str = "morning"
    created_at: str = ""
    closed_at: str | None = None
    close_kind: str = ""          # which condition fired, if any
    close_note: str = ""
    checkpoints: list[dict] = field(default_factory=list)


def _num(value, fallback):
    """A stored number, keeping a legitimate zero.

    ``row["conviction"] or 0.5`` reads 0.0 as missing and hands back 0.5 —
    so a thesis the agent had no conviction in came back as a medium one
    and sized its position near the top of the range instead of the floor.
    Zero is a real answer for every numeric column here.
    """
    return fallback if value is None else value


def _row_to_thesis(row) -> Thesis:
    conds = [Condition(**c) for c in json.loads(row["conditions"] or "[]")]
    return Thesis(
        id=row["id"], code=row["code"], name=row["name"] or "",
        theme=row["theme"] or "", claim=row["claim"] or "",
        horizon_days=_num(row["horizon_days"], 5),
        prob=_num(row["prob"], 0.5),
        conviction=_num(row["conviction"], 0.5), conditions=conds,
        status=row["status"], position_id=row["position_id"],
        created_by=row["created_by"] or "", created_at=row["created_at"] or "",
        closed_at=row["closed_at"], close_kind=row["close_kind"] or "",
        close_note=row["close_note"] or "",
        checkpoints=json.loads(row["checkpoints"] or "[]"),
    )


def create(thesis: Thesis) -> int:
    """Persist a new thesis. Returns its id."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO theses (code, name, theme, claim, horizon_days, prob, "
            " conviction, conditions, status, position_id, created_by, "
            " created_at, checkpoints) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'[]')",
            (thesis.code, thesis.name, thesis.theme, thesis.claim,
             thesis.horizon_days, thesis.prob, thesis.conviction,
             json.dumps([c.as_dict() for c in thesis.conditions],
                        ensure_ascii=False),
             thesis.status, thesis.position_id, thesis.created_by, now),
        )
        conn.commit()
        return cur.lastrowid


def get_active(code: str | None = None) -> list[Thesis]:
    conn = _get_conn()
    if code:
        rows = conn.execute(
            "SELECT * FROM theses WHERE status = ? AND code = ? ORDER BY id",
            (ACTIVE, code)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM theses WHERE status = ? ORDER BY id",
            (ACTIVE,)).fetchall()
    return [_row_to_thesis(r) for r in rows]


def get_by_position(position_id: int) -> Thesis | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM theses WHERE position_id = ? "
                       "ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    return _row_to_thesis(row) if row else None


def attach_position(thesis_id: int, position_id: int) -> None:
    """Link a thesis to the position it produced, once the order fills."""
    with _write_lock:
        conn = _get_conn()
        conn.execute("UPDATE theses SET position_id = ? WHERE id = ?",
                     (position_id, thesis_id))
        conn.commit()


def add_checkpoint(thesis_id: int, observation: str, verdict: str) -> None:
    """Record one re-read of a live thesis.

    Checkpoints are what let the review ask whether the agent changed its
    mind, and when — a thesis that flipped three times in a session is a
    different object from one held with conviction, even if both end flat.
    """
    with _write_lock:
        conn = _get_conn()
        row = conn.execute("SELECT checkpoints FROM theses WHERE id = ?",
                           (thesis_id,)).fetchone()
        if not row:
            return
        points = json.loads(row["checkpoints"] or "[]")
        points.append({"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "observation": observation[:300], "verdict": verdict})
        conn.execute("UPDATE theses SET checkpoints = ? WHERE id = ?",
                     (json.dumps(points, ensure_ascii=False), thesis_id))
        conn.commit()


def close(thesis_id: int, status: str, close_kind: str = "",
          close_note: str = "") -> None:
    if status not in CLOSED_STATUSES:
        raise ValueError(f"not a closing status: {status}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE theses SET status = ?, closed_at = ?, close_kind = ?, "
            "close_note = ? WHERE id = ?",
            (status, now, close_kind, close_note[:300], thesis_id))
        conn.commit()


def get_closed(days: int = 30) -> list[Thesis]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM theses WHERE status != ? "
        "AND closed_at >= date('now', ?) ORDER BY closed_at DESC",
        (ACTIVE, f"-{days} days")).fetchall()
    return [_row_to_thesis(r) for r in rows]


def from_recommendation(rec: dict, code: str, created_by: str) -> int | None:
    """Turn one pick into a thesis, whoever made the pick.

    Two producers, deliberately handled by one function. The morning agent
    writes its own claim, probability and invalidations. The intraday
    picks are *code-generated* — a scoring function, not a model — so
    there is nobody to ask, and the thesis is derived from the very
    signals that selected the stock. That is stricter than it sounds: the
    exit conditions become the entry reasons inverted, rather than
    thresholds a model guessed at.

    ``created_by`` is kept because it makes the two comparable. Once there
    are closed theses on both sides, the calibration split by producer
    answers a question worth asking — whether the agent's picks are better
    than the scorer's, or just more expensive.

    A pick with no usable invalidation still becomes a thesis. Refusing it
    would silently drop the position; recording it with an empty condition
    list makes the omission countable.
    """
    from alpha_agents.data.scoring import confidence_to_prob

    conditions = []
    raw = rec.get("invalidations")
    if isinstance(raw, list):
        conditions = [c for c in (validate_condition(i) for i in raw) if c]

    if not conditions:
        conditions = _derive_conditions(rec)

    if not conditions:
        logger.warning("Pick %s carries no usable invalidation — it can only "
                       "exit on horizon or hard stop", code)

    try:
        prob = min(max(float(rec.get("prob")), 0.05), 0.95)
    except (TypeError, ValueError):
        prob = confidence_to_prob(rec.get("confidence"))

    try:
        horizon = max(1, min(30, int(rec.get("horizon_days") or _DEFAULT_HORIZON)))
    except (TypeError, ValueError):
        horizon = _DEFAULT_HORIZON

    try:
        return create(Thesis(
            code=code,
            name=rec.get("name", ""),
            theme=rec.get("theme", ""),
            claim=(rec.get("claim") or rec.get("reason") or "")[:300],
            horizon_days=horizon,
            prob=prob,
            # Conviction tracks the stated probability rather than a second
            # number that would have to be kept consistent by hand.
            conviction=round((prob - 0.5) * 2, 3) if prob > 0.5 else 0.0,
            conditions=conditions,
            created_by=created_by,
        ))
    except Exception as e:
        logger.warning("Could not save thesis for %s: %s", code, e)
        return None


_DEFAULT_HORIZON = 5


def _derive_conditions(rec: dict) -> list[Condition]:
    """Invalidations for a pick that had no model to write them.

    Only conditions grounded in something the pick actually used. A stop
    that came from the scorer is a real level; a fixed drawdown percentage
    stamped on every pick is boilerplate, and boilerplate is what
    ``condition_usefulness`` exists to catch — writing it here would poison
    that statistic at the source.
    """
    out = []
    stop = rec.get("stop_loss")
    if stop:
        try:
            out.append(Condition("price_below", float(stop),
                                 "选股时算出的止损位"))
        except (TypeError, ValueError):
            logger.debug("Pick %s has an unparseable stop_loss %r — the "
                         "thesis loses its price condition",
                         rec.get("code", "?"), stop)

    # Only when the pick was made *because of* a theme. A stock picked on
    # its own merits should not be exited on a theme it was never in.
    if rec.get("theme"):
        out.append(Condition("theme_daily_score_below", 0,
                             "买入依据是主线在流入，当天转出即逻辑不成立"))

    out.append(Condition("no_progress_by_day", _DEFAULT_HORIZON,
                         "短线逻辑，不动就是错了"))
    return out


def prompt_vocabulary() -> str:
    """The condition list, rendered for an agent prompt.

    Generated from the same table the evaluator reads so the two cannot
    drift: a kind added here is immediately offered to the agent, and a
    kind the agent invents is dropped by ``validate_condition`` rather
    than silently never checked.
    """
    lines = []
    for kind, spec in _CONDITIONS.items():
        if kind == "narrative":
            continue
        unit = f"，单位{spec['unit']}" if spec["unit"] else ""
        hint = f"（{spec['hint']}）" if spec.get("hint") else ""
        lines.append(f'  "{kind}" — {spec["template"].format(v="X")}{hint}{unit}')
    lines.append('  "narrative" — 无法机械判定的条件，写在 note 里，'
                 '系统会在收盘前安排一次模型复核')
    return "\n".join(lines)
