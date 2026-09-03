import asyncio
import logging
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone

from core.config import cfg
from core.state import Signal, Position, state
from core import db
from exchange.bybit import BybitClient

log = logging.getLogger("trader")

_MONITORING = False
_ENTERING = 0   # счётчик выполняющихся enter_trade — shutdown их ждёт
_DAILY_LOCK = asyncio.Lock()

# Skip exchange-side "position disappeared" detection for positions younger
# than this: a monitor tick whose get_positions() request was already in
# flight when the entry filled would otherwise see a stale snapshot and
# wrongly mark the brand-new position as closed.
_MIN_POSITION_AGE_S = 90

# Счётчик неудачных попыток восстановить TP (символ -> попытки)
_TP_RETRIES: dict[str, int] = {}
# То же для SL. Без него ветка эскалации была недостижима: set_trading_stop
# возвращал True по retCode, монитор считал стоп восстановленным и каждые
# 30 секунд «чинил» его заново, а позиция жила без защиты (рецидив бага №1).
_SL_RETRIES: dict[str, int] = {}
_MAX_SL_RETRIES = 3
# Символы, по которым уже кричали о превышении потолка риска: без этого
# CRITICAL печатался бы каждые 30 секунд и утопил остальной лог.
_RISK_WARNED: set = set()


def _forget_symbol(symbol: str) -> None:
    """Единая точка снятия символа с учёта — счётчики попыток обязаны
    умирать вместе с позицией, иначе следующая сделка по тому же символу
    стартует с уже исчерпанным лимитом восстановления SL/TP."""
    state.positions.pop(symbol, None)
    state.pending_entries.pop(symbol, None)
    _TP_RETRIES.pop(symbol, None)
    _SL_RETRIES.pop(symbol, None)
    _RISK_WARNED.discard(symbol)


async def close_and_verify(client: BybitClient, symbol: str, side: str,
                           qty: float) -> tuple[bool, float]:
    """Закрыть позицию и ПОДТВЕРДИТЬ результат чтением живой позиции.

    retCode==0 у /v5/order/create означает «ордер принят», а не «позиция
    закрыта»: IOC reduceOnly в неликвиде заливается частично. Раньше учёт
    закрывался по retCode, остаток оставался на бирже без стопа, на
    следующем тике усыновлялся как MANUAL — и уже никогда не защищался,
    не занимал слот и не попадал в дневной предохранитель.

    Возвращает (closed, remaining):
      (True,  0.0)  — на бирже реально ноль;
      (False, >0)   — залилось частично, остаток столько;
      (False, -1.0) — проверить не удалось, состояние неизвестно.
    """
    try:
        res = await client.close_position(symbol, side, qty)
    except Exception as e:
        log.critical(f"{symbol}: запрос на закрытие не прошёл — {e}")
        return False, -1.0
    if res.get("retCode", -1) != 0:
        log.critical(f"{symbol}: закрытие отклонено — {res.get('retMsg', 'нет ответа')}")
        return False, -1.0
    await asyncio.sleep(1.0)  # бирже нужно время на исполнение IOC
    lp = await client.get_position(symbol)
    if lp is None:
        log.warning(f"{symbol}: не удалось сверить остаток после закрытия")
        return False, -1.0
    remaining = abs(float(lp.get("size") or 0))
    if remaining > 0:
        log.critical(f"{symbol}: закрыто ЧАСТИЧНО, остаток {remaining} — остаётся под наблюдением")
        return False, remaining
    return True, 0.0


def _quantize(value: float, tick: float, mode: str) -> float:
    """Привести цену к сетке tickSize. Decimal, а не float-арифметика:
    value/tick на мелких шагах даёт хвост вида 7.0564999999e-05, и
    round() уводил бы цену на тик в непредсказуемую сторону.

    mode='down' — вниз (для стопа LONG и тейка LONG),
    mode='up'   — вверх (для стопа SHORT и тейка SHORT)."""
    if tick <= 0:
        return value
    dv, dt = Decimal(str(value)), Decimal(str(tick))
    n = dv / dt
    n = n.to_integral_value(rounding=ROUND_FLOOR if mode == "down" else ROUND_CEILING)
    return float(n * dt)


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 8)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(round(value / step) * step, decimals)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reevaluate_halt() -> bool:
    """Authoritative halt check: computed from daily PnL and CURRENT balance.
    Must be re-run after balance becomes known — a restore that happened while
    balance was still 0 (process startup) could not evaluate the limit."""
    if state.trading_halted:
        return True
    if state.balance > 0 and state.daily_realized_pnl < 0:
        loss_pct = -state.daily_realized_pnl / state.balance * 100
        if loss_pct >= cfg.DAILY_LOSS_LIMIT_PCT:
            state.trading_halted = True
            state.halt_reason = "daily_loss"
            log.error(
                f"DAILY LOSS LIMIT HIT: {loss_pct:.2f}% >= "
                f"{cfg.DAILY_LOSS_LIMIT_PCT}% — halting new trades until UTC reset"
            )
            return True
    return False


async def _ensure_daily_state() -> None:
    """Rollover daily-loss tracking at UTC midnight; on rollover (including
    process start) the counter is rebuilt from the DB so a deploy or crash
    mid-day cannot silently reset the circuit breaker. Lock-protected:
    daily_pnl_date is stamped only AFTER the restore completes, so concurrent
    callers can't observe a half-restored state."""
    today = _today_utc()
    if state.daily_pnl_date == today:
        return
    async with _DAILY_LOCK:
        if state.daily_pnl_date == today:
            return
        try:
            pnl = await db.get_realized_pnl_since(f"{today}T00:00:00")
        except Exception as e:
            # Дату НЕ штампуем: иначе счётчик остался бы нулевым до конца
            # суток и бот торговал бы с потерянным убытком. Пока прочитать
            # не удалось — торговля стоит; следующий тик повторит.
            state.trading_halted = True
            state.halt_reason = state.halt_reason or "daily_pnl_unreadable"
            log.error(f"не удалось восстановить дневной PnL ({e}) — "
                      f"торговля приостановлена до успешного чтения")
            return
        state.daily_realized_pnl = pnl
        # Снимаем халт ТОЛЬКО если его поставил дневной лимит. Причины
        # вроде провала init_db суточным ролловером не лечатся, и снимать
        # их сменой даты значит начать торговать с неисправной базой.
        if state.halt_reason in ("", "daily_loss"):
            state.trading_halted = False
            state.halt_reason = ""
        elif state.trading_halted:
            log.warning(f"суточный ролловер не снимает халт: причина "
                        f"{state.halt_reason!r} не связана с дневным лимитом")
        state.daily_pnl_date = today
        _reevaluate_halt()  # no-op while balance is 0; re-run later in enter_trade


def record_realized_close(pnl: float) -> None:
    """Accumulate realized PnL into the daily circuit breaker. Single entry
    point used by the monitor loop, the manual dashboard close AND emergency
    closes — a close path that skips this lets losses bypass the daily limit."""
    state.daily_realized_pnl += pnl
    _reevaluate_halt()


