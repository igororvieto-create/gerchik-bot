import asyncio
import logging
import math
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


def _forget_symbol(symbol: str) -> None:
    """Единая точка снятия символа с учёта — счётчики попыток обязаны
    умирать вместе с позицией, иначе следующая сделка по тому же символу
    стартует с уже исчерпанным лимитом восстановления SL/TP."""
    state.positions.pop(symbol, None)
    state.pending_entries.pop(symbol, None)
    _TP_RETRIES.pop(symbol, None)
    _SL_RETRIES.pop(symbol, None)


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
            log.error(f"не удалось восстановить дневной PnL ({e}) — "
                      f"торговля приостановлена до успешного чтения")
            return
        state.daily_realized_pnl = pnl
        state.trading_halted = False
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
    for i in range(attempts):
        if i:
            await asyncio.sleep(delay)
        try:
            closed = await client.get_closed_pnl(pos.symbol, limit=5)
            for rec in closed:
                rec_ms = float(rec.get("updatedTime") or rec.get("createdTime") or 0)
                if rec_ms >= opened_ms - 60_000:  # 1min slack for clock skew
                    return float(rec.get("avgExitPrice", 0)), float(rec.get("closedPnl", 0))
        except Exception as ce:
            log.warning(f"{pos.symbol}: could not fetch closed PnL (attempt {i+1}) — {ce}")
    return 0.0, 0.0


