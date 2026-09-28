"""Бумажная стратегия «низкая волатильность» — форвард-тест замера X.

На истории эффект дважды показал один знак (explore +0.89%/нед, holdout
+0.98%/нед), но интервал включал ноль. Чистых данных для проверки больше
нет, поэтому эффект проверяется ВПЕРЁД, на бумаге, без единого ордера.

Правило — в точности то, что проверялось (docs/PREREGISTRATION.md, замер X):
  * вселенная: топ-60 по медиане дневного оборота в USDT за 30 дней;
  * волатильность: стандартное отклонение дневных лог-доходностей за 20
    закрытых дней;
  * лонг — нижние 20% по волатильности, шорт — верхние 20%, равные веса;
  * вход и выход — по закрытию дневного бара на понедельник 00:00 UTC;
  * издержки: комиссия круга + фактический фандинг за неделю удержания.
Любое отличие от проверенного правила сделало бы форвард-тест проверкой
ДРУГОЙ стратегии.
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
VOL_DAYS = 20
LIQ_DAYS = 30
MARKET = "BTCUSDT"
# Неделя закрывается, если с открытия прошло не меньше 6.5 суток: запуск в
# понедельник 00:10 после открытия в понедельник 00:10 — это ровно 7 суток,
# полсуток запаса покрывают рестарт в пределах окна задержки планировщика.
CLOSE_AFTER_MS = WEEK_MS - DAY_MS // 2

_REBALANCING = False


def closed_bars(klines: List[Dict], now_ms: int) -> List[Dict]:
    """Дневные бары, ЗАКРЫТЫЕ к now_ms. Формирующийся бар содержит будущее."""
    return [k for k in klines if k["ts"] + DAY_MS <= now_ms]


def realized_vol(closed: List[Dict]) -> Optional[float]:
    w = closed[-(VOL_DAYS + 1):]
    if len(w) < VOL_DAYS + 1:
        return None
    rets = []
    for prev, cur in zip(w, w[1:]):
        if prev["close"] <= 0 or cur["close"] <= 0:
            return None
        rets.append(math.log(cur["close"] / prev["close"]))
    return statistics.stdev(rets)


def median_turnover(closed: List[Dict]) -> Optional[float]:
    vals = [k.get("turnover", 0.0) for k in closed[-LIQ_DAYS:] if k.get("turnover", 0) > 0]
    if len(vals) < LIQ_DAYS // 2:
        return None
    return statistics.median(vals)


def pick_sides(cands: Dict[str, Tuple[float, float]]) -> Dict[str, int]:
    """cands: {символ: (волатильность, ликвидность)}. Топ-60 по ликвидности,
    внутри — лонг нижние 20% по волатильности, шорт верхние 20%."""
    liquid = sorted(cands.items(), key=lambda x: x[1][1], reverse=True)[:UNIVERSE_N]
    if len(liquid) < MIN_NAMES_PER_LEG * 2:
        return {}
    by_vol = sorted(liquid, key=lambda x: x[1][0])
    k = max(MIN_NAMES_PER_LEG, int(len(by_vol) * TOP_FRACTION))
    if k * 2 > len(by_vol):
        return {}
    out = {s: 1 for s, _ in by_vol[:k]}
    out.update({s: -1 for s, _ in by_vol[-k:]})
    return out


def leg_return(side: int, entry: float, exit_px: float, funding: float) -> float:
    """Нетто-доходность позиции: лонг платит положительный фандинг, шорт
    получает; комиссия круга — всегда."""
    return side * (exit_px / entry - 1.0) - side * funding - db.ROUND_TRIP_FEE_PCT / 100.0


async def _daily(client, symbol: str, limit: int) -> List[Dict]:
    try:
        return await client.get_klines(symbol, interval="D", limit=limit) or []
    except Exception as e:
        log.warning(f"{symbol}: дневные свечи недоступны — {e}")
        return []


async def _close_week(client, week: Dict, now_ms: int) -> bool:
    """True — неделя ДЕЙСТВИТЕЛЬНО закрыта в базе."""
    ws = int(week["week_start"])
    results = []
    for lg in week["legs"]:
        sym = lg["symbol"]
        closed = closed_bars(await _daily(client, sym, 10), now_ms)
        # Делистинг внутри недели: выход по ПОСЛЕДНЕЙ доступной цене после
        # входа (правило проверки на истории). Позиция без единой цены после
        # входа исключается из итога — и это пишется в лог.
        after = [k for k in closed if k["ts"] + DAY_MS > ws]
        if not after:
            log.error(f"lowvol: {sym} — нет цены после входа, позиция исключена")
            results.append({"symbol": sym})
            continue
        exit_px = after[-1]["close"]
        exit_ts = after[-1]["ts"] + DAY_MS
        try:
            fh = await client.get_funding_history(sym, ws, exit_ts)
        except Exception as e:
            log.warning(f"lowvol: {sym} — история фандинга недоступна ({e}), "
                        f"фандинг принят нулём")
            fh = []
        funding = sum(r["rate"] for r in fh if ws < r["ts"] <= exit_ts)
        results.append({"symbol": sym, "exit": exit_px, "funding": funding,
                        "ret": leg_return(int(lg["side"]), float(lg["entry"]),
                                          exit_px, funding)})
        await asyncio.sleep(0.05)
    btc = closed_bars(await _daily(client, MARKET, 10), now_ms)
    btc_exit = btc[-1]["close"] if btc else None
    if not await db.lowvol_close_week(ws, results, btc_exit):
        return False
    done = [r["ret"] for r in results if r.get("ret") is not None]
    if done:
        log.info(f"lowvol: неделя {ws} закрыта, {len(done)} позиций, "
                 f"итог {sum(done) / len(done) * 100:+.3f}%")
    return True


async def _open_week(client, now_ms: int) -> None:
    tickers = await client.get_tickers() or []
    usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
    cands: Dict[str, Tuple[float, float]] = {}
    entries: Dict[str, Tuple[float, int]] = {}
    for t in usdt[:PRESELECT_N]:
        sym = t["symbol"]
        closed = closed_bars(await _daily(client, sym, LIQ_DAYS + 10), now_ms)
        await asyncio.sleep(0.05)
        if len(closed) < LIQ_DAYS + 1:
            continue          # моложе месяца — ни ликвидность, ни вола не считаются
        # Цена обязана быть свежей: вход по вчерашнему закрытию, а не по
        # закрытию недельной давности у монеты, переставшей торговаться.
        if now_ms - (closed[-1]["ts"] + DAY_MS) > 2 * DAY_MS:
            continue
        vol, liq = realized_vol(closed), median_turnover(closed)
        if vol is None or liq is None or closed[-1]["close"] <= 0:
            continue
        cands[sym] = (vol, liq)
        entries[sym] = (closed[-1]["close"], closed[-1]["ts"] + DAY_MS)
    sides = pick_sides(cands)
    if not sides:
        log.error(f"lowvol: неделя не собрана — кандидатов {len(cands)}")
        return
    week_start = max(entries[s][1] for s in sides)
    btc = closed_bars(await _daily(client, MARKET, 10), now_ms)
    legs = [{"symbol": s, "side": v, "entry": entries[s][0]} for s, v in sides.items()]
    if await db.lowvol_open_week(week_start, legs, btc[-1]["close"] if btc else None):
        log.info(f"lowvol: открыта неделя {week_start}: "
                 f"{sum(1 for v in sides.values() if v > 0)} лонг / "
                 f"{sum(1 for v in sides.values() if v < 0)} шорт")


async def rebalance(client, now_ms: Optional[int] = None) -> None:
    """Еженедельный шаг: закрыть истёкшую неделю, открыть новую."""
    global _REBALANCING
    if _REBALANCING:
        return
    _REBALANCING = True
    try:
        now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        week = await db.lowvol_open_legs()
        if week:
            if now_ms - int(week["week_start"]) < CLOSE_AFTER_MS:
                return        # неделя ещё идёт (например, рестарт в среду)
            if not await _close_week(client, week, now_ms):
                # Новую неделю НЕ открываем. Иначе останутся две открытые, а
                # дальше всегда берётся самая свежая — старая не закроется
                # никогда. Следующий запуск повторит закрытие этой же недели
                # (удержание выйдет длиннее, но учёт не потеряется).
                log.error(f"lowvol: неделя {week['week_start']} не закрылась "
                          f"— новая не открывается до успешного закрытия")
                return
        await _open_week(client, now_ms)
    except Exception as e:
        log.error(f"lowvol: ребалансировка не удалась — {e}")
    finally:
        _REBALANCING = False


def summarize(weeks: List[Dict]) -> Dict:
    """Сводка закрытых недель: средняя, t, CI95, альфа к BTC."""
    done = [w for w in weeks if w.get("ret") is not None]
    rets = [w["ret"] for w in done]
    out: Dict = {"weeks": len(rets)}
    if len(rets) < 2:
        return out
    mean, sd = statistics.fmean(rets), statistics.stdev(rets)
    se = sd / math.sqrt(len(rets))
    out.update({"mean": mean, "t": mean / se if se > 0 else 0.0,
                "ci_lo": mean - 1.96 * se, "ci_hi": mean + 1.96 * se,
                "positive": sum(1 for r in rets if r > 0)})
    return out
