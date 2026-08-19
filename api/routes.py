import hmac
import json
import logging
from typing import Optional
import math
import os
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse as _BaseJSONResponse

from core.state import state
from core import db

log = logging.getLogger("api")
router = APIRouter()

_no_token_warned_at: float = 0.0


class JSONResponse(_BaseJSONResponse):
    """JSONResponse with explicit charset: starlette sends bare
    'application/json' (no charset for non-text/* types), and some mobile
    HTTP stacks then decode Cyrillic UTF-8 bodies as Latin-1 → mojibake."""
    media_type = "application/json; charset=utf-8"


def _require_token(request: Request) -> Optional[JSONResponse]:
    """Защита изменяющих/чувствительных эндпоинтов.

    Railway-домен публичный: без токена любой, кто знает адрес (или
    предзагрузчик браузера, превью-бот мессенджера), мог менять настройки,
    закрывать позиции и читать баланс. Токен задаётся переменной
    DASHBOARD_TOKEN; если она не задана — работаем как раньше, но пишем
    предупреждение, чтобы это не осталось незамеченным.
    """
    token = os.getenv("DASHBOARD_TOKEN", "").strip()
    if not token:
        # Раньше здесь стоял молчаливый return: докстринг обещал
        # предупреждение, а в логах не было ни строки, и оператор считал
        # защиту включённой. Пишем — с троттлингом, чтобы не залить лог.
        global _no_token_warned_at
        now = time.time()
        if now - _no_token_warned_at > 3600:
            _no_token_warned_at = now
            log.error(
                "DASHBOARD_TOKEN не задан — мутирующие эндпоинты открыты всем, "
                "кто знает адрес. Задай переменную в Railway."
            )
        return None
    got = (request.headers.get("X-Dashboard-Token")
           or request.query_params.get("token") or "")
    # compare_digest, а не ==: обычное сравнение строк выходит на первом
    # различающемся байте и по времени ответа выдаёт префикс токена.
    # Сравниваем БАЙТЫ: строковая форма бросает TypeError на не-ASCII, и
    # кириллический DASHBOARD_TOKEN ронял бы каждый защищённый запрос в 500
    # (фронтенд при этом молча показывал бы «—» без единого объяснения).
    if not hmac.compare_digest(got.encode("utf-8", "ignore"), token.encode("utf-8")):
        log.warning(f"Отклонён запрос без валидного токена: {request.url.path}")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


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
async def get_positions(request: Request):
    # Отдаёт entry/sl/tp2/qty/order_id живых позиций — тот же класс данных,
    # что баланс: точный уровень стопа и объём публичны для любого, кто
    # знает адрес Railway.
    if (deny := _require_token(request)) is not None:
        return deny
    positions = [p.to_dict() for p in state.positions.values() if p is not None]
    # _sanitize: unrealised_pnl is parsed from Bybit strings — a single NaN
    # would make JSONResponse raise (json.dumps allow_nan=False) and 500 the route
    return JSONResponse(_sanitize({"positions": positions, "count": len(positions)}))


