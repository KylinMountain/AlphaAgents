"""Pentagon Pizza Index — OSINT geopolitical tension tracker.

Monitors pizza delivery patterns near the Pentagon and correlates with
Polymarket prediction markets for early warning of military/geopolitical events.

Data from https://www.pizzint.watch (free, no API key).
"""

import json
import logging

import httpx

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)

BASE_URL = "https://www.pizzint.watch"


def _fetch_api(path: str, params: dict | None = None) -> dict:
    """Fetch from pizzint.watch API."""
    with no_proxy():
        r = httpx.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()


def get_pizzint_fn() -> str:
    """Fetch Pentagon Pizza Index dashboard data + geopolitical tension indicators.

    Returns combined data:
    - Pizza store activity near Pentagon (spike detection)
    - "Nothing Ever Happens" doomsday index (Polymarket-based)
    - Breaking prediction markets (biggest movers)
    - Bilateral threat levels (USA-Russia, USA-China, etc.)
    """
    result = {
        "pizza_stores": [],
        "doomsday_index": None,
        "breaking_markets": [],
        "bilateral_threats": [],
        "error": None,
    }

    try:
        # 1. Pizza store activity
        dashboard = _fetch_api("/api/dashboard-data")
        if dashboard.get("success") and dashboard.get("data"):
            for store in dashboard["data"]:
                result["pizza_stores"].append({
                    "name": store.get("name", ""),
                    "current_popularity": store.get("current_popularity"),
                    "percentage_of_usual": store.get("percentage_of_usual"),
                    "is_spike": store.get("is_spike", False),
                    "spike_magnitude": store.get("spike_magnitude"),
                    "recorded_at": store.get("recorded_at", ""),
                })
    except Exception as e:
        logger.warning("Failed to fetch pizza dashboard: %s", e)

    try:
        # 2. Doomsday index (Nothing Ever Happens Index)
        doomsday = _fetch_api("/api/neh-index/doomsday")
        markets = doomsday.get("markets", [])
        if markets:
            # Calculate aggregate index from market prices
            prices = [m.get("price", 0) for m in markets if m.get("price")]
            avg_price = sum(prices) / len(prices) if prices else 0
            index_value = int(avg_price * 100)

            top_risks = []
            for m in sorted(markets, key=lambda x: x.get("price", 0), reverse=True)[:5]:
                top_risks.append({
                    "label": m.get("label", ""),
                    "probability": f"{m.get('price', 0) * 100:.0f}%",
                    "volume_24h": m.get("volume_24h", 0),
                })

            result["doomsday_index"] = {
                "value": index_value,
                "scale": "0=nothing, 100=it happened",
                "top_risks": top_risks,
            }
    except Exception as e:
        logger.warning("Failed to fetch doomsday index: %s", e)

    try:
        # 3. Breaking markets (biggest movers in last 6h)
        breaking = _fetch_api("/api/markets/breaking", params={
            "window": "6h",
            "final_limit": "10",
            "format": "ticker",
        })
        for m in breaking.get("markets", [])[:10]:
            result["breaking_markets"].append({
                "title": m.get("title", ""),
                "price": m.get("latest_price"),
                "price_movement_24h": m.get("price_movement"),
            })
    except Exception as e:
        logger.warning("Failed to fetch breaking markets: %s", e)

    try:
        # 4. Bilateral threat levels
        gdelt = _fetch_api("/api/gdelt/batch", params={
            "pairs": "usa_russia,russia_ukraine,usa_china,china_taiwan,usa_iran,usa_venezuela",
        })
        if isinstance(gdelt, dict):
            for pair, data in gdelt.items():
                if isinstance(data, dict):
                    result["bilateral_threats"].append({
                        "pair": pair,
                        "threat_level": data.get("threat_level", ""),
                        "tone": data.get("tone"),
                        "event_count": data.get("event_count"),
                    })
    except Exception as e:
        logger.warning("Failed to fetch bilateral threats: %s", e)

    # Summary line for quick assessment
    spikes = [s for s in result["pizza_stores"] if s.get("is_spike")]
    doomsday_val = result["doomsday_index"]["value"] if result["doomsday_index"] else 0

    if spikes:
        result["alert"] = f"PIZZA SPIKE DETECTED at {len(spikes)} locations — potential unusual Pentagon activity"
    elif doomsday_val > 70:
        result["alert"] = f"HIGH TENSION — Doomsday index at {doomsday_val}/100"
    elif doomsday_val > 40:
        result["alert"] = f"ELEVATED — Doomsday index at {doomsday_val}/100"
    else:
        result["alert"] = f"NORMAL — Doomsday index at {doomsday_val}/100"

    return json.dumps(result, ensure_ascii=False)
