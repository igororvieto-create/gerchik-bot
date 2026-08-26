import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np

from core.config import cfg
from core.state import Signal, state
from core import db
from exchange.bybit import BybitClient
from notifications.ntfy import send_push
from strategy.trader import enter_trade

log = logging.getLogger("scanner")

_SCANNING = False

# Все пороги читаются из cfg НАПРЯМУЮ в функциях (не снапшотятся в
# модульные константы при импорте) — иначе изменения через /api/settings
# молча не доходили бы до сканера до рестарта процесса.

# Дата листинга не меняется — кэшируем per-symbol, чтобы не дёргать
# get_instrument_info на каждый скан для одних и тех же пар.
_LISTING_AGE_CACHE: dict[str, float] = {}  # symbol -> launchTime (ms since epoch)

# Один сетап = один сигнал. Весь анализ (VSA, объём, уровни) считается по
# ПОСЛЕДНЕЙ ЗАКРЫТОЙ 4h свече и не меняется до 4 часов, а сканы идут каждые
# 4 минуты — раньше одна и та же свеча порождала до 4 сигналов подряд по всё
# более высокой цене (вход вдогонку), и каждый шёл в статистику отдельно.
_SIGNALLED_CANDLE: dict[str, int] = {}  # symbol -> ts последней отсигналенной свечи
# Метки восстанавливаются из БД при первом скане после старта: без этого
# каждый деплой обнулял дедуп, и одна и та же закрытая свеча сигналила
# заново. Флаг, а не проверка на пустоту: пустой словарь — законное
# состояние (за 8 часов сигналов не было), и повторять запрос каждый скан
# из-за него не нужно.
_CANDLE_MARKS_LOADED = False


async def _is_listing_old_enough(client: BybitClient, symbol: str) -> bool:
    """
    True = пара торгуется достаточно давно (или дата листинга недоступна —
    fail-open с warning, чтобы баг в одном поле не остановил весь бот).
    False = слишком свежий листинг, пропускаем как требует инвариант
    "не торговать новостные/листинговые спайки".
    """
    launch_ms = _LISTING_AGE_CACHE.get(symbol)
    if launch_ms is None:
        try:
            info = await client.get_instrument_info(symbol)
            launch_ms = float(info.get("launchTime") or 0)
        except Exception as e:
            log.warning(f"{symbol}: get_instrument_info (listing age) failed — {e}")
            return True  # fail-open: не блокируем торговлю из-за сбоя API
        if launch_ms > 0:
            _LISTING_AGE_CACHE[symbol] = launch_ms
        else:
            # Бирже нечего вернуть по launchTime — не можем оценить возраст,
            # пропускаем как "недостаточно данных", а не как "слишком молодой".
            return True

    age_days = (datetime.now(timezone.utc).timestamp() * 1000 - launch_ms) / 86_400_000
    return age_days >= cfg.MIN_LISTING_AGE_DAYS


def _calc_atr(klines: list, period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    highs  = np.array([k["high"]  for k in klines])
    lows   = np.array([k["low"]   for k in klines])
    closes = np.array([k["close"] for k in klines])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        ),
    )
    # Proper Wilder's: seed with SMA of first 'period' TR bars, then EMA the rest
    atr_val = float(np.mean(tr[:period]))
    for t in tr[period:]:
        atr_val = (atr_val * (period - 1) + float(t)) / period
    return atr_val


def _trade_flow(trades: list) -> dict:
    """Направленное усилие из ленты исполненных сделок.

    Возвращает:
      delta    — (покупки − продажи) / оборот, от −1 до +1 по агрессору;
      span_min — сколько МИНУТ покрывает лента (у ликвидных монет 500
                 сделок это секунды, у неликвида — часы). Без этого числа
                 перекос несопоставим между символами, и сравнивать их
                 напрямую нельзя;
      turnover — оборот ленты в котируемой валюте;
      absorb   — признак поглощения: сильный односторонний агрессор, а цена
                 внутри ленты не сдвинулась. В VSA это ядро разворота:
                 усилие есть, результата нет, значит противоположная сторона
                 принимает объём.

    Намеренно НЕ возвращает bias/направление: пока метрика только
    записывается и проверяется на исходах, а не влияет на решения.
    """
    # delta=None означает «данных НЕТ» и отличается от 0.0 («лента ровная»).
    # Раньше обе ситуации давали 0.0, и сигналы с недоступной лентой падали
    # в корзину «нейтрально» — то есть срез, который должен проверять
    # предсказательную силу потока, наполнялся строками БЕЗ потока.
    out: dict = {"delta": None, "span_min": 0.0, "turnover": 0.0, "absorb": False}
    if not trades:
        return out
    buy = sell = 0.0
    hi = lo = trades[0]["price"]
    for t in trades:
        notional = t["price"] * t["qty"]
        if t["side"] == "Buy":
            buy += notional
        elif t["side"] == "Sell":
            sell += notional
        hi = max(hi, t["price"])
        lo = min(lo, t["price"])
    total = buy + sell
    if total <= 0:
        return out
    out["turnover"] = total
    out["delta"] = (buy - sell) / total
    out["span_min"] = max(0.0, (trades[-1]["ts"] - trades[0]["ts"]) / 60000.0)
    # Поглощение: перекос агрессора больше 35%, а цена за то же окно прошла
    # меньше 0.15% — объём принимают, не двигая цену.
    mid = (hi + lo) / 2
    move_pct = (hi - lo) / mid * 100 if mid > 0 else 0.0
    out["absorb"] = abs(out["delta"]) >= 0.35 and move_pct < 0.15
    return out


def _ob_imbalance(ob: dict) -> tuple[float, str]:
    """Returns (imbalance ratio, bias). ratio > 0 = more bids (buy pressure)."""
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    bid_vol = sum(p * q for p, q in bids)
    ask_vol = sum(p * q for p, q in asks)
    total = bid_vol + ask_vol
    if total < 1:
        return 0.0, "NEUTRAL"
    ratio = (bid_vol - ask_vol) / total
    if ratio > cfg.OB_IMBALANCE_THRESHOLD:
        return ratio, "BUY"
    if ratio < -cfg.OB_IMBALANCE_THRESHOLD:
        return ratio, "SELL"
    return ratio, "NEUTRAL"


