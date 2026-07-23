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
        pending = await db.get_pending_signals(max_age_hours=_MAX_AGE_HOURS + 2)
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
                relevant = [k for k in klines if k["ts"] >= sig_ms]
                if not relevant:
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
