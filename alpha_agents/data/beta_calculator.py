"""Offline beta calculation for sector-stock correlation.

Computes multi-period beta coefficients for all stocks within each active
concept sector. Designed to run weekly (Saturday after weekly report).

Beta = Cov(stock_returns, sector_returns) / Var(sector_returns)
"""

import json
import logging
from datetime import datetime

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection
from alpha_agents.data.market_data import get_stock_history
from alpha_agents.data.memory_store import _get_conn, _write_lock, get_active_themes

logger = logging.getLogger(__name__)

# Multi-period weights
W_20D = 0.50
W_60D = 0.30
W_120D = 0.20


def compute_beta(stock_returns: list[float], sector_returns: list[float]) -> float:
    """Compute beta = Cov(stock, sector) / Var(sector).

    Returns 0.0 if sector has zero variance (no movement).
    """
    n = min(len(stock_returns), len(sector_returns))
    if n < 5:
        return 0.0

    sr = sector_returns[:n]
    st = stock_returns[:n]

    mean_sr = sum(sr) / n
    mean_st = sum(st) / n

    var_sr = sum((x - mean_sr) ** 2 for x in sr) / n
    if var_sr < 1e-12:
        return 0.0

    cov = sum((st[i] - mean_st) * (sr[i] - mean_sr) for i in range(n)) / n
    return round(cov / var_sr, 4)


def weighted_beta(
    beta_20d: float | None = None,
    beta_60d: float | None = None,
    beta_120d: float | None = None,
) -> float:
    """Compute weighted beta from multiple periods."""
    total_w = 0
    total = 0
    if beta_20d is not None:
        total += beta_20d * W_20D
        total_w += W_20D
    if beta_60d is not None:
        total += beta_60d * W_60D
        total_w += W_60D
    if beta_120d is not None:
        total += beta_120d * W_120D
        total_w += W_120D
    return round(total / total_w, 4) if total_w > 0 else 0.0


def _get_concept_stocks(concept_name: str) -> list[dict]:
    """Get all stocks in a concept from stocks.db."""
    conn = get_connection(DB_PATH)
    rows = conn.execute(
        "SELECT cs.stock_code as code, s.name "
        "FROM concept_stocks cs "
        "JOIN concepts c ON cs.concept_id = c.id "
        "JOIN stocks s ON cs.stock_code = s.code "
        "WHERE c.name = ? AND (s.is_st = 0 OR s.is_st IS NULL) "
        "AND (s.is_suspended = 0 OR s.is_suspended IS NULL)",
        (concept_name,),
    ).fetchall()
    conn.close()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def _returns_from_history(history: list[dict]) -> list[float]:
    """Convert price history to daily returns."""
    returns = []
    for i in range(1, len(history)):
        prev = history[i - 1]["close"]
        curr = history[i]["close"]
        if prev > 0:
            returns.append((curr - prev) / prev)
        else:
            returns.append(0.0)
    return returns


def _compute_sector_returns(stocks: list[dict], days: int) -> list[float]:
    """Compute equal-weight sector returns from constituent stocks.

    Uses the average daily return across all stocks as the sector benchmark.
    """
    all_returns = []
    for stock in stocks[:50]:  # Cap at 50 to limit baostock calls
        history = get_stock_history(stock["code"], days=days)
        if history and len(history) >= 5:
            all_returns.append(_returns_from_history(history))

    if not all_returns:
        return []

    # Equal-weight average across stocks for each day
    min_len = min(len(r) for r in all_returns)
    sector_returns = []
    for day in range(min_len):
        avg = sum(r[day] for r in all_returns if day < len(r)) / len(all_returns)
        sector_returns.append(avg)

    return sector_returns


def calculate_concept_betas(concept_name: str) -> list[dict]:
    """Calculate beta for all stocks in a concept.

    Returns list of {code, name, beta_20d, beta_60d, beta_120d, beta_weighted, avg_daily_amount}.
    """
    stocks = _get_concept_stocks(concept_name)
    if not stocks:
        logger.info("No stocks found for concept '%s'", concept_name)
        return []

    logger.info("Calculating betas for '%s' (%d stocks)", concept_name, len(stocks))

    # Compute sector-level returns for each period
    sector_120 = _compute_sector_returns(stocks, days=120)
    sector_60 = sector_120[-59:] if len(sector_120) >= 59 else sector_120
    sector_20 = sector_120[-19:] if len(sector_120) >= 19 else sector_120

    results = []
    for stock in stocks:
        history = get_stock_history(stock["code"], days=120)
        if not history or len(history) < 10:
            continue

        stock_returns = _returns_from_history(history)

        # Calculate beta for each period — thresholds must match slice lengths
        # so the computed beta actually reflects the intended time window.
        beta_120d = compute_beta(stock_returns, sector_120) if len(stock_returns) >= 100 else None
        beta_60d = compute_beta(stock_returns[-59:], sector_60) if len(stock_returns) >= 59 else None
        beta_20d = compute_beta(stock_returns[-19:], sector_20) if len(stock_returns) >= 19 else None

        bw = weighted_beta(beta_20d, beta_60d, beta_120d)

        # Average daily trading amount (last 20 days)
        recent = history[-20:] if len(history) >= 20 else history
        volumes = [d["volume"] for d in recent]
        closes = [d["close"] for d in recent]
        avg_amount = sum(v * c for v, c in zip(volumes, closes)) / len(recent) if recent else 0

        results.append({
            "code": stock["code"],
            "name": stock["name"],
            "beta_20d": beta_20d,
            "beta_60d": beta_60d,
            "beta_120d": beta_120d,
            "beta_weighted": bw,
            "avg_daily_amount": round(avg_amount, 0),
        })

    results.sort(key=lambda x: x["beta_weighted"], reverse=True)
    logger.info("Computed betas for %d/%d stocks in '%s'", len(results), len(stocks), concept_name)
    return results


def save_concept_betas(concept_name: str, betas: list[dict]) -> int:
    """Save computed betas to the sector_betas table (upsert)."""
    now = datetime.now().isoformat()
    saved = 0
    with _write_lock:
        conn = _get_conn()
        for b in betas:
            conn.execute(
                "INSERT INTO sector_betas (concept, code, name, beta_20d, beta_60d, "
                "beta_120d, beta_weighted, avg_daily_amount, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(concept, code) DO UPDATE SET "
                "name=excluded.name, beta_20d=excluded.beta_20d, beta_60d=excluded.beta_60d, "
                "beta_120d=excluded.beta_120d, beta_weighted=excluded.beta_weighted, "
                "avg_daily_amount=excluded.avg_daily_amount, updated_at=excluded.updated_at",
                (concept_name, b["code"], b["name"], b["beta_20d"], b["beta_60d"],
                 b["beta_120d"], b["beta_weighted"], b["avg_daily_amount"], now),
            )
            saved += 1
        conn.commit()
    return saved


def get_cached_betas(concept_name: str) -> list[dict]:
    """Read cached betas from DB for a concept."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sector_betas WHERE concept = ? ORDER BY beta_weighted DESC",
        (concept_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def run_weekly_beta_calculation() -> int:
    """Calculate betas for all active theme concepts. Called after weekly report."""
    themes = get_active_themes()
    total = 0
    for theme in themes:
        name = theme["name"]
        try:
            betas = calculate_concept_betas(name)
            if betas:
                saved = save_concept_betas(name, betas)
                total += saved
                logger.info("Saved %d betas for '%s'", saved, name)
        except Exception as e:
            logger.warning("Beta calculation failed for '%s': %s", name, e)
    logger.info("Weekly beta calculation complete: %d total betas across %d themes", total, len(themes))
    return total