def _round_number_pos(price: float) -> Optional[float]:
    """Положение цены между соседними круглыми числами: -1..+1.

    Знак = где круглое число ОТНОСИТЕЛЬНО цены: отрицательное — ближайшее
    круглое ниже, положительное — выше. По Osler (2003) это разные вещи:
    над ценой копятся чужие тейк-профиты (тормоз), под ценой — чужие стопы
    (ускорение при пробое). Беззнаковая метрика складывала два
    противоположных эффекта в одну корзину, где они гасили друг друга.

    Модуль = доля пройденного пути до ближайшего круглого, 0 — точно на
    круглом числе, 1 — ровно посередине между двумя.

    ПОЧЕМУ НЕ В ATR (исправление собственной ошибки). Первая версия делила
    расстояние на ATR, и метрика измеряла НЕ близость к круглому числу, а
    ведущую цифру цены: максимум равен 5/(d*atr_pct), где d — первая цифра.
    Замер на 20 000 прогонов при atr_pct=2%:

        цена 1.xxx -> корзина «>0.75 ATR» в 54.6% случаев
        цена 9.xxx -> в 0% случаев, зато «<0.25 ATR» в 95%

    То есть срез сравнивал бы монеты по первой цифре цены. Нормировка на
    сам шаг сетки от волатильности и от цены не зависит по построению.

    ЭТО ТОЛЬКО ЗАМЕР: на score, направление и отбор не влияет, пока срез по
    исходам не покажет разницу в ev_r (docs/LITERATURE.md §6).
    """
    if price <= 0:
        return None
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0 or math.isinf(step):
        return None
    nearest = round(price / step) * step
    # step/2 — максимально возможное расстояние, отсюда модуль в [0, 1]
    pos = (nearest - price) / (step / 2)
    return max(-1.0, min(1.0, pos))


def _trend_direction(klines: list, lookback: Optional[int] = None) -> str:
    """Грубый тренд TF: сравниваем текущий закрытый close с close N баров назад."""
    if lookback is None:
        lookback = cfg.MTF_TREND_LOOKBACK
    completed = klines[:-1]  # исключаем незакрытую свечу
    if len(completed) <= lookback:
        return "NEUTRAL"
    recent = completed[-1]["close"]
    past   = completed[-1 - lookback]["close"]
    if past <= 0:
        return "NEUTRAL"
    change = (recent - past) / past
    if change > 0.002:
        return "UP"
    if change < -0.002:
        return "DOWN"
    return "NEUTRAL"


def _find_swing_levels(klines: list, price: float = 0.0, lookback: Optional[int] = None,
                        wing: Optional[int] = None, atr: float = 0.0) -> tuple[Optional[float], Optional[float]]:
    """
    Фрактальный поиск swing low / swing high за последние `lookback` завершённых
    свечей. Возвращает (support, resistance) — БЛИЖАЙШИЕ к цене уровни с
    правильной стороны: support НИЖЕ цены, resistance ВЫШЕ цены.

    Раньше брался просто последний по времени пивот в каждую сторону. Из-за
    этого: (а) валидный сетап у близкой поддержки отбраковывался, если позже
    нашёлся более глубокий пивот; (б) SL якорился за нерелевантным уровнем и
    риск раздувался; (в) для LONG resistance часто оказывался НИЖЕ цены, из-за
    чего гейт MIN_RR в _calc_levels молча отключался ("открытое небо").
    """
    if lookback is None:
        lookback = cfg.KEY_LEVEL_LOOKBACK
    if wing is None:
        wing = cfg.KEY_LEVEL_WING
    completed = klines[:-1]
    window = completed[-lookback:] if len(completed) > lookback else completed
    n = len(window)
    if n < wing * 2 + 1:
        return None, None

    if price <= 0:
        price = window[-1]["close"]

    supports: list[float] = []
    resistances: list[float] = []
    for i in range(wing, n - wing):
        seg_high = [window[j]["high"] for j in range(i - wing, i + wing + 1)]
        seg_low  = [window[j]["low"]  for j in range(i - wing, i + wing + 1)]
        if window[i]["high"] == max(seg_high):
            resistances.append(window[i]["high"])
        if window[i]["low"] == min(seg_low):
            supports.append(window[i]["low"])

    # Отсекаем шумовые пивоты вплотную к цене: внутридневная рябь в 0.3-0.5 ATR
    # не является ни опорой для стопа, ни целью. Без этого ближайшим
    # "сопротивлением" оказывался случайный микро-хай в 0.4 ATR, до него
    # получалось 0.4R, и проверка MIN_RR резала нормальные сетапы (реальное
    # сопротивление при этом было в 2R выше).
    noise = atr * cfg.LEVEL_NOISE_ATR if atr > 0 else 0.0
    below = [s for s in supports if s < price - noise]
    above = [r for r in resistances if r > price + noise]
    support    = max(below) if below else None   # ближайшая значимая поддержка
    resistance = min(above) if above else None   # ближайшее значимое сопротивление

    # Запасной вариант — границы диапазона окна. В безоткатном движении
    # фрактальных пивотов может не быть вовсе (в строгом падении ни один бар
    # не является swing high), и разворотный сетап оставался бы без цели.
    # При этом если цена САМА стоит на границе окна, запасного уровня с этой
    # стороны нет — и вход "по рынку на хае" по-прежнему отсекается.
    if support is None:
        lo = min(k["low"] for k in window)
        if lo < price - noise:
            support = lo
    if resistance is None:
        hi = max(k["high"] for k in window)
        if hi > price + noise:
            resistance = hi
    return support, resistance


def _vsa_classify(klines: list, vol_avg: float, atr: float) -> tuple[str, str]:
    """
    Упрощённый VSA: effort (объём) vs result (спред свечи).
    - ABSORPTION: высокий объём, узкий спред -> усилие поглощено, вероятен разворот
    - CLIMAX: высокий объём, широкий спред, закрытие у края диапазона -> истощение движения
    - NO_DEMAND_SUPPLY: низкий объём, узкий спред -> отсутствие интереса
    Возвращает (тип, bias LONG/SHORT/NEUTRAL).
    """
    completed = klines[:-1]
    if len(completed) < 2 or atr <= 0 or vol_avg <= 0:
        return "NEUTRAL", "NEUTRAL"

    c = completed[-1]
    spread = c["high"] - c["low"]
    if spread <= 0:
        return "NEUTRAL", "NEUTRAL"

    close_pos = (c["close"] - c["low"]) / spread  # 0 = закрытие у лоу, 1 = закрытие у хая
    vol_ratio_local = c["volume"] / vol_avg
    spread_atr = spread / atr

    if vol_ratio_local >= 2.0 and spread_atr <= 0.6:
        bias = "SHORT" if close_pos > 0.5 else "LONG"
        return "ABSORPTION", bias

    if vol_ratio_local >= 2.5 and spread_atr >= 1.5:
        # Климакс ГАСИТ предшествующее движение — направление задаёт контекст,
        # а не положение закрытия. Раньше учитывался только close_pos, и свеча,
        # взлетевшая вверх и закрывшаяся у своего лоу (классический отбой
        # сверху, медвежий), классифицировалась как "CLIMAX LONG".
        prior = completed[-6:-1] if len(completed) >= 6 else completed[:-1]
        if len(prior) >= 2 and prior[0]["close"] > 0:
            drift = (prior[-1]["close"] - prior[0]["close"]) / prior[0]["close"]
        else:
            drift = 0.0
        if drift > 0.01:
            return "CLIMAX", "SHORT"   # истощение роста -> разворот вниз
        if drift < -0.01:
            return "CLIMAX", "LONG"    # истощение падения -> разворот вверх
        # Без выраженного предшествующего движения климакс двусмыслен
        return "CLIMAX", "NEUTRAL"

    if vol_ratio_local <= 0.6 and spread_atr <= 0.5:
        return "NO_DEMAND_SUPPLY", "NEUTRAL"

    return "NEUTRAL", "NEUTRAL"


