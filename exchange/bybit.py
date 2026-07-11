import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger("bybit")

BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
RECV_WINDOW = 5000


class BybitClient:
    def __init__(self, api_key: str = "", secret: str = ""):
        self.api_key = api_key
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

        # Primary proxy from env; secondary from BYBIT_PROXY_2
        proxy1 = os.getenv("BYBIT_PROXY", "").strip() or None
        proxy2 = os.getenv("BYBIT_PROXY_2", "").strip() or None

        # Build ordered list: configured proxies first, direct last
        self._proxy_list: List[Optional[str]] = []
        for p in (proxy1, proxy2):
            if p and p not in self._proxy_list:
                self._proxy_list.append(p)
        self._proxy_list.append(None)  # direct connection always last

        # Index of currently active proxy in _proxy_list
        self._proxy_idx: int = 0
        # Timestamp when we last switched away from a proxy (for retry cooldown)
        self._proxy_failed_at: float = 0.0

        if proxy1:
            log.info(f"BybitClient: primary proxy={proxy1}" +
                     (f" fallback={proxy2}" if proxy2 else "") +
                     " | direct connection also available")
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

    async def _raw_get(self, url: str, headers: Dict) -> Tuple[int, str]:
        """GET with automatic proxy fallback."""
        session = await self._get_session()
        tried: List[Optional[str]] = []
        while True:
            proxy = self._proxy
            if proxy in tried:
                break
            tried.append(proxy)
            try:
                kw: Dict = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=10)}
                if proxy:
                    kw["proxy"] = proxy
                async with session.get(url, **kw) as r:
                    return r.status, await r.text()
            except Exception as e:
                if proxy is not None and self._advance_proxy():
                    continue
                raise

    async def _raw_post(self, url: str, headers: Dict, data: str) -> Tuple[int, str]:
        """POST with automatic proxy fallback."""
        session = await self._get_session()
        tried: List[Optional[str]] = []
        while True:
            proxy = self._proxy
            if proxy in tried:
                break
            tried.append(proxy)
            try:
                kw: Dict = {"headers": headers, "data": data,
                            "timeout": aiohttp.ClientTimeout(total=10)}
                if proxy:
                    kw["proxy"] = proxy
                async with session.post(url, **kw) as r:
                    return r.status, await r.text()
            except Exception as e:
                if proxy is not None and self._advance_proxy():
                    continue
                raise

    async def _get(self, path: str, params: Dict = None, auth: bool = False) -> Dict:
        params = params or {}
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = BASE_URL + path + (f"?{query}" if query else "")
        for attempt in range(3):
            try:
                headers = self._sign(query) if auth else {}
                status, text = await self._raw_get(url, headers)
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
                await asyncio.sleep(1)
        return {}

    async def _post(self, path: str, body: dict = None) -> Dict:
        raw = json.dumps(body or {})
        url = BASE_URL + path
        for attempt in range(3):
            try:
                headers = self._sign(raw)
                status, text = await self._raw_post(url, headers, raw)
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
                await asyncio.sleep(1)
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

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        data = await self._get("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": limit,
        })
        result = data.get("result", {})
        bids = [[float(p), float(q)] for p, q in result.get("b", [])]
        asks = [[float(p), float(q)] for p, q in result.get("a", [])]
        return {"bids": bids, "asks": asks}

    async def get_instrument_info(self, symbol: str) -> Dict:
        data = await self._get("/v5/market/instruments-info", {
            "category": "linear", "symbol": symbol,
        })
        items = data.get("result", {}).get("list", [])
        return items[0] if items else {}

    # ── Authenticated trading ─────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return available USDT balance (tries UNIFIED then CONTRACT account)."""
        for acc_type in ("UNIFIED", "CONTRACT"):
            data = await self._get("/v5/account/wallet-balance",
                                   {"accountType": acc_type}, auth=True)
            ret_code = data.get("retCode", -1)
            ret_msg  = data.get("retMsg", "no response")
            if ret_code != 0:
                log.warning(f"get_balance {acc_type}: retCode={ret_code} msg={ret_msg}")
                continue
            try:
                for acc in data.get("result", {}).get("list", []):
                    for coin in acc.get("coin", []):
                        if coin.get("coin") == "USDT":
                            available = float(coin.get("availableToWithdraw") or 0)
                            if available == 0:
                                available = float(coin.get("availableBalance") or 0)
                            if available == 0:
                                available = float(coin.get("walletBalance") or 0)
                            log.info(f"get_balance {acc_type}: USDT available={available}")
                            if available > 0:
                                return available
            except Exception as e:
                log.warning(f"get_balance {acc_type}: parse error — {e}")
        log.warning("get_balance: 0 — check API key permissions and IP whitelist")
        return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        data = await self._post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        })
        return data.get("retCode", -1) in (0, 110043)

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float, tp: float) -> Dict:
        """Market order with stop-loss and take-profit."""
        return await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side,
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "IOC",
            "stopLoss":    str(round(sl, 8)),
            "takeProfit":  str(round(tp, 8)),
            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice",
            "positionIdx": 0,
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
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break
        return [p for p in positions if float(p.get("size", 0)) > 0]

    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        close_side = "Sell" if side == "Buy" else "Buy"
        return await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "IOC",
            "reduceOnly":  True,
            "positionIdx": 0,
        })
