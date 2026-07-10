import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import cfg
from core.state import state
from core import db

log = logging.getLogger("api")
router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status":       "ok",
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "ws_clients":   len(state.ws_clients),
    }


@router.get("/api/signals")
async def get_signals(hours: int = 24, limit: int = 100):
    rows = await db.get_recent_signals(hours=hours, limit=limit)
    for r in rows:
        if r.get("ts") and not r["ts"].endswith("Z"):
            r["ts"] = r["ts"] + "Z"
    return JSONResponse({"signals": rows, "count": len(rows)})


@router.get("/api/stats")
async def get_stats():
    rows = await db.get_recent_signals(hours=24, limit=500)
    by_type: dict[str, int] = {}
    by_dir:  dict[str, int] = {}
    for r in rows:
        by_type[r["signal_type"]] = by_type.get(r["signal_type"], 0) + 1
        by_dir[r["direction"]]    = by_dir.get(r["direction"], 0) + 1
    return {
        "total_24h":    len(rows),
        "by_type":      by_type,
        "by_direction": by_dir,
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
    }


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.add_ws(ws)
    log.info(f"WS connected (total: {len(state.ws_clients)})")
    try:
        # Send recent signals on connect
        rows = await db.get_recent_signals(hours=6, limit=50)
        await ws.send_text(json.dumps({"type": "history", "data": rows}))

        while True:
            # Keep connection alive — client should send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug(f"WS error: {e}")
    finally:
        state.remove_ws(ws)
        log.info(f"WS disconnected (total: {len(state.ws_clients)})")
