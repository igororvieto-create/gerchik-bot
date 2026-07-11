import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from core.state import state
from core import db

log = logging.getLogger("api")
router = APIRouter()

_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def _read_static(name: str) -> str:
    path = os.path.join(_static_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


@router.get("/health")
async def health():
    return {
        "status":       "ok",
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "ws_clients":   len(state.ws_clients),
    }


@router.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_read_static("index.html"))


@router.get("/manifest.json")
async def manifest():
    return JSONResponse(json.loads(_read_static("manifest.json") or "{}"))


@router.get("/sw.js")
async def sw():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_read_static("sw.js"), media_type="application/javascript")


@router.get("/api/positions")
async def get_positions():
    positions = [p.to_dict() for p in state.positions.values()]
    return JSONResponse({"positions": positions, "count": len(positions)})


@router.get("/api/balance")
async def get_balance():
    return JSONResponse({"balance": round(state.balance, 2), "currency": "USDT"})


@router.get("/api/debug")
async def debug():
    from core.config import cfg
    info = {
        "auto_trade":    cfg.AUTO_TRADE,
        "api_key_set":   bool(cfg.BYBIT_API_KEY),
        "secret_set":    bool(cfg.BYBIT_SECRET),
        "balance_state": state.balance,
        "scan_count":    state.scan_count,
        "positions":     len(state.positions),
    }
    if state.client and cfg.BYBIT_API_KEY:
        try:
            for acc_type in ("UNIFIED", "CONTRACT"):
                raw = await state.client._get(
                    "/v5/account/wallet-balance",
                    {"accountType": acc_type}, auth=True,
                )
                info[f"bybit_{acc_type.lower()}"] = raw
        except Exception as e:
            info["bybit_error"] = str(e)
    return JSONResponse(info)


@router.get("/api/trades")
async def get_trades(limit: int = 50):
    rows = await db.get_trades(limit=limit)
    return JSONResponse({"trades": rows, "count": len(rows)})


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
        rows = await db.get_recent_signals(hours=6, limit=50)
        await ws.send_text(json.dumps({"type": "history", "data": rows}))
        while True:
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