def _classify_type(oi_change: float, vol_ratio: float, funding: float,
                   price_change: float, vsa_type: str, vsa_bias: str = "NEUTRAL") -> str:
    """Тип сигнала. Вынесено отдельно, чтобы знать тип ДО применения гейтов:
    освобождение от анти-спайка и MTF должно действовать только когда сделка
    РЕАЛЬНО является VSA-разворотом, а не когда VSA-паттерн просто обнаружен
    на свече (иначе трендовый ACCUMULATION получал исключение и входил
    вдогонку прямо на вертикальной свече)."""
    # VSA-разворот идёт ПЕРВЫМ: климакс/поглощение — это точка разворота,
    # ядро методологии. Раньше OI проверялся раньше, и климакс при ΔOI≥2%
    # становился ACCUMULATION: вместо +20 очков получал -20 за противоречие
    # направлению, то есть VSA систематически ШТРАФОВАЛ сигнал.
    # Требуется НАПРАВЛЕННЫЙ разворот: климакс без выраженного предшествующего
    # движения двусмыслен, направление у него взялось бы из стакана, а
    # освобождение от анти-спайка пропустило бы вертикальную свечу в сделку.
    if vsa_type in ("CLIMAX", "ABSORPTION") and vsa_bias != "NEUTRAL":
        return "VSA_" + vsa_type
    # Пороги симметричны: раньше DISTRIBUTION требовал price_change < -0.3,
    # а ACCUMULATION ловил ВСЁ остальное, включая полосу [-0.3, 0]. Рост OI
    # при движении цены −0.2% читался как накопление и форсировал LONG, тогда
    # как зеркальный вход +0.2% давал тот же LONG — несимметричность.
    elif oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change <= -cfg.PRICE_CHANGE_MIN:
        return "DISTRIBUTION"
    elif oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change >= cfg.PRICE_CHANGE_MIN:
        return "ACCUMULATION"
    # Мёртвая зона |price_change| < PRICE_CHANGE_MIN проваливается дальше:
    # рост OI без движения цены направления не задаёт, и навешивать на него
    # ярлык ACCUMULATION (=безусловный LONG) было нечестно. Ниже направление
    # выведется из голосов, а confidence честно это учтёт.
    elif oi_change <= -cfg.OI_CHANGE_THRESHOLD:
        return "SQUEEZE"
    elif vol_ratio >= cfg.VOL_SPIKE_MULT * 1.5:
        return "VOLUME_SPIKE"
    elif abs(funding) >= cfg.FUNDING_EXTREME:
        return "FUNDING_EXTREME"
    return "MOMENTUM"


def _score_signal(
    oi_change: float,
    vol_ratio: float,
    funding: float,
    ob_ratio: float,
    price_change: float,
    vsa_type: str = "NEUTRAL",
    level_dist_atr: Optional[float] = None,
    vsa_bias: str = "NEUTRAL",
    direction: Optional[str] = None,
) -> tuple[int, str]:
    """Score 0-100 и классификация типа сигнала.

    direction=None означает «направление ещё не известно» — тогда очки за
    стакан и фандинг начисляются по модулю (режим совместимости для быстрых
    прикидок). Боевой путь в _analyze_symbol ВСЕГДА передаёт направление."""
    score = 0

    # OI change component (0-30 pts) — немного урезано, освободили место под VSA/уровни
    oi_abs = abs(oi_change)
    if oi_abs >= 10:
        score += 30
    elif oi_abs >= 7:
        score += 23
    elif oi_abs >= 5:
        score += 16
    elif oi_abs >= 3:
        score += 9
    elif oi_abs >= 2:
        score += 5

    # Volume spike component (0-20 pts)
    if vol_ratio >= 4:
        score += 20
    elif vol_ratio >= 3:
        score += 15
    elif vol_ratio >= 2:
        score += 10
    elif vol_ratio >= 1.5:
        score += 5
    elif vol_ratio >= 1.3:
        score += 2

    # Funding extremity (0-10 pts) — ТОЛЬКО если фандинг подтверждает сделку.
    # Раньше очки шли по abs(): фандинг, кричащий против направления, добавлял
    # те же +10, и именно они переводили сигнал через TRADE_MIN_SCORE
    # (рецидивирующий баг №2 — фактор обязан подтверждать направление).
    # Нижний тир начинается с FUNDING_EXTREME: 0.01% — это НЕЙТРАЛЬНАЯ ставка
    # Bybit, очки за неё получал практически каждый перп.
    fund_side = "LONG" if funding < 0 else "SHORT"   # контрарно: платящая сторона проигрывает
    if direction is None or direction == fund_side:
        fund_abs = abs(funding)
        if fund_abs >= 0.1:
            score += 10
        elif fund_abs >= 0.05:
            score += 7
        elif fund_abs >= cfg.FUNDING_EXTREME:
            score += 4

    # Orderbook imbalance (0-10 pts) — та же логика: стакан, перекошенный
    # ПРОТИВ сделки, очков не даёт.
    ob_side = "LONG" if ob_ratio > 0 else "SHORT"
    if direction is None or direction == ob_side:
        ob_abs = abs(ob_ratio)
        if ob_abs >= 0.30:
            score += 10
        elif ob_abs >= 0.20:
            score += 7
        elif ob_abs >= 0.10:
            score += 4
        elif ob_abs >= 0.05:
            score += 2

    # VSA effort/result (0-20 pts) — ядро методологии Герчика.
    # Очки даются ТОЛЬКО за направленное прочтение: двусмысленный климакс
    # (bias=NEUTRAL) раньше добавлял +20, но направление не задавал и под
    # штраф за расхождение не попадал — score рос за счёт фактора, который
    # направление никак не подтверждает.
    if vsa_bias == "NEUTRAL":
        pass
    elif vsa_type == "CLIMAX":
        score += 20
    elif vsa_type == "ABSORPTION":
        score += 15
    # NO_DEMAND_SUPPLY очков не даёт: _vsa_classify всегда возвращает для
    # него bias=NEUTRAL, направление он не подтверждает (см. правило "фактор
    # без направления не даёт очков" в docs/REVIEW.md).

    # Близость к ключевому уровню (0-10 pts) — чем ближе, тем выше
    # Тиры начинаются ВЫШЕ фильтра шума (LEVEL_NOISE_ATR): уровни ближе
    # него отбрасываются в _find_swing_levels, поэтому прежние пороги
    # 0.25/0.5 были математически недостижимы, и компонент давал только
    # 4 очка или 0 — флагманский сетап методологии не дотягивал до порога.
    if level_dist_atr is not None:
        near = cfg.LEVEL_NOISE_ATR
        if level_dist_atr <= near * 1.4:      # ~0.7 ATR — вплотную к уровню
            score += 10
        elif level_dist_atr <= near * 2.0:    # ~1.0 ATR
            score += 7
        elif level_dist_atr <= cfg.KEY_LEVEL_ATR_MULT * 2.5:
            score += 4

    return min(score, 100), _classify_type(oi_change, vol_ratio, funding, price_change, vsa_type, vsa_bias)


