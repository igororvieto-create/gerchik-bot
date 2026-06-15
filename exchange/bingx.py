import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

import os as _os

log = logging.getLogger("bingx")
BASE = _os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_RETRIES = 3


class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret  = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self):
        if self._session and not self._session.closed:
            return self._session
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    def _sign(self, params):
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query + "&signature=" + sig

    async def _get(self, path, params=None, signed=False):
        params = params or {}
        if not signed:
            url = f"{BASE}{path}" + ("?" + urlencode(params) if params else "")
        headers = {"X-BX-APIKEY": self.api_key}
        delay = 2
        for attempt in range(_RETRIES):
            if signed:
                url = f"{BASE}{path}?{self._sign(params)}"  # refresh timestamp on every attempt
            try:
                sess = await self._sess()
                async with sess.get(url, headers=headers) as r:
                    if r.status == 429:
                        if attempt == _RETRIES - 1:
                            raise aiohttp.ClientError(f"Rate limited: GET {path}")
                        log.warning(f"GET {path} rate limited, retry in {delay}s")
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
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
        headers = {
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        delay = 2
        for attempt in range(_RETRIES):
            qs = self._sign(params)  # refresh timestamp on every attempt
            try:
                sess = await self._sess()
                async with sess.post(f"{BASE}{path}", data=qs, headers=headers) as r:
                    if r.status == 429:
                        if attempt == _RETRIES - 1:
                            raise aiohttp.ClientError(f"Rate limited: POST {path}")
                        log.warning(f"POST {path} rate limited, retry in {delay}s")
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
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

        MIN_VOLUME_USDT = 5_000_000  # skip illiquid coins (< $5M/day)
        scored = []
        for t in tickers:
            sym = t.get("symbol", "")
            if "USDT" not in sym:
                continue
            try:
                volume = float(t.get("quoteVolume", 0))
                if volume < MIN_VOLUME_USDT:
                    continue
                change = abs(float(t.get("priceChangePercent", 0)))
                # Momentum score: high volume + moving (0.5–12% in 24h)
                # Exclude flat (<0.5%) and overextended (>15%) coins
                if change < 0.5 or change > 15.0:
                    momentum = 0.5  # deprioritize, don't exclude completely
                else:
                    momentum = 1.0 + change / 10.0  # 1.05 – 2.2×
                scored.append((sym, volume * momentum))
            except Exception:
                continue

        scored.sort(key=lambda x: x[1], reverse=True)
        symbols = [s for s, _ in scored]
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

    @staticmethod
    def _parse_balance_data(d: dict) -> tuple:
        """Parse BingX /user/balance data dict → (balance, available_margin).

        Handles all 3 API response shapes:
          Format 1 — d["balance"] is a dict of fields
          Format 2 — d["balance"] is a list of asset dicts
          Format 3 — d itself is the flat balance dict
        """
        balance = 0.0
        avail = 0.0
        avail_found = False

        bal = d.get("balance") if isinstance(d, dict) else None

        if isinstance(bal, dict):
            for field in ("equity", "balance", "availableMargin", "available"):
                if field in bal and float(bal[field]) > 0 and balance == 0.0:
                    balance = float(bal[field])
            for field in ("availableMargin", "available"):
                if field in bal:
                    avail = float(bal[field])
                    avail_found = True
                    break
        elif isinstance(bal, list):
            for a in bal:
                if a.get("asset") in ("USDT", "usdt"):
                    for field in ("equity", "balance", "availableMargin", "available"):
                        if field in a and float(a[field]) > 0 and balance == 0.0:
                            balance = float(a[field])
                    for field in ("availableMargin", "available"):
                        if field in a:
                            avail = float(a[field])
                            avail_found = True
                            break

        # Format 3 fallback
        if isinstance(d, dict):
            if balance == 0.0:
                for field in ("equity", "balance", "availableMargin", "available"):
                    if field in d and float(d[field]) > 0:
                        balance = float(d[field])
                        break
            if not avail_found:
                for field in ("availableMargin", "available"):
                    if field in d:
                        avail = float(d[field])
                        break

        return balance, avail

    async def get_balance(self):
        data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
        try:
            balance, _ = self._parse_balance_data(data.get("data", {}))
            return balance
        except Exception as e:
            log.error(f"get_balance parse error: {e} | response: {data}")
        return 0.0

    async def get_balance_raw(self):
        """Returns the raw API response for debugging."""
        return await self._get("/openApi/swap/v2/user/balance", {}, signed=True)

    async def get_balance_and_margin(self) -> tuple:
        """Returns (equity/balance, available_margin) in a single API call."""
        data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
        try:
            return self._parse_balance_data(data.get("data", {}))
        except Exception as e:
            log.error(f"get_balance_and_margin parse error: {e} | response: {data}")
        return 0.0, 0.0

    async def get_available_margin(self):
        """Returns available (free) margin for new positions."""
        data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
        try:
            _, avail = self._parse_balance_data(data.get("data", {}))
            return avail
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

    async def set_leverage(self, symbol, leverage) -> bool:
        """Returns True if leverage was set successfully on both sides."""
        ok = True
        for side in ("LONG", "SHORT"):
            r = await self._post("/openApi/swap/v2/trade/leverage",
                                 {"symbol": symbol, "side": side, "leverage": leverage})
            if r.get("code") != 0:
                # Code 80012 = leverage already set to this value — not an error
                if r.get("code") != 80012:
                    log.warning(f"set_leverage {symbol} {side} x{leverage}: {r.get('msg','')}")
                    ok = False
        return ok

    async def set_margin_type(self, symbol):
        """Set isolated margin mode for both sides."""
        for side in ("LONG", "SHORT"):
            try:
                await self._post("/openApi/swap/v2/trade/marginType",
                                 {"symbol": symbol, "side": side, "marginType": "ISOLATED"})
            except Exception as e:
                log.debug(f"set_margin_type {symbol} {side}: {e}")

    async def get_orderbook(self, symbol: str, limit: int = 100) -> dict:
        """Fetch order book snapshot. Public endpoint, no auth required."""
        allowed = {5, 10, 20, 50, 100, 500, 1000}
        if limit not in allowed:
            limit = min((l for l in sorted(allowed) if l >= limit), default=100)
        return await self._get("/openApi/swap/v2/quote/depth",
                               {"symbol": symbol, "limit": limit})

    async def get_open_orders(self, symbol: str) -> list:
        """Returns list of open orders for a symbol (pending SL/TP on exchange)."""
        data = await self._get("/openApi/swap/v2/trade/openOrders",
                               {"symbol": symbol}, signed=True)
        try:
            orders = data.get("data", {})
            if isinstance(orders, dict):
                orders = orders.get("orders", [])
            return orders if isinstance(orders, list) else []
        except Exception as e:
            log.error(f"get_open_orders {symbol}: {e}")
            return []

    async def close(self):
        if self._session:
            await self._session.close()
