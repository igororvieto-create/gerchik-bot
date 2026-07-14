import asyncio
import json
import logging
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

# --- Новые настраиваемые пороги (можно вынести в core/config.py; сейчас
# читаются через getattr с безопасными дефолтами, чтобы не требовать
# правки cfg прямо сейчас) ---
MIN_RR             = getattr(cfg, "MIN_RR", 3.0)              # Герчик: минимум 3:1
KEY_LEVEL_LOOKBACK = getattr(cfg, "KEY_LEVEL_LOOKBACK", 20)    # свечей для поиска swing high/low
KEY_LEVEL_WING     = getattr(cfg, "KEY_LEVEL_WING", 2)         # "плечи" фрактала для пивота
KEY_LEVEL_ATR_MULT = getattr(cfg, "KEY_LEVEL_ATR_MULT", 1.0)   # макс. расстояние цены до уровня (в ATR)
REQUIRE_MTF_ALIGN  = getattr(cfg, "REQUIRE_MTF_ALIGN", True)   # жёсткий фильтр по совпадению трендов TF
MTF_TREND_LOOKBACK = getattr(cfg, "MTF_TREND_LOOKBACK", 6)     # свечей назад для определения тренда
MIN_LISTING_AGE_DAYS = getattr(cfg, "MIN_LISTING_AGE_DAYS", 14)  # не торговать свежие листинги младше N дней

# Дата листинга не меняется — кэшируем per-symbol, чтобы не дёргать
# get_instrument_info на каждый скан для одних и тех же пар.
_LISTING_AGE_CACHE: dict[str, float] = {}  # symbol -> launchTime (ms since epoch)


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
    return age_days >= MIN_LISTING_AGE_DAYS


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


def _trend_direction(klines: list, lookback: int = MTF_TREND_LOOKBACK) -> str:
    """Грубый тренд TF: сравниваем текущий закрытый close с close N баров назад."""
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


def _find_swing_levels(klines: list, lookback: int = KEY_LEVEL_LOOKBACK,
                        wing: int = KEY_LEVEL_WING) -> tuple[Optional[float], Optional[float]]:
    """
    Простой фрактальный поиск ближайших swing low / swing high (support/resistance)
    за последние `lookback` завершённых свечей. Возвращает (support, resistance) —
    самый последний найденный пивот в каждую сторону, либо None если не найден.
    """
    completed = klines[:-1]
    window = completed[-lookback:] if len(completed) > lookback else completed
    n = len(window)
    if n < wing * 2 + 1:
        return None, None

    support = None
    resistance = None
    for i in range(wing, n - wing):
        seg_high = [window[j]["high"] for j in range(i - wing, i + wing + 1)]
        seg_low  = [window[j]["low"]  for j in range(i - wing, i + wing + 1)]
        if window[i]["high"] == max(seg_high):
            resistance = window[i]["high"]  # берём последний найденный -> самый свежий пивот
        if window[i]["low"] == min(seg_low):
            support = window[i]["low"]
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
        if close_pos >= 0.6:
            return "CLIMAX", "SHORT"   # климакс на хаях после роста -> вероятен разворот вниз
        if close_pos <= 0.4:
            return "CLIMAX", "LONG"    # климакс на лоях после падения -> вероятен разворот вверх
        return "CLIMAX", "NEUTRAL"

    if vol_ratio_local <= 0.6 and spread_atr <= 0.5:
        return "NO_DEMAND_SUPPLY", "NEUTRAL"

    return "NEUTRAL", "NEUTRAL"


def _score_signal(
    oi_change: float,
    vol_ratio: float,
    funding: float,
    ob_ratio: float,
    price_change: float,
    vsa_type: str = "NEUTRAL",
    level_dist_atr: Optional[float] = None,
) -> tuple[int, str]:
    """Score 0-100 и классификация типа сигнала."""
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

    # Funding extremity (0-10 pts)
    fund_abs = abs(funding)
    if fund_abs >= 0.1:
        score += 10
    elif fund_abs >= 0.05:
        score += 7
    elif fund_abs >= 0.03:
        score += 4
    elif fund_abs >= 0.01:
        score += 2

    # Orderbook imbalance (0-10 pts)
    ob_abs = abs(ob_ratio)
    if ob_abs >= 0.30:
        score += 10
    elif ob_abs >= 0.20:
        score += 7
    elif ob_abs >= 0.10:
        score += 4
    elif ob_abs >= 0.05:
        score += 2

    # VSA effort/result (0-20 pts) — ядро методологии Герчика
    if vsa_type == "CLIMAX":
        score += 20
    elif vsa_type == "ABSORPTION":
        score += 15
    elif vsa_type == "NO_DEMAND_SUPPLY":
        score += 5  # само по себе слабый сигнал, но полезно как контекст

    # Близость к ключевому уровню (0-10 pts) — чем ближе, тем выше
    if level_dist_atr is not None:
        if level_dist_atr <= 0.25:
            score += 10
        elif level_dist_atr <= 0.5:
            score += 7
        elif level_dist_atr <= KEY_LEVEL_ATR_MULT:
            score += 4

    # Классификация типа сигнала (как раньше, по OI/price)
    if oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change < -0.3:
        sig_type = "DISTRIBUTION"
    elif oi_change >= cfg.OI_CHANGE_THRESHOLD:
        sig_type = "ACCUMULATION"
    elif oi_change <= -cfg.OI_CHANGE_THRESHOLD:
        sig_type = "SQUEEZE"
    elif vsa_type in ("CLIMAX", "ABSORPTION"):
        sig_type = "VSA_" + vsa_type
    elif vol_ratio >= cfg.VOL_SPIKE_MULT * 1.5:
        sig_type = "VOLUME_SPIKE"
    elif fund_abs >= cfg.FUNDING_EXTREME:
        sig_type = "FUNDING_EXTREME"
    else:
        sig_type = "MOMENTUM"

    return min(score, 100), sig_type


