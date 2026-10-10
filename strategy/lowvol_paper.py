"""Бумажная стратегия «низкая волатильность» — форвард-тест замера X.

Правило проверено на истории ОДИН раз — на holdout (docs/PREREGISTRATION.md,
замер X): +0.975%/нед, альфа к BTC +1.07%/нед, интервал включал ноль. Цифра
+0.89%/нед из замера IX относится к многофакторной модели, а не к этому
правилу. Чистых данных для второй проверки нет, поэтому правило проверяется
ВПЕРЁД, на бумаге, без единого ордера. Планка и горизонт — в
docs/PREREGISTRATION.md, «Форвард-тест замера X».

Правило — в точности то, что проверялось (tools/lowvol.py поверх
tools/xsec_universe.universe_at). Любое отличие сделало бы форвард-тест
проверкой ДРУГОЙ стратегии, поэтому совпадение закреплено тестами:
  * допуск монеты на дату t: >= 15 дней с оборотом в окне 30 дней, есть цена
    на t−14д, последний закрытый бар не старше 2 суток (ts — начало бара);
  * вселенная: топ-60 допущенных по медиане дневного оборота в USDT;
  * ПОСЛЕ отсечки топ-60 отбрасываются монеты без 20-дневной волатильности
    (21 закрытый бар) — поэтому в ноге бывает 11, а не 12;
  * лонг — нижние 20% по волатильности, шорт — верхние 20%, равные веса;
  * вход — закрытие бара на понедельник 00:00 UTC; выход — закрытие бара
    РОВНО через 7 суток, когда бы ни выполнялся расчёт;
  * делистинг внутри недели: выход по последней цене после входа;
  * издержки: комиссия круга + фактический фандинг за эти 7 суток;
  * неделя с числом оценённых позиций меньше 6 не засчитывается.
"""
import asyncio
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core import db

log = logging.getLogger("lowvol")

DAY_MS = 24 * 3600 * 1000
WEEK_MS = 7 * DAY_MS
UNIVERSE_N = 60
PRESELECT_N = 150          # запросов свечей в неделю; с запасом над 60
TOP_FRACTION = 0.20
MIN_NAMES_PER_LEG = 3
MIN_POSITIONS = MIN_NAMES_PER_LEG * 2   # как tools/multifactor.portfolio_week
VOL_DAYS = 20
LIQ_DAYS = 30
LIQ_MIN_DAYS = LIQ_DAYS // 2            # как xsec_universe.liquidity
LOOKBACK_DAYS = 14                      # как xsec_universe.universe_at
FRESH_DAYS = 2                          # как xsec_universe._MAX_STALE_DAYS
MARKET = "BTCUSDT"
# Бар выхода закрывается ровно в ws + 7 суток; 10 минут — на публикацию.
CLOSE_DELAY_MS = 10 * 60 * 1000
# Сколько ждать данные выхода, прежде чем закрыть неделю по тому, что есть.
# Отказ запроса и пустой ответ неотличимы, поэтому сначала — повторы.
CLOSE_GIVE_UP_MS = 3 * DAY_MS

# Первая неделя по ИСПРАВЛЕННОМУ правилу (ревью 2026-10-06). Недели 28.09 и
# 05.10 открыты прежним отбором, расходившимся с проверенным на истории:
# требование 31 дня выкидывало свежие листинги (на истории — 10% шорта), и
# порядок отсечки был другим. Это была другая стратегия, поэтому в
# форвард-тест они не входят. Данные не стираются — только не засчитываются.
FORWARD_START_MS = 1791763200000          # понедельник 2026-10-12 00:00 UTC

_REBALANCING = False


def closed_bars(klines: List[Dict], now_ms: int) -> List[Dict]:
    """Дневные бары, ЗАКРЫТЫЕ к now_ms. Формирующийся бар содержит будущее."""
    return [k for k in klines if k["ts"] + DAY_MS <= now_ms]


def monday_of(now_ms: int) -> int:
    """Понедельник 00:00 UTC недели, в которую попадает now_ms."""
    days = now_ms // DAY_MS
    # 1970-01-01 — четверг; (days + 3) % 7 — номер дня от понедельника
    return (days - (days + 3) % 7) * DAY_MS