async def fetch_matching_closed_pnl(client: BybitClient, pos: Position,
                                    attempts: int = 1, delay: float = 1.5) -> tuple[float, float]:
    """(exit_price, pnl) for THIS position's close. Bybit's closed-pnl list can
    lag and/or lead: closed[0] may be a previous trade on the same symbol.
    Match by record time >= position open time instead of blindly taking [0].
    Retries because the record may take a few seconds to appear after a close.

    tz: pos.ts is naive-UTC; naive .timestamp() would use the process's LOCAL
    timezone — replace(tzinfo=utc) keeps this correct even if TZ != UTC."""
    opened_ms = pos.ts.replace(tzinfo=timezone.utc).timestamp() * 1000
    # Верхняя граница обязательна. Раньше стояла только нижняя, и для
    # позиции, открытой часы назад, условие выполнялось для ЛЮБОЙ записи —
    # брался closed[0], то есть САМАЯ СВЕЖАЯ сделка по символу. Это давало
    # двойной учёт (повторный вход по тому же символу) и занос ЧУЖОГО PnL,
    # включая ручные сделки пользователя, специально исключённые из
    # дневного предохранителя. Окно: от открытия до сейчас + запас.
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    upper_ms = now_ms + 60_000
    for i in range(attempts):
        if i:
            await asyncio.sleep(delay)
        try:
            closed = await client.get_closed_pnl(pos.symbol, limit=10)
            # СУММА всех записей окна, а не одна выбранная.
            #
            # Раньше бралась самая ранняя подходящая. У позиции бывают СВОИ
            # более ранние записи: риск-гард сокращает размер сразу после
            # входа (close_position на излишек), и частичный залив
            # close_and_verify тоже оставляет запись. Тогда «самая ранняя»
            # оказывалась сокращением на -0.12, а реальный выход по стопу
            # на -15.0 не учитывался вовсе — и в trades писалась цена
            # выхода, равная цене входа.
            #
            # Позиция может закрываться несколькими событиями, поэтому её
            # PnL — это сумма. Цена выхода берётся у ПОСЛЕДНЕЙ записи: она
            # и есть фактический выход.
            hits = []
            for rec in closed:
                rec_ms = float(rec.get("updatedTime") or rec.get("createdTime") or 0)
                if opened_ms - 60_000 <= rec_ms <= upper_ms:
                    hits.append((rec_ms, rec))
            if hits:
                hits.sort(key=lambda x: x[0])
                # ОГРАНИЧЕНИЕ ПО ОБЪЁМУ. Одного окна по времени мало.
                #
                # Верхняя граница окна — «сейчас», а у восстановленной
                # строки pos.ts это время ОТКРЫТИЯ, то есть окно растянуто
                # на всё время, пока строка висела open. Ручная сделка
                # пользователя по тому же символу за этот период попадала
                # в сумму: реальный убыток -15 превращался в фиктивную
                # победу +185, и чужие деньги уходили в дневной
                # предохранитель. Тот самый класс, от которого защищает
                # ветка signal_type == "MANUAL".
                #
                # Позицию размера Q могут закрыть только записи, суммарный
                # closedSize которых равен Q. Берём записи по порядку и
                # останавливаемся, как только позиция закрыта полностью;
                # всё, что дальше, — чужое.
                # Ограничиваем ИСХОДНЫМ размером, а не текущим.
                #
                # pos.qty уменьшается при частичном закрытии (монитор
                # синхронизирует его с живым размером) и обнуляется
                # риск-гардом. От этого ломались обе стороны: при остатке
                # цикл обрывался на первой же частичной записи и терял PnL
                # финального выхода, а при нуле условие `qty > 0`
                # отключало ограничитель целиком — и в сумму снова
                # попадали ЧУЖИЕ сделки, ровно тот случай, от которого
                # ограничитель и написан.
                qty = abs(pos.qty_opened or pos.qty)
                taken, acc = [], 0.0
                for rec_ms, rec in hits:
                    taken.append((rec_ms, rec))
                    try:
                        acc += abs(float(rec.get("closedSize") or 0))
                    except (TypeError, ValueError):
                        acc = 0.0
                    # 1% допуск на округление лота биржей
                    if qty > 0 and acc >= qty * 0.99:
                        break
                # closedSize не пришёл ни в одной записи — объём проверить
                # нечем. Берём ТОЛЬКО первую: занизить учёт безопаснее, чем
                # приписать себе чужую сделку.
                # Объём проверить нечем — ни closedSize, ни исходного
                # размера. Берём ТОЛЬКО первую запись: занизить учёт
                # безопаснее, чем приписать себе чужую сделку.
                if acc <= 0 or qty <= 0:
                    taken = hits[:1]
                total = sum(float(r.get("closedPnl", 0)) for _, r in taken)
                exit_px = float(taken[-1][1].get("avgExitPrice", 0))
                if len(taken) > 1:
                    log.info(f"{pos.symbol}: закрытие из {len(taken)} записей, "
                             f"суммарный pnl={total:+.2f}")
                if len(hits) > len(taken):
                    log.warning(f"{pos.symbol}: в окне {len(hits)} записей, "
                                f"учтено {len(taken)} по объёму позиции "
                                f"({qty}) — остальные не наши")
                return exit_px, total
        except Exception as ce:
            log.warning(f"{pos.symbol}: could not fetch closed PnL (attempt {i+1}) — {ce}")
    # (0.0, 0.0) означает «не нашли», а НЕ «PnL равен нулю» — вызывающий
    # обязан различать: запись нулём запечатывает строку навсегда.
    return 0.0, 0.0


async def _settle_closed_position(client: BybitClient, pos: Position,
                                  attempts: int = 4) -> bool:
    """Записать закрытие и учесть PnL в предохранителе. True — учтено.

    Обе охраны ниже уже стояли в блоке реконсиляции простоя, но три
    остальных пути закрытия (штатный SL/TP, аварийное закрытие, round-trip
    прерванного входа) их не имели — а именно штатный путь отрабатывает
    почти каждое закрытие. Поэтому охрана вынесена сюда, а не скопирована
    в третий раз: одно место — один инвариант.

    False означает «не учтено, повторим на следующем тике». Вызывающий
    ОБЯЗАН не звать _forget_symbol при False: позиция должна остаться под
    наблюдением, иначе повторять будет некому.
    """
    exit_price, pnl = await fetch_matching_closed_pnl(client, pos, attempts=attempts)
    if exit_price <= 0 and pnl == 0.0:
        # Запись closed-pnl не найдена (лаг биржи, 403 через прокси,
        # гео-блок). Писать (0,0) НЕЛЬЗЯ: строка перестанет быть open,
        # следующий тик её не увидит, и реальный убыток исчезнет из
        # дневного предохранителя НАВСЕГДА, оставшись в истории нулём.
        log.warning(f"{pos.symbol}: закрытие не подтверждено биржей — "
                    f"повтор на следующем тике")
        return False
    # PnL идёт в предохранитель ТОЛЬКО после успешной записи. Иначе при
    # заблокированной базе строка остаётся open, символ уже забыт, и блок
    # реконсиляции в ЭТОМ ЖЕ тике учитывает тот же PnL повторно.
    outcome = await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
    if outcome == db.CLOSE_ABSENT:
        # Переводить нечего: строку уже закрыл другой путь. PnL не наш —
        # его учёл тот, кто закрыл. Повтор не нужен, слот освобождаем.
        return True
    # Проверка ПОЛОЖИТЕЛЬНАЯ: любое неожиданное значение обязано означать
    # «не учтено». Обратная форма (`if outcome == CLOSE_FAILED: return`)
    # молча проваливалась в учёт PnL — это поймал существующий тест
    # test_pnl_not_counted_when_db_write_fails, подавая старое False.
    if outcome != db.CLOSE_OK:
        log.error(f"{pos.symbol}: закрытие не записано ({outcome!r}) — "
                  f"PnL не учтён, повтор на следующем тике")
        return False
    record_realized_close(pnl)
    log.info(f"{pos.symbol}: закрытие учтено exit={exit_price:.4f} pnl={pnl:+.2f}")
    return True


async def _record_round_trip(client: BybitClient, pos: Position,
                             attempts: int = 3) -> None:
    """Persist an open+close round trip (used by emergency close) and feed
    the realized PnL into the circuit breaker — fees/slippage of an aborted
    entry are real losses and must not vanish from the books."""
    try:
        if not await db.save_trade_open(pos):
            # Возврат проверяется здесь так же, как в остальных путях:
            # без строки save_trade_close вернёт CLOSE_ABSENT, а он значит
            # «PnL учёл кто-то другой» — здесь не учёл никто.
            log.error(f"{pos.symbol}: строка round-trip не записана — "
                      f"издержки прерванного входа могут потеряться")
    except Exception as e:
        log.error(f"{pos.symbol}: save_trade_open (round-trip) failed — {e}")
    # Не подтвердилось — строка остаётся open, и её подберёт блок
    # реконсиляции простоя на одном из следующих тиков. Издержки
    # прерванного входа (комиссии + проскальзывание) реальны и терять их
    # нельзя, но и записывать нулём вместо них — тоже.
    await _settle_closed_position(client, pos, attempts=attempts)


