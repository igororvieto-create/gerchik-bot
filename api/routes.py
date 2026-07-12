import json
import logging
import os
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from core.state import state
from core import db

log = logging.getLogger("api")
router = APIRouter()

_balance_cache: dict = {"value": 0.0, "ts": 0.0}
_BALANCE_CACHE_TTL = 30

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
    positions = [p.to_dict() for p in state.positions.values() if p is not None]
    return JSONResponse({"positions": positions, "count": len(positions)})


@router.get("/api/balance")
async def get_balance():
    bal = round(state.balance, 2)
    result: dict = {"balance": bal, "currency": "USDT"}
    now = time.time()
    if bal == 0 and state.client and (now - _balance_cache["ts"]) > _BALANCE_CACHE_TTL:
        _balance_cache["ts"] = now
        from core.config import cfg
        if not cfg.BYBIT_API_KEY:
            result["warn"] = "BYBIT_API_KEY not set"
        else:
            try:
                fresh = await state.client.get_balance()
                if fresh > 0:
                    state.balance = fresh
                    result["balance"] = round(fresh, 2)
                    _balance_cache["value"] = fresh
            except Exception as e:
                result["error"] = str(e)
    return JSONResponse(result)


@router.get("/api/debug")
async def debug():
    from core.config import cfg
    info = {
        "auto_trade":    cfg.AUTO_TRADE,
        "min_score":     cfg.MIN_SCORE,
        "api_key_set":   bool(cfg.BYBIT_API_KEY),
        "secret_set":    bool(cfg.BYBIT_SECRET),
        "balance_state": state.balance,
        "scan_count":    state.scan_count,
        "positions":     len(state.positions),
    }
    if state.client:
        try:
            # Test public API
            tickers = await state.client.get_tickers()
            usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
            info["tickers_total"] = len(tickers)
            info["tickers_usdt"]  = len(usdt)
            if usdt:
                usdt.sort(key=lambda t: float(t.get("volume24h", 0)), reverse=True)
                info["top3_tickers"] = [
                    {
                        "symbol":   t["symbol"],
                        "price_chg_pct": round(float(t.get("price24hPcnt", 0)) * 100, 2),
                        "funding_pct":   round(float(t.get("fundingRate", 0)) * 100, 4),
                    }
                    for t in usdt[:3]
                ]
        except Exception as e:
            info["tickers_error"] = str(e)

        if cfg.BYBIT_API_KEY:
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


@router.get("/api/scan")
async def trigger_scan():
    """Manually trigger a scan and return top signals found."""
    from core.config import cfg
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)
    from strategy.scanner import scan_all
    import asyncio
    try:
        signals = await asyncio.wait_for(scan_all(state.client), timeout=120)
        return JSONResponse({
            "signals_found": len(signals),
            "min_score":     cfg.MIN_SCORE,
            "top10": [s.to_dict() for s in signals[:10]],
        })
    except asyncio.TimeoutError:
        return JSONResponse({"error": "scan timed out (>120s)"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/diagnostic")
async def diagnostic():
    """Deep pipeline test: fetches tickers + runs full analysis on top symbol."""
    import asyncio
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)

    result: dict = {}
    try:
        # Step 1: tickers
        tickers = await state.client.get_tickers()
        usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        result["tickers_total"] = len(tickers)
        result["tickers_usdt"]  = len(usdt)
        if not usdt:
            result["verdict"] = "FAIL: get_tickers returned 0 USDT tickers — Bybit API unreachable or IP blocked"
            return JSONResponse(result)

        usdt.sort(key=lambda t: float(t.get("volume24h", 0)), reverse=True)
        sym = usdt[0]["symbol"]
        result["test_symbol"] = sym

        # Step 2: per-symbol data
        oi_hist, klines, ob = await asyncio.gather(
            state.client.get_open_interest(sym, interval="4h", limit=12),
            state.client.get_klines(sym, interval="240", limit=26),
            state.client.get_orderbook(sym, limit=20),
        )
        result["oi_records"]    = len(oi_hist)
        result["kline_records"] = len(klines)
        result["ob_bids"]       = len(ob.get("bids", []))
        result["ob_asks"]       = len(ob.get("asks", []))

        if oi_hist:
            result["oi_latest"] = oi_hist[-1]
            result["oi_prev"]   = oi_hist[-2] if len(oi_hist) >= 2 else None
        if klines:
            result["kline_latest"] = klines[-1]

        # Step 3: balance (authenticated)
        from core.config import cfg
        if cfg.BYBIT_API_KEY:
            try:
                bal = await state.client.get_balance()
                result["balance_usdt"] = bal
                result["balance_ok"] = bal > 0
                if bal == 0:
                    result["balance_warn"] = "Balance is 0 — check API key permissions or fund your account"
            except Exception as be:
                result["balance_error"] = str(be)
        else:
            result["balance_usdt"] = None
            result["balance_warn"] = "BYBIT_API_KEY not set"

        # Step 4: full _analyze_symbol
        from strategy.scanner import _analyze_symbol
        ticker = usdt[0]
        sig = await _analyze_symbol(state.client, ticker)
        if sig:
            result["signal"] = sig.to_dict()
            result["verdict"] = f"OK: signal found score={sig.score}"
        else:
            result["signal"] = None
            result["verdict"] = "No signal generated (score < 10 or analysis error)"

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


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