async def _record_round_trip(client: BybitClient, pos: Position,
                             attempts: int = 3) -> None:
    """Persist an open+close round trip (used by emergency close) and feed
    the realized PnL into the circuit breaker — fees/slippage of an aborted
    entry are real losses and must not vanish from the books."""
    try:
        await db.save_trade_open(pos)
    except Exception as e:
        log.error(f"{pos.symbol}: save_trade_open (round-trip) failed — {e}")
    exit_price, pnl = await fetch_matching_closed_pnl(client, pos, attempts=attempts)
    try:
        await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
    except Exception as e:
        log.error(f"{pos.symbol}: save_trade_close (round-trip) failed — {e}")
    record_realized_close(pnl)


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

        # Position size: risk RISK_PER_TRADE% of balance
        risk_usdt     = balance * cfg.RISK_PER_TRADE / 100
        position_usdt = risk_usdt / (sig.sl_pct / 100)  # notional value

        # Cap by max margin: never use more than MAX_MARGIN_PCT% of balance as margin
        max_notional_margin = balance * cfg.MAX_MARGIN_PCT / 100 * cfg.LEVERAGE
        # Also cap at balance × leverage to avoid margin rejection
        max_notional = min(balance * cfg.LEVERAGE, max_notional_margin)
        if position_usdt > max_notional:
            position_usdt = max_notional
            log.debug(f"{sig.symbol}: notional capped to {max_notional:.2f}")

        # Qty precision from instrument info
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

        qty = _round_step(position_usdt / sig.entry, qty_step)
        if qty < min_qty:
            qty = min_qty
        # Проверяем ФАКТИЧЕСКИЙ риск после округления, а не только случай
        # qty < min_qty: _round_step округляет к БЛИЖАЙШЕМУ шагу, поэтому
        # qty 0.51 при шаге 1 превращалась в 1.0 (риск ×1.96) и проверка
        # min_qty не срабатывала вовсе — потолок риска обходился молча.
        actual_risk = qty * sig.entry * sig.sl_pct / 100
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

        if qty * sig.entry < 5.0:
            log.debug(f"{sig.symbol}: notional {qty*sig.entry:.2f} < 5 USDT min")
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
            symbol=sig.symbol, side=side, qty=qty, sl=sig.sl, tp=sig.tp2,
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
            entry=sig.entry, sl=sig.sl,
            tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3,
            qty=qty, score=sig.score,
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
        try:
            await db.save_trade_open(pos)
        except Exception as dbe:
            log.error(f"{sig.symbol}: save_trade_open failed — {dbe}")

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
            live_pos = await client.get_position(sig.symbol)
            if not live_pos:
                continue  # None = сбой API, {} = ещё не появилась — обе ждут ретрая
            exch_sl = float(live_pos.get("stopLoss") or 0)
            exch_tp = float(live_pos.get("takeProfit") or 0)
            verified = True
            if exch_sl > 0 and exch_tp > 0:
                break

        if verified and live_pos:
            # Инвариант «риск ≤3% баланса» проверялся по sig.entry, а вход идёт
            # ПО РЫНКУ: при проскальзывании реальное расстояние до стопа больше
            # расчётного, и потолок пробивался незамеченным (риск 3% + 0.8%
            # проскальзывания = 4.6% баланса). Считаем по фактической цене.
            fill_px  = float(live_pos.get("avgPrice") or 0)
            fill_qty = abs(float(live_pos.get("size") or 0))
            if fill_px > 0 and fill_qty > 0 and sig.sl > 0:
                real_risk = fill_qty * abs(fill_px - sig.sl)
                if real_risk > balance * 3.0 / 100:
                    allowed_qty = _round_step(
                        (balance * 3.0 / 100) / abs(fill_px - sig.sl), qty_step)
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
                                    left_risk = qty * abs(fill_px - sig.sl)
                                    if left_risk > balance * 3.0 / 100:
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
                tp_ok = await client.set_trading_stop(sig.symbol, tp=sig.tp2)
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

        log.info(
            f"Opened {side} {sig.symbol} qty={qty} "
            f"entry≈{sig.entry:.4f} SL={sig.sl:.4f} TP={sig.tp2:.4f} "
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
                            qty=size, score=row["score"] or 0,
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
                            qty=size, score=0, signal_type="MANUAL",
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
                lp = live_map.get(sym)
                if lp is None:
                    log.warning(f"{sym}: зависшая бронь входа, позиции на бирже нет — слот освобождён")
                    _forget_symbol(sym)
                    continue
                # Позиция всё-таки открылась. Берём под учёт: следующие ветки
                # этого же цикла проверят стоп и при необходимости починят его.
                try:
                    entry_px = float(lp.get("avgPrice") or lp.get("entryPrice") or 0)
                    pos = Position(
                        symbol=sym, side=lp.get("side", "Buy"), entry=entry_px,
                        sl=float(lp.get("stopLoss") or 0), tp1=0.0,
                        tp2=float(lp.get("takeProfit") or 0), tp3=0.0,
                        qty=abs(float(lp.get("size") or 0)), score=0,
                        signal_type="RECOVERED",
                    )
                    state.positions[sym] = pos
                    state.pending_entries.pop(sym, None)
                    await db.save_trade_open(pos)
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
                exit_price, pnl = await fetch_matching_closed_pnl(client, pos, attempts=4)
                if pos.signal_type == "MANUAL":
                    # Чужая сделка: не пишем в историю бота и не учитываем в
                    # дневном лимите — иначе прибыль ручного трейда могла бы
                    # "разморозить" сработавший предохранитель, а убыток —
                    # остановить автоторговлю без причины.
                    _forget_symbol(sym)
                    log.info(f"{sym}: ручная позиция закрыта (pnl={pnl:+.2f}) — вне учёта бота")
                else:
                    if not pos.order_id:
                        # Восстановленная позиция могла не иметь строки в trades
                        await db.save_trade_open(pos)
                    await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
                    _forget_symbol(sym)
                    log.info(f"{sym}: closed (SL/TP) exit={exit_price:.4f} pnl={pnl:+.2f}")
                    record_realized_close(pnl)
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

                # Continuous SL verification — the "monitor re-checks" that
                # enter_trade's unverified path relies on. A live position
                # without a stop-loss is the recurring bug #1 made real.
                exch_sl = float(lp.get("stopLoss") or 0)
                exch_tp = float(lp.get("takeProfit") or 0)
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
                            exit_price, pnl = await fetch_matching_closed_pnl(
                                client, pos, attempts=2)
                            await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
                            record_realized_close(pnl)
                            _forget_symbol(sym)
                            log.info(f"{sym}: emergency-closed (no SL) pnl={pnl:+.2f}")
                        else:
                            # Учёт НЕ закрываем: позиция (или её остаток) жива
                            # на бирже. Закрыв строку в trades, мы бы на
                            # следующем тике усыновили остаток как MANUAL и
                            # больше никогда его не защитили.
                            if remaining > 0:
                                pos.qty = remaining
                                _SL_RETRIES[sym] = 0  # остаток — новая цель, даём полный круг попыток
                            log.critical(f"{sym}: аварийное закрытие не завершено — повтор на следующем тике")

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
