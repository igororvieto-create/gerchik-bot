import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("bingx")
BASE     = "https://open-api.bingx.com"
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_RETRIES = 3

# BingX error codes that mean "slow down"
_RATE_LIMIT_CODES = {80001, -1003, 429}
# BingX error codes that are permanent (don't retry)
_FATAL_CODES = {100001, 100413, 100414, 100421, 100500}


class BingXAPIError(Exception):
    def __init__(self, code: int, msg: str, path: str = ""):
        self.code = code
        self.msg  = msg
        super().__init__(f"BingX [{code}] {path}: {msg}")


class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret  = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    def _sign(self, params: dict) -> str:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query + "&signature=" + sig

    def _check(self, data: dict, path: str) -> dict:
        """Validate API response. Raises BingXAPIError on non-zero code."""
        if not isinstance(data, dict):
            raise BingXAPIError(-1, f"Unexpected response type: {type(data)}", path)
        code = data.get("code", 0)
        if code != 0:
            msg = data.get("msg", str(data))
            raise BingXAPIError(code, msg, path)
        return data

    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        params = dict(params or {})
        if signed:
            url = f"{BASE}{path}?{self._sign(params)}"
        else:
            url = f"{BASE}{path}" + ("?" + urlencode(params) if params else "")
        headers = {"X-BX-APIKEY": self.api_key}
        delay = 2
        last_exc = None
        for attempt in range(_RETRIES):
            try:
                sess = await self._sess()
                async with sess.get(url, headers=headers) as r:
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        text = await r.text()
                        raise BingXAPIError(-1, f"Non-JSON response: {text[:200]}", path)
                    code = data.get("code", 0) if isinstance(data, dict) else -1
                    if code in _RATE_LIMIT_CODES:
                        wait = delay * 2
                        log.warning(f"Rate limit on GET {path}, wait {wait}s")
                        await asyncio.sleep(wait)
                        delay *= 2
                        continue
                    if code in _FATAL_CODES:
                        msg = data.get("msg", "") if isinstance(data, dict) else str(data)
                        raise BingXAPIError(code, msg, path)
                    if code != 0:
                        log.error(f"GET {path} code={code}: {data.get('msg', data)}")
                    return data
            except BingXAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt == _RETRIES - 1:
                    raise
                log.warning(f"GET {path} attempt {attempt+1} failed: {e}, retry in {delay}s")
                await asyncio.sleep(delay)
                delay *= 2
        raise last_exc

    async def _post(self, path: str, params: dict) -> dict:
        qs = self._sign(dict(params))
        headers = {
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        delay = 2
        last_exc = None
        for attempt in range(_RETRIES):
            try:
                sess = await self._sess()
                async with sess.post(f"{BASE}{path}", data=qs, headers=headers) as r:
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        text = await r.text()
                        raise BingXAPIError(-1, f"Non-JSON response: {text[:200]}", path)
                    code = data.get("code", 0) if isinstance(data, dict) else -1
                    if code in _RATE_LIMIT_CODES:
                        wait = delay * 2
                        log.warning(f"Rate limit on POST {path}, wait {wait}s")
                        await asyncio.sleep(wait)
                        delay *= 2
                        continue
                    if code in _FATAL_CODES:
                        msg = data.get("msg", "") if isinstance(data, dict) else str(data)
                        raise BingXAPIError(code, msg, path)
                    if code != 0:
                        log.error(f"POST {path} code={code}: {data.get('msg', data)}")
                    return data
            except BingXAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt == _RETRIES - 1:
                    raise
                log.warning(f"POST {path} attempt {attempt+1} failed: {e}, retry in {delay}s")
                await asyncio.sleep(delay)
                delay *= 2
        raise last_exc

    # ------------------------------------------------------------------ market data

    async def get_top_symbols(self, n: int = 0) -> list:
        try:
            data    = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": ""})
            tickers = data.get("data") or []
            if isinstance(tickers, dict):
                tickers = [tickers]
            if not isinstance(tickers, list):
                log.error(f"get_top_symbols: unexpected data type {type(tickers)}")
                return []
            sorted_t = sorted(
                tickers,
                key=lambda x: float(x.get("quoteVolume") or 0),
                reverse=True,
            )
            symbols = [t["symbol"] for t in sorted_t if "USDT" in t.get("symbol", "")]
            return symbols[:n] if n > 0 else symbols
        except Exception as e:
            log.error(f"get_top_symbols: {e}")
            return []

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        try:
            data = await self._get(
                "/openApi/swap/v3/quote/klines",
                {"symbol": symbol, "interval": interval, "limit": limit},
            )
            result = data.get("data")
            if result is None:
                log.warning(f"get_klines {symbol}/{interval}: data=null in response")
                return []
            if not isinstance(result, list):
                log.warning(f"get_klines {symbol}/{interval}: unexpected type {type(result)}")
                return []
            return result
        except Exception as e:
            log.error(f"get_klines {symbol}/{interval}: {e}")
            return []

    async def get_funding_rate(self, symbol: str) -> float:
        try:
            data = await self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
            d    = data.get("data")
            if not isinstance(d, dict):
                log.warning(f"get_funding_rate {symbol}: unexpected data {type(d)}")
                return 0.0
            rate = d.get("lastFundingRate")
            if rate is None:
                log.warning(f"get_funding_rate {symbol}: lastFundingRate missing")
                return 0.0
            return float(rate) * 100
        except Exception as e:
            log.error(f"get_funding_rate {symbol}: {e}")
            return 0.0

    async def get_ticker(self, symbol: str) -> dict:
        try:
            data    = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
            tickers = data.get("data")
            if isinstance(tickers, dict):
                return tickers
            if isinstance(tickers, list):
                for t in tickers:
                    if t.get("symbol") == symbol:
                        return t
            log.warning(f"get_ticker {symbol}: symbol not found in response")
            return {}
        except Exception as e:
            log.error(f"get_ticker {symbol}: {e}")
            return {}

    # ------------------------------------------------------------------ account

    def _extract_balance(self, data: dict) -> tuple[float, float]:
        """
        Parse BingX balance response (3 known formats).
        Returns (equity, available_margin).
        """
        d = data.get("data") or {}
        if not isinstance(d, dict):
            return 0.0, 0.0

        def _pick(obj: dict, equity_fields, margin_fields):
            equity = 0.0
            margin = 0.0
            for f in equity_fields:
                v = obj.get(f)
                if v is not None:
                    try:
                        equity = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            for f in margin_fields:
                v = obj.get(f)
                if v is not None:
                    try:
                        margin = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            return equity, margin

        eq_fields  = ("equity", "balance", "availableMargin", "available")
        mar_fields = ("availableMargin", "available", "equity", "balance")

        # Format 1: data.balance is a dict
        bal = d.get("balance")
        if isinstance(bal, dict):
            return _pick(bal, eq_fields, mar_fields)

        # Format 2: data.balance is a list of assets
        if isinstance(bal, list):
            for a in bal:
                if a.get("asset") in ("USDT", "usdt"):
                    return _pick(a, eq_fields, mar_fields)

        # Format 3: data itself is the balance dict
        return _pick(d, eq_fields, mar_fields)

    async def get_balance(self) -> float:
        try:
            data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
            equity, _ = self._extract_balance(data)
            if equity <= 0:
                log.warning(f"get_balance: parsed equity={equity} | raw={data}")
            return equity
        except Exception as e:
            log.error(f"get_balance: {e}")
            return 0.0

    async def get_balance_raw(self) -> dict:
        return await self._get("/openApi/swap/v2/user/balance", {}, signed=True)

    async def get_available_margin(self) -> float:
        try:
            data = await self._get("/openApi/swap/v2/user/balance", {}, signed=True)
            _, margin = self._extract_balance(data)
            return margin
        except Exception as e:
            log.error(f"get_available_margin: {e}")
            return 0.0

    async def get_open_positions(self) -> list:
        try:
            data      = await self._get("/openApi/swap/v2/user/positions", {}, signed=True)
            positions = data.get("data")
            if positions is None:
                return []
            if not isinstance(positions, list):
                log.warning(f"get_open_positions: unexpected type {type(positions)}")
                return []
            result = []
            for p in positions:
                try:
                    if float(p.get("positionAmt") or 0) != 0:
                        result.append(p)
                except (ValueError, TypeError):
                    continue
            return result
        except Exception as e:
            log.error(f"get_open_positions: {e}")
            return []

    # ------------------------------------------------------------------ trading

    async def place_order(self, symbol, side, qty, price=0,
                          order_type="MARKET", position_side="LONG",
                          stop_price=0, reduce_only=False) -> dict:
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

    async def place_stop_loss(self, symbol, side, qty, stop_price) -> dict:
        ps = "LONG" if side == "BUY" else "SHORT"
        cs = "SELL" if ps == "LONG" else "BUY"
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "STOP_MARKET", "quantity": qty,
            "stopPrice": stop_price,
        })

    async def place_take_profit(self, symbol, side, qty, tp_price) -> dict:
        ps = "LONG" if side == "BUY" else "SHORT"
        cs = "SELL" if ps == "LONG" else "BUY"
        return await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "TAKE_PROFIT_MARKET", "quantity": qty,
            "stopPrice": tp_price,
        })

    async def cancel_order(self, symbol, order_id) -> dict:
        return await self._post(
            "/openApi/swap/v2/trade/cancelOrder",
            {"symbol": symbol, "orderId": order_id},
        )

    async def close_position(self, symbol, qty, side) -> dict:
        cs = "SELL" if side == "LONG" else "BUY"
        ps = side if side in ("LONG", "SHORT") else "LONG"
        result = await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": cs, "positionSide": ps,
            "type": "MARKET", "quantity": qty,
        })
        if result.get("code") != 0:
            raise RuntimeError(f"close_position failed: {result}")
        return result

    async def set_leverage(self, symbol, leverage) -> None:
        for side in ("LONG", "SHORT"):
            try:
                await self._post(
                    "/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "side": side, "leverage": leverage},
                )
            except Exception as e:
                log.warning(f"set_leverage {symbol} {side}: {e}")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
