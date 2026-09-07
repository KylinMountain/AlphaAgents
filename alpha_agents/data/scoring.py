"""Scoring a prediction: Brier plus factor-residual alpha.

Why not cumulative return. SE(SR) ≈ √[(K + SR²/2)/T], so an annualised
Sharpe of 1.0 needs roughly four years of live trading to reach t=2. A
playbook change can never be evaluated on P&L within a season. A proper
scoring rule can: Brier is bounded, decomposes into calibration plus
resolution, and reaches useful precision in a few hundred observations.

Why residual and not raw return. KTD-Fin ran ten frontier models over 548
CSI300 sessions and attributed daily cross-sections onto style factors:
every model collected +11%~+29% from passive market and style exposure,
while selection alpha was negative for nine of the ten. Grading on raw
return teaches the agent beta. What is left after neutralising the
factors is the only part attributable to picking.

Both numbers are computed from market data, never from an LLM — that is
what makes them usable as a gate (see docs/self_improvement_roadmap.md,
G1 and G3).
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "market_history.db"

# Horizon a prediction's probability refers to: "P(excess return over the
# benchmark is positive within N trading days)".
DEFAULT_HORIZON_DAYS = 5

# Factors neutralised before attributing the remainder to selection.
# Deliberately a small, cheap set computable from daily OHLCV alone —
# a full Barra model is not available here and would not change the sign
# of what we are measuring.
FACTOR_NAMES = ("momentum_20d", "reversal_5d", "volatility_20d", "turnover_proxy")


def brier_score(prob: float, outcome: bool) -> float:
    """Squared error of a probabilistic forecast. Lower is better.

    0.25 is what a permanently uncertain forecaster (p=0.5) scores, so it
    is the line to beat. A confident wrong call costs up to 1.0, which is
    the point — it penalises overconfidence, unlike accuracy.
    """
    p = min(max(float(prob), 0.0), 1.0)
    return round((p - (1.0 if outcome else 0.0)) ** 2, 6)


def log_score(prob: float, outcome: bool, eps: float = 1e-6) -> float:
    """Negative log likelihood. Harsher on confident errors than Brier.

    Clipped by eps so a p=0 call that lands does not return infinity.
    """
    import math
    p = min(max(float(prob), eps), 1.0 - eps)
    return round(-math.log(p if outcome else 1.0 - p), 6)


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.debug("scoring: cannot open %s: %s", DB_PATH, e)
        return None


def _bars(conn: sqlite3.Connection, code: str, end: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT date, close, volume, turnover_rate FROM daily_kline "
        "WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?",
        (code, end, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _forward_return(conn: sqlite3.Connection, code: str, entry_date: str,
                    horizon: int) -> float | None:
    """Return over the `horizon` trading days after entry_date, in percent."""
    rows = conn.execute(
        "SELECT date, close FROM daily_kline "
        "WHERE code = ? AND date >= ? ORDER BY date LIMIT ?",
        (code, entry_date, horizon + 1),
    ).fetchall()
    if len(rows) < horizon + 1:
        return None
    start, end = rows[0]["close"], rows[horizon]["close"]
    if not start or start <= 0 or not end:
        return None
    return (end - start) / start * 100


def _market_forward_return(conn: sqlite3.Connection, entry_date: str,
                           horizon: int) -> float | None:
    """Median forward return across the market — the benchmark leg.

    Median rather than mean: A-share cross-sections are right-skewed, and
    comparing a stock against a mean manufactures a fake negative edge.
    """
    dates = conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date >= ? "
        "ORDER BY date LIMIT ?", (entry_date, horizon + 1),
    ).fetchall()
    if len(dates) < horizon + 1:
        return None
    d0, dN = dates[0]["date"], dates[horizon]["date"]

    row = conn.execute(
        "SELECT a.close AS c0, b.close AS cN FROM daily_kline a "
        "JOIN daily_kline b ON a.code = b.code AND b.date = ? "
        "WHERE a.date = ? AND a.close > 0",
        (dN, d0),
    ).fetchall()
    rets = sorted((r["cN"] - r["c0"]) / r["c0"] * 100
                  for r in row if r["c0"] and r["cN"])
    if len(rets) < 50:
        return None
    mid = len(rets) // 2
    return rets[mid] if len(rets) % 2 else (rets[mid - 1] + rets[mid]) / 2


def compute_factors(conn: sqlite3.Connection, code: str, as_of: str) -> dict | None:
    """Style exposures for one stock, from daily bars only."""
    bars = _bars(conn, code, as_of, 25)
    if len(bars) < 21:
        return None
    closes = [b["close"] for b in bars if b["close"]]
    if len(closes) < 21:
        return None

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 20:
        return None

    mean_r = sum(rets[-20:]) / 20
    var = sum((r - mean_r) ** 2 for r in rets[-20:]) / 20

    turnovers = [b["turnover_rate"] for b in bars[-20:]
                 if b.get("turnover_rate") is not None]

    return {
        "momentum_20d": (closes[-1] - closes[-21]) / closes[-21] * 100,
        "reversal_5d": (closes[-1] - closes[-6]) / closes[-6] * 100,
        "volatility_20d": var ** 0.5 * 100,
        "turnover_proxy": (sum(turnovers) / len(turnovers)) if turnovers else 0.0,
    }


def _ols_residual(y: list[float], X: list[list[float]], y0: float,
                  x0: list[float]) -> float | None:
    """Fit y ~ X across the cross-section, return the residual for (y0, x0).

    Normal equations with a small ridge term — the factors are correlated
    (momentum and reversal especially) and a plain inverse is unstable on
    a thin cross-section.
    """
    n, k = len(y), len(X[0]) if X else 0
    if n < k + 10 or k == 0:
        return None

    # Design matrix with intercept.
    A = [[1.0] + row for row in X]
    kk = k + 1
    XtX = [[sum(A[i][a] * A[i][b] for i in range(n)) for b in range(kk)]
           for a in range(kk)]
    Xty = [sum(A[i][a] * y[i] for i in range(n)) for a in range(kk)]
    for a in range(kk):
        XtX[a][a] += 1e-6                       # ridge

    # Gauss-Jordan.
    M = [XtX[a][:] + [Xty[a]] for a in range(kk)]
    for col in range(kk):
        pivot = max(range(col, kk), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            return None
        M[col], M[pivot] = M[pivot], M[col]
        pv = M[col][col]
        M[col] = [v / pv for v in M[col]]
        for r in range(kk):
            if r != col and M[r][col]:
                f = M[r][col]
                M[r] = [a_ - f * b_ for a_, b_ in zip(M[r], M[col])]
    beta = [M[a][kk] for a in range(kk)]

    fitted = beta[0] + sum(beta[i + 1] * x0[i] for i in range(k))
    return y0 - fitted


def residual_alpha(code: str, entry_date: str,
                   horizon: int = DEFAULT_HORIZON_DAYS,
                   sample_size: int = 300) -> dict | None:
    """Forward return with market and style exposure taken out.

    Fits the cross-section of forward returns on the style factors and
    returns this stock's residual — the part not explained by what it was
    already exposed to.

    Returns None when the data is not there; callers keep the raw return
    and record residual as unavailable rather than guessing.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        own_ret = _forward_return(conn, code, entry_date, horizon)
        if own_ret is None:
            return None
        own_factors = compute_factors(conn, code, entry_date)
        if not own_factors:
            return None

        market_ret = _market_forward_return(conn, entry_date, horizon)

        peers = conn.execute(
            "SELECT DISTINCT code FROM daily_kline WHERE date = ? "
            "AND code != ? LIMIT ?", (entry_date, code, sample_size * 3),
        ).fetchall()

        ys: list[float] = []
        Xs: list[list[float]] = []
        for p in peers:
            if len(ys) >= sample_size:
                break
            pc = p["code"]
            r = _forward_return(conn, pc, entry_date, horizon)
            if r is None:
                continue
            f = compute_factors(conn, pc, entry_date)
            if not f:
                continue
            ys.append(r)
            Xs.append([f[n] for n in FACTOR_NAMES])

        resid = _ols_residual(ys, Xs, own_ret,
                              [own_factors[n] for n in FACTOR_NAMES])
        return {
            "raw_return": round(own_ret, 4),
            "market_return": round(market_ret, 4) if market_ret is not None else None,
            "excess_return": (round(own_ret - market_ret, 4)
                              if market_ret is not None else None),
            "residual_alpha": round(resid, 4) if resid is not None else None,
            "factors": {k: round(v, 4) for k, v in own_factors.items()},
            "peer_count": len(ys),
            "horizon_days": horizon,
        }
    except sqlite3.Error as e:
        logger.debug("scoring: residual_alpha failed for %s: %s", code, e)
        return None
    finally:
        conn.close()