def _direction(sig_type: str, price_change: float, ob_bias: str, funding: float,
               vsa_bias: str = "NEUTRAL") -> tuple[str, float]:
    """
    Направление сделки + confidence (0-1): доля независимых голосов
    (цена / стакан / фандинг / VSA), согласных с выбранным направлением.
    Confluence-механизм сохранён намеренно — см. CLAUDE.md, рецидивирующий баг:
    score и direction не должны считаться независимо друг от друга.
    """
    votes: list[str] = []
    if abs(price_change) > 0.1:
        votes.append("LONG" if price_change > 0 else "SHORT")
    if ob_bias != "NEUTRAL":
        votes.append("LONG" if ob_bias == "BUY" else "SHORT")
    # Отрицательный фандинг: шорты платят лонгам -> давление вверх (контрарно)
    if abs(funding) >= 0.01:
        votes.append("LONG" if funding < 0 else "SHORT")
    if vsa_bias != "NEUTRAL":
        votes.append(vsa_bias)

    if sig_type.startswith("VSA_") and vsa_bias != "NEUTRAL":
        primary = vsa_bias
    elif sig_type == "ACCUMULATION":
        primary = "LONG"
    elif sig_type == "DISTRIBUTION":
        primary = "SHORT"
    elif sig_type == "SQUEEZE":
        # Резкое падение OI само по себе направления не задаёт — решает
        # большинство голосов, при ничьей падаем на движение цены.
        long_votes  = votes.count("LONG")
        short_votes = votes.count("SHORT")
        if long_votes > short_votes:
            primary = "LONG"
        elif short_votes > long_votes:
            primary = "SHORT"
        else:
            primary = "LONG" if price_change > 0 else "SHORT"
    elif sig_type == "FUNDING_EXTREME":
        primary = "SHORT" if funding > 0 else "LONG"
    elif ob_bias != "NEUTRAL":
        primary = "LONG" if ob_bias == "BUY" else "SHORT"
    else:
        primary = "LONG" if price_change > 0 else "SHORT"

    if votes:
        agree = sum(1 for v in votes if v == primary)
        confidence = agree / len(votes)
    else:
        confidence = 0.4  # нет независимых голосов -> низкая уверенность, не нейтрально

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
                  support: Optional[float], resistance: Optional[float]) -> Optional[dict]:
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

    # Если есть реальный противоположный уровень — проверяем, что до него хватает
    # расстояния на MIN_RR. Если уровня нет ("открытое небо") — доверяем стандартной
    # 1R/2R/3R сетке, т.к. TP3 и так на 3R.
    if target_level is not None:
        achievable = abs(target_level - price) / risk
        if achievable < MIN_RR:
            return None  # недостаточно места до цели для честного 3:1 — не торгуем

    if direction == "LONG":
        tp1, tp2, tp3 = price + risk * 1.0, price + risk * 2.0, price + risk * 3.0
    else:
        tp1, tp2, tp3 = price - risk * 1.0, price - risk * 2.0, price - risk * 3.0

    sl_pct = sl_dist / price * 100

    return {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr": MIN_RR, "sl_pct": sl_pct,
    }


