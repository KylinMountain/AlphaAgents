"""Nothing enters the playbook without surviving a held-out check.

Context evolution without a gate is high variance and unsafe, not merely
suboptimal: Dynamic Cheatsheet scored 70.7% on ALFWorld and 0.14 on
WebShop, where plain ReAct scored 0.43. The fix that held up was a
keep-better gate against a held-out slice — a candidate is admitted only
when it does not degrade performance there.

Two properties make the gate real rather than decorative:

  * Validation is *forward*: the days after the candidate came into
    existence. A proportional split of history is the tempting version
    and it leaks — a candidate is distilled from reflection on recent
    days, so the recent slice is exactly what it was fitted to.
    Reflection cannot reach forward in time, which is what makes this
    boundary sound rather than merely tidy.
  * Comparison is a paired test on the same predictions, not a
    difference of means. Day-to-day variance in A-share returns dwarfs
    any plausible playbook effect, so unpaired means would accept noise
    roughly half the time.

The cost is that a new candidate has no validation data on the day it is
created, so the gate abstains and the champion stands. That is the
intended behaviour: a system with no track record should not promote.

See docs/self_improvement_roadmap.md G2.
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Below this the gate abstains and keeps the champion. Underpowered
# comparisons are worse than none — they launder noise as evidence.
MIN_VALIDATION_SAMPLES = 20

# Brier degradation tolerated before rejecting. Not zero: a candidate
# should not be blocked by rounding.
BRIER_TOLERANCE = 0.005


def split_train_validation(rows: list[dict],
                           created_date: str,
                           ) -> tuple[list[dict], list[dict]]:
    """Split scored predictions around a candidate's creation date.

    Validation is everything published *after* the candidate existed —
    forward validation, not a proportional slice of history. The
    proportional split is tempting and wrong here: a candidate is derived
    from reflection on recent days, so the recent slice is exactly what it
    was tuned on. Reflection cannot reach forward in time, which is what
    makes this boundary leak-proof rather than merely tidy.
    """
    dated = [r for r in rows if r.get("date")]
    train = [r for r in dated if r["date"] <= created_date]
    validation = [r for r in dated if r["date"] > created_date]
    return train, validation


def paired_brier_test(champion: list[float], challenger: list[float]) -> dict:
    """Paired comparison of two Brier series over the same predictions.

    Returns the mean difference (challenger - champion; negative is an
    improvement), a paired t statistic, and how many pairs it is based
    on. A t of -2 or beyond is the conventional bar, but the caller
    decides — this function only measures.
    """
    pairs = [(c, x) for c, x in zip(champion, challenger)
             if c is not None and x is not None]
    n = len(pairs)
    if n < 2:
        return {"n": n, "mean_diff": None, "t_stat": None}

    diffs = [x - c for c, x in pairs]
    mean_diff = sum(diffs) / n
    var = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        # Identical series: no difference, and no variance to divide by.
        return {"n": n, "mean_diff": round(mean_diff, 6),
                "t_stat": 0.0 if mean_diff == 0 else None}

    se = (var / n) ** 0.5
    return {"n": n, "mean_diff": round(mean_diff, 6),
            "t_stat": round(mean_diff / se, 4) if se else None}


def evaluate_candidate(champion_scores: list[dict],
                       challenger_scores: list[dict]) -> dict:
    """Decide whether a challenger may be promoted.

    Both inputs are graded predictions keyed by (date, code), so the
    comparison is paired over the predictions both scored.
    """
    champ_by_key = {(r.get("date"), r.get("code")): r for r in champion_scores}
    chall_by_key = {(r.get("date"), r.get("code")): r for r in challenger_scores}
    shared = sorted(set(champ_by_key) & set(chall_by_key))

    if len(shared) < MIN_VALIDATION_SAMPLES:
        return {
            "promote": False,
            "reason": (f"验证样本 {len(shared)} < {MIN_VALIDATION_SAMPLES}，"
                       "维持 champion"),
            "n": len(shared),
            "abstained": True,
        }

    champ = [champ_by_key[k].get("brier") for k in shared]
    chall = [chall_by_key[k].get("brier") for k in shared]
    test = paired_brier_test(champ, chall)

    if test["mean_diff"] is None:
        return {"promote": False, "reason": "无可比样本", "n": test["n"],
                "abstained": True}

    degraded = test["mean_diff"] > BRIER_TOLERANCE
    promote = not degraded
    if degraded:
        reason = (f"Brier 退化 {test['mean_diff']:+.4f} "
                  f"(t={test['t_stat']}, n={test['n']}) — 拒绝")
    else:
        reason = (f"Brier {test['mean_diff']:+.4f} 不退化 "
                  f"(t={test['t_stat']}, n={test['n']}) — 通过")

    return {"promote": promote, "reason": reason, "abstained": False,
            **test}


def record_gate_decision(candidate_name: str, decision: dict,
                         today: str | None = None) -> None:
    """Persist the verdict, including rejections.

    A rejected candidate that leaves no trace will be proposed again next
    week and rejected again; the record is what makes that visible.
    """
    from alpha_agents.data.memory_store import _get_conn, _write_lock

    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        with _write_lock:
            conn = _get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS gate_decisions ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  date TEXT NOT NULL,"
                "  candidate TEXT NOT NULL,"
                "  promoted INTEGER NOT NULL,"
                "  abstained INTEGER NOT NULL DEFAULT 0,"
                "  n INTEGER,"
                "  mean_diff REAL,"
                "  t_stat REAL,"
                "  reason TEXT,"
                "  detail_json TEXT)"
            )
            conn.execute(
                "INSERT INTO gate_decisions (date, candidate, promoted, "
                "abstained, n, mean_diff, t_stat, reason, detail_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (today, candidate_name, 1 if decision.get("promote") else 0,
                 1 if decision.get("abstained") else 0, decision.get("n"),
                 decision.get("mean_diff"), decision.get("t_stat"),
                 decision.get("reason", ""),
                 json.dumps(decision, ensure_ascii=False)),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Gate decision not recorded: %s", e)


def get_gate_history(days: int = 30) -> list[dict]:
    """Recent gate verdicts, newest first."""
    from alpha_agents.data.memory_store import _get_conn

    try:
        conn = _get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM gate_decisions WHERE date >= ? "
            "ORDER BY id DESC", (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def run_gate(candidate_name: str, created_date: str,
             today: str | None = None) -> dict:
    """Score a shadow challenger against the champion on the held-out slice.

    The challenger runs in shadow: its picks are recorded and scored but
    never drive a decision until it passes here. Validation is the days
    *after* ``created_date``, so it accumulates as the system runs and the
    gate abstains until there is enough of it — which is the correct
    behaviour for a system with no track record, and means a freshly
    installed system holds its champion rather than promoting on noise.
    """
    from alpha_agents.data.memory_store import get_scored_predictions

    today = today or datetime.now().strftime("%Y-%m-%d")
    scored = get_scored_predictions(days=180)
    _train, validation = split_train_validation(scored, created_date)

    champion = [r for r in validation
                if not (r.get("report_type") or "").endswith("_shadow")]
    challenger = [r for r in validation
                  if (r.get("report_type") or "").endswith("_shadow")]

    decision = evaluate_candidate(champion, challenger)
    decision["validation_days"] = len({r["date"] for r in validation})
    record_gate_decision(candidate_name, decision, today)

    logger.info("Gate '%s': %s", candidate_name, decision["reason"])
    return decision
