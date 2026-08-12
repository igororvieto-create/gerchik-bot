"""Signal outcome tracker — форвард-тест стратегии без сделок.

Каждые 30 минут проходит по сигналам без исхода и по 15-минутным свечам
проверяет, куда цена дошла раньше: до TP2 (WIN) или до SL (LOSS).
Если за 48 часов не дошла ни туда, ни туда — EXPIRED.

Консервативное правило: если SL и TP2 задеты В ОДНОЙ 15м-свече,
засчитывается LOSS (внутрисвечный порядок неизвестен — считаем худшее).
"""
import asyncio
import logging
from datetime import datetime, timezone

from core import db
from exchange.bybit import BybitClient

log = logging.getLogger("evaluator")

_EVALUATING = False
_MAX_AGE_HOURS = 48


def _judge(direction: str, sl: float, tp2: float, klines: list) -> tuple[str, float] | None:
    """Walk candles chronologically; return (outcome, price) or None if undecided."""
    for k in klines:
        hi, lo = k["high"], k["low"]
        if direction == "LONG":
            hit_sl = lo <= sl
            hit_tp = hi >= tp2
        else:  # SHORT
            hit_sl = hi >= sl
            hit_tp = lo <= tp2
        if hit_sl:            # включая случай "обе в одной свече" → худшее
            return "LOSS", sl
        if hit_tp:
            return "WIN", tp2
    return None


async def evaluate_signal_outcomes(client: BybitClient) -> None:
    global _EVALUATING
    if _EVALUATING:
        return
    _EVALUATING = True
    try:
        pending = await db.get_pending_signals(max_age_hours=_MAX_AGE_HOURS * 3)
        if not pending:
            return
        now = datetime.now(timezone.utc)
        decided = 0
        for row in pending:
            try:
                ts_raw = row["ts"].rstrip("Z")
                sig_ts = datetime.fromisoformat(ts_raw).replace(tzinfo=timezone.utc)
                age_h = (now - sig_ts).total_seconds() / 3600
                if age_h < 0.25:
                    continue  # слишком свежий — ещё нечего оценивать

                # 15м-свечи от момента сигнала (лимит Bybit — 1000, нам ≤200)
                need = min(int(age_h * 4) + 3, 200)
                klines = await client.get_klines(row["symbol"], interval="15", limit=need)
                sig_ms = sig_ts.timestamp() * 1000
                # Окно ограничено С ДВУХ сторон. Bybit отдаёт ПОСЛЕДНИЕ N свечей,
                # а 200 свечей по 15м покрывают лишь 50 часов. Для сигнала
                # возрастом 100ч фильтр ts >= sig_ms не отсекал ничего, и
                # оценщик судил по интервалу 50-100ч, не видя первых суток:
                # сигнал, поймавший стоп на 3-м часу и дошедший до цели на 55-м,
                # записывался ПОБЕДОЙ. Форвард-тест — единственное основание
                # включать автоторговлю, и завышался он именно на залипших.
                end_ms = sig_ms + _MAX_AGE_HOURS * 3600 * 1000
                relevant = [k for k in klines if sig_ms <= k["ts"] <= end_ms]
                if relevant and klines and klines[0]["ts"] > sig_ms:
                    # Начало окна не покрыто — вердикт по неполным данным хуже
                    # отсутствия вердикта: неизвестно, был ли стоп задет раньше.
                    log.warning(
                        f"evaluate {row['symbol']}: свечи начинаются позже сигнала "
                        f"(возраст {age_h:.0f}ч) — закрываю как EXPIRED без вердикта"
                    )
                    await db.set_signal_outcome(row["id"], "EXPIRED", relevant[-1]["close"])
                    decided += 1
                    await asyncio.sleep(0.3)
                    continue
                if not relevant:
                    # Данных нет (пауза торгов, делистинг, сбой API). Если
                    # сигнал уже перезрел — закрываем как EXPIRED, иначе он
                    # навсегда остаётся OPEN и портит знаменатель винрейта.
                    if age_h >= _MAX_AGE_HOURS:
                        await db.set_signal_outcome(row["id"], "EXPIRED", 0.0)
                        decided += 1
                    continue

                verdict = _judge(row["direction"], row["sl"], row["tp2"], relevant)
                if verdict:
                    await db.set_signal_outcome(row["id"], verdict[0], verdict[1])
                    decided += 1
                elif age_h >= _MAX_AGE_HOURS:
                    last_close = relevant[-1]["close"]
                    await db.set_signal_outcome(row["id"], "EXPIRED", last_close)
                    decided += 1
            except Exception as e:
                log.warning(f"evaluate {row.get('symbol')}: {e}")
            await asyncio.sleep(0.3)  # щадим rate-limit

        if decided:
            stats = await db.get_outcome_stats(days=7)
            log.info(
                f"evaluator: {decided} outcome(s) recorded | 7d: "
                f"{stats['win']}W/{stats['loss']}L/{stats['expired']}E "
                f"winrate={stats['winrate']}%"
            )
    except Exception as e:
        log.error(f"evaluate_signal_outcomes error: {e}")
    finally:
        _EVALUATING = False