def _tie_break(price_change: float) -> str:
    """Направление по движению цены — или NEUTRAL, если движения нет.

    Симметрично относительно нуля: прежнее `LONG if pc > 0 else SHORT`
    отдавало SHORT и при pc == 0, то есть направление задавала не рыночная
    информация, а форма записи условия."""
    if price_change > 0:
        return "LONG"
    if price_change < 0:
        return "SHORT"
    return "NEUTRAL"


def _direction(sig_type: str, price_change: float, ob_bias: str, funding: float,
               vsa_bias: str = "NEUTRAL") -> tuple[str, float]:
    """
    Направление сделки + confidence (0-1): доля независимых голосов
    (цена / стакан / фандинг / VSA), согласных с выбранным направлением.
    Confluence-механизм сохранён намеренно — см. CLAUDE.md, рецидивирующий баг:
    score и direction не должны считаться независимо друг от друга.
    """
    votes: list[str] = []
    is_reversal = sig_type.startswith("VSA_")
    # Для разворотов голос 24h-цены не учитывается: он всегда против сделки
    # (в том и смысл разворота), из-за чего confluence-кап резал score до 55
    # и ни один контртрендовый вход не мог дойти до TRADE_MIN_SCORE.
    price_voted = not is_reversal and abs(price_change) > 0.1
    if price_voted:
        votes.append("LONG" if price_change > 0 else "SHORT")
    if ob_bias != "NEUTRAL":
        votes.append("LONG" if ob_bias == "BUY" else "SHORT")
    # Отрицательный фандинг: шорты платят лонгам -> давление вверх (контрарно).
    # Порог — FUNDING_EXTREME, а НЕ 0.01: базовая ставка Bybit равна ровно
    # 0.01%, то есть прежний порог стоял на нейтральном значении и выдавал
    # голос почти по каждому символу. Поскольку фандинг чаще положителен, это
    # был постоянный голос SHORT — им и задавалось направление 2/3 сделок
    # (наблюдалось 24 шорта на 13 лонгов за сутки).
    if cfg.FUNDING_VOTE and abs(funding) >= cfg.FUNDING_EXTREME:
        votes.append("LONG" if funding < 0 else "SHORT")
    if vsa_bias != "NEUTRAL":
        votes.append(vsa_bias)

    derived_from_votes = False
    if sig_type.startswith("VSA_") and vsa_bias != "NEUTRAL":
        primary = vsa_bias
    elif sig_type == "ACCUMULATION":
        # Классификация теперь требует price_change >= PRICE_CHANGE_MIN (0.3),
        # а голос цены срабатывает уже при 0.1 — значит голос ГАРАНТИРОВАННО
        # совпадает с primary и подтверждать его не может. Раньше симметрии не
        # было и ACCUMULATION случался при pc ∈ [-0.3, 0.1] без такого голоса.
        derived_from_votes = True
        primary = "LONG"
    elif sig_type == "DISTRIBUTION":
        derived_from_votes = True
        primary = "SHORT"
    elif sig_type == "SQUEEZE":
        # Резкое падение OI само по себе направления не задаёт — решает
        # большинство голосов, при ничьей падаем на движение цены.
        derived_from_votes = True
        long_votes  = votes.count("LONG")
        short_votes = votes.count("SHORT")
        if long_votes > short_votes:
            primary = "LONG"
        elif short_votes > long_votes:
            primary = "SHORT"
        else:
            # Ничья голосов. Тай-брейк по цене работает, только пока цена
            # реально куда-то идёт: при price_change == 0 выражение
            # "LONG if >0 else SHORT" всегда давало SHORT, и бесцельный
            # squeeze систематически становился шортом (до score 53,
            # то есть выше торгового порога) — ровно тот же дефект, что и
            # константный голос фандинга. Нет информации — нет направления.
            # Ничья достижима ТОЛЬКО когда стакан и фандинг смотрят в разные
            # стороны, то есть это максимально противоречивый сетап — ровно
            # тот, ради которого написан _apply_confluence_cap.
            #
            # Принцип: движение цены, слишком слабое чтобы подать голос
            # (|pc| <= 0.1), слишком слабое и чтобы РЕШИТЬ сторону. Иначе
            # выходит абсурд: сторону реальной сделки со score 70 выбирает
            # суточное движение в 0.0001%. _tie_break уже возвращает NEUTRAL
            # при ровно нуле — здесь то же правило распространяется на весь
            # зазор ниже порога голоса.
            #
            # Прежняя правка этого зазора закрыла ошибку (вычёркивался не тот
            # голос), но подняла кап с 35 до 75 у 14 856 сочетаний, сделав их
            # торгуемыми. Снимать кап с противоречивых сетапов нельзя —
            # CLAUDE.md и docs/LITERATURE.md §4.
            primary = _tie_break(price_change) if price_voted else "NEUTRAL"
            # Сюда с ненейтральным primary можно попасть только при
            # price_voted, поэтому здесь именно True, а не price_voted:
            # условность была бы мёртвой (мутация в неё не меняет поведения).
            derived_from_votes = True
    elif sig_type == "FUNDING_EXTREME":
        # Тип присваивается при |funding| >= FUNDING_EXTREME — ровно при том
        # же пороге, при котором фандинг подаёт голос. Голос тавтологичен.
        # Без этого флага фандинг в мёртвой зоне цены делал всё сразу: выбирал
        # направление, давал решающие +10 очков и сам себя подтверждал,
        # поднимая confidence с 0.0 до 0.5 — рецидив бага №2.
        derived_from_votes = True
        primary = "SHORT" if funding > 0 else "LONG"
    elif ob_bias != "NEUTRAL":
        derived_from_votes = True
        primary = "LONG" if ob_bias == "BUY" else "SHORT"
    else:
        # Тот же принцип, что и в тай-брейке SQUEEZE выше: движение слабее
        # порога голоса сторону не выбирает. Иначе одинокий голос фандинга —
        # фактор без литературной опоры (docs/LITERATURE.md §4) — единолично
        # снимал бы кап 55, а направление задавало бы движение в 0.0001%.
        # Оставлять правило половинчатым нельзя: несогласованность вернётся
        # находкой следующего ревью.
        primary = _tie_break(price_change) if price_voted else "NEUTRAL"
        derived_from_votes = True

    # Голос VSA у разворота тавтологически совпадает с primary — он задаёт
    # направление, а не подтверждает его. Считаем уверенность по НЕЗАВИСИМЫМ
    # голосам (цена/стакан/фандинг), иначе одинокий vsa_bias давал бы
    # confidence=1.0 и кап не срабатывал бы вообще.
    independent = list(votes)
    if is_reversal and vsa_bias in independent:
        independent.remove(vsa_bias)
    elif derived_from_votes and independent:
        # primary выведен ИЗ голосов (SQUEEZE, фолбэки по стакану/цене) —
        # тот голос, что задал направление, не может его же подтверждать.
        # Без этого одинокое 24h-движение давало confidence=1.0, и вход
        # вдогонку проходил без капа, тогда как разворот резался до 55.
        try:
            independent.remove(primary)
        except ValueError:
            pass

    if independent:
        agree = sum(1 for v in independent if v == primary)
        confidence = agree / len(independent)
    else:
        confidence = 0.4  # подтверждений нет -> низкая уверенность, не нейтрально

    return primary, confidence


