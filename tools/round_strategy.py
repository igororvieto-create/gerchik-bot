"""Вход по круглым числам — проверка механизма Osler (2003).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Это ДРУГАЯ стратегия, а не настройка боевой:
сделка привязана к круглому числу, а не к VSA-климаксу. Боевой путь не
трогается вовсе — модуль живёт в tools/ и вызывается только прогоном.

МЕХАНИЗМ (docs/LITERATURE.md §1). Osler на данных реальных ордеров
дилинговых банков показала два разных эффекта:
  * тейк-профиты копятся НА круглых числах -> подход к нему тормозит цену
    и часто разворачивает («round_fade»);
  * стоп-лоссы копятся ЧУТЬ ЗА ним -> после пробоя срабатывает каскад и
    движение продолжается («round_break»).
Эффекты противоположны, поэтому проверяются оба.

ГДЕ СТОИТ СТОП. За круглым числом плюс 0.5 ATR, а НЕ вплотную за ним.
Вплотную — это ровно зона чужих стоп-кластеров: там нас и выбьет каскадом.
Это прямое следствие того же механизма и урок из LITERATURE §1, где
разбирается, почему боевой стоп с буфером 0.25 ATR стоит в худшем месте.

ЧТО ПЕРЕИСПОЛЬЗУЕТСЯ. ATR, гейты ликвидности и возраста листинга,
ограничение ширины стопа, судейство исходов и арифметика матожидания —
всё боевое. Новое здесь только правило входа.
"""
import math
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import cfg          # noqa: E402
from core.state import Signal        # noqa: E402
import strategy.scanner as scanner   # noqa: E402

# Доля половины шага сетки, ближе которой цена считается «у круглого».
# Выбрано ДО прогона и не подбирается: 0.25 — четверть пути до соседнего
# круглого, естественная граница «рядом».
_NEAR = 0.25
# Буфер стопа за круглым числом в ATR. Тоже зафиксирован заранее.
_BUF_ATR = 0.5


def round_grid(price: float) -> Optional[tuple]:
    """(шаг сетки, ближайшее круглое) — два значащих разряда.

    Масштабно-инвариантно: для BTC ~111 340 шаг 10 000, для альта 0.0234 —
    0.001. Аналог «00-уровней», о которых и идёт речь у Osler.
    """
    if price <= 0:
        return None
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0 or math.isinf(step):
        return None
    return step, round(price / step) * step


async def analyze_round(client, ticker: dict, mode: str) -> Optional[Signal]:
    """Сигнал по круглому числу или None. mode: 'fade' | 'break'."""
    symbol = ticker.get("symbol", "")
    try:
        price = float(ticker.get("lastPrice") or 0)
        vol_24h = float(ticker.get("volume24h") or 0)
        funding = float(ticker.get("fundingRate") or 0) * 100
        if price <= 0 or vol_24h < cfg.MIN_VOL_24H:
            return None
        # Инвариант «не торговать листинги» — боевой гейт как есть.
        if not await scanner._is_listing_old_enough(client, symbol):
            return None

        klines = await client.get_klines(symbol, interval="240", limit=26)
        if len(klines) < 20:
            return None
        atr = scanner._calc_atr(klines[:-1])
        if atr <= 0:
            return None
        atr_pct = atr / price * 100

        # Анти-спайк оставлен: входить сразу после вертикальной свечи плохо
        # независимо от того, круглое рядом или нет (инвариант CLAUDE.md).
        for k in klines[-2:]:
            if (k["high"] - k["low"]) / atr > cfg.MAX_LAST_CANDLE_ATR:
                return None

        grid = round_grid(price)
        if grid is None:
            return None
        step, nearest = grid
        last = klines[-2]          # последняя ЗАКРЫТАЯ свеча

        if mode == "fade":
            # Цена подошла к круглому, но ещё не прошла его.
            pos = (nearest - price) / (step / 2)
            if abs(pos) > _NEAR:
                return None
            if nearest > price:
                # круглое СВЕРХУ: там чужие тейки лонгов -> давление вниз
                direction, stop = "SHORT", nearest + _BUF_ATR * atr
            else:
                direction, stop = "LONG", nearest - _BUF_ATR * atr
        elif mode == "break":
            # Последняя закрытая свеча пересекла круглое число.
            crossed = None
            for edge in (nearest, nearest + step, nearest - step):
                if last["open"] < edge <= last["close"]:
                    crossed = ("LONG", edge)
                    break
                if last["open"] > edge >= last["close"]:
                    crossed = ("SHORT", edge)
                    break
            if crossed is None:
                return None
            direction, edge = crossed
            # Стоп по ДРУГУЮ сторону пробитого уровня: если каскад настоящий,
            # цена туда не вернётся.
            stop = (edge - _BUF_ATR * atr) if direction == "LONG" else (edge + _BUF_ATR * atr)
        else:
            raise ValueError(f"неизвестный режим {mode}")

        risk = abs(price - stop)
        if risk <= 0:
            return None
        if risk / atr > cfg.MAX_SL_ATR:      # боевой потолок ширины стопа
            return None
        sl_pct = risk / price * 100
        tp2 = price + 2 * risk if direction == "LONG" else price - 2 * risk
        tp3 = price + 3 * risk if direction == "LONG" else price - 3 * risk

        return Signal(
            symbol=symbol, signal_type=f"ROUND_{mode.upper()}",
            direction=direction, score=50, price=price,
            oi_change=0.0, vol_ratio=1.0, funding=funding,
            ob_bias="NEUTRAL", atr_pct=atr_pct,
            details=f"round={nearest:.8g} step={step:.8g}",
            entry=price, sl=stop, tp1=0.0, tp2=tp2, tp3=tp3,
            rr=2.0, headroom=2.0, sl_pct=sl_pct,
            round_pos=scanner._round_number_pos(price),
        )
    except Exception as e:
        print(f"{symbol}: round analyze error — {e}", file=sys.stderr)
        return None