def realized_vol(closed: List[Dict]) -> Optional[float]:
    """Стандартное отклонение лог-доходностей за 20 дней (21 закрытый бар)."""
    w = closed[-(VOL_DAYS + 1):]
    if len(w) < VOL_DAYS + 1:
        return None
    rets = []
    for prev, cur in zip(w, w[1:]):
        if prev["close"] <= 0 or cur["close"] <= 0:
            return None
        rets.append(math.log(cur["close"] / prev["close"]))
    return statistics.stdev(rets)


def liquidity(closed: List[Dict], now_ms: int) -> Optional[float]:
    """Медиана оборота за 30 дней ДО now_ms; нужно >= 15 дней с оборотом."""
    lo = now_ms - LIQ_DAYS * DAY_MS
    vals = [k["turnover"] for k in closed
            if k["ts"] >= lo and k.get("turnover", 0) > 0]
    if len(vals) < LIQ_MIN_DAYS:
        return None
    return statistics.median(vals)


def eligible(closed: List[Dict], now_ms: int) -> bool:
    """Допуск во вселенную — те же условия, что xsec_universe.universe_at."""
    if not closed or closed[-1]["close"] <= 0:
        return False
    if now_ms - closed[-1]["ts"] > FRESH_DAYS * DAY_MS:
        return False                      # цена протухла — купить нельзя
    # Цена 14 дней назад. При дневных барах это условие следует из
    # требования 15 дней оборота в окне 30 дней (15 баров не помещаются в
    # последние 14 суток), поэтому мутацией его не поймать — оно оставлено
    # ради буквального совпадения с xsec_universe.universe_at.
    back = now_ms - LOOKBACK_DAYS * DAY_MS
    return any(k["ts"] + DAY_MS <= back for k in closed)


def pick_sides(cands: Dict[str, Tuple[Optional[float], float]]) -> Dict[str, int]:
    """cands: {символ: (волатильность или None, ликвидность)}.

    Порядок как на истории: СНАЧАЛА топ-60 по ликвидности, ПОТОМ отсев
    монет без волатильности. Обратный порядок добирал бы вселенную до 60
    следующими по ликвидности и отбирал бы не те монеты."""
    liquid = sorted(cands.items(), key=lambda x: x[1][1], reverse=True)[:UNIVERSE_N]
    with_vol = [(s, v[0]) for s, v in liquid if v[0] is not None]
    if len(with_vol) < MIN_POSITIONS:
        return {}
    with_vol.sort(key=lambda x: x[1])
    k = max(MIN_NAMES_PER_LEG, int(len(with_vol) * TOP_FRACTION))
    if k * 2 > len(with_vol):
        return {}
    out = {s: 1 for s, _ in with_vol[:k]}
    out.update({s: -1 for s, _ in with_vol[-k:]})
    return out


def select(bars: Dict[str, List[Dict]], ws: int) -> Tuple[Dict[str, int], Dict[str, float]]:
    """Отбор недели ws по дневным барам: стороны и цены входа.

    Решение принимается ТОЛЬКО по барам, закрытым к понедельнику 00:00,
    даже если расчёт идёт днём: бары понедельника — уже будущее. Чистая
    функция — чтобы её можно было сверить с проверенным на истории
    tools/lowvol.sides_at на одних и тех же данных."""
    cands: Dict[str, Tuple[Optional[float], float]] = {}
    entries: Dict[str, float] = {}
    for sym, raw in bars.items():
        closed = closed_bars(raw, ws)
        if not eligible(closed, ws):
            continue
        liq = liquidity(closed, ws)
        if liq is None:
            continue
        cands[sym] = (realized_vol(closed), liq)
        entries[sym] = closed[-1]["close"]
    return pick_sides(cands), entries


def leg_return(side: int, entry: float, exit_px: float, funding: float) -> float:
    """Нетто-доходность позиции: лонг платит положительный фандинг, шорт
    получает; комиссия круга — всегда."""
    return side * (exit_px / entry - 1.0) - side * funding - db.ROUND_TRIP_FEE_PCT / 100.0


def exit_price(closed: List[Dict], ws: int) -> Optional[Tuple[float, int]]:
    """Цена выхода и её момент: закрытие бара в ws + 7 суток.

    Делистинг внутри недели — выход по последней цене ПОСЛЕ входа, как
    xsec_universe._exit_price. Бары позже ws + 7 суток не берутся никогда:
    поздний расчёт не имеет права удлинить удержание."""
    end = ws + WEEK_MS
    window = [k for k in closed if ws <= k["ts"] + DAY_MS <= end]
    if not window:
        return None
    last = window[-1]
    return last["close"], last["ts"] + DAY_MS