async def enter_trade(client: BybitClient, sig: Signal) -> bool:
    """Try to open a position based on a signal. Returns True if order placed."""
    if not cfg.AUTO_TRADE:
        return False
    if not client.api_key or not client.secret:
        log.warning("AUTO_TRADE=true but BYBIT_API_KEY/BYBIT_SECRET not set")
        return False
    if sig.score < cfg.TRADE_MIN_SCORE:
        return False
    if sig.direction == "NEUTRAL":
        return False
    if sig.sl_pct <= 0 or sig.entry <= 0 or sig.sl <= 0:
        return False
    # Запас до встречного уровня меньше 2R означает, что торговая цель TP2=2R
    # стоит ЗА уровнем, от которого сделка рассчитывает оттолкнуться: тезис
    # отрабатывает, а WIN не засчитывается. Показывать такой сигнал можно
    # (MIN_RR=1.5), торговать — нет.
    # Без нижней границы: headroom==0 означает «данных нет» (сигнал построен
    # мимо _calc_levels — восстановлен из БД, создан вручную через API), и это
    # обязано быть отказом, а не молчаливым отключением гейта целиком.
    if sig.headroom < cfg.MIN_TRADE_HEADROOM_R:
        log.info(
            f"{sig.symbol}: запас до цели {sig.headroom:.2f}R < "
            f"{cfg.MIN_TRADE_HEADROOM_R}R — TP2 за уровнем, сигнал не торгуется"
        )
        return False

    await _ensure_daily_state()
    if state.trading_halted:
        log.warning(f"{sig.symbol}: daily loss limit hit — trading halted until UTC reset, skip")
        return False

    if sig.symbol in state.positions:
        log.debug(f"{sig.symbol}: already in position, skip")
        return False
    bot_positions = [p for p in state.positions.values()
                     if p is None or getattr(p, "signal_type", "") != "MANUAL"]
    if len(bot_positions) >= cfg.MAX_POSITIONS:
        log.info(f"Max positions ({cfg.MAX_POSITIONS}) reached, skip {sig.symbol}")
        return False

    # Correlation guard: cap how many concurrent positions can face the same
    # direction. Three uncorrelated setups is diversification; three LONGs in
    # correlated alts is one oversized directional bet wearing three tickets.
    side = "Buy" if sig.direction == "LONG" else "Sell"
    # Входы В ПОЛЁТЕ тоже считаются: sentinel None стороны не несёт, поэтому
    # два параллельных enter_trade видели same_dir_count=0 и вместе пробивали
    # кап. MAX_POSITIONS от этого защищён (sentinel попадает в bot_positions),
    # а направленческий кап — не был.
    same_dir_count = sum(
        1 for p in bot_positions if p is not None and p.side == side
    ) + sum(
        1 for s_sym, (s_side, _) in state.pending_entries.items()
        if s_side == side and s_sym != sig.symbol
    )
    if same_dir_count >= cfg.MAX_SAME_DIRECTION:
        log.info(
            f"{sig.symbol}: {same_dir_count} {side} position(s) already open "
            f"(correlation cap {cfg.MAX_SAME_DIRECTION}), skip"
        )
        return False

    # Reserve slot immediately to prevent concurrent duplicate entries.
    # Обе записи ставятся без await между ними — иначе резервирование
    # перестаёт быть атомарным относительно параллельного входа.
    state.positions[sig.symbol] = None  # sentinel; replaced with real Position or removed
    # utcnow() (наивный), а не now(timezone.utc): монитор сравнивает эту
    # отметку с now_utc = datetime.utcnow() и с Position.ts — смешение
    # aware/naive уронило бы вычитание TypeError'ом прямо в цикле монитора.
    state.pending_entries[sig.symbol] = (side, datetime.utcnow())

    global _ENTERING
    _ENTERING += 1
    try:
        balance = await client.get_balance()
        if balance < 10:
            log.warning(f"Insufficient balance: {balance:.2f} USDT")
            _forget_symbol(sig.symbol)
            return False

        state.balance = balance

        # Authoritative halt re-check now that balance is known: the startup
        # restore runs with balance=0 and cannot evaluate the limit itself
        if _reevaluate_halt():
            log.warning(f"{sig.symbol}: daily loss limit (post-balance check) — skip")
            _forget_symbol(sig.symbol)
            return False

        # Параметры инструмента ДО расчёта размера: квантование цен по шагу
        # биржи меняет расстояние до стопа, а значит и sl_pct, от которого
        # считается объём. Считать объём по неквантованному стопу — значит
        # промахнуться мимо целевого риска.
        info = await client.get_instrument_info(sig.symbol)
        lot  = info.get("lotSizeFilter", {})
        if not lot:
            # Пустой ответ = сбой API. Подставлять шаг 0.001 наугад опасно:
            # для символа с реальным шагом 1 это ломает и размер, и риск-гард.
            log.warning(f"{sig.symbol}: нет данных инструмента (lotSizeFilter пуст) — пропуск входа")
            _forget_symbol(sig.symbol)
            return False
        qty_step = float(lot.get("qtyStep",      "0.001"))
        min_qty  = float(lot.get("minOrderQty",  "0.001"))
        min_notional = float(lot.get("minNotionalValue", "5") or 5)

        # Квантование SL/TP по tickSize. Bybit валидирует ценовые поля по
        # priceFilter.tickSize, а _calc_levels отдаёт сырые float'ы — на
        # символах с мелким шагом НИ ОДНО значение не было кратно тику, и
        # первый же ордер был бы отвергнут (либо принят без стопа, что есть
        # рецидивирующий баг №1). Стоп округляем ОТ входа, тейк — К входу:
        # так квантование не может ужесточить риск и не может завысить цель.
        tick = float(info.get("priceFilter", {}).get("tickSize", "0") or 0)
        entry_px = sig.entry
        sl_px, tp_px = sig.sl, sig.tp2
        if tick > 0:
            if sig.direction == "LONG":
                sl_px = _quantize(sl_px, tick, "down")   # стоп ниже входа — дальше
                tp_px = _quantize(tp_px, tick, "down")   # тейк выше входа — ближе
            else:
                sl_px = _quantize(sl_px, tick, "up")     # стоп выше входа — дальше
                tp_px = _quantize(tp_px, tick, "up")     # тейк ниже входа — ближе
            if sl_px <= 0 or tp_px <= 0 or abs(sl_px - entry_px) <= 0:
                log.warning(f"{sig.symbol}: после квантования по тику {tick} стоп вырожден — пропуск")
                _forget_symbol(sig.symbol)
                return False

        # sl_pct пересчитывается от КВАНТОВАННОГО стопа — именно он поедет на биржу
        sl_pct = abs(entry_px - sl_px) / entry_px * 100
        if sl_pct <= 0:
            _forget_symbol(sig.symbol)
            return False

        # Position size: risk RISK_PER_TRADE% of balance
        risk_usdt     = balance * cfg.RISK_PER_TRADE / 100
        position_usdt = risk_usdt / (sl_pct / 100)  # notional value

        # Cap by max margin: never use more than MAX_MARGIN_PCT% of balance as margin
        max_notional_margin = balance * cfg.MAX_MARGIN_PCT / 100 * cfg.LEVERAGE
        # Also cap at balance × leverage to avoid margin rejection
        max_notional = min(balance * cfg.LEVERAGE, max_notional_margin)
        if position_usdt > max_notional:
            position_usdt = max_notional
            log.debug(f"{sig.symbol}: notional capped to {max_notional:.2f}")

        qty = _round_step(position_usdt / sig.entry, qty_step)
        bumped = qty < min_qty
        if bumped:
            qty = min_qty
        # Проверяем ФАКТИЧЕСКИЙ риск после округления, а не только случай
        # qty < min_qty: _round_step округляет к БЛИЖАЙШЕМУ шагу, поэтому
        # qty 0.51 при шаге 1 превращалась в 1.0 (риск ×1.96) и проверка
        # min_qty не срабатывала вовсе — потолок риска обходился молча.
        actual_risk = qty * entry_px * sl_pct / 100
        # Два потолка: относительный (допуск на округление лота) и АБСОЛЮТНЫЙ
        # (инвариант проекта — не более 3% баланса). Раньше был только первый,
        # и при RISK_PER_TRADE=3% округление вверх давало 4% фактического
        # риска, проходя проверку 1.5× — инвариант пробивался молча.
        hard_cap = balance * 3.0 / 100
        if actual_risk > min(risk_usdt * 1.5, hard_cap):
            log.info(
                f"{sig.symbol}: лот {qty} (шаг {qty_step}, мин {min_qty}) даёт риск "
                f"{actual_risk:.2f} USDT ({actual_risk/balance*100:.2f}% баланса) "
                f"вместо целевых {risk_usdt:.2f} — пропуск"
            )
            _forget_symbol(sig.symbol)
            return False

        # ПОТОЛОК МАРЖИ перепроверяется ПОСЛЕ подъёма до минимального лота.
        #
        # Раньше после подъёма проверялся только риск, а верхняя граница
        # нотионала — нет. На малом счёте это пробивало MAX_MARGIN_PCT в
        # разы: при балансе 50 USDT и минимальном лоте BTCUSDT 0.001
        # нотионал выходил 60 USDT, маржа 12 USDT = 24% баланса при
        # потолке 10%. Инвариант по риску при этом оставался цел, поэтому
        # проверка риска подъём и пропускала. Бьёт тем сильнее, чем меньше
        # счёт, — то есть ровно на первых реальных сделках.
        if bumped and qty * entry_px > max_notional:
            log.info(
                f"{sig.symbol}: минимальный лот {min_qty} даёт нотионал "
                f"{qty * entry_px:.2f} при потолке {max_notional:.2f} "
                f"(маржа {qty * entry_px / cfg.LEVERAGE / balance * 100:.1f}% "
                f"баланса при лимите {cfg.MAX_MARGIN_PCT}%) — пропуск"
            )
            _forget_symbol(sig.symbol)
            return False

        # minNotionalValue из инструмента, а не хардкод 5.0: у части символов
        # минимум другой, и ордер отвергался бы биржей.
        if qty * entry_px < min_notional:
            log.debug(f"{sig.symbol}: notional {qty*entry_px:.2f} < {min_notional} USDT min")
            _forget_symbol(sig.symbol)
            return False

        ok = await client.set_leverage(sig.symbol, cfg.LEVERAGE)
        if not ok:
            # Placing a trade whose sizing assumed cfg.LEVERAGE without
            # confirming the exchange applied it => wrong margin usage and
            # liquidation distance. Abort by default (ABORT_ON_LEVERAGE_FAIL).
            log.error(f"{sig.symbol}: set_leverage({cfg.LEVERAGE}) failed")
            if cfg.ABORT_ON_LEVERAGE_FAIL:
                _forget_symbol(sig.symbol)
                return False
            log.warning(f"{sig.symbol}: continuing despite leverage-set failure (ABORT_ON_LEVERAGE_FAIL=False)")

        # Use TP2 (1:2 R:R) as primary target — more likely to be hit than TP3
        result = await client.place_order(
            symbol=sig.symbol, side=side, qty=qty, sl=sl_px, tp=tp_px,
        )

        ret_code = result.get("retCode", -1)
        ret_msg  = str(result.get("retMsg", ""))
        if ret_code != 0:
            # Duplicate orderLinkId (110072): the FIRST attempt actually
            # filled and only its response was lost to a timeout — the retry
            # got rejected as a duplicate. Treating this as failure would
            # leave a live UNTRACKED position; reconcile as success instead.
            if ret_code == 110072 or "duplicate" in ret_msg.lower():
                log.warning(
                    f"{sig.symbol}: duplicate orderLinkId — first attempt filled, "
                    f"reconciling as success"
                )
            elif not result:
                # Пустой ответ = все три попытки _post умерли на транспорте.
                # Это НЕ «ордер отклонён»: первая попытка могла быть принята
                # биржей, а ответ потерян. Освобождать слот вслепую нельзя —
                # живая позиция осталась бы без строки в trades и на следующем
                # тике была бы усыновлена как MANUAL: без стопа, вне слотов и
                # мимо дневного лимита. Спрашиваем биржу, что там на самом деле.
                log.error(f"{sig.symbol}: ответ на ордер потерян — сверяю с биржей")
                lp = await client.get_position(sig.symbol)
                if lp is None:
                    log.critical(
                        f"{sig.symbol}: биржа недоступна — состояние ордера НЕИЗВЕСТНО. "
                        f"Слот остаётся занят, монитор разберётся на следующем тике"
                    )
                    return False  # sentinel НЕ снимаем: усыновление опознает позицию
                if not lp:
                    log.warning(f"{sig.symbol}: позиции нет — ордер действительно не прошёл")
                    _forget_symbol(sig.symbol)
                    return False
                if lp.get("side") and lp.get("side") != side:
                    # На символе живёт позиция ПРОТИВОПОЛОЖНОЙ стороны — она не
                    # наша (ручная сделка, ещё не усыновлённая монитором).
                    # Оформив её как свою, мы бы слали reduceOnly неверной
                    # стороны и получали отказы вместо честного «это не наше».
                    log.critical(
                        f"{sig.symbol}: на бирже позиция {lp.get('side')}, а мы входили {side} — "
                        f"чужая позиция, не трогаю"
                    )
                    _forget_symbol(sig.symbol)
                    return False
                log.warning(f"{sig.symbol}: позиция ЖИВА (size={lp.get('size')}) — оформляю как свою")
                qty = abs(float(lp.get("size") or qty))
            else:
                log.error(f"{sig.symbol}: order failed — {ret_msg}")
                _forget_symbol(sig.symbol)
                return False

        order_id = result.get("result", {}).get("orderId", "")
        pos = Position(
            symbol=sig.symbol, side=side,
            entry=sig.entry, sl=sl_px,
            tp1=sig.tp1, tp2=tp_px, tp3=sig.tp3,
            qty=qty, qty_opened=qty, score=sig.score,
            signal_type=sig.signal_type, order_id=order_id,
        )
        # Track the position BEFORE verification/DB writes: the order is live
        # on the exchange from this point, and losing track of it (on any
        # later exception) is worse than any bookkeeping failure.
        state.positions[sig.symbol] = pos
        # Бронь снимается ровно здесь: с этого момента сторону несёт сам
        # Position, и учитывать её ВТОРОЙ раз через pending_entries нельзя —
        # иначе одна живая позиция считается дважды, MAX_SAME_DIRECTION=2
        # работает как 1, а ключи копятся навсегда и после пары сделок
        # блокируют направление при НУЛЕ открытых позиций.
        state.pending_entries.pop(sig.symbol, None)
        # Запись в trades СРАЗУ, до верификации SL: усыновление после
        # рестарта опознаёт свою позицию только по этой строке. Без неё
        # позиция бота получала ярлык MANUAL и навсегда лишалась защиты.
        # Возврат проверяется и повторяется: строка в trades — ЕДИНСТВЕННЫЙ
        # признак «своя позиция» после рестарта, а деплой Railway случается на
        # каждый пуш. Без строки живая позиция бота усыновляется как MANUAL, а
        # ручным монитор принципиально не досылает стоп — рецидив бага №1.
        _row_written = False
        for _attempt in range(3):
            try:
                if await db.save_trade_open(pos):
                    _row_written = True
                    break
            except Exception as dbe:
                log.error(f"{sig.symbol}: save_trade_open failed — {dbe}")
            await asyncio.sleep(0.5)
        if not _row_written:
            log.critical(
                f"{sig.symbol}: позиция ОТКРЫТА, но строка в trades не записана. "
                f"После рестарта она будет опознана как ручная и останется без "
                f"досылки стопа — закрой её вручную или почини запись в БД."
            )

        # IMPORTANT — recurring historical bug: SL/TP have previously been
        # placed as chart markers only, without actually reaching the
        # exchange. retCode==0 confirms the entry order was accepted; it does
        # NOT confirm the exchange attached stopLoss/takeProfit. Verify by
        # reading the live position back.
        #
        # Three outcomes:
        #  - verified protected  -> proceed
        #  - verified UNPROTECTED -> emergency close (recorded as a round trip)
        #  - could NOT verify (API failure / propagation lag) -> keep tracked;
        #    monitor_positions re-checks SL attachment on every tick and
        #    re-attaches or closes if it's really missing.
        exch_sl = exch_tp = 0.0
        verified = False
        live_pos = None
        for attempt in range(3):
            await asyncio.sleep(0.5 if attempt == 0 else 1.5)
            snap = await client.get_position(sig.symbol)
            if not snap:
                continue  # None = сбой API, {} = ещё не появилась — обе ждут ретрая
            # Не затираем последний НЕПУСТОЙ снимок: verified взводится на
            # первой удачной попытке и не сбрасывается, а live_pos раньше
            # обнулялся при сбое API в попытках 2-3. Тогда условие
            # `verified and live_pos` было ложным, и вместе с ним
            # пропускались И фиксация фактической цены входа, И ПОСТ-проверка
            # потолка риска 3% по этой цене.
            live_pos = snap
            exch_sl = float(live_pos.get("stopLoss") or 0)
            exch_tp = float(live_pos.get("takeProfit") or 0)
            verified = True
            if exch_sl > 0 and exch_tp > 0:
                break

        if verified and live_pos:
            # Фактическая цена залива ОБЯЗАНА заменить цену сигнала: от
            # pos.entry считается и безубыток, и весь последующий разбор в R
            # по таблице trades. При проскальзывании 0.8% «безубыток»
            # фиксировал бы гарантированный минус в ~11% целевого риска.
            _fill = float(live_pos.get("avgPrice") or 0)
            if _fill > 0:
                pos.entry = _fill
                try:
                    await db.update_trade_entry(pos.order_id, sig.symbol, _fill)
                except Exception as ue:
                    log.warning(f"{sig.symbol}: не удалось обновить цену входа в БД — {ue}")
            # Инвариант «риск ≤3% баланса» проверялся по sig.entry, а вход идёт
            # ПО РЫНКУ: при проскальзывании реальное расстояние до стопа больше
            # расчётного, и потолок пробивался незамеченным (риск 3% + 0.8%
            # проскальзывания = 4.6% баланса). Считаем по фактической цене.
            fill_px  = float(live_pos.get("avgPrice") or 0)
            fill_qty = abs(float(live_pos.get("size") or 0))
            if fill_px > 0 and fill_qty > 0 and sl_px > 0:
                real_risk = fill_qty * abs(fill_px - sl_px)
                if real_risk > balance * 3.0 / 100:
                    # ВНИЗ, а не к ближайшему: округление вверх оставляло
                    # left_risk выше потолка, и следующая проверка отправляла
                    # здоровую позицию в полное закрытие (спред + две
                    # тейкер-комиссии). На крупном шаге лота оно же съедало
                    # весь excess, и позиция жила с риском 3.25% — то есть
                    # инвариант пробивался. Для потолка риска корректно
                    # только округление вниз.
                    allowed_qty = math.floor(
                        (balance * 3.0 / 100) / abs(fill_px - sl_px) / qty_step
                    ) * qty_step
                    allowed_qty = round(allowed_qty, 8)
                    excess = _round_step(fill_qty - allowed_qty, qty_step)
                    log.error(
                        f"{sig.symbol}: проскальзывание подняло риск до "
                        f"{real_risk/balance*100:.2f}% (вход {fill_px} вместо {sig.entry}) — "
                        f"сокращаю позицию на {excess}"
                    )
                    if excess >= min_qty:
                        try:
                            red = await client.close_position(sig.symbol, side, excess)
                            if red.get("retCode", -1) == 0:
                                await asyncio.sleep(1.0)
                                after = await client.get_position(sig.symbol)
                                if after is None:
                                    # Сбой API — состояние неизвестно. НЕ пишем
                                    # «сокращено»: монитор пересверит размер.
                                    log.critical(f"{sig.symbol}: не удалось сверить размер после сокращения")
                                elif not after:
                                    log.warning(f"{sig.symbol}: позиция закрылась целиком при сокращении")
                                    qty = 0.0
                                    pos.qty = 0.0
                                else:
                                    qty = abs(float(after.get("size") or qty))
                                    pos.qty = qty
                                    # retCode==0 = ордер ПРИНЯТ: reduceOnly IOC в
                                    # неликвиде заливается частично. Сверяем, что
                                    # риск реально ушёл под потолок, а не просто
                                    # уменьшился — иначе «сокращено» маскировало
                                    # позицию, всё ещё несущую >3% риска.
                                    left_risk = qty * abs(fill_px - sl_px)
                                    # допуск в один шаг лота: иначе превышение
                                    # на доли цента гонит позицию в закрытие
                                    tol = qty_step * abs(fill_px - sl_px)
                                    if left_risk > balance * 3.0 / 100 + tol:
                                        log.critical(
                                            f"{sig.symbol}: сокращение залилось частично — "
                                            f"риск всё ещё {left_risk/balance*100:.2f}% "
                                            f"(> 3%), закрываю позицию полностью"
                                        )
                                        done, rem = await close_and_verify(
                                            client, sig.symbol, side, qty)
                                        if done:
                                            await _record_round_trip(client, pos)
                                            _forget_symbol(sig.symbol)
                                            return False
                                        if rem > 0:
                                            pos.qty = rem
                                    else:
                                        log.warning(f"{sig.symbol}: размер сокращён до {qty}")
                            else:
                                log.critical(
                                    f"{sig.symbol}: сокращение отклонено — {red.get('retMsg')}"
                                )
                        except Exception as rde:
                            log.critical(f"{sig.symbol}: сокращение не прошло — {rde}")
                    else:
                        log.critical(
                            f"{sig.symbol}: превышение меньше минимального лота — "
                            f"позиция несёт {real_risk/balance*100:.2f}% риска"
                        )

        if verified and exch_tp <= 0 and exch_sl > 0:
            # TP не долетел, но SL стоит — позиция ЗАЩИЩЕНА, закрывать её
            # (платя спред дважды) нельзя. Досылаем тейк отдельным запросом.
            log.warning(f"{sig.symbol}: TP не прикрепился (SL={exch_sl}) — досылаю takeProfit")
            tp_ok = False
            try:
                tp_ok = await client.set_trading_stop(sig.symbol, tp=tp_px)
            except Exception as te:
                log.warning(f"{sig.symbol}: не удалось дослать TP — {te}")
            if not tp_ok:
                # Биржа отклонила — монитор повторит на следующем тике.
                # Раньше результат игнорировался, и позиция навсегда
                # оставалась без тейка: доходила до +2R и возвращалась в стоп.
                log.error(f"{sig.symbol}: TP отклонён биржей — монитор повторит")

        if verified and exch_sl <= 0:
            # Риск несёт только отсутствующий SL — вот тут закрываем.
            log.error(
                f"{sig.symbol}: order filled but exchange shows "
                f"SL={exch_sl} TP={exch_tp} — position UNPROTECTED, closing immediately."
            )
            close_ok, remaining = await close_and_verify(client, sig.symbol, side, qty)
            if remaining > 0:
                pos.qty = remaining
            if close_ok:
                # The aborted entry still paid fees/slippage — record the
                # round trip so history and the daily breaker see the loss
                await asyncio.sleep(1.0)
                await _record_round_trip(client, pos)
                _forget_symbol(sig.symbol)
                return False
            # Close failed: position is live and unprotected — KEEP it tracked;
            # monitor_positions will re-attach SL or close it on its next tick.
            log.critical(
                f"{sig.symbol}: UNPROTECTED position remains open and tracked — "
                f"monitor will retry protection"
            )
        elif not verified:
            log.error(
                f"{sig.symbol}: could not verify SL/TP attachment (API failures "
                f"or propagation lag) — keeping position tracked, monitor re-checks SL"
            )

        if qty <= 0:
            # Сокращение риск-гардом закрыло позицию целиком. Рапортовать
            # «Opened ... qty=0.0» и возвращать True значило бы сообщать об
            # открытии позиции, которой на бирже уже нет.
            log.warning(f"{sig.symbol}: позиция закрылась целиком при "
                        f"сокращении — входа нет")
            await db.abandon_trade(sig.symbol, pos.order_id if pos else "")
            _forget_symbol(sig.symbol)
            return False
        log.info(
            f"Opened {side} {sig.symbol} qty={qty} "
            f"entry={pos.entry:.8f} SL={sl_px:.8f} TP={tp_px:.8f} "
            f"risk={risk_usdt:.2f} USDT orderId={order_id}"
        )
        return True

    except BaseException as e:
        # BaseException, не Exception: asyncio.CancelledError (таймаут /api/scan,
        # рестарт uvicorn) прежде проскакивал мимо очистки, и sentinel-слот
        # оставался в state.positions НАВСЕГДА — занимал место из MAX_POSITIONS,
        # блокировал повторный вход по символу и был невидим для монитора.
        if not isinstance(state.positions.get(sig.symbol), Position):
            _forget_symbol(sig.symbol)
        log.error(f"enter_trade {sig.symbol}: {type(e).__name__}: {e}")
        if isinstance(e, asyncio.CancelledError):
            raise
        return False
    finally:
        _ENTERING -= 1


