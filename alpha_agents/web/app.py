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

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent.parent / "web" / "dist"

app = FastAPI(title="AlphaAgents", docs_url=None, redoc_url=None)


# --- REST endpoints ---

@app.get("/api/snapshot")
async def get_snapshot():
    """Current pipeline state snapshot."""
    return JSONResponse(event_bus.get_snapshot())


@app.get("/api/reports")
async def get_reports():
    """Historical analysis reports (most recent 50)."""
    return JSONResponse({"reports": event_bus._reports})


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