def _apply_confluence_cap(score: int, confidence: float) -> int:
    """Противоречащие факторы режут потолок скора: магнитуда реальна,
    но направление — гадание, и такой сигнал не должен доходить до 90+."""
    if confidence < 0.34:
        return min(score, 35)
    if confidence < 0.5:
        return min(score, 55)
    if confidence < 0.75:
        return min(score, 75)
    return score


def _calc_levels(price: float, atr: float, direction: str,
                  support: Optional[float], resistance: Optional[float],
                  sl_anchor: Optional[float] = None) -> Optional[dict]:
    """
    SL ставится ЗА ключевым уровнем (support для LONG, resistance для SHORT) + буфер 0.25×ATR,
    либо, если уровня нет, на 1.5×ATR как раньше. TP1/TP2/TP3 = 1R/2R/3R от риска.
    Если до противоположного уровня (цели) реально достижимый R:R < MIN_RR — сигнал отбраковывается
    (возвращает None), т.к. это ядро требования Герчика "минимум 3:1".
    """
    if price <= 0 or atr <= 0:
        return None

    buffer = atr * 0.25
    min_sl_dist = max(atr * 1.5, price * 0.003)

    # sl_anchor: экстремум сигнальной свечи. Для VSA-разворота стоп ставится
    # за неё, а не за старый пивот — фрактальный поиск свежую свечу не видит
    # (нужно wing баров после неё), поэтому без якоря разворотный вход
    # оставался бы вообще без валидной опоры для стопа.
    if sl_anchor is not None and sl_anchor > 0:
        # Для разворота якорь ИМЕЕТ ПРИОРИТЕТ, даже если он дальше найденного
        # пивота: тезис сделки в том, что экстремум сигнальной свечи устоит.
        # Стоп внутри диапазона этой свечи (за более близким уровнем) выбило
        # бы обычным шумом задолго до того, как тезис реально сломается.
        if direction == "LONG" and sl_anchor < price:
            support = sl_anchor if support is None else min(support, sl_anchor)
        elif direction == "SHORT" and sl_anchor > price:
            resistance = sl_anchor if resistance is None else max(resistance, sl_anchor)

    if direction == "LONG":
        if support is not None and support < price:
            sl_dist = max(price - support + buffer, min_sl_dist * 0.5)
        else:
            sl_dist = min_sl_dist
        entry = price
        sl = price - sl_dist
        target_level = resistance if (resistance is not None and resistance > price) else None
    else:  # SHORT
        if resistance is not None and resistance > price:
            sl_dist = max(resistance - price + buffer, min_sl_dist * 0.5)
        else:
            sl_dist = min_sl_dist
        entry = price
        sl = price + sl_dist
        target_level = support if (support is not None and support < price) else None

    risk = sl_dist
    if risk <= 0:
        return None
    # Потолок: стоп шире MAX_SL_ATR × ATR — это уже не сделка, а лотерея.
    # Размер позиции при таком стопе схлопывается, а шанс задеть его высок.
    if risk > atr * cfg.MAX_SL_ATR:
        return None

    # Если есть реальный противоположный уровень — проверяем, что до него хватает
    # расстояния на MIN_RR. Если уровня нет ("открытое небо") — доверяем стандартной
    # 1R/2R/3R сетке, т.к. TP3 и так на 3R.
    if target_level is None:
        # Нет противоположного уровня в окне = цена на экстремуме = покупка
        # хая / продажа лоя. Раньше здесь проверка MIN_RR пропускалась целиком.
        return None
    achievable = abs(target_level - price) / risk
    if achievable < cfg.MIN_RR:
        return None  # недостаточно места до цели — не торгуем

    if direction == "LONG":
        tp1, tp2, tp3 = price + risk * 1.0, price + risk * 2.0, price + risk * 3.0
    else:
        tp1, tp2, tp3 = price - risk * 1.0, price - risk * 2.0, price - risk * 3.0

    sl_pct = sl_dist / price * 100

    return {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr": 2.0,              # сделка целится в TP2 = 2R
        "headroom": achievable,  # фактический запас до противоположного уровня, в R
        "sl_pct": sl_pct,
    }


