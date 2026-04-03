"""FastAPI backend for AlphaAgents Web UI.

Serves the React frontend as static files and provides:
- WebSocket /ws for real-time pipeline events
- REST /api/snapshot for current pipeline state
- REST /api/reports for historical analysis reports
"""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from alpha_agents.web.events import event_bus
from alpha_agents.data.report_store import (
    get_recent_reports, get_recent_reviews,
    get_predictions_by_date, get_event_graph,
)

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent.parent / "web" / "dist"

app = FastAPI(title="AlphaAgents", docs_url=None, redoc_url=None)

# Reference to monitor (set by main.py cmd_web)
_monitor = None


def set_monitor(monitor):
    global _monitor
    _monitor = monitor


# --- REST endpoints ---

@app.get("/api/snapshot")
async def get_snapshot():
    """Current pipeline state snapshot."""
    return JSONResponse(event_bus.get_snapshot())


@app.get("/api/reports")
async def get_reports():
    """Historical analysis reports from DB + in-memory."""
    db_reports = await asyncio.to_thread(get_recent_reports, 20)
    return JSONResponse({"reports": db_reports, "live": event_bus._reports})


@app.get("/api/reviews")
async def get_reviews():
    """Historical daily reviews."""
    reviews = await asyncio.to_thread(get_recent_reviews, 10)
    return JSONResponse({"reviews": reviews})


@app.get("/api/predictions/{date}")
async def get_predictions(date: str):
    """Get predictions for a specific date."""
    preds = await asyncio.to_thread(get_predictions_by_date, date)
    return JSONResponse({"date": date, "predictions": preds})


@app.get("/api/event-graph")
async def get_event_graph_api():
    """Get event relationship graph for visualization."""
    graph = await asyncio.to_thread(get_event_graph, 50)
    return JSONResponse(graph)


@app.post("/api/trigger")
async def trigger_analysis():
    """Manually trigger one analysis cycle."""
    if _monitor is None:
        return JSONResponse({"error": "Monitor not initialized"}, status_code=503)
    # Run one cycle in background
    asyncio.create_task(_monitor_one_cycle())
    return JSONResponse({"status": "triggered"})


async def _monitor_one_cycle():
    """Run a single monitor cycle (for manual trigger)."""
    if _monitor is None:
        return
    try:
        raw_items = await _monitor._fetch_all_sources()
        new_items = _monitor.deduplicate(raw_items)
        if not new_items:
            return
        from alpha_agents.pipeline.digest import digest_news
        events = await digest_news(new_items)
        if not events:
            return
        from alpha_agents.agents.strategist import run_analysis
        events_json = json.dumps(events, ensure_ascii=False, indent=2)
        prompt = (
            f"以下是经过预处理的{len(events)}条重要事件摘要，"
            f"请对高重要性事件进行深度多市场影响分析，输出完整分析报告：\n\n"
            f"{events_json}"
        )
        await run_analysis(prompt)
    except Exception:
        logger.exception("Manual trigger failed")


@app.post("/api/review")
async def trigger_review():
    """Manually trigger daily review."""
    from alpha_agents.pipeline.daily_review import run_daily_review
    result = await run_daily_review()
    return JSONResponse(result)


@app.get("/api/sources")
async def get_sources():
    """List of configured news sources."""
    return JSONResponse({"sources": [
        {"id": "eastmoney", "name": "东方财富", "type": "domestic"},
        {"id": "eastmoney_live", "name": "东方财富7x24", "type": "domestic"},
        {"id": "cls", "name": "财联社电报", "type": "domestic"},
        {"id": "wallstreetcn", "name": "华尔街见闻", "type": "domestic"},
        {"id": "jin10", "name": "金十数据", "type": "domestic"},
        {"id": "xinhua", "name": "新华社", "type": "domestic"},
        {"id": "pboc", "name": "人民银行", "type": "domestic"},
        {"id": "world_rss", "name": "BBC/CNBC/Google", "type": "international"},
        {"id": "whitehouse", "name": "白宫", "type": "international"},
        {"id": "fed", "name": "美联储", "type": "international"},
        {"id": "sec", "name": "SEC", "type": "international"},
        {"id": "social", "name": "社交媒体", "type": "social"},
    ]})


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue = event_bus.subscribe()
    try:
        # Send current snapshot on connect
        snapshot = event_bus.get_snapshot()
        await ws.send_text(json.dumps({"type": "snapshot", "data": snapshot}, ensure_ascii=False))

        # Stream events
        while True:
            msg = await queue.get()
            event_bus.mark_consumed(queue)
            await ws.send_text(json.dumps({"type": "event", "data": json.loads(msg)}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket closed", exc_info=True)
    finally:
        event_bus.unsubscribe(queue)


# --- Static files (React frontend) ---

@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    """Serve React SPA — try exact file, fallback to index.html."""
    file_path = FRONTEND_DIR / full_path
    if full_path and file_path.is_file():
        return FileResponse(file_path)
    index = FRONTEND_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {"error": "Frontend not built. Run: cd web && npm install && npm run build"},
        status_code=404,
    )