async def _analyze_symbol(client: BybitClient, ticker: dict) -> Optional[Signal]:
    symbol = ticker.get("symbol", "")
    if symbol in cfg.BLACKLIST:
        return None

    try:
        price       = float(ticker.get("lastPrice",     0))
        price_chg   = float(ticker.get("price24hPcnt",  0)) * 100
        funding     = float(ticker.get("fundingRate",   0)) * 100
        vol_24h     = float(ticker.get("volume24h",     0))
        oi_usdt_now = float(ticker.get("openInterestValue", 0))

        if price <= 0:
            return None

        if vol_24h < cfg.MIN_VOL_24H:
            return None

        if abs(price_chg) < cfg.PRICE_CHANGE_MIN and abs(funding) < 0.01:
            return None

        # Инвариант: не торговать новостные/листинговые спайки. Проверяем
        # ДО тяжёлых запросов (klines/orderbook/OI), чтобы не тратить на
        # свежие листинги лишние вызовы API.
        if not await _is_listing_old_enough(client, symbol):
            return None

        # Добавили 1h klines для MTF-подтверждения тренда наряду с 4h
        oi_hist, klines, ob, klines_1h = await asyncio.gather(
            client.get_open_interest(symbol, interval="4h", limit=2),
            client.get_klines(symbol, interval="240", limit=26),
            client.get_orderbook(symbol, limit=20),
            client.get_klines(symbol, interval="60", limit=max(MTF_TREND_LOOKBACK + 3, 10)),
        )

        if not oi_hist or not klines:
            log.warning(f"{symbol}: partial data — oi_hist={len(oi_hist)} klines={len(klines)}")
            if not klines:
                return None

        if len(oi_hist) >= 1 and price > 0:
            oi_prev_usdt = oi_hist[-1]["oi"] * price
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

        # Ключевые уровни на 4h
        support, resistance = _find_swing_levels(klines)
        level_dist_atr = None
        if atr > 0:
            dists = []
            if support is not None:
                dists.append(abs(price - support) / atr)
            if resistance is not None:
                dists.append(abs(price - resistance) / atr)
            if dists:
                level_dist_atr = min(dists)

        score, sig_type = _score_signal(
            oi_change, vol_ratio, funding, ob_ratio, price_chg,
            vsa_type=vsa_type, level_dist_atr=level_dist_atr,
        )

        direction, confidence = _direction(
            sig_type, price_chg, ob_bias, funding, vsa_bias=vsa_bias,
        )

        # Confluence cap ПОСЛЕ определения направления, ДО порога MIN_SCORE —
        # противоречащий сигнал не должен проходить фильтр на сырой магнитуде
        score = _apply_confluence_cap(score, confidence)
        if score < cfg.MIN_SCORE:
            return None

        # MTF-фильтр: тренд на 1h не должен противоречить тренду на 4h и направлению сделки.
        # Это жёсткий отсекающий фильтр (не просто очки в score), как требует методология.
        if REQUIRE_MTF_ALIGN:
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
        relevant_level = support if direction == "LONG" else resistance
        if relevant_level is None or atr <= 0:
            return None
        relevant_dist_atr = abs(price - relevant_level) / atr
        if relevant_dist_atr > KEY_LEVEL_ATR_MULT:
            return None

        levels = _calc_levels(price, atr, direction, support, resistance)
        if levels is None:
            # либо нет валидного риска, либо не набирается MIN_RR до цели
            return None

        details = (
            f"{sig_type} | {direction} | score={score} | conf={confidence:.2f} | "
            f"OI {oi_change:+.1f}% | vol {vol_ratio:.1f}x | "
            f"funding {funding:+.3f}% | OB {ob_bias} | VSA {vsa_type} | "
            f"ATR {atr_pct:.2f}% | RR {levels['rr']:.1f}"
        )

        return Signal(
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
            sl_pct=levels["sl_pct"],
        )
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
            log.warning("scan_all: 0 symbols after filter — Bybit API may be unreachable")
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

        signals.sort(key=lambda s: s.score, reverse=True)
        state.last_scan_at = datetime.utcnow()
        state.scan_count += 1
        state.total_signals += len(signals)
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
        state.last_scan_error = str(e)
        log.error(f"scan_all error (scan #{state.scan_count}): {e}")
        return []
    finally:
        _SCANNING = False


async def run_scan_and_broadcast(client: BybitClient, ntfy_url: str = "") -> List[Signal]:
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
            continue
        state.signal_seen[sig.symbol] = now

        try:
            await db.save_signal(sig)
        except Exception as dbe:
            log.error(f"run_scan_and_broadcast: db.save_signal({sig.symbol}) failed — {dbe}")

        await enter_trade(client, sig)

        try:
            msg = json.dumps({"type": "signal", "data": sig.to_dict()})
        except Exception as je:
            log.error(f"run_scan_and_broadcast: sig.to_dict() failed for {sig.symbol} — {je}")
            continue
        dead = set()
        for ws in state.ws_clients:
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
    for ws in state.ws_clients:
        try:
            await ws.send_text(heartbeat)
        except Exception:
            dead.add(ws)
    for ws in dead:
        state.remove_ws(ws)

    return signals
