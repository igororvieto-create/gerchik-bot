import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("bybit")

BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
RECV_WINDOW = 5000


def _fmt_price(v: float) -> str:
    """Цена как строка БЕЗ научной нотации.

    str(round(x, 8)) переключается на экспоненту ниже 1e-4: 7.3e-06,
    9.8e-05. Bybit такие строки не принимает, то есть на дешёвых монетах
    ордер отвергался бы вместе со стопом — и это касалось не только входа,
    но и досылки стопа монитором. qty сериализуется этой же схемой давно.
    """
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


class BybitClient:
    def __init__(self, api_key: str = "", secret: str = "",
                 extra_proxies: List[str] = None):
        self.api_key = api_key
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._instrument_cache: Dict[str, Dict] = {}

        # Build proxy list: env vars → extra_proxies from Webshare → direct
        seen: set = set()
        self._proxy_list: List[Optional[str]] = []
        candidates = []
        for p in (os.getenv("BYBIT_PROXY", "").strip(),
                  os.getenv("BYBIT_PROXY_2", "").strip()):
            if p:
                candidates.append(p)
        if extra_proxies:
            candidates.extend(extra_proxies)
        for p in candidates:
            if p and p not in seen:
                seen.add(p)
                self._proxy_list.append(p)
        self._proxy_list.append(None)  # direct connection always last

        # Index of currently active proxy in _proxy_list
        self._proxy_idx: int = 0
        # Timestamp when we last switched away from a proxy (for retry cooldown)
        self._proxy_failed_at: float = 0.0

        n = len(self._proxy_list) - 1  # exclude the None (direct)
        if n > 0:
            log.info(f"BybitClient: {n} proxy(ies) configured + direct fallback")
        else:
            log.info("BybitClient: no proxy — direct connection")

    @property
    def _proxy(self) -> Optional[str]:
        """Currently active proxy (None = direct)."""
        # Re-try dead proxy after 5-minute cooldown
        if (self._proxy_idx > 0 and self._proxy_failed_at and
                time.time() - self._proxy_failed_at > 300):
            log.info("BybitClient: retrying proxy after 5-min cooldown")
            self._proxy_idx = 0
            self._proxy_failed_at = 0.0
        return self._proxy_list[self._proxy_idx] if self._proxy_idx < len(self._proxy_list) else None

    def _advance_proxy(self) -> bool:
        """Move to the next proxy in the list. Returns True if there is a next one."""
        if self._proxy_idx + 1 < len(self._proxy_list):
            old = self._proxy_list[self._proxy_idx]
            self._proxy_idx += 1
            self._proxy_failed_at = time.time()
            new = self._proxy_list[self._proxy_idx]
            log.warning(f"BybitClient: proxy {old} failed → switching to "
                        f"{'direct' if new is None else new}")
            return True
        return False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)",
            })
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, payload: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        raw = f"{ts}{self.api_key}{RECV_WINDOW}{payload}"
        sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY":     self.api_key,
            "X-BAPI-SIGN":        sig,
            "X-BAPI-SIGN-TYPE":   "2",
            "X-BAPI-TIMESTAMP":   ts,
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
            "Content-Type":       "application/json",
        }

    async def _raw_request(self, method: str, url: str,
                           sign_fn: Callable[[], Dict],
                           data: Optional[str] = None) -> Tuple[int, str]:
        """HTTP-запрос с перебором прокси.

        sign_fn вызывается ЗАНОВО на каждой попытке, а не подписывает один
        раз: RECV_WINDOW = 5 секунд, и медленный ответ (прокси отдал 502
        через 6 с) означал, что ретрай через следующий прокси уходит с
        протухшим X-BAPI-TIMESTAMP и получает retCode=10002. Для
        set_trading_stop это выглядело как «биржа отвергла стоп».
        Тело запроса при этом НЕ пересобирается, поэтому orderLinkId
        стабилен и идемпотентность ордера сохраняется.

        Failover раньше срабатывал ТОЛЬКО на исключении транспорта. Ответ
        с HTTP 403 (гео-блок Bybit на IP прокси) считался успехом: тело —
        не-JSON, `_get` возвращал {}, `get_positions()` → None, монитор
        выходил со «skipping close check» каждые 30 секунд, а рабочее
        прямое соединение в списке так и не пробовалось.

        Прикладные ошибки Bybit приходят с HTTP 200 и полем retCode,
        поэтому 4xx/5xx — почти всегда инфраструктура между нами и биржей.
        """
        session = await self._get_session()
        tried: List[Optional[str]] = []
        last: Optional[Tuple[int, str]] = None
        while True:
            proxy = self._proxy
            if proxy in tried:
                # Круг замкнулся: список прокси исчерпан. Возвращаем последний
                # реальный ответ, если он был, иначе — честная ошибка (раньше
                # здесь был `break`, функция отдавала None, и вызывающий падал
                # на распаковке кортежа с бессмысленным TypeError в логе).
                if last is not None:
                    return last
                raise RuntimeError(f"{method} {url}: все прокси исчерпаны")
            tried.append(proxy)
            try:
                kw: Dict = {"headers": sign_fn(),
                            "timeout": aiohttp.ClientTimeout(total=10, connect=3)}
                if data is not None:
                    kw["data"] = data
                if proxy:
                    kw["proxy"] = proxy
                async with session.request(method, url, **kw) as r:
                    body = await r.text()
                    if r.status >= 400:
                        last = (r.status, body)
                        log.warning(
                            f"{method} {url.split('?')[0]} HTTP {r.status} через "
                            f"{'direct' if proxy is None else proxy}"
                        )
                        if self._advance_proxy():
                            continue
                        return last
                    return r.status, body
            except Exception:
                if proxy is not None and self._advance_proxy():
                    continue
                raise

    async def _raw_get(self, url: str, sign_fn: Callable[[], Dict]) -> Tuple[int, str]:
        return await self._raw_request("GET", url, sign_fn)

    async def _raw_post(self, url: str, sign_fn: Callable[[], Dict], data: str) -> Tuple[int, str]:
        return await self._raw_request("POST", url, sign_fn, data)

    async def _get(self, path: str, params: Dict = None, auth: bool = False) -> Dict:
        params = params or {}
        query = urlencode(sorted(params.items()))
        url = BASE_URL + path + (f"?{query}" if query else "")
        for attempt in range(3):
            try:
                sign_fn = (lambda: self._sign(query)) if auth else (lambda: {})
                status, text = await self._raw_get(url, sign_fn)
                try:
                    data = json.loads(text)
                except Exception:
                    log.error(f"GET {path} non-JSON HTTP {status}: {text[:300]}")
                    if attempt == 2:
                        return {}
                    await asyncio.sleep(1)
                    continue
                if data.get("retCode", 0) != 0:
                    log.warning(f"GET {path} -> {data.get('retCode')}: {data.get('retMsg')}")
                return data
            except Exception as e:
                if attempt == 2:
                    log.error(f"GET {path} failed after retries: {e}")
                    return {}
                await asyncio.sleep(2 ** attempt)
        return {}

    async def _post(self, path: str, body: dict = None) -> Dict:
        raw = json.dumps(body or {})
        url = BASE_URL + path
        for attempt in range(3):
            try:
                status, text = await self._raw_post(url, lambda: self._sign(raw), raw)
                try:
                    data = json.loads(text)
                except Exception:
                    log.error(f"POST {path} non-JSON HTTP {status}: {text[:300]}")
                    if attempt == 2:
                        return {}
                    await asyncio.sleep(1)
                    continue
                if data.get("retCode", 0) != 0:
                    log.warning(f"POST {path} -> {data.get('retCode')}: {data.get('retMsg')}")
                return data
            except Exception as e:
                if attempt == 2:
                    log.error(f"POST {path} failed after retries: {e}")
                    return {}
                await asyncio.sleep(2 ** attempt)
        return {}

    # ── Public market data ────────────────────────────────────────────────────

    async def get_tickers(self) -> List[Dict]:
        data = await self._get("/v5/market/tickers", {"category": "linear"})
        result = data.get("result", {}).get("list", [])
        if not result:
            log.warning(f"get_tickers empty: retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
        return result

    async def get_klines(self, symbol: str, interval: str = "240", limit: int = 25) -> List[Dict]:
        data = await self._get("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": interval, "limit": limit,
        })
        raw = data.get("result", {}).get("list", [])
        return [
            {"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
            for r in reversed(raw)
        ]

    async def get_open_interest(self, symbol: str, interval: str = "4h", limit: int = 12) -> List[Dict]:
        data = await self._get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol,
            "intervalTime": interval, "limit": limit,
        })
        raw = data.get("result", {}).get("list", [])
        return [{"ts": int(r["timestamp"]), "oi": float(r["openInterest"])} for r in reversed(raw)]

    async def get_closed_pnl(self, symbol: str, limit: int = 1) -> List[Dict]:
        """Get recently closed P&L for a symbol (authenticated)."""
        data = await self._get("/v5/position/closed-pnl", {
            "category": "linear", "symbol": symbol, "limit": str(limit),
        }, auth=True)
        return data.get("result", {}).get("list", [])

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        data = await self._get("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": limit,
        })
        result = data.get("result", {})
        bids = [[float(p), float(q)] for p, q in result.get("b", [])]
        asks = [[float(p), float(q)] for p, q in result.get("a", [])]
        return {"bids": bids, "asks": asks}

    async def get_instrument_info(self, symbol: str) -> Dict:
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        data = await self._get("/v5/market/instruments-info", {
            "category": "linear", "symbol": symbol,
        })
        items = data.get("result", {}).get("list", [])
        info = items[0] if items else {}
        if info:
            self._instrument_cache[symbol] = info
        return info

    # ── Authenticated trading ─────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return available USDT balance (tries UNIFIED then CONTRACT account).
        Records the reason for a zero result in state.last_balance_error so the
        API/dashboard can show it, not just the server log."""
        from core.state import state
        errors: list = []
        key_worked = False
        for acc_type in ("UNIFIED", "CONTRACT"):
            data = await self._get("/v5/account/wallet-balance",
                                   {"accountType": acc_type}, auth=True)
            ret_code = data.get("retCode", -1)
            ret_msg  = data.get("retMsg", "no response")
            if ret_code != 0:
                errors.append(f"{acc_type}: retCode={ret_code} {ret_msg}")
                log.warning(f"get_balance {acc_type}: retCode={ret_code} msg={ret_msg}")
                continue
            key_worked = True
            try:
                for acc in data.get("result", {}).get("list", []):
                    for coin in acc.get("coin", []):
                        if coin.get("coin") == "USDT":
                            available = float(coin.get("availableBalance") or 0)
                            if available == 0:
                                available = float(coin.get("availableToWithdraw") or 0)
                            if available == 0:
                                available = float(coin.get("walletBalance") or 0)
                            log.info(f"get_balance {acc_type}: USDT available={available}")
                            if available > 0:
                                state.last_balance_error = ""
                                return available
            except Exception as e:
                errors.append(f"{acc_type}: parse error {e}")
                log.warning(f"get_balance {acc_type}: parse error — {e}")
        if key_worked:
            # At least one account type answered with retCode=0 — the key is
            # fine, the account simply holds no USDT. Errors from the other
            # account type (e.g. "CONTRACT not supported" on UTA accounts)
            # are noise for this diagnosis.
            state.last_balance_error = (
                "API key OK, but USDT balance is 0 on the Unified Trading account — "
                "transfer funds (Funding → Unified Trading)"
            )
        else:
            state.last_balance_error = "; ".join(errors) if errors else "no response from Bybit"
        log.warning(f"get_balance: 0 — {state.last_balance_error}")
        return 0.0

    async def set_trading_stop(self, symbol: str, sl: float = 0.0, tp: float = 0.0) -> bool:
        """(Re)attach SL/TP to an existing position. retCode 34040 = 'not
        modified' (already set to these values) — счётся успехом."""
        if sl <= 0 and tp <= 0:
            log.error(f"{symbol}: set_trading_stop вызван без SL и TP — нечего ставить")
            return False
        body: Dict = {"category": "linear", "symbol": symbol, "positionIdx": 0}
        if sl > 0:
            body["stopLoss"] = _fmt_price(sl)
            body["slTriggerBy"] = "MarkPrice"
        if tp > 0:
            body["takeProfit"] = _fmt_price(tp)
            body["tpTriggerBy"] = "LastPrice"
        data = await self._post("/v5/position/trading-stop", body)
        return data.get("retCode", -1) in (0, 34040)

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        data = await self._post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        })
        return data.get("retCode", -1) in (0, 110043)

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float, tp: float) -> Dict:
        """Market order with stop-loss and take-profit.

        orderLinkId: _post retries on network exceptions (including a response
        timeout AFTER Bybit already accepted the order). Without an idempotency
        key each retry would be a brand-new market order — doubling or tripling
        the position versus the risk-sized qty. A stable client order id makes
        Bybit reject the duplicate instead."""
        return await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side,
            "orderType":   "Market",
            "qty":         f"{qty:.8f}".rstrip('0').rstrip('.'),
            "timeInForce": "IOC",
            "stopLoss":    _fmt_price(sl),
            "takeProfit":  _fmt_price(tp),
            "slTriggerBy": "MarkPrice",
            "tpTriggerBy": "LastPrice",
            "positionIdx": 0,
            "orderLinkId": f"gb-{uuid.uuid4().hex[:24]}",
        })

    async def get_positions(self) -> Optional[List[Dict]]:
        """All open linear USDT perp positions. Returns None on API failure."""
        positions = []
        cursor = ""
        while True:
            params: Dict = {"category": "linear", "settleCoin": "USDT", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/v5/position/list", params, auth=True)
            if not data or data.get("retCode", -1) != 0:
                return None
            result = data.get("result", {})
            positions.extend(result.get("list", []))
            nxt = result.get("nextPageCursor", "")
            # Без этой защиты неизменный курсор от биржи давал бесконечный
            # цикл: monitor_positions висел с _MONITORING=True, и НЕПРЕРЫВНАЯ
            # проверка стопов — единственная страховка для всех веток
            # «не удалось подтвердить» — прекращалась навсегда.
            if not nxt or nxt == cursor or len(positions) > 2000:
                break
            cursor = nxt
        return [p for p in positions if float(p.get("size", 0)) > 0]

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Живая позиция по символу.

        None  — API недоступен, состояние НЕизвестно (трогать позицию нельзя);
        {}    — API ответил, позиции нет (size == 0);
        dict  — позиция есть.

        Отдельный метод, потому что связка get_positions()+next(...) нужна в
        четырёх местах, и в каждом важно не спутать «нет позиции» с «не смог
        проверить»: первое разрешает закрыть учёт, второе — запрещает.
        """
        live = await self.get_positions()
        if live is None:
            return None
        return next((p for p in live if p.get("symbol") == symbol), {})

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        close_side = "Sell" if side == "Buy" else "Buy"
        return await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         f"{qty:.8f}".rstrip('0').rstrip('.'),
            "timeInForce": "IOC",
            "reduceOnly":  True,
            "positionIdx": 0,
            "orderLinkId": f"gb-close-{uuid.uuid4().hex[:18]}",
        })