def score_prediction(code: str, entry_date: str, prob: float,
                     horizon: int = DEFAULT_HORIZON_DAYS) -> dict | None:
    """Grade one probabilistic prediction once its horizon has elapsed.

    The outcome is "did this stock beat the market", not "did it go up".
    """
    attrib = residual_alpha(code, entry_date, horizon)
    if attrib is None:
        return None

    basis = attrib["excess_return"]
    if basis is None:
        return None
    outcome = basis > 0

    return {
        **attrib,
        "prob": round(float(prob), 4),
        "outcome": outcome,
        "brier": brier_score(prob, outcome),
        "log_score": log_score(prob, outcome),
        "scored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# Starting priors for the ordinal confidence the pipeline already
# produces. Deliberately close to 0.5: the base rate of beating the
# market is ~50%, and the attribution literature puts selection alpha
# near zero, so a system with no track record has no business claiming
# 0.8. These are a seed to be re-fit once enough Brier history exists —
# overconfident seeds would just book bad scores until then.
_CONFIDENCE_PRIORS = {"high": 0.58, "medium": 0.53, "low": 0.50}

# Each cross-validation dimension that passes nudges the probability;
# 4/4 lands at 0.60, 0/4 at 0.44.
_DIM_STEP = 0.04
_DIM_BASE = 0.44


def confidence_to_prob(confidence: str | None = None,
                       dims_passed: int | None = None,
                       dims_total: int = 4) -> float:
    """Seed probability for a recommendation.

    Prefers the evidence count (how many cross-validation dimensions
    actually passed) over the ordinal label, since the count is what the
    label was derived from and it degrades more gracefully.
    """
    if dims_passed is not None:
        p = _DIM_BASE + _DIM_STEP * max(0, min(dims_passed, dims_total))
        return round(min(max(p, 0.35), 0.75), 4)
    return _CONFIDENCE_PRIORS.get((confidence or "").lower(), 0.50)


def summarize_scores(rows: list[dict]) -> dict:
    """Aggregate graded predictions into the numbers worth reporting.

    ``brier_skill`` is versus the p=0.5 reference (0.25): positive means
    the probabilities carried information, zero means they did not, and
    negative means they were worse than shrugging.
    """
    scored = [r for r in rows if r.get("brier") is not None]
    if not scored:
        return {"n": 0}

    def _won(row: dict) -> bool:
        """Outcome from either a fresh score dict or a DB row.

        score_prediction returns ``outcome``; the predictions table
        persists it as ``hit`` (0/1), so this has to read both or the
        summary breaks on whichever it was not written for.
        """
        if "outcome" in row:
            return bool(row["outcome"])
        return bool(row.get("hit"))

    n = len(scored)
    mean_brier = sum(r["brier"] for r in scored) / n
    resid = [r["residual_alpha"] for r in scored
             if r.get("residual_alpha") is not None]
    excess = [r["excess_return"] for r in scored
              if r.get("excess_return") is not None]

    def _median(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    return {
        "n": n,
        "brier": round(mean_brier, 4),
        "brier_skill": round(1 - mean_brier / 0.25, 4),
        "hit_rate": round(sum(1 for r in scored if _won(r)) / n, 4),
        "median_excess": _median(excess),
        "median_residual_alpha": _median(resid),
        "n_with_residual": len(resid),
    }