@router.get("/api/balance")
async def get_balance(request: Request):
    # Баланс — ровно то чувствительное чтение, ради которого вводился токен
    # (см. докстринг _require_token); незащищённым он остался по недосмотру.
    if (deny := _require_token(request)) is not None:
        return deny
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
async def debug(request: Request):
    if (deny := _require_token(request)) is not None:
        return deny
    from core.config import cfg
    info: dict = {
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
async def trigger_scan(request: Request):
    """Ручной скан: сохраняет сигналы, шлёт по WS, НЕ открывает сделки."""
    if (deny := _require_token(request)) is not None:
        return deny
    from core.config import cfg
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)
    from strategy.scanner import run_scan_and_broadcast
    import asyncio
    try:
        signals = await asyncio.wait_for(
            run_scan_and_broadcast(state.client, cfg.NTFY_URL, allow_trading=False),
            timeout=120,
        )
        return JSONResponse(_sanitize({
            "signals_found": len(signals),
            "min_score":     cfg.MIN_SCORE,
            "top10": [s.to_dict() for s in signals[:10]],
        }))
    except asyncio.TimeoutError:
        return JSONResponse({"error": "scan timed out (>120s)"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/diagnostic")
async def diagnostic(request: Request):
    """Deep pipeline test: fetches tickers + runs full analysis on top symbol.

    ФИКС: раньше get_tickers() был внутри общего try/except, и на его сбое
    result["tickers_error"] никогда не устанавливался — только result["error"].
    Фронтенд (runDiag() в index.html) ждёт именно tickers_error, чтобы
    показать подсказку "Добавьте BYBIT_PROXY в Railway Variables" — она
    никогда не срабатывала. Теперь get_tickers() обёрнут отдельно.
    """
    import asyncio
    # Пять REST-запросов к Bybit на вызов, один из них аутентифицированный:
    # без токена цикл таких запросов выжигает rate-limit ключа, и плановый
    # скан начинает падать. Плюс ответ содержит balance_usdt.
    if (deny := _require_token(request)) is not None:
        return deny
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)

    result: dict = {}
    # Реальная проба БД: фронтенд рисовал «DB ✅ OK» безусловно, и при
    # read-only томе (или кончившемся месте) диагностика бодро рапортовала
    # исправность, пока ни один сигнал не сохранялся.
    try:
        await db.get_recent_signals(hours=1, limit=1)
        result["db"] = "ok"
    except Exception as dbe:
        result["db"] = str(dbe)
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

    return JSONResponse(_sanitize(result))


@router.get("/api/status")
async def get_status(request: Request):
    """Quick bot status: DB health, Bybit reachability, scan state."""
    if (deny := _require_token(request)) is not None:
        return deny
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
        # Дневной предохранитель был невидим снаружи: бот стоял, а дашборд
        # показывал «🤖 Авто», и пользователь считал его вооружённым.
        "trading_halted":     state.trading_halted,
        "daily_realized_pnl": round(state.daily_realized_pnl, 2),
        "daily_loss_limit_pct": cfg.DAILY_LOSS_LIMIT_PCT,
        "scan_error":     state.last_scan_error or None,
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


@router.get("/api/outcomes")
async def get_outcomes(request: Request, days: int = 7):
    # История сделок и форвард-тест содержат реализованный PnL — тот же
    # класс данных, что и баланс, ради которого вводился токен.
    if (deny := _require_token(request)) is not None:
        return deny
    """Forward-test breakdown: winrate by score bucket, direction, signal type."""
    summary = await db.get_outcome_stats(days=days)
    breakdown = await db.get_outcome_breakdown(days=days)
    return JSONResponse(_sanitize({"summary": summary, **breakdown}))


@router.get("/api/trades")
async def get_trades(request: Request, limit: int = 50):
    # История сделок и форвард-тест содержат реализованный PnL — тот же
    # класс данных, что и баланс, ради которого вводился токен.
    if (deny := _require_token(request)) is not None:
        return deny
    rows = await db.get_trades(limit=limit)
    for r in rows:
        for f in ("opened_at", "closed_at"):
            if r.get(f) and not str(r[f]).endswith("Z"):
                r[f] = r[f] + "Z"
    return JSONResponse(_sanitize({"trades": rows, "count": len(rows)}))


@router.get("/api/signals")
async def get_signals(hours: int = 24, limit: int = 100):
    rows = await db.get_recent_signals(hours=hours, limit=limit)
    for r in rows:
        if r.get("ts") and not r["ts"].endswith("Z"):
            r["ts"] = r["ts"] + "Z"
    return JSONResponse({"signals": rows, "count": len(rows)})


@router.get("/api/stats")
async def get_stats(request: Request):
    # daily_realized_pnl — денежная величина; закрываем тем же токеном.
    if (deny := _require_token(request)) is not None:
        return deny
    rows = await db.get_recent_signals(hours=24, limit=500)
    by_type: dict[str, int] = {}
    by_dir:  dict[str, int] = {}
    for r in rows:
        by_type[r["signal_type"]] = by_type.get(r["signal_type"], 0) + 1
        by_dir[r["direction"]]    = by_dir.get(r["direction"], 0) + 1
    outcomes = await db.get_outcome_stats(days=7)
    from strategy.trader import _today_utc
    # JSONResponse, а не голый dict: starlette отдал бы его без charset, и
    # scan_error с русским текстом («все прокси исчерпаны») приезжал бы на
    # телефон кракозябрами. _sanitize заодно чистит NaN в outcomes_7d.
    return JSONResponse(_sanitize({
        "total_24h":    len(rows),
        "by_type":      by_type,
        "by_direction": by_dir,
        "outcomes_7d":  outcomes,
        "scan_count":   state.scan_count,
        "last_scan_found": state.last_scan_found,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        # Сканер намеренно двигает счётчик и время даже при провале, поэтому
        # без этого поля HTTP-фолбэк рисовал растущий «Скан #43» и зелёный
        # пульс, пока каждый скан падал с ошибкой Bybit.
        "scan_error":   state.last_scan_error or None,
        # Предохранитель виден и в фолбэке, иначе остановленный бот выглядит
        # работающим ровно в том режиме, где WS недоступен.
        "trading_halted": state.trading_halted,
        # Причина остановки: halt выставляется и при сбое чтения БД, а фронт
        # печатал единственную формулировку «дневной лимит убытка достигнут» —
        # пользователь шёл искать несуществующие потери.
        # Сравнение с СЕГОДНЯШНЕЙ датой, а не проверка на truthy: при сбое БД
        # _ensure_daily_state выходит ДО присвоения daily_pnl_date, и она
        # остаётся вчерашней (то есть truthy). После полуночного роллбэка это
        # давало "daily_loss" с суммой за вчерашний, уже закрытый день —
        # ровно та ложь, ради устранения которой поле и вводилось.
        "halt_reason": (
            "daily_loss" if state.daily_pnl_date == _today_utc() else "db_error"
        ) if state.trading_halted else None,
        "daily_realized_pnl": round(state.daily_realized_pnl, 2),
    }))


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
        # Пороги, по которым фронт решает, возьмёт ли бот сигнал. Раньше он
        # хардкодил MIN_TRADE_HEADROOM_R=2.0, и при другом значении в env
        # бейдж «запас» врал о решении бота — ровно тот дефект, ради
        # которого убрали константный «1:2.0».
        "min_trade_headroom_r": cfg.MIN_TRADE_HEADROOM_R,
        "min_rr": cfg.MIN_RR,
    })