async def _analyze_symbol(client: BybitClient, ticker: dict) -> Optional[Signal]:
    symbol = ticker.get("symbol", "")
    if symbol in cfg.BLACKLIST:
        return None

    try:
        # `or 0`: Bybit шлёт пустые строки в этих полях для части контрактов
        # (пре-опен, нет фандинга) — float('') роняет анализ символа навсегда
        price       = float(ticker.get("lastPrice")     or 0)
        price_chg   = float(ticker.get("price24hPcnt")  or 0) * 100
        funding     = float(ticker.get("fundingRate")   or 0) * 100
        vol_24h     = float(ticker.get("volume24h")     or 0)
        oi_usdt_now = float(ticker.get("openInterestValue") or 0)

        if price <= 0:
            return None

        if vol_24h < cfg.MIN_VOL_24H:
            return None

        # Порог фандинга — FUNDING_EXTREME, а не 0.01: нейтральная ставка Bybit
        # равна ровно 0.01%, поэтому правая половина условия почти всегда была
        # ложной, и префильтр мёртвых тикеров не отсеивал ничего — на каждый
        # такой символ тратилось 4 запроса к API каждый скан.
        if abs(price_chg) < cfg.PRICE_CHANGE_MIN and abs(funding) < cfg.FUNDING_EXTREME:
            return None

        # Инвариант: не торговать новостные/листинговые спайки. Проверяем
        # ДО тяжёлых запросов (klines/orderbook/OI), чтобы не тратить на
        # свежие листинги лишние вызовы API.
        if not await _is_listing_old_enough(client, symbol):
            return None

        # Добавили 1h klines для MTF-подтверждения тренда наряду с 4h.
        # limit 4h-свечей масштабируется под лукбеки: захардкоженные 26 при
        # KEY_LEVEL_LOOKBACK/MTF_TREND_LOOKBACK > 25 молча отключали бы
        # 4h-часть MTF-фильтра (NEUTRAL) и урезали окно поиска уровней.
        kline_4h_limit = max(26, cfg.KEY_LEVEL_LOOKBACK + 2, cfg.MTF_TREND_LOOKBACK + 3)
        # Лента сделок добавлена ПЯТЫМ запросом и пока только измеряется:
        # на решения она не влияет, пока не наберётся статистика исходов.
        # return_exceptions: сбой ленты не должен ронять анализ символа —
        # это дополнительная метрика, а не обязательные данные.
        oi_hist, klines, ob, klines_1h, trades = await asyncio.gather(
            client.get_open_interest(symbol, interval="4h", limit=2),
            client.get_klines(symbol, interval="240", limit=kline_4h_limit),
            client.get_orderbook(symbol, limit=20),
            client.get_klines(symbol, interval="60", limit=max(cfg.MTF_TREND_LOOKBACK + 3, 10)),
            client.get_recent_trades(symbol, limit=cfg.TRADE_FLOW_LIMIT)
            if cfg.TRADE_FLOW_LIMIT > 0 else asyncio.sleep(0, result=[]),
            return_exceptions=True,
        )
        if isinstance(trades, BaseException):
            log.debug(f"{symbol}: лента сделок недоступна — {trades}")
            trades = []
        for _name, _val in (("oi_hist", oi_hist), ("klines", klines),
                            ("ob", ob), ("klines_1h", klines_1h)):
            if isinstance(_val, BaseException):
                log.warning(f"{symbol}: {_name} — {_val}")
                return None
        flow = _trade_flow(trades)

        if not oi_hist or not klines:
            log.warning(f"{symbol}: partial data — oi_hist={len(oi_hist)} klines={len(klines)}")
            if not klines:
                return None

        # Базлайн OI — ПРЕДЫДУЩИЙ 4h-снапшот (oi_hist[0]), а не свежайший:
        # список отсортирован по возрастанию времени, и oi_hist[-1] — это
        # начало ТЕКУЩЕГО периода. Сравнение с ним сразу после границы 4h
        # давало oi_change ≈ 0 и систематически глушило OI-сигналы.
        if oi_hist and price > 0:
            baseline = oi_hist[0] if len(oi_hist) >= 2 else oi_hist[-1]
            oi_prev_usdt = baseline["oi"] * price
            oi_change = (oi_usdt_now - oi_prev_usdt) / oi_prev_usdt * 100 if oi_prev_usdt > 0 else 0.0
        else:
            oi_change = 0.0

        if len(klines) >= 22:
            volumes  = np.array([k["volume"] for k in klines])
            vol_avg  = float(np.mean(volumes[-22:-2]))
            vol_curr = float(volumes[-2])
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 1.0
        elif len(klines) >= 3:
            volumes  = np.array([k["volume"] for k in klines])
            vol_avg  = float(np.mean(volumes[:-2])) if len(volumes) > 2 else 1.0
            vol_curr = float(volumes[-2])
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 1.0
        else:
            vol_avg = 1.0
            vol_ratio = 1.0

        atr = _calc_atr(klines[:-1])
        atr_pct = atr / price * 100 if price > 0 else 0.0

        ob_ratio, ob_bias = _ob_imbalance(ob)

        # VSA: effort (объём) vs result (спред) на последней завершённой 4h свече
        vsa_type, vsa_bias = _vsa_classify(klines, vol_avg, atr)
        # Освобождение от анти-спайка/MTF даётся, только если сделка и есть
        # VSA-разворот. Раньше проверялся сам факт VSA-паттерна — и трендовый
        # ACCUMULATION проскакивал гейт на вертикальной свече со score 70.
        provisional_type = _classify_type(oi_change, vol_ratio, funding, price_chg, vsa_type, vsa_bias)
        is_vsa_reversal = provisional_type.startswith("VSA_")

        # Анти-спайк: не входить вдогонку после вертикального движения.
        # Проверяются ОБЕ свечи — последняя закрытая И текущая формирующаяся
        # (именно её пропускал прежний гейт: кейс NOM score 91, где сигнал
        # выдавался на пике ещё не закрытой свечи).
        # Исключение — VSA-развороты: климакс по определению широкая свеча,
        # и торгуется он ПРОТИВ неё, а не вдогонку.
        spike_exempt = is_vsa_reversal and cfg.VSA_SPIKE_EXEMPT
        if atr > 0 and not spike_exempt:
            for k in klines[-2:]:
                if (k["high"] - k["low"]) / atr > cfg.MAX_LAST_CANDLE_ATR:
                    return None

        # Ключевые уровни на 4h — ближайшие к текущей цене с правильных сторон
        support, resistance = _find_swing_levels(klines, price=price, atr=atr)

        # Тип сигнала уже посчитан выше (provisional_type) — _score_signal
        # вернул бы ровно его же на тех же входах. Направление определяем от
        # него, и только ПОТОМ считаем score один раз: очки за стакан, фандинг
        # и уровень зависят от направления, поэтому раньше их было не посчитать.
        sig_type = provisional_type
        direction, confidence = _direction(
            sig_type, price_chg, ob_bias, funding, vsa_bias=vsa_bias,
        )
        if direction == "NEUTRAL":
            # Направления нет вовсе (ничья голосов при неподвижной цене).
            # Явный отказ обязателен: ниже по коду direction сравнивается
            # только с "LONG", и NEUTRAL молча трактовался бы как SHORT —
            # то есть сетап без направления стал бы шортом.
            return None

        # Дистанция до уровня, который реально станет опорой стопа для ЭТОЙ
        # сделки. Якорь обязан совпадать с тем, что возьмёт _calc_levels:
        # там для разворота берётся min(support, экстремум свечи) — если
        # считать очки только по экстремуму, сетап получал +10 «вплотную к
        # уровню», имея реальный стоп в 3+ ATR.
        level_dist_atr = None
        if atr > 0:
            anchor_lvl = support if direction == "LONG" else resistance
            if is_vsa_reversal and len(klines) >= 2:
                ext = klines[-2]["low"] if direction == "LONG" else klines[-2]["high"]
                if direction == "LONG" and ext < price:
                    anchor_lvl = ext if support is None else min(support, ext)
                elif direction == "SHORT" and ext > price:
                    anchor_lvl = ext if resistance is None else max(resistance, ext)
            if anchor_lvl is not None:
                level_dist_atr = abs(price - anchor_lvl) / atr

        score, _ = _score_signal(
            oi_change, vol_ratio, funding, ob_ratio, price_chg,
            vsa_type=vsa_type, level_dist_atr=level_dist_atr, vsa_bias=vsa_bias,
            direction=direction,
        )

        # Здесь стояло снятие вклада VSA при расхождении с direction. Блок
        # был НЕДОСТИЖИМ: перебор всех 1575 достижимых сочетаний
        # (_vsa_classify × oi × price_change × funding × ob_bias) даёт ноль
        # срабатываний — цепочка жёсткая: vsa_bias != NEUTRAL влечёт
        # sig_type = "VSA_*", а тогда _direction ставит primary = vsa_bias.
        # Реальную защиту от рецидива №2 даёт _score_signal: при
        # vsa_bias == NEUTRAL очки за VSA не начисляются вовсе. Мёртвый блок
        # дублировал константы 20/15 из _score_signal — смена веса в одном
        # месте и не в другом молча разошлась бы. Инвариант проверяется
        # тестом test_vsa_contribution_cannot_contradict_direction.

        # Confluence cap ПОСЛЕ определения направления, ДО порога MIN_SCORE —
        # противоречащий сигнал не должен проходить фильтр на сырой магнитуде
        score = _apply_confluence_cap(score, confidence)
        if score < cfg.MIN_SCORE:
            return None

        # MTF-фильтр: тренды 1h/4h не должны противоречить направлению сделки.
        # VSA-развороты (климакс/поглощение) освобождены: они по определению
        # торгуются ПРОТИВ предшествующего движения, и прежний безусловный
        # фильтр вырезал их все до единого — оставляя только входы по тренду,
        # то есть вдогонку (это и давало 1W/14L в форвард-тесте).
        if cfg.REQUIRE_MTF_ALIGN and not is_vsa_reversal:
            trend_4h = _trend_direction(klines)
            trend_1h = _trend_direction(klines_1h)
            opposite = "DOWN" if direction == "LONG" else "UP"
            if trend_4h == opposite or trend_1h == opposite:
                return None  # таймфреймы против направления сделки — пропускаем

        # Требуем, чтобы цена была НЕДАЛЕКО от уровня, который реально станет
        # SL-якорем для этого direction (resistance для SHORT, support для
        # LONG). Раньше здесь проверялась близость к ЛЮБОМУ ближайшему уровню
        # (level_dist_atr = min(support, resistance)), а SL в _calc_levels
        # ставится за уровнем В СТОРОНУ СДЕЛКИ — это два разных уровня, и
        # сигнал мог пройти фильтр "рядом с уровнем" по одной стороне, а SL
        # уехать далеко на другой (реальный кейс: HOME/USDT, SL%=23% при
        # ATR%=3.27%, в 7 раз шире нормы).
        # Уровень обязан быть С ПРАВИЛЬНОЙ СТОРОНЫ цены (support НИЖЕ для
        # LONG, resistance ВЫШЕ для SHORT) — abs() принимал бы и уровень с
        # неправильной стороны (лонг сразу под свежепробитой поддержкой),
        # при этом _calc_levels молча падал бы на generic 1.5×ATR стоп без
        # реального уровня за спиной.
        if not is_vsa_reversal:
            # Для трендовых входов цена обязана стоять У уровня, который станет
            # опорой стопа. Развороты освобождены: их опора — экстремум самой
            # сигнальной свечи, и расстояние до неё равно ширине этой свечи
            # (у настоящего климакса это 2-3 ATR, гейт 1.2 отсекал бы их все).
            relevant_level = support if direction == "LONG" else resistance
            if relevant_level is None or atr <= 0:
                return None
            if direction == "LONG" and relevant_level >= price:
                return None
            if direction == "SHORT" and relevant_level <= price:
                return None
            if abs(price - relevant_level) / atr > cfg.KEY_LEVEL_ATR_MULT:
                return None
        elif atr <= 0:
            return None
        else:
            # Разворот входит ОТ сетапа, а не догоняет его: цена обязана
            # оставаться рядом с закрытием сигнальной свечи. Иначе сигнал,
            # отклонённый сразу после закрытия свечи, мог "дозреть" через
            # пару часов и дать вход в нескольких ATR от точки разворота —
            # то есть тот же вход вдогонку, только в обратную сторону.
            sig_c = klines[-2]
            drift_atr = abs(price - sig_c["close"]) / atr
            if drift_atr > cfg.REVERSAL_MAX_DRIFT_ATR:
                return None
            # Модуль сноса не отличал «цена пошла в сторону сделки» от
            # «сетап уже сломан». Если цена ушла ЗА экстремум сигнальной
            # свечи — тезис («экстремум устоит») недействителен, а якорь
            # стопа при этом молча отбрасывался в _calc_levels.
            if direction == "LONG" and price <= sig_c["low"]:
                return None
            if direction == "SHORT" and price >= sig_c["high"]:
                return None

        # Для разворота опора стопа — экстремум сигнальной свечи
        sl_anchor = None
        if is_vsa_reversal and len(klines) >= 2:
            sig_candle = klines[-2]
            sl_anchor = sig_candle["low"] if direction == "LONG" else sig_candle["high"]
        levels = _calc_levels(price, atr, direction, support, resistance, sl_anchor=sl_anchor)
        if levels is None:
            # либо нет валидного риска, либо не набирается MIN_RR до цели
            return None

        # Один сетап — один сигнал: та же закрытая свеча повторно не сигналит.
        # Пометку ставит run_scan_and_broadcast ПОСЛЕ доставки — иначе отмена
        # скана (таймаут /api/scan, рестарт) "расходовала" свечу впустую и
        # сетап терялся до следующей 4h-свечи.
        candle_ts = int(klines[-2]["ts"])
        if _SIGNALLED_CANDLE.get(symbol) == candle_ts:
            return None

        details = (
            f"{sig_type} | {direction} | score={score} | conf={confidence:.2f} | "
            f"OI {oi_change:+.1f}% | vol {vol_ratio:.1f}x | "
            f"funding {funding:+.3f}% | OB {ob_bias} | VSA {vsa_type} | "
            f"ATR {atr_pct:.2f}% | RR {levels['rr']:.1f} | запас {levels['headroom']:.1f}R"
        )

        sig = Signal(
            symbol=symbol,
            signal_type=sig_type,
            direction=direction,
            score=score,
            price=price,
            oi_change=oi_change,
            vol_ratio=vol_ratio,
            funding=funding,
            ob_bias=ob_bias,
            atr_pct=atr_pct,
            details=details,
            entry=levels["entry"],
            sl=levels["sl"],
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            rr=levels["rr"],
            headroom=levels["headroom"],
            flow_delta=flow["delta"],
            flow_span_min=flow["span_min"],
            flow_absorb=flow["absorb"],
            sl_pct=levels["sl_pct"],
            # Замеры без влияния на решение (docs/LITERATURE.md §3 и §1):
            # ob_ratio — числовая величина перекоса, а не только корзина
            # BUY/SELL/NEUTRAL: без неё нельзя проверить, добавляет ли голос
            # стакана что-либо к ev_r. confidence — доля согласных голосов,
            # нужна чтобы проверить сам confluence-кап на исходах.
            ob_ratio=ob_ratio,
            confidence=confidence,
            round_pos=_round_number_pos(levels["entry"]),
        )
        sig.candle_ts = candle_ts   # для дедупа после доставки
        return sig
    except Exception as e:
        log.warning(f"{symbol}: analysis error — {e}")
        return None


