import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional

import aiohttp

log = logging.getLogger("bybit")

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = 5000


class BybitClient:
    def __init__(self, api_key: str = "", secret: str = ""):
        self.api_key = api_key
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
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

    async def _get(self, path: str, params: Dict = None, auth: bool = False) -> Dict:
        session = await self._get_session()
        params = params or {}
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = self._sign(query) if auth else {}
        url = BASE_URL + path + (f"?{query}" if query else "")
        for attempt in range(3):
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    if data.get("retCode", 0) != 0:
                        log.warning(f"GET {path} -> {data.get('retCode')}: {data.get('retMsg')}")
                    return data
            except Exception as e:
                if attempt == 2:
                    log.error(f"GET {path} failed: {e}")
                    return {}
                await __import__("asyncio").sleep(1)
        return {}

    async def _post(self, path: str, body: dict = None) -> Dict:
        session = await self._get_session()
        raw = json.dumps(body or {})
        headers = self._sign(raw)
        url = BASE_URL + path
        for attempt in range(3):
            try:
                async with session.post(url, headers=headers, data=raw,
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    if data.get("retCode", 0) != 0:
                        log.warning(f"POST {path} -> {data.get('retCode')}: {data.get('retMsg')}")
                    return data
            except Exception as e:
                if attempt == 2:
                    log.error(f"POST {path} failed: {e}")
                    return {}
                await __import__("asyncio").sleep(1)
        return {}

    # ── Public market data ────────────────────────────────────────────────────

    async def get_tickers(self) -> List[Dict]:
        data = await self._get("/v5/market/tickers", {"category": "linear"})
        return data.get("result", {}).get("list", [])

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
        return [{"ts": int(r["timestamp"]), "oi": float(r["openInterest"])} for r in raw]

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
            try:
                for acc in data.get("result", {}).get("list", []):
                    for coin in acc.get("coin", []):
                        if coin.get("coin") == "USDT":
                            bal = float(coin.get("walletBalance", 0))
                            if bal > 0:
                                return bal
            except Exception:
                pass
        return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        data = await self._post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        })
        # 110043 = leverage unchanged (already at this value) — also OK
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
        })

    async def get_positions(self) -> List[Dict]:
        """All open linear USDT perp positions."""
        data = await self._get("/v5/position/list", {
            "category": "linear", "settleCoin": "USDT",
        }, auth=True)
        return [p for p in data.get("result", {}).get("list", [])
                if float(p.get("size", 0)) > 0]

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
        })