@router.post("/api/settings")
async def update_settings(request: Request):
    if (deny := _require_token(request)) is not None:
        return deny
    from core.config import cfg
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Валидируем ВСЁ до применения: раньше ошибка на позднем поле
    # возвращала 400 уже ПОСЛЕ того, как ранние поля были применены —
    # UI показывал "не сохранено", а риск и авто-торговля уже изменились.
    def _as_bool(v):
        # bool("false") is True — строка "false" ВКЛЮЧАЛА реальную торговлю.
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return v.strip().lower() in ("true", "1", "yes")
        raise ValueError(f"ожидается true/false, получено {v!r}")

    spec: dict = {
        "auto_trade":      (_as_bool, None,     None),
        "min_score":       (int,   5,           100),
        "trade_min_score": (int,   5,           100),
        "risk_per_trade":  (float, 0.1,         3.0),   # инвариант: риск ≤3%
        "max_positions":   (int,   1,           20),
        "leverage":        (int,   1,           5),     # инвариант: плечо ≤5x
    }
    field_map = {
        "auto_trade": "AUTO_TRADE", "min_score": "MIN_SCORE",
        "trade_min_score": "TRADE_MIN_SCORE", "risk_per_trade": "RISK_PER_TRADE",
        "max_positions": "MAX_POSITIONS", "leverage": "LEVERAGE",
    }
    pending: dict = {}
    rejected: dict = {}
    for key, (caster, lo, hi) in spec.items():
        if key not in body:
            continue
        try:
            v = caster(body[key])
        except (TypeError, ValueError):
            rejected[key] = f"недопустимое значение: {body[key]!r}"
            continue
        if lo is not None and not (lo <= v <= hi):
            rejected[key] = f"вне диапазона [{lo}, {hi}]"
            continue
        pending[key] = round(v, 2) if caster is float else v  # bool/int уходят как есть

    # Связь из docs/REVIEW.md: порог показа выше торгового делает торговый
    # порог фиктивным — сигналы отсеиваются раньше, чем дойдут до входа.
    _new_min = pending.get("min_score", cfg.MIN_SCORE)
    _new_trade = pending.get("trade_min_score", cfg.TRADE_MIN_SCORE)
    if _new_min > _new_trade:
        rejected["min_score"] = (f"порог показа {_new_min} выше торгового "
                                 f"{_new_trade} — торговый стал бы фиктивным")

    # Связь из docs/REVIEW.md §2: полный набор позиций не должен пробивать
    # дневной лимит. Поштучная валидация её не ловила — risk_per_trade=3.0 и
    # max_positions=20 оба лежат в своих диапазонах и давали 60% одновременного
    # риска при лимите 6%. Предохранитель считает только РЕАЛИЗОВАННЫЙ убыток,
    # поэтому все позиции успевали открыться до первого стопа.
    _new_risk = pending.get("risk_per_trade", cfg.RISK_PER_TRADE)
    _new_pos  = pending.get("max_positions", cfg.MAX_POSITIONS)
    _worst = _new_risk * _new_pos
    if _worst > cfg.DAILY_LOSS_LIMIT_PCT:
        rejected["max_positions"] = (
            f"риск {_new_risk}% × {_new_pos} позиций = {_worst:.1f}% "
            f"одновременного риска при дневном лимите {cfg.DAILY_LOSS_LIMIT_PCT}% — "
            f"максимум {max(1, int(cfg.DAILY_LOSS_LIMIT_PCT // _new_risk))} позиций"
        )

    if rejected:
        # Ничего не применяем — частичное сохранение хуже отказа
        return JSONResponse(
            {"error": "некорректные параметры", "rejected": rejected}, status_code=400
        )

    changes: dict = {}
    for key, v in pending.items():
        setattr(cfg, field_map[key], v)
        changes[key] = v

    log.info(f"Settings updated: {changes}")
    return JSONResponse({"ok": True, "changed": changes})