async def scan_all(client: BybitClient) -> List[Signal]:
    global _SCANNING
    if _SCANNING:
        log.info("scan_all: already running, skipping")
        return []
    _SCANNING = True
    signals: List[Signal] = []

    try:
        global _CANDLE_MARKS_LOADED
        if not _CANDLE_MARKS_LOADED:
            try:
                marks = await db.get_recent_candle_marks()
                for _sym, _ts in marks.items():
                    _SIGNALLED_CANDLE.setdefault(_sym, _ts)
                _CANDLE_MARKS_LOADED = True
                log.info(f"scan_all: восстановлено меток свечей из БД — {len(marks)}")
            except Exception as e:
                # Не взводим флаг: повторим на следующем скане. Продолжать
                # без меток можно (дубли хуже пропуска, но не фатальны),
                # молчать — нельзя.
                log.error(f"scan_all: не удалось восстановить дедуп свечей — {e}")

        tickers = await client.get_tickers()
        tickers = [
            t for t in tickers
            if t.get("symbol", "").endswith("USDT")
            and t.get("symbol") not in cfg.BLACKLIST
        ]
        try:
            tickers.sort(key=lambda t: float(t.get("volume24h", 0)), reverse=True)
        except Exception:
            pass
        if cfg.TOP_N_PAIRS > 0:
            tickers = tickers[:cfg.TOP_N_PAIRS]

        log.info(f"scan_all: {len(tickers)} symbols to scan "
                 f"(batch={cfg.SCAN_BATCH_SIZE} delay={cfg.SCAN_BATCH_DELAY}s)")
        if not tickers:
            # Ветка «0 символов» обходила учёт: счётчик скана замирал, ошибка
            # не выставлялась, и дашборд продолжал показывать номер и число
            # находок ПРЕДЫДУЩЕГО удачного скана. Снаружи недоступный Bybit
            # выглядел как работающий бот. Ровно тот случай, ради которого
            # ветка except ниже намеренно двигает счётчик.
            log.warning("scan_all: 0 symbols after filter — Bybit API may be unreachable")
            state.last_scan_at = datetime.utcnow()
            state.scan_count += 1
            state.last_scan_found = 0
            state.last_scan_error = "0 символов после фильтра — Bybit недоступен?"
            return []

        # Из-за добавленного 1h-запроса нагрузка на API выросла ~на треть (3->4 вызова
        # на символ). Если полезут rate-limit warnings — увеличь SCAN_BATCH_DELAY или
        # уменьши SCAN_BATCH_SIZE / TOP_N_PAIRS.
        batch_size = cfg.SCAN_BATCH_SIZE
        errors = 0
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            results = await asyncio.gather(
                *[_analyze_symbol(client, t) for t in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal):
                    signals.append(r)
                elif isinstance(r, Exception):
                    errors += 1
            if i + batch_size < len(tickers):
                await asyncio.sleep(cfg.SCAN_BATCH_DELAY)

        if errors:
            log.warning(f"scan_all: {errors}/{len(tickers)} symbols failed with exceptions")

        # Кэши растут по мере появления новых пар — держим в границах
        if len(_SIGNALLED_CANDLE) > 2000:
            _SIGNALLED_CANDLE.clear()
        if len(_LISTING_AGE_CACHE) > 2000:
            _LISTING_AGE_CACHE.clear()

        signals.sort(key=lambda s: s.score, reverse=True)
        state.last_scan_at = datetime.utcnow()
        state.scan_count += 1
        state.total_signals += len(signals)
        state.last_scan_found = len(signals)
        state.last_scan_error = ""

        log.info(f"scan_all: found {len(signals)} signals (scan #{state.scan_count})")
        if signals:
            top = signals[:3]
            log.info("Top signals: " + " | ".join(
                f"{s.symbol} score={s.score} {s.direction} {s.signal_type}" for s in top
            ))
        else:
            log.info(f"scan_all: no signals above MIN_SCORE={cfg.MIN_SCORE}")
        return signals

    except Exception as e:
        # Неудачный скан тоже учитываем: иначе счётчик замирает, дашборд
        # вечно показывает старый номер скана и сбой снаружи не виден
        state.last_scan_at = datetime.utcnow()
        state.scan_count += 1
        # Обнулять обязательно: иначе провалившийся скан наследует число
        # предыдущего удачного и в строке «Скан #N · найдено: 35» стоит
        # результат чужого скана.
        state.last_scan_found = 0
        state.last_scan_error = str(e)
        log.error(f"scan_all error (scan #{state.scan_count}): {e}")
        return []
    finally:
        _SCANNING = False


async def run_scan_and_broadcast(client: BybitClient, ntfy_url: str = "",
                                 allow_trading: bool = True) -> List[Signal]:
    """Called by APScheduler: scan, save to DB, broadcast via WS, push via ntfy."""
    if client.api_key and client.secret:
        try:
            bal = await client.get_balance()
            if bal > 0:
                state.balance = bal
        except Exception as be:
            log.warning(f"run_scan_and_broadcast: get_balance failed — {be}")

    signals = await scan_all(client)

    now = datetime.utcnow()
    cooldown = timedelta(minutes=cfg.SIGNAL_COOLDOWN_MIN)

    for sig in signals:
        last_seen = state.signal_seen.get(sig.symbol)
        if last_seen and (now - last_seen) < cooldown:
            continue  # пометка свечи ещё не поставлена — сигнал не потерян
        state.signal_seen[sig.symbol] = now
        ct = sig.candle_ts or None
        if ct is not None:
            _SIGNALLED_CANDLE[sig.symbol] = ct

        try:
            await db.save_signal(sig)
        except Exception as dbe:
            log.error(f"run_scan_and_broadcast: db.save_signal({sig.symbol}) failed — {dbe}")

        # allow_trading=False для ручного скана из дашборда: GET-запрос
        # не должен открывать позиции на реальные деньги.
        if allow_trading:
            await enter_trade(client, sig)

        try:
            msg = json.dumps({"type": "signal", "data": sig.to_dict()})
        except Exception as je:
            log.error(f"run_scan_and_broadcast: sig.to_dict() failed for {sig.symbol} — {je}")
            continue
        dead = set()
        # снапшот list(...): подключение/отключение клиента во время await
        # мутирует set и роняет итерацию RuntimeError'ом, обрывая весь скан
        for ws in list(state.ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            state.remove_ws(ws)

        if ntfy_url and sig.score >= 60:
            try:
                icon = "🟢" if sig.direction == "LONG" else "🔴"
                await send_push(
                    ntfy_url,
                    title=f"{icon} {sig.symbol} — {sig.signal_type}",
                    message=sig.details,
                    priority="high" if sig.score >= 75 else "default",
                    tags=["chart_with_upwards_trend"] if sig.direction == "LONG" else ["chart_with_downwards_trend"],
                )
            except Exception as pe:
                log.warning(f"run_scan_and_broadcast: send_push({sig.symbol}) failed — {pe}")

    heartbeat = json.dumps({
        "type":         "heartbeat",
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "signals_found": len(signals),
        "scan_error":   state.last_scan_error or None,
    })
    dead = set()
    for ws in list(state.ws_clients):
        try:
            await ws.send_text(heartbeat)
        except Exception:
            dead.add(ws)
    for ws in dead:
        state.remove_ws(ws)

    return signals
