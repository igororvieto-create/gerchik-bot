"""
scalp/liquidation_feed.py

WebSocket-клиент к Binance Futures для приёма форс-ликвидаций.
Используется как сигнальный источник для liquidation hunting на BingX.

Поток: !forceOrder@arr — все символы, по одной (самой крупной) ликвидации
на символ за окно 1000мс. Фильтруем по нашему whitelist (BTC/ETH/SOL/BNB)
и эмитим события через asyncio.Queue или callback.

Использование:
    feed = LiquidationFeed(symbols={"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"})
    async for event in feed.stream():
        # event это LiquidationEvent
        ...

Или с callback:
    feed = LiquidationFeed(symbols={...}, on_event=async_callback)
    await feed.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)

# Binance USDS-margined futures WebSocket — комбинированный поток всех ликвидаций.
# С 2026-04-23 старые URL без routed path будут отключены, поэтому
# используем явный /ws/ путь (поддерживается на всех версиях).
BINANCE_FUTURES_WSS = "wss://fstream.binance.com/ws/!forceOrder@arr"

# Параметры реконнекта
INITIAL_BACKOFF_SEC = 1.0
MAX_BACKOFF_SEC     = 60.0
BACKOFF_MULTIPLIER  = 2.0

# Binance шлёт ping каждые 3 минуты, и ждёт pong в течение 10 минут.
# Библиотека websockets отвечает на ping автоматически, но мы держим
# свой watchdog на случай "тихих" обрывов.
SILENT_TIMEOUT_SEC = 240  # 4 минуты без сообщений = считаем мёртвым


@dataclass(frozen=True)
class LiquidationEvent:
    """
    Нормализованное событие ликвидации.

    side='SELL' в исходных данных Binance означает ликвидацию ЛОНГА
    (биржа продаёт позицию ликвидируемого лонгиста).
    side='BUY' означает ликвидацию ШОРТА.

    Поле `liquidated_side` приводит к более понятному виду:
        'LONG'  — был ликвидирован лонг (цена упала)
        'SHORT' — был ликвидирован шорт (цена выросла)
    """
    symbol:           str    # 'BTCUSDT'
    liquidated_side:  str    # 'LONG' or 'SHORT'
    price:            float  # avg fill price
    quantity:         float  # base asset quantity
    usd_value:        float  # price * quantity
    trade_time_ms:    int    # биржевой timestamp ордера
    received_at_ms:   int    # локальный timestamp получения

    @classmethod
    def from_force_order(cls, payload: dict, received_at_ms: int) -> "LiquidationEvent":
        """
        Парсит payload вида:
            {
              "e": "forceOrder", "E": 1568014460893,
              "o": {
                "s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC",
                "q": "0.014", "p": "9910", "ap": "9910", "X": "FILLED",
                "l": "0.014", "z": "0.014", "T": 1568014460893
              }
            }
        """
        order = payload["o"]
        side  = order["S"]
        # avg price надёжнее чем p — это фактическая цена исполнения
        price = float(order["ap"])
        qty   = float(order["q"])
        return cls(
            symbol          = order["s"],
            liquidated_side = "LONG" if side == "SELL" else "SHORT",
            price           = price,
            quantity        = qty,
            usd_value       = price * qty,
            trade_time_ms   = int(order["T"]),
            received_at_ms  = received_at_ms,
        )


EventCallback = Callable[[LiquidationEvent], Awaitable[None]]


class LiquidationFeed:
    """
    Асинхронный WebSocket-клиент с автореконнектом и фильтром по символам.

    Два режима использования:
    1. Pull (async iterator):
           async for event in feed.stream():
               ...
    2. Push (callback):
           feed = LiquidationFeed(..., on_event=my_async_handler)
           await feed.run()
    """

    def __init__(
        self,
        symbols:       Iterable[str],
        on_event:      Optional[EventCallback] = None,
        url:           str = BINANCE_FUTURES_WSS,
        queue_maxsize: int = 1000,
    ):
        # Нормализуем символы к верхнему регистру для сравнения
        self._symbols:          set[str]                      = {s.upper() for s in symbols}
        self._on_event:         Optional[EventCallback]       = on_event
        self._url:              str                           = url
        self._queue:            asyncio.Queue[LiquidationEvent] = asyncio.Queue(maxsize=queue_maxsize)
        self._running:          bool                          = False
        self._last_message_ts:  float                         = 0.0
        self._stats = {
            "received_total": 0,
            "filtered_in":    0,
            "filtered_out":   0,
            "reconnects":     0,
            "parse_errors":   0,
        }

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def stream(self) -> AsyncIterator[LiquidationEvent]:
        """
        Async generator. Запускает фоновый таск чтения и yield-ит события из очереди.
        Удобно когда хочется писать `async for event in feed.stream()`.
        """
        task = asyncio.create_task(self.run(), name="liquidation_feed_run")
        try:
            while True:
                event = await self._queue.get()
                yield event
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        """
        Главный цикл. Подключается, читает, при ошибке — экспоненциальный backoff.
        Работает бесконечно пока не вызван stop().
        """
        self._running = True
        backoff = INITIAL_BACKOFF_SEC

        while self._running:
            try:
                logger.info("Connecting to Binance liquidation stream: %s", self._url)
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info(
                        "Connected. Listening for liquidations on %d symbols",
                        len(self._symbols),
                    )
                    backoff = INITIAL_BACKOFF_SEC
                    self._last_message_ts = time.monotonic()

                    watchdog_task = asyncio.create_task(self._silent_watchdog(ws))
                    try:
                        await self._read_loop(ws)
                    finally:
                        watchdog_task.cancel()
                        try:
                            await watchdog_task
                        except asyncio.CancelledError:
                            pass

            except asyncio.CancelledError:
                logger.info("LiquidationFeed cancelled")
                raise
            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning("WebSocket error: %s. Reconnecting in %.1fs", e, backoff)
                self._stats["reconnects"] += 1
            except Exception as e:
                logger.exception("Unexpected error in liquidation feed: %s", e)
                self._stats["reconnects"] += 1

            if not self._running:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

    async def _read_loop(self, ws) -> None:
        """Читает сообщения пока соединение живо."""
        async for raw in ws:
            self._last_message_ts = time.monotonic()
            self._stats["received_total"] += 1

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self._stats["parse_errors"] += 1
                logger.warning("Bad JSON from WS: %r", raw[:200])
                continue

            # !forceOrder@arr приходит как одиночные события вида {"e":"forceOrder","o":{...}}
            # (не массив, несмотря на @arr). Проверим event type на всякий случай.
            if payload.get("e") != "forceOrder":
                continue

            order  = payload.get("o", {})
            symbol = order.get("s", "").upper()
            if symbol not in self._symbols:
                self._stats["filtered_out"] += 1
                continue

            try:
                event = LiquidationEvent.from_force_order(
                    payload, int(time.time() * 1000)
                )
            except (KeyError, ValueError, TypeError) as e:
                self._stats["parse_errors"] += 1
                logger.warning("Failed to parse forceOrder: %s, payload=%r", e, payload)
                continue

            self._stats["filtered_in"] += 1
            await self._dispatch(event)

    async def _dispatch(self, event: LiquidationEvent) -> None:
        """Отправляет событие либо в очередь, либо в callback."""
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("Error in on_event callback for %s", event.symbol)
        else:
            # Если очередь забита — сбрасываем самое старое, чтобы не блокировать чтение.
            # Скальпинг: устаревшие события бесполезны.
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await self._queue.put(event)

    async def _silent_watchdog(self, ws) -> None:
        """Закрывает соединение если давно ничего не приходило."""
        while True:
            await asyncio.sleep(30)
            silence = time.monotonic() - self._last_message_ts
            if silence > SILENT_TIMEOUT_SEC:
                logger.warning("No messages for %.0fs, forcing reconnect", silence)
                await ws.close(code=1000, reason="silent timeout")
                return

    def stop(self) -> None:
        """Останавливает цикл реконнекта."""
        self._running = False


# ──────────────────────────────────────────────────────────────────────────────
# Standalone-режим для отладки: просто печатает все ликвидации в консоль.
# Запуск:  python -m scalp.liquidation_feed
# ──────────────────────────────────────────────────────────────────────────────

async def _demo() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
    feed    = LiquidationFeed(symbols=symbols)

    print(f"Listening for liquidations on: {sorted(symbols)}")
    print("Format: TIME  SYMBOL  SIDE  $VALUE  @PRICE")
    print("-" * 70)

    try:
        async for event in feed.stream():
            ts = time.strftime("%H:%M:%S", time.localtime(event.trade_time_ms / 1000))
            print(
                f"{ts}  {event.symbol:>9}  {event.liquidated_side:>5}  "
                f"${event.usd_value:>12,.0f}  @ ${event.price:,.2f}"
            )
    except KeyboardInterrupt:
        print("\nStopped. Stats:", feed.stats)


if __name__ == "__main__":
    asyncio.run(_demo())