@router.post("/api/close/{symbol}")
async def close_position_route(symbol: str, request: Request):
    if (deny := _require_token(request)) is not None:
        return deny
    if state.client is None:
        return JSONResponse({"error": "client not initialized"}, status_code=503)
    from core.state import Position
    from strategy.trader import (fetch_matching_closed_pnl, record_realized_close,
                                 close_and_verify, _forget_symbol)
    import asyncio as _asyncio
    pos = state.positions.get(symbol)
    if not isinstance(pos, Position):
        # None means either absent or enter_trade sentinel (entry still in-flight)
        return JSONResponse({"error": f"no open position for {symbol}"}, status_code=404)
    try:
        # close_and_verify, а не голый close_position: retCode==0 означает
        # «ордер принят», а не «позиция закрыта». IOC reduceOnly в неликвиде
        # заливается частично, и прежний код снимал позицию с учёта, оставляя
        # остаток на бирже — он тут же усыновлялся как MANUAL и уже никогда
        # не защищался стопом и не занимал слот.
        closed, remaining = await close_and_verify(
            state.client, symbol, pos.side, pos.qty)
        if not closed:
            if remaining > 0:
                pos.qty = remaining  # остаётся под наблюдением монитора
                return JSONResponse(
                    {"error": f"закрыто частично, остаток {remaining} — "
                              f"позиция под наблюдением, повторите"},
                    status_code=409,
                )
            return JSONResponse(
                {"error": "закрытие не подтверждено биржей — позиция под наблюдением"},
                status_code=502,
            )
        is_manual = getattr(pos, "signal_type", "") == "MANUAL"
        # _forget_symbol, а не pop: счётчики попыток обязаны умирать вместе
        # с позицией. Пользователь жмёт «закрыть» как раз тогда, когда видит
        # предупреждение о неподтверждённом стопе, то есть при _SL_RETRIES=2 —
        # и следующая сделка по этому символу шла бы в аварийное закрытие
        # с первой же осечки.
        _forget_symbol(symbol)
        if is_manual:
            # Чужая сделка: закрыли по просьбе пользователя, но в историю
            # бота и в дневной предохранитель она не идёт.
            log.info(f"Ручная позиция {symbol} закрыта через дашборд — вне учёта бота")
            return JSONResponse({"ok": True, "symbol": symbol, "manual": True})
        # Fetch the real exit price / PnL (same path as monitor_positions).
        # Retries: Bybit's closed-pnl record can lag several seconds —
        # a single attempt would permanently record pnl=0 and the loss
        # would bypass the daily circuit breaker
        await _asyncio.sleep(1.0)
        exit_price, pnl = await fetch_matching_closed_pnl(
            state.client, pos, attempts=4, delay=1.5)
        # Ручное закрытие с дашборда: PnL в дневной предохранитель идёт
        # ТОЛЬКО при подтверждённой записи, иначе блок реконсиляции учтёт
        # его повторно на ближайшем тике монитора.
        if await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl) == db.CLOSE_OK:
            record_realized_close(pnl)
        log.info(f"Position {symbol} closed via dashboard exit={exit_price:.4f} pnl={pnl:+.2f}")
        return JSONResponse({"ok": True, "symbol": symbol,
                             "exit_price": exit_price, "pnl": pnl})
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
        for _r in rows:  # без 'Z' браузер трактует время как локальное
            if _r.get("ts") and not str(_r["ts"]).endswith("Z"):
                _r["ts"] = _r["ts"] + "Z"
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