async def monitor_positions(client: BybitClient) -> None:
    """Check if exchange positions still exist; detect SL/TP closes; verify
    every live position still carries a stop-loss (recurring bug #1) and
    re-attach or emergency-close if it doesn't."""
    global _MONITORING
    if _MONITORING:
        log.warning("monitor_positions: previous call still running, skipping this tick")
        return
    _MONITORING = True
    try:
        await _ensure_daily_state()

        # Always monitor even if AUTO_TRADE was toggled off mid-session —
        # otherwise open positions become orphaned (never recorded as closed)
        if not client.api_key:
            return

        # Always update balance
        bal = await client.get_balance()
        if bal > 0:
            state.balance = bal
            _reevaluate_halt()

        # НЕТ раннего выхода по пустому state.positions: именно в этом
        # состоянии (после каждого рестарта) и нужна сверка с биржей —
        # иначе блок усыновления сирот ниже недостижим, и позиция бота
        # остаётся без проверки стопа, без записи закрытия и вне лимита.
        live = await client.get_positions()
        if live is None:
            # API failure — do NOT wipe positions; wait for next cycle
            log.warning("monitor_positions: get_positions API failed, skipping close check")
            return

        live_map = {p["symbol"]: p for p in live}
        now_utc = datetime.utcnow()



        # Усыновление осиротевших позиций: state.positions живёт только в
        # памяти и обнуляется при каждом рестарте, а деплой Railway при
        # открытых позициях оставлял их без мониторинга — без проверки SL,
        # без записи закрытия и без учёта в дневном лимите. Хуже: enter_trade
        # считал символ свободным и мог удвоить позицию в one-way режиме.
        orphans = [sym for sym in live_map if sym not in state.positions]
        if orphans:
            # Открытые сделки бота из БД — единственный надёжный признак
            # "своя позиция" после рестарта (state.positions живёт в памяти)
            try:
                # reversed: get_open_trades отдаёт DESC, а в словаре побеждает
                # последняя итерация — без reversed выигрывала бы САМАЯ СТАРАЯ
                # строка (зависшая после краша), и позиция восстанавливалась бы
                # с чужими целями и order_id.
                bot_trades = {r["symbol"]: r for r in reversed(await db.get_open_trades())}
            except Exception as te:
                # НЕ продолжаем с пустым словарём: «БД недоступна» выглядело бы
                # как «своих сделок нет», и КАЖДАЯ позиция бота получила бы
                # ярлык MANUAL — то есть навсегда лишилась бы досылки стопа,
                # перестала занимать слот и выпала из дневного лимита.
                # Пропускаем усыновление целиком, повторим на следующем тике.
                log.error(f"не удалось прочитать открытые сделки ({te}) — "
                          f"усыновление отложено, чтобы не пометить свои позиции как MANUAL")
                orphans = []
                bot_trades = {}
            for sym in orphans:
                lp = live_map[sym]
                try:
                    size = abs(float(lp.get("size") or 0))
                    if size <= 0:
                        continue
                    entry_px = float(lp.get("avgPrice") or 0)
                    row = bot_trades.get(sym)
                    if row:
                        # Своя позиция: восстанавливаем цели и order_id из БД,
                        # дальше она защищается и учитывается как обычная
                        adopted = Position(
                            symbol=sym, side=lp.get("side", row["side"]),
                            entry=entry_px or row["entry"],
                            sl=float(lp.get("stopLoss") or 0) or (row["sl"] or 0),
                            tp1=row["tp1"] or 0.0, tp2=row["tp2"] or 0.0, tp3=row["tp3"] or 0.0,
                            qty=size, qty_opened=size, score=row["score"] or 0,
                            signal_type=row["signal_type"] or "RESTORED",
                            order_id=row["order_id"] or "",
                        )
                        log.warning(f"{sym}: позиция бота восстановлена после рестарта "
                                    f"({adopted.side} {size} @ {entry_px})")
                    else:
                        # Ручная сделка пользователя: только наблюдаем.
                        # MANUAL исключается из слотов и из дневного лимита.
                        adopted = Position(
                            symbol=sym, side=lp.get("side", "Buy"), entry=entry_px,
                            sl=float(lp.get("stopLoss") or 0), tp1=0.0,
                            tp2=float(lp.get("takeProfit") or 0), tp3=0.0,
                            qty=size, qty_opened=size, score=0,
                            signal_type="MANUAL",
                        )
                        log.info(f"{sym}: ручная позиция на бирже "
                                 f"({adopted.side} {size} @ {entry_px}) — только наблюдение")
                    state.positions[sym] = adopted
                except Exception as ae:
                    log.error(f"{sym}: не удалось взять позицию под учёт — {ae}")

        for sym in list(state.positions.keys()):
            pos = state.positions.get(sym)
            if pos is None:
                # Sentinel заброни­рованного входа. В норме живёт секунды, но
                # enter_trade оставляет его намеренно, когда ответ биржи на
                # ордер потерян И сверка тоже не прошла: снять слот вслепую
                # значило бы бросить возможную живую позицию без стопа.
                # Здесь этот слот разрешается — иначе он вечно занимает место
                # из MAX_POSITIONS и блокирует повторный вход по символу.
                started = state.pending_entries.get(sym, (None, None))[1]
                if started is None or (now_utc - started).total_seconds() < _MIN_POSITION_AGE_S:
                    continue  # вход ещё может быть в полёте
                if _ENTERING > 0:
                    # enter_trade прямо сейчас работает. При деградировавших
                    # POST-путях (3 попытки × 10 с × 2 прокси) он легко живёт
                    # дольше 90 с, и без этой проверки монитор успевал забрать
                    # его позицию как RECOVERED, не увидеть ещё не
                    # пропагировавшийся stopLoss и аварийно её закрыть — а
                    # enter_trade следом рапортовал успешный вход по позиции,
                    # которой на бирже уже нет.
                    continue
                lp_opt = live_map.get(sym)
                if lp_opt is None:
                    log.warning(f"{sym}: зависшая бронь входа, позиции на бирже нет — слот освобождён")
                    _forget_symbol(sym)
                    continue
                lp = lp_opt
                # Позиция всё-таки открылась. Берём под учёт: следующие ветки
                # этого же цикла проверят стоп и при необходимости починят его.
                try:
                    entry_px = float(lp.get("avgPrice") or lp.get("entryPrice") or 0)
                    pos = Position(
                        symbol=sym, side=lp.get("side", "Buy"), entry=entry_px,
                        sl=float(lp.get("stopLoss") or 0), tp1=0.0,
                        tp2=float(lp.get("takeProfit") or 0), tp3=0.0,
                        qty=abs(float(lp.get("size") or 0)),
                        qty_opened=abs(float(lp.get("size") or 0)), score=0,
                        signal_type="RECOVERED",
                    )
                    if not await db.save_trade_open(pos):
                        # Без строки в trades следующий рестарт опознает её как
                        # чужую. Под учёт не берём — повторим на след. тике,
                        # позиция остаётся видимой на бирже и в логе.
                        log.error(f"{sym}: восстановленную позицию не удалось "
                                  f"записать в trades — повтор на следующем тике")
                        continue
                    state.positions[sym] = pos
                    state.pending_entries.pop(sym, None)
                    log.warning(f"{sym}: позиция найдена на бирже после потери ответа — взята под учёт")
                except Exception as re_:
                    log.error(f"{sym}: не удалось оформить восстановленную позицию — {re_}")
                    continue
            if sym not in live_map:
                # Grace period: a position opened while THIS get_positions
                # request was in flight is missing from the (stale) snapshot —
                # don't mark it closed until it has had time to appear.
                age_s = (now_utc - pos.ts).total_seconds()
                if age_s < _MIN_POSITION_AGE_S:
                    log.debug(f"{sym}: {age_s:.0f}s old, absent from snapshot — grace period")
                    continue
                # Position closed by exchange (SL or TP hit)
                # attempts=4 с паузой: запись closed-pnl у Bybit регулярно
                # отстаёт на несколько секунд. Раньше двух быстрых попыток не
                # хватало, и убыток писался как pnl=0 — дневной лимит его не видел.
                await asyncio.sleep(1.0)
                if pos.signal_type == "MANUAL":
                    # Чужая сделка: не пишем в историю бота и не учитываем в
                    # дневном лимите — иначе прибыль ручного трейда могла бы
                    # "разморозить" сработавший предохранитель, а убыток —
                    # остановить автоторговлю без причины.
                    _forget_symbol(sym)
                    log.info(f"{sym}: ручная позиция закрыта — вне учёта бота")
                else:
                    if not pos.order_id:
                        # Восстановленная позиция могла не иметь строки в trades.
                        # Возврат проверяем обязательно: без строки
                        # save_trade_close вернёт CLOSE_ABSENT, а он означает
                        # «PnL учёл тот, кто закрыл» — здесь не учёл НИКТО, и
                        # убыток исчез бы молча. Повторим на следующем тике.
                        if not await db.save_trade_open(pos):
                            log.error(f"{sym}: строка сделки не записана — "
                                      f"учёт закрытия отложен")
                            continue
                    # _forget_symbol ТОЛЬКО после успешного учёта: иначе
                    # неподтверждённое закрытие теряет убыток навсегда, а
                    # неудачная запись даёт двойной счёт через блок простоя.
                    if not await _settle_closed_position(client, pos, attempts=4):
                        # Повтор ОГРАНИЧЕН по возрасту позиции. Запись
                        # closed-pnl может не появиться никогда: ордер не
                        # залился (IOC без встречной ликвидности), либо
                        # эндпоинт закрыт гео-блоком. Бесконечный повтор
                        # держал слот из MAX_POSITIONS вечно — три таких
                        # символа тихо выключают автоторговлю, — и каждый
                        # тик тратил секунды на четыре попытки.
                        age_h = (datetime.now(timezone.utc)
                                 - pos.ts.replace(tzinfo=timezone.utc)
                                 ).total_seconds() / 3600
                        if age_h >= db.SETTLE_GIVE_UP_HOURS:
                            await db.abandon_trade(sym, pos.order_id)
                            log.critical(
                                f"{sym}: закрытие не подтверждено биржей за "
                                f"{age_h:.0f} ч — слот освобождён, РЕЗУЛЬТАТ "
                                f"СДЕЛКИ НЕИЗВЕСТЕН и в статистику не войдёт")
                            _forget_symbol(sym)
                        continue
                    _forget_symbol(sym)
            else:
                lp = live_map[sym]
                pos.unrealised_pnl = float(lp.get("unrealisedPnl", 0))

                # Сверка размера: вход идёт IOC-ордером, который может залиться
                # частично, а ветка реконсиляции дубликата (110072) вообще не
                # видит отчёта о заливе. Закрытие по устаревшему qty биржа
                # отвергнет — и незащищённая позиция осталась бы висеть.
                live_size = abs(float(lp.get("size") or 0))
                if live_size > 0 and abs(live_size - pos.qty) / max(pos.qty, 1e-9) > 0.01:
                    log.warning(f"{sym}: размер позиции {pos.qty} → {live_size} (сверка с биржей)")
                    pos.qty = live_size

                # Сверка ЦЕНЫ ВХОДА с биржей. Раньше pos.entry обновлялся
                # только в ветке `verified and live_pos` внутри enter_trade,
                # а ветка «верифицировать не удалось» (гео-блок, 403)
                # штатная — и позиция жила с ценой СИГНАЛА вместо цены
                # залива. Последствия задевали две защиты сразу:
                #   * детектор потолка риска считал risk по цене сигнала и
                #     был слеп ровно в том случае, ради которого написан:
                #     при проскальзывании 1.5% реальные 4.5% риска
                #     выглядели как ровно 3.00% и CRITICAL не печатался;
                #   * перенос в безубыток брал fee и уровень от цены
                #     сигнала и ставил «безубыток» НИЖЕ фактического входа,
                #     то есть фиксировал гарантированный минус.
                # avgPrice с биржи — это факт, синхронизируем всегда.
                live_entry = float(lp.get("avgPrice") or 0)
                if (live_entry > 0 and pos.entry > 0
                        and abs(live_entry - pos.entry) / pos.entry > 0.0005):
                    log.warning(f"{sym}: цена входа {pos.entry} → {live_entry} "
                                f"(сверка с биржей, проскальзывание "
                                f"{(live_entry - pos.entry) / pos.entry * 100:+.2f}%)")
                    pos.entry = live_entry
                    # Строка в trades тоже обязана нести цену залива: иначе
                    # весь разбор в R по этой сделке смещён на проскальзывание.
                    try:
                        await db.update_trade_entry(pos.order_id, sym, live_entry)
                    except Exception as ue:
                        log.error(f"{sym}: не удалось записать цену входа — {ue}")

                # Потолок риска 3% проверялся ТОЛЬКО в enter_trade, и только
                # при удачной верификации. Дальше позиция жила без надзора:
                # если верификация не прошла (гео-блок, 403), сокращение
                # пропускалось, а строкой выше монитор ещё и принимает
                # увеличенный размер с биржи — например после ручной доливки.
                # Инвариант «риск 1-3%» при этом молча нарушался.
                #
                # Здесь только ОБНАРУЖЕНИЕ и громкий лог: автоматическое
                # сокращение позиции по возможно устаревшему балансу может
                # навредить сильнее самой проблемы, это решение владельца.
                if (state.balance > 0 and pos.entry > 0 and pos.sl > 0
                        and pos.signal_type != "MANUAL"):
                    real_risk = pos.qty * abs(pos.entry - pos.sl)
                    risk_pct = real_risk / state.balance * 100
                    if risk_pct > 3.0 and sym not in _RISK_WARNED:
                        _RISK_WARNED.add(sym)
                        log.critical(
                            f"{sym}: риск позиции {risk_pct:.2f}% превышает потолок 3% "
                            f"(qty={pos.qty}, стоп {abs(pos.entry - pos.sl):.6f}, "
                            f"баланс {state.balance:.2f}). Сократи вручную — "
                            f"автоматически не трогаю."
                        )

                # Continuous SL verification — the "monitor re-checks" that
                # enter_trade's unverified path relies on. A live position
                # without a stop-loss is the recurring bug #1 made real.
                exch_sl = float(lp.get("stopLoss") or 0)
                exch_tp = float(lp.get("takeProfit") or 0)

                # ── Перенос стопа в безубыток ──────────────────────────────
                # 14.7% сигналов доходят до +1R и возвращаются в стоп: сейчас
                # это полный убыток. Перенос превращает их в ~ноль и даёт
                # +0.146R на сделку БЕЗ какого-либо преимущества сетапа —
                # единственная правка с положительным ожиданием, не зависящая
                # от угадывания направления.
                if (cfg.BREAKEVEN_AT_R > 0 and exch_sl > 0 and not pos.breakeven_done
                        and pos.signal_type not in ("MANUAL",) and pos.entry > 0):
                    risk = abs(pos.entry - pos.sl)
                    mark = float(lp.get("markPrice") or lp.get("avgPrice") or 0)
                    if risk > 0 and mark > 0:
                        fav_r = ((mark - pos.entry) if pos.side == "Buy"
                                 else (pos.entry - mark)) / risk
                        if fav_r >= cfg.BREAKEVEN_AT_R:
                            fee = pos.entry * cfg.BREAKEVEN_FEE_PCT / 100
                            be = pos.entry + fee if pos.side == "Buy" else pos.entry - fee
                            # Квантование по сетке биржи — как и для входа.
                            # Некратная цена либо отвергается (10001) и шлётся
                            # каждые 30 секунд без счётчика попыток, либо
                            # принимается с округлением, и тогда сверка не
                            # сходится: в памяти остаётся старый стоп, а ветка
                            # восстановления потом ОТКАТЫВАЕТ уже поставленную
                            # защиту обратно на полный 1R.
                            _tick = 0.0
                            try:
                                _pf = (await client.get_instrument_info(sym)).get("priceFilter", {})
                                _tick = float(_pf.get("tickSize", "0") or 0)
                            except Exception:
                                pass
                            if _tick > 0:
                                be = _quantize(be, _tick, "up" if pos.side == "Buy" else "down")
                            # Переносим только ВПЕРЁД: если стоп уже выгоднее
                            # безубытка, трогать его нельзя — это ухудшило бы
                            # защиту уже прибыльной позиции.
                            better = be > exch_sl if pos.side == "Buy" else be < exch_sl
                            if better:
                                try:
                                    if await client.set_trading_stop(sym, sl=be):
                                        await asyncio.sleep(0.5)
                                        chk = await client.get_position(sym)
                                        # Подтверждаем чтением: retCode не
                                        # доказывает, что стоп на бирже.
                                        _tol = max(_tick, be * 1e-6)
                                        if chk and abs(float(chk.get("stopLoss") or 0) - be) <= _tol:
                                            pos.sl = be
                                            pos.breakeven_done = True
                                            exch_sl = be
                                            log.info(
                                                f"{sym}: +{fav_r:.2f}R — стоп перенесён "
                                                f"в безубыток {be:.8f}".rstrip('0')
                                            )
                                        else:
                                            log.warning(f"{sym}: перенос стопа в БУ не подтверждён — повтор на след. тике")
                                except Exception as be_e:
                                    log.warning(f"{sym}: перенос стопа в БУ не прошёл — {be_e}")

                if exch_sl > 0:
                    # Счётчик означает «N осечек ПОДРЯД», как и говорит лог
                    # «(1/3)». Без сброса он копился за всю жизнь позиции:
                    # Bybit периодически отдаёт пустой stopLoss в снимке, хотя
                    # стоп стоит, и три такие осечки за 6 часов приводили к
                    # аварийному закрытию прибыльной ЗАЩИЩЁННОЙ позиции.
                    _SL_RETRIES.pop(sym, None)
                # Тейк отсутствует, но стоп есть и позиция наша — досылаем.
                # Отсутствие TP не опасно (риск закрыт стопом), поэтому это
                # тихая починка, без аварийного закрытия.
                if (exch_sl > 0 and exch_tp <= 0 and pos.tp2 > 0
                        and pos.signal_type != "MANUAL"
                        and _TP_RETRIES.get(sym, 0) < 5):
                    try:
                        if await client.set_trading_stop(sym, tp=pos.tp2):
                            log.info(f"{sym}: takeProfit восстановлен на {pos.tp2:.4f}")
                            _TP_RETRIES.pop(sym, None)
                        else:
                            # Без счётчика биржа отвергала бы запрос на каждом
                            # тике часами (например, TP по неверной стороне),
                            # молча съедая лимит запросов.
                            _TP_RETRIES[sym] = _TP_RETRIES.get(sym, 0) + 1
                            if _TP_RETRIES[sym] >= 5:
                                log.error(f"{sym}: TP не принимается биржей (5 попыток) — "
                                          f"позиция работает без тейка, риск закрыт стопом")
                    except Exception as te:
                        _TP_RETRIES[sym] = _TP_RETRIES.get(sym, 0) + 1
                        log.warning(f"{sym}: не удалось восстановить TP — {te}")

                if exch_sl <= 0 and pos.signal_type == "MANUAL":
                    # Позиция открыта НЕ ботом (усыновлена с биржи — возможно,
                    # твоя ручная сделка). Мониторим и учитываем в статистике,
                    # но НИКОГДА не трогаем: ни довешивание стопа, ни закрытие.
                    log.warning(f"{sym}: ручная позиция без SL — не вмешиваюсь (открыта не ботом)")
                elif exch_sl <= 0:
                    log.error(f"{sym}: live position has NO stop-loss — re-attaching SL")
                    reattached = False
                    if pos.sl <= 0:
                        # Известного стопа нет (позиция восстановлена из строки
                        # без sl). Отправлять set_trading_stop бессмысленно:
                        # запрос уйдёт без поля stopLoss и вернёт "успех".
                        log.critical(f"{sym}: неизвестен уровень стопа — аварийное закрытие")
                        _SL_RETRIES[sym] = _MAX_SL_RETRIES
                    else:
                        try:
                            # ТОЛЬКО стоп, без tp2. Bybit валидирует запрос
                            # целиком: невалидный takeProfit (цена уже прошла
                            # его после рестарта) отклонял ВЕСЬ запрос, и
                            # здоровая прибыльная позиция шла в аварийное
                            # закрытие. TP чинит отдельный блок выше.
                            ok = await client.set_trading_stop(sym, sl=pos.sl)
                            if ok:
                                # retCode==0 НЕ доказывает, что стоп на бирже
                                # (рецидивирующий баг №1): при tpslMode=Partial
                                # запрос уровня позиции принимается, а поле
                                # stopLoss остаётся пустым. Подтверждаем чтением.
                                await asyncio.sleep(0.5)
                                chk = await client.get_position(sym)
                                if chk is None:
                                    # Состояние неизвестно — не эскалируем,
                                    # повторим на следующем тике.
                                    log.warning(f"{sym}: не удалось подтвердить SL — повтор на следующем тике")
                                    continue
                                if not chk:
                                    continue  # позиции уже нет — закрытие поймает следующий тик
                                reattached = float(chk.get("stopLoss") or 0) > 0
                                if not reattached:
                                    log.error(f"{sym}: биржа приняла set_trading_stop, но stopLoss пуст")
                        except Exception as se:
                            log.error(f"{sym}: set_trading_stop raised — {se}")
                    if reattached:
                        log.info(f"{sym}: SL восстановлен на {pos.sl:.6f} (подтверждён биржей)")
                        _SL_RETRIES.pop(sym, None)
                    else:
                        _SL_RETRIES[sym] = _SL_RETRIES.get(sym, 0) + 1
                        if _SL_RETRIES[sym] < _MAX_SL_RETRIES:
                            log.error(
                                f"{sym}: стоп не подтверждён "
                                f"({_SL_RETRIES[sym]}/{_MAX_SL_RETRIES}) — повтор на следующем тике"
                            )
                            continue
                        log.critical(f"{sym}: could not re-attach SL — emergency closing")
                        closed, remaining = await close_and_verify(
                            client, sym, pos.side, pos.qty)
                        if closed:
                            # attempts=4, как в штатном пути: двух попыток
                            # (~1.5 с) не хватало на лаг closed-pnl у Bybit,
                            # и аварийное закрытие писалось нулём.
                            if not await _settle_closed_position(client, pos, attempts=4):
                                # Позиция на бирже уже закрыта, но учёт не
                                # прошёл. Символ НЕ забываем: на следующем
                                # тике её подберёт ветка «отсутствует в
                                # снимке» и повторит учёт.
                                continue
                            _forget_symbol(sym)
                            log.info(f"{sym}: emergency-closed (no SL)")
                        else:
                            # Учёт НЕ закрываем: позиция (или её остаток) жива
                            # на бирже. Закрыв строку в trades, мы бы на
                            # следующем тике усыновили остаток как MANUAL и
                            # больше никогда его не защитили.
                            if remaining > 0:
                                pos.qty = remaining
                                _SL_RETRIES[sym] = 0  # остаток — новая цель, даём полный круг попыток
                            log.critical(f"{sym}: аварийное закрытие не завершено — повтор на следующем тике")

        # Сделки, закрывшиеся ПОКА ПРОЦЕСС ЛЕЖАЛ. Оба цикла выше идут от
        # того, что видно СЕЙЧАС: orphans — по бирже, основной — по памяти.
        # Позиция, чей стоп сработал во время рестарта, не попадает ни туда,
        # ни туда: на бирже её уже нет, в памяти ещё нет. Строка в trades
        # оставалась 'open' с pnl=NULL, а get_realized_pnl_since суммирует
        # только 'closed' — то есть убыток НИКОГДА не входил в дневной
        # предохранитель, а через сутки строка становилась 'stale' и
        # терялась насовсем. При деплое после каждого пуша это штатная
        # ситуация, а не экзотика.
        try:
            # get_unsettled_trades, а не get_open_trades: строку мог
            # пометить 'stale' сторож зависших в ЭТОМ ЖЕ тике, и обещанный
            # «повтор на следующем тике» не случался никогда.
            for row in await db.get_unsettled_trades():
                sym_r = row["symbol"]
                if sym_r in live_map or sym_r in state.positions:
                    continue
                opened = row.get("opened_at") or ""
                try:
                    op_ts = datetime.fromisoformat(str(opened).rstrip("Z"))
                except (ValueError, TypeError):
                    continue
                # Грейс-период: строка могла быть записана секунду назад
                # входом, чья позиция ещё не появилась в снимке биржи.
                if (now_utc - op_ts).total_seconds() < _MIN_POSITION_AGE_S:
                    continue
                ghost = Position(
                    symbol=sym_r, side=row["side"], entry=row["entry"] or 0.0,
                    sl=row["sl"] or 0.0, tp1=row["tp1"] or 0.0,
                    tp2=row["tp2"] or 0.0, tp3=row["tp3"] or 0.0,
                    qty=row["qty"] or 0.0, qty_opened=row["qty"] or 0.0,
                    score=row["score"] or 0,
                    signal_type=row["signal_type"] or "RESTORED",
                    order_id=row["order_id"] or "",
                )
                ghost.ts = op_ts
                exit_price, pnl = await fetch_matching_closed_pnl(
                    client, ghost, attempts=4)
                if exit_price <= 0 and pnl == 0.0:
                    # Запись closed-pnl не найдена (гео-блок, 403, лаг
                    # биржи). Писать (0,0) НЕЛЬЗЯ: строка перестанет быть
                    # open, следующий тик её не увидит, и убыток потеряется
                    # НАВСЕГДА — ровно то, что этот блок и должен был
                    # починить. Оставляем как есть, повторим на след. тике.
                    # Повтор ОГРАНИЧЕН: запись closed-pnl может не
                    # появиться никогда (ордер не залился, гео-блок
                    # эндпоинта). Бесконечный повтор держал бы слот вечно.
                    age_h = (now_utc - op_ts).total_seconds() / 3600
                    if age_h >= db.SETTLE_GIVE_UP_HOURS:
                        if await db.abandon_trade(sym_r, row["order_id"] or ""):
                            log.critical(
                                f"{sym_r}: закрытие не подтверждено биржей за "
                                f"{age_h:.0f} ч — строка помечена abandoned, "
                                f"РЕЗУЛЬТАТ СДЕЛКИ НЕИЗВЕСТЕН и в статистику "
                                f"не войдёт")
                        continue
                    log.warning(f"{sym_r}: закрытие во время простоя не подтверждено "
                                f"биржей — повтор на следующем тике "
                                f"(прошло {age_h:.1f} ч из {db.SETTLE_GIVE_UP_HOURS})")
                    continue
                # PnL идёт в предохранитель ТОЛЬКО после успешной записи:
                # save_trade_close глушит свои исключения, и при
                # заблокированной базе один и тот же убыток капал в
                # счётчик каждые 30 секунд (три тика — тройной убыток).
                outcome_r = await db.save_trade_close(ghost, exit_price=exit_price, pnl=pnl)
                if outcome_r != db.CLOSE_OK:
                    # CLOSE_ABSENT: строку уже закрыл штатный путь, PnL им и
                    # учтён — повторный record_realized_close дал бы ровно
                    # тот двойной счёт, ради которого охрана и стоит.
                    # Всё остальное — запись не удалась, повторим на след.
                    # тике. Проверка положительная: неизвестное значение
                    # обязано означать «не учтено», а не «учтено».
                    log.error(f"{sym_r}: закрытие не записано ({outcome_r!r}) — "
                              f"PnL не учтён")
                    continue
                record_realized_close(pnl)
                log.warning(
                    f"{sym_r}: сделка закрылась во время простоя — "
                    f"учтена задним числом, pnl={pnl:+.2f}"
                )
        except Exception as ge:
            log.error(f"реконсиляция закрытых во время простоя: {ge}")

        # Реконсиляция "зависших" open-строк — ПОСЛЕ обработки закрытий:
        # если сделать раньше, строка помечается stale, и последующий
        # save_trade_close (WHERE status='open') не находит её, теряя PnL.
        try:
            await db.close_stale_open_trades(list(live_map.keys()))
        except Exception as re_:
            log.warning(f"reconcile stale trades: {re_}")

    except Exception as e:
        log.error(f"monitor_positions error: {e}")
    finally:
        _MONITORING = False
