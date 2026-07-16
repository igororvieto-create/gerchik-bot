import json
import logging
import math
import os
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from core.state import state
from core import db

log = logging.getLogger("api")
router = APIRouter()


def _sanitize(obj):
    """Recursively replace NaN/Inf floats with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj

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


@router.get("/ping")
async def ping():
    return "pong"


@router.get("/health")
async def health():
    return {
        "status":       "ok",
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "ws_clients":   len(state.ws_clients),
        "scan_error":   state.last_scan_error or None,
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
    # Surface the exact Bybit-side reason recorded by the client (IP whitelist,
    # missing permissions, empty account, ...) — a bare 0.0 is undiagnosable
    if result["balance"] == 0 and state.last_balance_error and "error" not in result:
        result["error"] = state.last_balance_error
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
    """Manually trigger a scan: saves signals to DB, pushes via WS, returns top results."""
    from core.config import cfg
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)
    from strategy.scanner import run_scan_and_broadcast
    import asyncio
    try:
        signals = await asyncio.wait_for(
            run_scan_and_broadcast(state.client, cfg.NTFY_URL), timeout=120
        )
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
    """Deep pipeline test: fetches tickers + runs full analysis on top symbol.

    ФИКС: раньше get_tickers() был внутри общего try/except, и на его сбое
    result["tickers_error"] никогда не устанавливался — только result["error"].
    Фронтенд (runDiag() в index.html) ждёт именно tickers_error, чтобы
    показать подсказку "Добавьте BYBIT_PROXY в Railway Variables" — она
    никогда не срабатывала. Теперь get_tickers() обёрнут отдельно.
    """
    import asyncio
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)

    result: dict = {}
    try:
        # Step 1: tickers — отдельный try/except, чтобы гарантированно
        # заполнить tickers_error при сбое (его ждёт фронтенд)
        try:
            tickers = await state.client.get_tickers()
        except Exception as te:
            result["tickers_error"] = str(te)
            result["verdict"] = f"FAIL: get_tickers raised {te} — Bybit API unreachable or IP blocked"
            return JSONResponse(result)

        usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        result["tickers_total"] = len(tickers)
        result["tickers_usdt"]  = len(usdt)
        if not usdt:
            result["tickers_error"] = "get_tickers returned 0 USDT tickers"
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
            from core.config import cfg as _cfg
            result["signal"] = None
            result["verdict"] = (
                f"Топ монета ({sym}) без сигнала — score < {_cfg.MIN_SCORE}. "
                "Это норма: BTC/ETH часто не дают входов. "
                "Реальные сигналы смотри на дашборде."
            )

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


@router.get("/api/status")
async def get_status():
    """Quick bot status: DB health, Bybit reachability, scan state."""
    from core.config import cfg
    info: dict = {
        "db":             "ok",
        "bybit_reachable": False,
        "tickers_count":  0,
        "api_key_set":    bool(cfg.BYBIT_API_KEY),
        "auto_trade":     cfg.AUTO_TRADE,
        "scan_count":     state.scan_count,
        "last_scan_at":   state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "positions":      len(state.positions),
        "ws_clients":     len(state.ws_clients),
        "balance":        round(state.balance, 2),
        "bybit_error":    None,
    }
    if state.client:
        try:
            tickers = await state.client.get_tickers()
            info["bybit_reachable"] = len(tickers) > 0
            info["tickers_count"] = len(tickers)
        except Exception as e:
            info["bybit_error"] = str(e)
    try:
        rows = await db.get_recent_signals(hours=1, limit=1)
        info["signals_last_hour"] = len(rows)
    except Exception as e:
        info["db"] = f"error: {e}"
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


@router.get("/api/settings")
async def get_settings():
    from core.config import cfg
    return JSONResponse({
        "auto_trade":          cfg.AUTO_TRADE,
        "min_score":           cfg.MIN_SCORE,
        "trade_min_score":     cfg.TRADE_MIN_SCORE,
        "risk_per_trade":      cfg.RISK_PER_TRADE,
        "max_positions":       cfg.MAX_POSITIONS,
        "leverage":            cfg.LEVERAGE,
        "scan_interval_min":   cfg.SCAN_INTERVAL_MIN,
        "signal_cooldown_min": cfg.SIGNAL_COOLDOWN_MIN,
    })


@router.post("/api/settings")
async def update_settings(request: Request):
    from core.config import cfg
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    changes: dict = {}
    try:
        if "auto_trade" in body:
            cfg.AUTO_TRADE = bool(body["auto_trade"])
            changes["auto_trade"] = cfg.AUTO_TRADE
        if "min_score" in body:
            v = int(body["min_score"])
            if 5 <= v <= 100:
                cfg.MIN_SCORE = v
                changes["min_score"] = v
        if "trade_min_score" in body:
            v = int(body["trade_min_score"])
            if 5 <= v <= 100:
                cfg.TRADE_MIN_SCORE = v
                changes["trade_min_score"] = v
        if "risk_per_trade" in body:
            v = float(body["risk_per_trade"])
            # Инвариант проекта: риск на сделку 1-3%, потолок жёсткий
            if 0.1 <= v <= 3.0:
                cfg.RISK_PER_TRADE = round(v, 2)
                changes["risk_per_trade"] = cfg.RISK_PER_TRADE
        if "max_positions" in body:
            v = int(body["max_positions"])
            if 1 <= v <= 20:
                cfg.MAX_POSITIONS = v
                changes["max_positions"] = v
        if "leverage" in body:
            v = int(body["leverage"])
            if 1 <= v <= 50:
                cfg.LEVERAGE = v
                changes["leverage"] = v
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"invalid parameter value: {exc}"}, status_code=400)
    log.info(f"Settings updated: {changes}")
    return JSONResponse({"ok": True, "changed": changes})


@router.post("/api/close/{symbol}")
async def close_position_route(symbol: str):
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)
    from core.state import Position
    pos = state.positions.get(symbol)
    if not isinstance(pos, Position):
        # None means either absent or enter_trade sentinel (entry still in-flight)
        return JSONResponse({"error": f"no open position for {symbol}"}, status_code=404)
    try:
        result = await state.client.close_position(symbol, pos.side, pos.qty)
        if result.get("retCode", -1) == 0:
            state.positions.pop(symbol, None)
            await db.save_trade_close(pos)
            log.info(f"Position {symbol} closed via dashboard")
            return JSONResponse({"ok": True, "symbol": symbol})
        return JSONResponse({"error": result.get("retMsg", "unknown error")}, status_code=400)
    except Exception as e:
        log.error(f"close_position {symbol}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.add_ws(ws)
    log.info(f"WS connected (total: {len(state.ws_clients)})")
    try:
        rows = await db.get_recent_signals(hours=6, limit=50)
        # _sanitize converts NaN/Inf → None so json.dumps never produces invalid JSON
        await ws.send_text(json.dumps(_sanitize({"type": "history", "data": rows})))
        while True:
            # Use receive() directly: receive_text() returns None for binary frames
            # in Starlette 0.37 and raises RuntimeError in newer versions.
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))
            # Only handle text frames; silently ignore binary frames.
            text = msg.get("text")
            if text == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"WS error: {e}")
    finally:
        state.remove_ws(ws)
        log.info(f"WS disconnected (total: {len(state.ws_clients)})")