async def _daily(client, symbol: str, limit: int) -> List[Dict]:
    try:
        return await client.get_klines(symbol, interval="D", limit=limit) or []
    except Exception as e:
        log.warning(f"{symbol}: дневные свечи недоступны — {e}")
        return []


async def _close_week(client, week: Dict, now_ms: int) -> bool:
    """Закрыть неделю по барам ровно на ws + 7 суток. True — закрыта в базе.

    Пустой ответ биржи НЕ считается делистингом: клиент возвращает [] и при
    сетевом сбое, и при лимите, и эти случаи неотличимы. До CLOSE_GIVE_UP_MS
    любая недостающая цена или пустая история фандинга — повод повторить
    через час, а не записать неверное число безвозвратно."""
    ws = int(week["week_start"])
    give_up = now_ms >= ws + WEEK_MS + CLOSE_GIVE_UP_MS
    need = max(10, int((now_ms - ws) // DAY_MS) + 3)
    results = []
    for lg in week["legs"]:
        sym = lg["symbol"]
        bars = await _daily(client, sym, need)
        if not bars and not give_up:
            log.warning(f"lowvol: {sym} — свечи не получены, закрытие недели {ws} "
                        f"отложено до следующего часа")
            return False
        px = exit_price(closed_bars(bars, now_ms), ws)
        if px is None and bars:
            # Бары есть, но ни одного в окне недели: монета исчезла сразу
            # после понедельника — выход по цене входа (как на истории).
            # Это факт, а не сбой, поэтому от срока ожидания не зависит.
            px = (float(lg["entry"]), ws)
        if px is None:
            log.error(f"lowvol: {sym} — нет цены выхода и после ожидания, "
                      f"позиция исключена")
            results.append({"symbol": sym})
            continue
        exit_px, exit_ts = px
        try:
            fh = await client.get_funding_history(sym, ws, exit_ts)
        except Exception as e:
            log.warning(f"lowvol: {sym} — история фандинга недоступна ({e})")
            fh = []
        events = [r for r in fh if ws < r["ts"] <= exit_ts]
        if not events and exit_ts > ws and not give_up:
            # За неделю у перпетуала не бывает ни одной выплаты — значит,
            # ответ пустой из-за сбоя, а не из-за отсутствия фандинга.
            log.warning(f"lowvol: {sym} — история фандинга пуста, закрытие "
                        f"недели {ws} отложено")
            return False
        funding = sum(r["rate"] for r in events)
        results.append({"symbol": sym, "exit": exit_px, "funding": funding,
                        "ret": leg_return(int(lg["side"]), float(lg["entry"]),
                                          exit_px, funding)})
        await asyncio.sleep(0.1)

    priced = [r for r in results if r.get("ret") is not None]
    void = len(priced) < MIN_POSITIONS
    if void:
        log.error(f"lowvol: неделя {ws} — оценено {len(priced)} позиций из "
                  f"{len(results)}, меньше {MIN_POSITIONS}: неделя не засчитывается")
    btc = exit_price(closed_bars(await _daily(client, MARKET, need), now_ms), ws)
    if not await db.lowvol_close_week(ws, results, btc[0] if btc else None,
                                      void=void):
        return False
    if priced and not void:
        week_ret = sum(r['ret'] for r in priced) / len(priced)
        log.info(f"lowvol: неделя {ws} закрыта, {len(priced)} позиций, итог "
                 f"{week_ret * 100:+.3f}%")
        try:
            from notifications.notify import broadcast
            counted = ws >= FORWARD_START_MS
            await broadcast("📊 Бумажная «низкая вола»: неделя закрыта",
                            f"итог недели {week_ret * 100:+.2f}% ({len(priced)} поз.)"
                            + ("" if counted else " — старое правило, не засчитывается"),
                            tag="lowvol-week")
        except Exception as e:
            log.warning(f"lowvol: уведомление не отправлено — {e}")
    return True


async def _open_week(client, now_ms: int, ws: int) -> None:
    """Открыть неделю ws (понедельник 00:00): вход по барам, закрытым к ws."""
    tickers = await client.get_tickers() or []
    usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
    if not usdt:
        log.warning(f"lowvol: тикеры не получены — неделя {ws} откроется "
                    f"в следующий час")
        return
    bars: Dict[str, List[Dict]] = {}
    failed = 0
    for t in usdt[:PRESELECT_N]:
        sym = t["symbol"]
        bars[sym] = await _daily(client, sym, LIQ_DAYS + 10)
        failed += 0 if bars[sym] else 1
        await asyncio.sleep(0.1)
    # Пустой ответ — это сбой, а не «монеты нет»: у монеты из списка тикеров
    # свечи есть всегда. Если сбоев много, вселенная получилась бы случайно
    # урезанной — отбор переносится на следующий час (неделя по-прежнему
    # стартует от бара на понедельник 00:00).
    if failed > len(bars) // 10:
        log.warning(f"lowvol: свечи не получены у {failed} из {len(bars)} — "
                    f"открытие недели {ws} отложено на час")
        return
    sides, entries = select(bars, ws)
    if not sides:
        log.error(f"lowvol: неделя {ws} не собрана — допущено {len(entries)}")
        return
    btc = closed_bars(await _daily(client, MARKET, 10), ws)
    legs = [{"symbol": s, "side": v, "entry": entries[s]} for s, v in sides.items()]
    if await db.lowvol_open_week(ws, legs, btc[-1]["close"] if btc else None):
        log.info(f"lowvol: открыта неделя {ws}: "
                 f"{sum(1 for v in sides.values() if v > 0)} лонг / "
                 f"{sum(1 for v in sides.values() if v < 0)} шорт")


async def rebalance(client, now_ms: Optional[int] = None) -> None:
    """Ежечасный шаг. Закрывает КАЖДУЮ открытую неделю, чей бар выхода уже
    закрыт; по понедельникам открывает неделю этого понедельника, если её
    ещё нет. Повторный запуск безопасен — это и есть механизм повтора."""
    global _REBALANCING
    if _REBALANCING:
        return
    _REBALANCING = True
    try:
        now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        for week in await db.lowvol_open_weeks():
            if now_ms >= int(week["week_start"]) + WEEK_MS + CLOSE_DELAY_MS:
                await _close_week(client, week, now_ms)
        ws = monday_of(now_ms)
        # Открываем только в понедельник: неделя, открытая во вторник,
        # стартовала бы не от понедельника, а решение по барам на ws
        # принималось бы, когда цены уже ушли.
        if now_ms - ws < DAY_MS and not await db.lowvol_week_exists(ws):
            await _open_week(client, now_ms, ws)
    except Exception as e:
        log.error(f"lowvol: ребалансировка не удалась — {e}")
    finally:
        _REBALANCING = False


def _alpha(ys: List[float], xs: List[float]) -> Optional[Dict]:
    """МНК y = a + b·x: альфа к рынку и её стандартная ошибка."""
    n = len(ys)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    s2 = sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / (n - 2)
    se = math.sqrt(s2 * (1.0 / n + mx * mx / sxx))
    return {"alpha": a, "beta": b, "ci_lo": a - 1.96 * se, "ci_hi": a + 1.96 * se}


def summarize(weeks: List[Dict]) -> Dict:
    """Сводка засчитанных недель: средняя, t, CI95 и альфа к BTC.

    Критерий — альфа (урок замера VII): портфель «лонг спокойных, шорт
    бурных» на падающем рынке зарабатывает бетой."""
    counted = [w for w in weeks if int(w.get("week_start") or 0) >= FORWARD_START_MS]
    done = [w for w in counted if w.get("ret") is not None and not w.get("void")]
    rets = [w["ret"] for w in done]
    out: Dict = {"weeks": len(rets),
                 "void": sum(1 for w in counted if w.get("void")),
                 "pre_rule": len(weeks) - len(counted)}
    if len(rets) < 2:
        return out
    mean, sd = statistics.fmean(rets), statistics.stdev(rets)
    se = sd / math.sqrt(len(rets))
    out.update({"mean": mean, "t": mean / se if se > 0 else 0.0,
                "ci_lo": mean - 1.96 * se, "ci_hi": mean + 1.96 * se,
                "positive": sum(1 for r in rets if r > 0)})
    pairs = [(w["ret"], w["btc_exit"] / w["btc_entry"] - 1.0) for w in done
             if w.get("btc_entry") and w.get("btc_exit")]
    if len(pairs) >= 3:
        out["alpha"] = _alpha([p[0] for p in pairs], [p[1] for p in pairs])
    return out
