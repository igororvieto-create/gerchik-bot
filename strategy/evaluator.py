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
from core.config import cfg
from exchange.bybit import BybitClient

log = logging.getLogger("evaluator")

_EVALUATING = False
_MAX_AGE_HOURS = 48


def _mfe(direction: str, entry: float, sl: float, klines: list) -> float:
    """Максимальный ход В ПЛЮС в единицах R по всему окну.

    Отдельно от _judge намеренно: у EXPIRED вердикта нет, а `_judge(...) or
    (None, None, 0.0)` подставлял ноль КАЖДОМУ просроченному сигналу —
    функция детерминирована, повторный вызов давал то же None. Именно у
    LOSS и EXPIRED эта величина и информативна (у WIN она тавтологична),
    а доля EXPIRED доходит до 90% при широких стопах.
    """
    risk = abs(entry - sl) if entry > 0 else 0.0
    if risk <= 0:
        return 0.0
    best = 0.0
    for k in klines:
        fav = (k["high"] - entry) if direction == "LONG" else (entry - k["low"])
        best = max(best, fav / risk)
    return best


def _judge(direction: str, sl: float, tp2: float, klines: list,
           entry: float = 0.0) -> tuple[str, float, float] | None:
    """Walk candles chronologically; return (outcome, price, mfe_r) or None.

    Судейство ОБЯЗАНО повторять то, что делает бот на бирже, иначе
    форвард-тест меряет не ту стратегию. Бот переносит стоп в безубыток
    после хода в плюс на BREAKEVEN_AT_R (trader.monitor_positions), поэтому
    и здесь после достижения этого уровня стоп становится безубыточным.

    Исходы: WIN (дошли до TP2), BE (сработал перенесённый стоп — примерно
    ноль), LOSS (исходный стоп), None (ещё не решено).

    mfe_r — максимальный ход В ПЛЮС в единицах R до момента исхода. Нужен,
    чтобы оценивать пороги переноса по фактическим данным, а не по модели.
    """
    if direction not in ("LONG", "SHORT"):
        # Раньше всё, кроме "LONG", молча трактовалось как SHORT — тот же
        # класс, что рецидив «NEUTRAL превращается в шорт» в сканере.
        return None
    if entry <= 0:
        entry = 0.0
    risk = abs(entry - sl) if entry > 0 else 0.0
    be_arm = cfg.BREAKEVEN_AT_R if (cfg.BREAKEVEN_AT_R > 0 and risk > 0) else 0.0
    fee = entry * cfg.BREAKEVEN_FEE_PCT / 100 if entry > 0 else 0.0
    be_price = (entry + fee) if direction == "LONG" else (entry - fee)

    cur_sl = sl
    armed = False
    mfe_r = 0.0
    for k in klines:
        hi, lo = k["high"], k["low"]
        # Ход в плюс на этой свече — считаем ДО проверки стопа, иначе
        # свеча, в которой цель и стоп задеты вместе, не даст своего MFE.
        if risk > 0:
            fav = (hi - entry) if direction == "LONG" else (entry - lo)
            mfe_r = max(mfe_r, fav / risk)

        if direction == "LONG":
            hit_sl = lo <= cur_sl
            hit_tp = hi >= tp2
        else:
            hit_sl = hi >= cur_sl
            hit_tp = lo <= tp2

        # Стоп проверяется ПЕРВЫМ: внутрисвечный порядок неизвестен, берём
        # худшее. Это же правило действовало и до переноса.
        if hit_sl:
            return ("BE" if armed else "LOSS"), cur_sl, mfe_r
        if hit_tp:
            return "WIN", tp2, mfe_r

        # Взвод по ЗАКРЫТИЮ свечи, а не по её хвосту. Иначе свеча, которая
        # дотянулась до порога хвостом и закрылась ниже безубытка, ставила
        # cur_sl ВЫШЕ рынка, и следующая свеча давала исход BE по цене, до
        # которой рынок в тот момент не доходил, — то есть смещение В ПОЛЬЗУ
        # переноса (1.1% исходов при пороге 0.3R).
        if be_arm and not armed and mfe_r >= be_arm:
            close = k.get("close", 0.0)
            reached = (close >= be_price) if direction == "LONG" else (close <= be_price)
            if reached:
                armed = True
                cur_sl = be_price
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
        failed = 0
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
                # Шорткат берём ТОЛЬКО когда окно невосстановимо: упёрлись в
                # лимит 200 свечей (≈50ч) или сигнал уже перезрел. Иначе
                # временный провал в истории (пауза торгов, техработы у биржи)
                # закрывал бы 4-часовой сигнал как EXPIRED навсегда, хотя у
                # него оставалось ещё 44 часа и данные вот-вот вернутся.
                if (relevant and klines and klines[0]["ts"] > sig_ms
                        and (need >= 200 or age_h >= _MAX_AGE_HOURS)):
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

                verdict = _judge(row["direction"], row["sl"], row["tp2"], relevant,
                                 entry=row["entry"] or 0.0)
                if verdict:
                    # decided растёт ТОЛЬКО при подтверждённой записи: иначе
                    # лог рапортует о вердиктах, которых в базе нет, а
                    # решение о деньгах принимается по этой цифре.
                    if await db.set_signal_outcome(row["id"], verdict[0], verdict[1],
                                                   mfe_r=verdict[2]):
                        decided += 1
                    else:
                        failed += 1
                elif age_h >= _MAX_AGE_HOURS:
                    last_close = relevant[-1]["close"]
                    # MFE у просроченного тоже полезен: показывает, насколько
                    # близко сетап подходил к цели, прежде чем застрять.
                    mfe = _mfe(row["direction"], row["entry"] or 0.0,
                               row["sl"], relevant)
                    if await db.set_signal_outcome(row["id"], "EXPIRED",
                                                   last_close, mfe_r=mfe):
                        decided += 1
                    else:
                        failed += 1
            except Exception as e:
                log.warning(f"evaluate {row.get('symbol')}: {e}")
            await asyncio.sleep(0.3)  # щадим rate-limit

        if failed:
            log.error(f"evaluator: {failed} вердикт(ов) НЕ записаны — "
                      f"строки будут пересужены на следующем проходе")
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
