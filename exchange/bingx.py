import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("bingx")
BASE = "https://open-api.bingx.com"
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_RETRIES = 3


class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret  = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    def _sign(self, params):
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query + "&signature=" + sig

    async def _get(self, path, params=None, signed=False):
        params = params or {}
        if signed:
            url = f"{BASE}{path}?{self._sign(params)}"
        else:
            url = f"{BASE}{path}" + ("?" + urlencode(params) if params else "")
        headers = {"X-BX-APIKEY": self.api_key}
        delay = 2
        for attempt in range(_RETRIES):
            try:
                sess = await self._sess()
                async with sess.get(url, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") != 0:
                        log.error(f"GET {path}: {data}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == _RETRIES - 1:
                    raise
                log.warning(f"GET {path} attempt {attempt+1} failed: {e}, retry in {delay}s")
                await asyncio.sleep(delay)
                delay *= 2

    async def _post(self, path, params):
        qs = self._sign(params)
        headers = {
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        delay = 2
        for attempt in range(_RETRIES):
            try:
                sess = await self._sess()
                async with sess.post(f"{BASE}{path}", data=qs, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") != 0:
                        log.error(f"POST {path}: {data}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == _RETRIES - 1:
                    raise
                log.warning(f"POST {path} attempt {attempt+1} failed: {e}, retry in {delay}s")
                await asyncio.sleep(delay)
                delay *= 2

    async def get_top_symbols(self, n=0):
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": ""})
        tickers = data.get("data", [])
        if isinstance(tickers, dict):
            tickers = [tickers]
        sorted_t = sorted(tickers, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        symbols = [t["symbol"] for t in sorted_t if "USDT" in t.get("symbol", "")]
        return symbols[:n] if n > 0 else symbols

    async def get_klines(self, symbol, interval, limit=200):
        data = await self._get("/openApi/swap/v3/quote/klines",
                               {"symbol": symbol, "interval": interval, "limit": limit})
        return data.get("data", [])

    async def get_funding_rate(self, symbol):
        data = await self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        try:
            return float(data["data"]["lastFundingRate"]) * 100
        except Exception:
            return 0.0

    async def get_ticker(self, symbol):
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        tickers = data.get("data", [])
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("symbol") == symbol:
                    return t
        return tickers if isinstance(tickers, dict) else {}

    async def get_balance(self):
        data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
        try:
            d = data.get("data", {})
            # Format 1: data.balance is a dict
            if isinstance(d, dict) and "balance" in d:
                bal = d["balance"]
                if isinstance(bal, dict):
                    # Prefer equity (total account value incl. unrealized PnL)
                    for field in ("equity", "balance", "availableMargin", "available"):
                        if field in bal and float(bal[field]) > 0:
                            return float(bal[field])
                # Format 2: data.balance is a list of assets
                if isinstance(bal, list):
                    for a in bal:
                        if a.get("asset") in ("USDT", "usdt"):
                            for field in ("equity", "balance", "availableMargin", "available"):
                                if field in a:
                                    return float(a[field])
            # Format 3: data itself is the balance dict
            if isinstance(d, dict):
                for field in ("equity", "balance", "availableMargin", "available"):
                    if field in d:
                        return float(d[field])
        except Exception as e:
            log.error(f"get_balance parse error: {e} | response: {data}")
        return 0.0

    async def get_balance_raw(self):
        """Returns the raw API response for debugging."""
        return await self._get("/openApi/swap/v2/user/balance", {}, signed=True)

    async def get_available_margin(self):
        """Returns available (free) margin for new positions."""
        data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
        try:
            d = data.get("data", {})
            if isinstance(d, dict) and "balance" in d:
                bal = d["balance"]
                if isinstance(bal, dict):
                    for field in ("availableMargin", "available"):
                        if field in bal:
                            return float(bal[field])
                if isinstance(bal, list):
                    for a in bal:
                        if a.get("asset") in ("USDT", "usdt"):
                            for field in ("availableMargin", "available"):
                                if field in a:
                                    return float(a[field])
            if isinstance(d, dict):
                for field in ("availableMargin", "available"):
                    if field in d:
                        return float(d[field])
        except Exception as e:
            log.error(f"get_available_margin error: {e}")
        return 0.0

    async def get_open_positions(self):
        data = await self._get("/openApi/swap/v2/user/positions", {}, signed=True)
        try:
            positions = data.get("data", [])
            if not isinstance(positions, list):
                return []
            return [p for p in positions if float(p.get("positionAmt", 0)) != 0]
        except Exception as e:
            log.error(f"get_open_positions error: {e}")
            return []

    async def place_order(self, symbol, side, qty, price=0,
                          order_type="MARKET", position_side="LONG",
                          stop_price=0, reduce_only=False):
        params = {
            "symbol": symbol, "side": side,
            "positionSide": position_side, "type": order_type, "quantity": qty,
        }
        if order_type == "LIMIT" and price:
            params["price"] = price
        if stop_price:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/openApi/swap/v2/trade/order", params)

    async def place_stop_loss(self, symbol, side, qty, stop_price):
        ps = "LONG" if side == "BUY" else "SHORT"
        cs = "SELL" if ps == "LONG" else "BUY"
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "STOP_MARKET", "quantity": qty,
            "stopPrice": stop_price,
        })

    async def place_take_profit(self, symbol, side, qty, tp_price):
        ps = "LONG" if side == "BUY" else "SHORT"
        cs = "SELL" if ps == "LONG" else "BUY"
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "TAKE_PROFIT_MARKET", "quantity": qty,
            "stopPrice": tp_price,
        })

    async def cancel_order(self, symbol, order_id):
        return await self._post("/openApi/swap/v2/trade/cancelOrder",
                                {"symbol": symbol, "orderId": order_id})

    async def close_position(self, symbol, qty, side):
        cs = "SELL" if side == "LONG" else "BUY"
        ps = side if side in ("LONG", "SHORT") else "LONG"
        # В hedge mode (positionSide=LONG/SHORT) reduceOnly не нужен
        result = await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "MARKET", "quantity": qty,
        })
        if result.get("code") != 0:
            raise RuntimeError(f"close_position failed: {result}")
        return result

    async def set_leverage(self, symbol, leverage):
        await self._post("/openApi/swap/v2/trade/leverage",
                         {"symbol": symbol, "side": "LONG",  "leverage": leverage})
        await self._post("/openApi/swap/v2/trade/leverage",
                         {"symbol": symbol, "side": "SHORT", "leverage": leverage})

    async def close(self):
        if self._session:
            await self._session.close()
