import logging
from dataclasses import dataclass
import numpy as np

log = logging.getLogger("strategy")

# Scan rejection stats — reset before each scan, read after
_stats: dict = {}

def reset_stats():
    global _stats
    _stats = {}

def get_stats() -> dict:
    return dict(_stats)

def _reject(reason: str):
    _stats[reason] = _stats.get(reason, 0) + 1

def _px(p: float) -> float:
    """Round price to exchange-compatible precision."""
    if p >= 10:   return round(p, 2)
    if p >= 1:    return round(p, 4)
    if p >= 0.01: return round(p, 5)
    return round(p, 6)

@dataclass
class Signal:
    symbol:  str
    side:    str
    entry:   float
    sl:      float
    tp1:     float
    tp2:     float
    tp3:     float
    rr:      float
    pattern: str
    tf:      str
    score:   int
    reason:  str

# ──────────────────────────────────────────── helpers ──

def parse_klines(raw):
    if not raw:
        return {}
    rows = []
    for k in raw:
        try:
            if isinstance(k, dict):
                t = float(k.get("time", k.get("t", 0)))
                o = float(k.get("open",   k.get("o", 0)))
                h = float(k.get("high",   k.get("h", 0)))
                l = float(k.get("low",    k.get("l", 0)))
                c = float(k.get("close",  k.get("c", 0)))
                v = float(k.get("volume", k.get("v", 0)))
            else:
                t, o, h, l, c, v = (float(x) for x in k[:6])
            if o <= 0 or h <= 0 or l <= 0 or c <= 0 or l > h or o > h or c > h or o < l or c < l:
                continue
            rows.append([t, o, h, l, c, v])
        except Exception:
            continue
    if not rows:
        return {}
    arr = np.array(rows)
    return {
        "ts": arr[:,0], "open": arr[:,1], "high": arr[:,2],
        "low": arr[:,3], "close": arr[:,4], "volume": arr[:,5],
    }

def ema(values, period):
    result = np.zeros(len(values))
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result

def rsi(closes, period=14):
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.zeros(len(closes))
    avg_l  = np.zeros(len(closes))
    avg_g[period] = gains[:period].mean()
    avg_l[period] = losses[:period].mean()
    for i in range(period+1, len(closes)):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i-1]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i-1]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l == 0, np.where(avg_g == 0, 0.0, 100.0), avg_g / avg_l)
    # Flat price (avg_g==0 and avg_l==0) → RSI 50 (neutral), not 100
    return np.where(avg_l == 0, np.where(avg_g == 0, 50.0, 100.0), 100 - 100/(1+rs))

def atr(highs, lows, closes, period=14):
    n  = len(closes)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i],
                    abs(highs[i]-closes[i-1]),
                    abs(lows[i]-closes[i-1]))
    result = np.zeros(n)
    result[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        result[i] = (result[i-1]*(period-1) + tr[i]) / period
    return result

def vol_ma(volumes, period=20):
    result = np.zeros_like(volumes)
    for i in range(period-1, len(volumes)):
        result[i] = volumes[i-period+1:i+1].mean()
    return result

def _merge_levels(levels, merge_pct=1.0):
    """Deduplicate levels within merge_pct% of each other, keeping the cluster mean."""
    if not levels:
        return []
    sorted_lvls = sorted(levels)
    merged = []
    cluster_start = sorted_lvls[0]
    cluster = [sorted_lvls[0]]
    for lvl in sorted_lvls[1:]:
        if cluster_start > 0 and abs(lvl - cluster_start) / cluster_start * 100 <= merge_pct:
            cluster.append(lvl)
        else:
            merged.append(sum(cluster) / len(cluster))
            cluster_start = lvl
            cluster = [lvl]
    merged.append(sum(cluster) / len(cluster))
    return merged

def find_levels(highs, lows, lookback=80):
    rh, rl = highs[-lookback:], lows[-lookback:]
    res, sup = [], []
    for i in range(5, len(rh)-5):
        if rh[i] > max(rh[i-1], rh[i-2], rh[i-3], rh[i-4], rh[i-5],
                       rh[i+1], rh[i+2], rh[i+3], rh[i+4], rh[i+5]):
            res.append(float(rh[i]))
    for i in range(5, len(rl)-5):
        if rl[i] < min(rl[i-1], rl[i-2], rl[i-3], rl[i-4], rl[i-5],
                       rl[i+1], rl[i+2], rl[i+3], rl[i+4], rl[i+5]):
            sup.append(float(rl[i]))
    return {"resistance": _merge_levels(res), "support": _merge_levels(sup)}


def find_weekly_levels(w1):
    """
    Find key weekly levels from W1 candles — the 'Gerchik levels'.
    These are the major pivots visible on weekly chart, same as what
    Gerchik uses to predict significant price targets.
    """
    if not w1 or len(w1["close"]) < 10:
        return {"resistance": [], "support": []}
    return find_levels(w1["high"], w1["low"], lookback=min(52, len(w1["high"])))


def nearest_weekly_levels(price, w1, count=3):
    """
    Return the nearest weekly support and resistance levels above/below price.
    Used for target projection — 'where will price go next'.
    """
    lvls = find_weekly_levels(w1)
    supports    = sorted([l for l in lvls["support"]    if l < price], reverse=True)[:count]
    resistances = sorted([l for l in lvls["resistance"] if l > price])[:count]
    return {"support": supports, "resistance": resistances}

def level_last_touch_age(level, highs, lows, tol=0.5):
    """Returns candles since the level was last touched. Large = stale level."""
    t = level * tol / 100
    for i in range(len(highs) - 1, -1, -1):
        if abs(highs[i] - level) <= t or abs(lows[i] - level) <= t:
            return len(highs) - 1 - i
    return len(highs)


def near_level(price, levels, tol=0.8):
    if price <= 0:
        return False, 0.0
    best = (False, 0.0, 999.0)
    for lvl in levels:
        dist = abs(price-lvl)/price*100
        if dist <= tol and dist < best[2]:
            best = (True, lvl, dist)
    return best[0], best[1]

def level_touches(level, highs, lows, tol=0.5):
    t = level*tol/100
    return sum(1 for h,l in zip(highs, lows) if abs(h-level)<=t or abs(l-level)<=t)

def trend_slope(values, period=5):
    e = ema(values, 20)
    ref = e[-period] if len(e) >= period else 0
    return (e[-1] - ref) / ref * 100 if ref > 0 else 0.0

def adx(highs, lows, closes, period=14):
    n = len(closes)
    tr = np.zeros(n); plus_dm = np.zeros(n); minus_dm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i]  = up   if up > down and up > 0   else 0
        minus_dm[i] = down if down > up and down > 0 else 0
    atr_s = np.zeros(n); pdm_s = np.zeros(n); mdm_s = np.zeros(n)
    atr_s[period] = tr[1:period+1].sum()
    pdm_s[period] = plus_dm[1:period+1].sum()
    mdm_s[period] = minus_dm[1:period+1].sum()
    for i in range(period+1, n):
        atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
        pdm_s[i] = pdm_s[i-1] - pdm_s[i-1]/period + plus_dm[i]
        mdm_s[i] = mdm_s[i-1] - mdm_s[i-1]/period + minus_dm[i]
    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr_s > 0, pdm_s / atr_s * 100, 0)
        mdi = np.where(atr_s > 0, mdm_s / atr_s * 100, 0)
        dx  = np.where((pdi + mdi) > 0, np.abs(pdi - mdi) / (pdi + mdi) * 100, 0)
    adx_v = np.zeros(n)
    start = 2 * period
    if start < n:
        adx_v[start] = dx[period:start+1].mean()
        for i in range(start+1, n):
            adx_v[i] = (adx_v[i-1] * (period-1) + dx[i]) / period
    return adx_v

def macd(closes, fast=12, slow=26, signal_period=9):
    macd_line   = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(macd_line, signal_period)
    return macd_line, signal_line, macd_line - signal_line


def detect_rsi_divergence(closes, rsi_vals, signal_side, lookback=24):
    """
    Detect RSI divergence against signal direction on H4.
    Returns True = weakening momentum = score penalty.

    LONG warning  (bearish divergence): price higher high, RSI lower high
    SHORT warning (bullish divergence): price lower low,  RSI higher low
    """
    n = min(lookback, len(closes), len(rsi_vals))
    if n < 10:
        return False
    rec_c = closes[-n:]
    rec_r = rsi_vals[-n:]
    mid   = n // 2
    if signal_side == "LONG":
        i1 = int(np.argmax(rec_c[:mid]))
        i2 = mid + int(np.argmax(rec_c[mid:]))
        return rec_c[i2] > rec_c[i1] * 1.01 and rec_r[i2] < rec_r[i1] - 5
    else:
        i1 = int(np.argmin(rec_c[:mid]))
        i2 = mid + int(np.argmin(rec_c[mid:]))
        return rec_c[i2] < rec_c[i1] * 0.99 and rec_r[i2] > rec_r[i1] + 5

# ─────────────────────────────────────── patterns ──

def hammer(o,h,l,c):
    body=abs(c-o); full=h-l
    return full>0 and body>0 and c>o and body/full<=0.4 and (min(o,c)-l)>=body*1.5

def shooting_star(o,h,l,c):
    body=abs(c-o); full=h-l
    return full>0 and body>0 and c<o and body/full<=0.4 and (h-max(o,c))>=body*1.5

def bull_engulf(o1,c1,o2,c2):
    body1 = abs(c1-o1); body2 = abs(c2-o2)
    return c1<o1 and c2>o2 and o2<=c1 and c2>=o1 and (body1==0 or body2>=body1*1.2)

def bear_engulf(o1,c1,o2,c2):
    body1 = abs(c1-o1); body2 = abs(c2-o2)
    return c1>o1 and c2<o2 and o2>=c1 and c2<=o1 and (body1==0 or body2>=body1*1.2)

def bull_pin(o,h,l,c):
    body=abs(c-o); full=h-l
    return full>0 and (min(o,c)-l)>full*0.55 and body<full*0.3 and c>=o

def bear_pin(o,h,l,c):
    body=abs(c-o); full=h-l
    return full>0 and (h-max(o,c))>full*0.55 and body<full*0.3 and c<=o

def doji(o,h,l,c):
    body=abs(c-o); full=h-l
    return full>0 and body/full<=0.12

def inside_bar(h, l, h_prev, l_prev):
    """Current candle range is completely inside the previous candle — compression before move."""
    return h < h_prev and l > l_prev and (h_prev - l_prev) > 0

def detect_pattern(candles, idx=-1):
    i  = idx if idx >= 0 else len(candles["open"])+idx
    o  = candles["open"][i];  h = candles["high"][i]
    l  = candles["low"][i];   c = candles["close"][i]
    o1 = candles["open"][i-1];c1= candles["close"][i-1]
    h1p= candles["high"][i-1];l1p= candles["low"][i-1]
    if hammer(o,h,l,c):            return "Молот","LONG"
    if bull_pin(o,h,l,c):          return "Пин-бар (бычий)","LONG"
    if bull_engulf(o1,c1,o,c):     return "Бычье поглощение","LONG"
    if shooting_star(o,h,l,c):     return "Падающая звезда","SHORT"
    if bear_pin(o,h,l,c):          return "Пин-бар (медвежий)","SHORT"
    if bear_engulf(o1,c1,o,c):     return "Медвежье поглощение","SHORT"
    if inside_bar(h, l, h1p, l1p): return "Внутренний бар","DOJI"
    if doji(o,h,l,c):              return "Доджи","DOJI"
    return "",""

# ─────────────────────────────────────── main ──

def analyze(symbol, d1, h4, h1, funding, cfg, d1_levels=None):
    if not d1 or not h4 or not h1:
        _reject("нет данных")
        return None
    if len(d1["close"]) < cfg.TREND_EMA_D1:
        _reject("мало D1 свечей")
        return None
    if len(h4["close"]) < cfg.TREND_EMA_H4 + 5:
        _reject("мало H4 свечей")
        return None
    if len(h1["close"]) < 40:
        _reject("мало H1 свечей")
        return None

    # ── D1 trend ──
    ema200  = ema(d1["close"], cfg.TREND_EMA_D1)
    d1_up   = d1["close"][-1] > ema200[-1]
    trend   = "LONG" if d1_up else "SHORT"
    d1_slope= trend_slope(d1["close"], 10)

    # Require minimum distance from EMA200 — price too close = trend unclear
    d1_ema_dist = abs(d1["close"][-1] - ema200[-1]) / ema200[-1] * 100 if ema200[-1] > 0 else 0
    if d1_ema_dist < 0.3:
        _reject("D1 у EMA200 (зона неопределённости)")
        return None

    # Slope filter: block only strong reversals (> D1_SLOPE_MAX_DECLINE% over 10 days)
    if trend == "LONG"  and d1_slope < -cfg.D1_SLOPE_MAX_DECLINE:
        _reject("D1 разворот вниз")
        return None
    if trend == "SHORT" and d1_slope > cfg.D1_SLOPE_MAX_DECLINE:
        _reject("D1 разворот вверх")
        return None

    # ── H4 filter ──
    ema50   = ema(h4["close"], cfg.TREND_EMA_H4)
    h4_up   = h4["close"][-1] > ema50[-1]
    h4_dn   = h4["close"][-1] < ema50[-1]
    h4_aligned = (trend=="LONG" and h4_up) or (trend=="SHORT" and h4_dn)
    h4_near    = abs(h4["close"][-1]-ema50[-1])/ema50[-1]*100 < cfg.H4_NEAR_PCT
    if not h4_aligned and not h4_near:
        _reject("H4 против тренда")
        return None
    if h4_near and not h4_aligned:
        h4_slope = (ema50[-1] - ema50[-5]) / ema50[-5] * 100 if ema50[-5] > 0 else 0
        if trend == "LONG"  and h4_slope < cfg.H4_SLOPE_FLOOR:
            _reject("H4 против тренда")
            return None
        if trend == "SHORT" and h4_slope > abs(cfg.H4_SLOPE_FLOOR):
            _reject("H4 против тренда")
            return None
        # Reject: price approaching EMA50 from the WRONG side (bounce into resistance).
        # For LONG: price below EMA50 must have been ABOVE it recently (pullback from above).
        # For SHORT: price above EMA50 must have been BELOW it recently (bounce from below).
        lookback = min(cfg.H4_WAS_ABOVE_LOOKBACK, len(h4["close"]) - 1)
        if trend == "LONG":
            was_above = any(h4["close"][-lookback-1:-1] > ema50[-lookback-1:-1])
            if not was_above:
                _reject("H4 у EMA снизу (не откат — сопротивление)")
                return None
        else:
            was_below = any(h4["close"][-lookback-1:-1] < ema50[-lookback-1:-1])
            if not was_below:
                _reject("H4 у EMA сверху (не откат — поддержка)")
                return None

    # H4 bearish/bullish impulse guard: if 3+ of last 4 H4 candles go against trend with
    # meaningful bodies (≥0.3% of price), skip — market is in corrective impulse.
    _h4_ref = max(float(h4["close"][-1]), 0.001)
    _h4_body_min = _h4_ref * 0.003
    h4_bear_cnt = sum(
        1 for i in (-1,-2,-3,-4)
        if h4["close"][i] < h4["open"][i]
        and (h4["open"][i] - h4["close"][i]) >= _h4_body_min
    )
    h4_bull_cnt = sum(
        1 for i in (-1,-2,-3,-4)
        if h4["close"][i] > h4["open"][i]
        and (h4["close"][i] - h4["open"][i]) >= _h4_body_min
    )
    if trend == "LONG"  and h4_bear_cnt >= 4:
        _reject("H4 медвежий импульс")
        return None
    if trend == "SHORT" and h4_bull_cnt >= 4:
        _reject("H4 бычий импульс")
        return None

    price = h1["close"][-1]

    # ── RSI filter ──
    h1_rsi  = rsi(h1["close"], 14)
    cur_rsi = h1_rsi[-1]
    if trend == "LONG"  and cur_rsi > 75:
        _reject("RSI перекуплен")
        return None
    if trend == "SHORT" and cur_rsi < 25:
        _reject("RSI перепродан")
        return None

    # ── S/R levels — LONG at support, SHORT at resistance ──
    lv4 = find_levels(h4["high"], h4["low"], lookback=120)
    lv1 = find_levels(h1["high"], h1["low"], lookback=80)
    if trend == "LONG":
        primary = lv4["support"] + lv1["support"]
    else:
        primary = lv4["resistance"] + lv1["resistance"]
    near, level = near_level(price, primary, tol=cfg.SR_NEAR_PCT)
    if not near:
        _reject("не у уровня S/R")
        return None

    touches = level_touches(level, h4["high"][-120:], h4["low"][-120:])
    if touches == 0:
        # Level may be H1-specific — fall back to H1 confirmation
        touches = level_touches(level, h1["high"][-80:], h1["low"][-80:])
    if touches == 0:
        _reject("уровень не подтверждён (0 касаний H4/H1)")
        return None
    if touches > 6:
        _reject("уровень пробит (>6 касаний)")
        return None

    # ── ATR-based SL ──
    h1_atr  = atr(h1["high"], h1["low"], h1["close"], 14)
    cur_atr = h1_atr[-1]

    # ── ADX — market regime filter (skip ranging markets) ──
    h4_adx  = adx(h4["high"], h4["low"], h4["close"], 14)
    cur_adx = h4_adx[-1]
    if cur_adx < cfg.ADX_MIN:
        _reject("ADX низкий (боковик)")
        return None

    # ── MACD on H1 — momentum confirmation ──
    _, _, macd_hist = macd(h1["close"])
    macd_aligned = (trend == "LONG"  and macd_hist[-1] > macd_hist[-2]) or \
                   (trend == "SHORT" and macd_hist[-1] < macd_hist[-2])

    # ── ATR volatility — skip flat or explosive markets ──
    atr_pct = cur_atr / price * 100
    if atr_pct < 0.2:
        _reject("ATR слишком мал (флэт)")
        return None
    if atr_pct > 5.0:
        _reject("ATR слишком велик (взрыв)")
        return None
    # ATR spike: current ATR > 2× recent mean = market in explosion mode, skip
    _atr_hist_mean = float(np.mean(h1_atr[-20:-1])) if len(h1_atr) > 20 else cur_atr
    if _atr_hist_mean > 0 and cur_atr / _atr_hist_mean > 2.0:
        _reject("ATR взрыв (спайк волатильности)")
        return None

    # ── Candle pattern on H1: use index -2 (last COMPLETED candle) ──
    # Reject micro-candles: range < 25% of ATR means it's noise, not a real pattern
    pat_range = h1["high"][-2] - h1["low"][-2]
    if cur_atr > 0 and pat_range < cur_atr * 0.25:
        _reject("паттерн: свеча слишком мала (шум)")
        return None
    pname, pside = detect_pattern(h1, -2)
    if not pname:
        _reject("нет паттерна H1")
        return None
    is_doji = pside == "DOJI"
    if is_doji:
        pside = trend
    if pside != trend:
        _reject("паттерн против тренда")
        return None

    # Pattern invalidation
    pat_close = h1["close"][-2]
    pat_low   = h1["low"][-2]
    pat_high  = h1["high"][-2]
    if trend == "LONG"  and price < pat_low:
        _reject("паттерн недействителен")
        return None
    if trend == "SHORT" and price > pat_high:
        _reject("паттерн недействителен")
        return None
    if trend == "LONG"  and price > pat_close * 1.015:
        _reject("цена ушла от паттерна")
        return None
    if trend == "SHORT" and price < pat_close * 0.985:
        _reject("цена ушла от паттерна")
        return None

    h4p, h4s = detect_pattern(h4, -2)
    h4ok = h4p != "" and (h4s == trend or h4s == "DOJI")
    if is_doji and not h4ok:
        _reject("доджи без H4 подтверждения")
        return None
    # Near-EMA zone (not aligned): H4 pattern gives score bonus but is NOT a hard requirement.
    # was-above/was-below guard already ensures this is a genuine pullback, not a resistance approach.

    # ── Volume ──
    vm    = vol_ma(h1["volume"], cfg.VOLUME_MA_PERIOD)
    vrat  = h1["volume"][-2] / vm[-2] if vm[-2] > 0 else 0
    if vrat < cfg.VOLUME_MULT:
        _reject("объём низкий")
        return None

    # ── Funding rate ──
    if funding is not None:
        if trend=="LONG"  and funding > cfg.FUNDING_MAX_LONG:
            _reject("фандинг высокий")
            return None
        if trend=="SHORT" and funding < cfg.FUNDING_MAX_SHORT:
            _reject("фандинг низкий")
            return None

    buf     = price * cfg.SL_BUFFER_PCT / 100
    atr_sl  = cur_atr * 1.5  # 1.5× ATR gives trade room to breathe vs noise

    if trend == "LONG":
        sl_candle = h1["low"][-2] - buf
        sl_atr    = price - atr_sl
        # Prefer structural candle SL; use ATR only if candle SL is too tight (<0.4%)
        sl = sl_candle if (price - sl_candle) / price >= 0.004 else sl_atr
    else:
        sl_candle = h1["high"][-2] + buf
        sl_atr    = price + atr_sl
        sl = sl_candle if (sl_candle - price) / price >= 0.004 else sl_atr

    sld = abs(price - sl)
    if price <= 0 or sld <= 0 or sld / price < 0.004:
        _reject("SL слишком узкий (шум)")
        return None
    if sld / price > 0.05:
        _reject("SL слишком широкий")
        return None

    sl  = _px(sl)
    tp1 = _px(price + sld*cfg.TP1_RR if trend=="LONG" else price - sld*cfg.TP1_RR)
    tp2 = _px(price + sld*cfg.TP2_RR if trend=="LONG" else price - sld*cfg.TP2_RR)
    tp3 = _px(price + sld*cfg.TP3_RR if trend=="LONG" else price - sld*cfg.TP3_RR)
    rr  = cfg.TP2_RR
    if rr < cfg.MIN_RR:
        _reject(f"R/R слишком мал ({rr:.1f} < {cfg.MIN_RR})")
        return None

    # ── Score ──
    score = 50
    # Volume
    if vrat >= 2.5:   score += 12
    elif vrat >= 2.0: score += 10
    elif vrat >= 1.5: score += 6
    else:             score += 3
    # Level quality — Gerchik: 2–3 touches optimal, 1 = unconfirmed (no bonus), 5+ = exhausted
    if touches == 2:    score += 12
    elif touches == 3:  score += 10
    elif touches == 4:  score += 5
    # 5+ touches: no bonus (level near exhaustion)
    # Level freshness — recently touched level is still respected by market
    # Check both H1 and H4 (level may originate from either TF), use the freshest
    touch_age_h1 = level_last_touch_age(level, h1["high"][-60:], h1["low"][-60:])
    touch_age_h4 = level_last_touch_age(level, h4["high"][-120:], h4["low"][-120:]) * 4  # convert H4→H1 units
    touch_age = min(touch_age_h1, touch_age_h4)
    if touch_age <= 5:    score += 8
    elif touch_age <= 15: score += 5
    elif touch_age <= 30: score += 2
    # H4 confirmation
    if h4ok:          score += 10
    if h4_aligned:    score += 5
    elif h4_near:     score += 3
    # RSI positioning: good zone (+8), ideal zone adds extra +4
    rsi_ok    = (trend=="LONG" and 35 <= cur_rsi <= 60) or \
                (trend=="SHORT" and 40 <= cur_rsi <= 65)
    rsi_ideal = (trend=="LONG" and 42 <= cur_rsi <= 55) or \
                (trend=="SHORT" and 45 <= cur_rsi <= 58)
    if rsi_ok:    score += 8
    if rsi_ideal: score += 4   # perfect momentum window
    # Funding (None when API unavailable — no bonus, no penalty)
    if funding is not None:
        if abs(funding) < 0.01:   score += 8
        elif abs(funding) < 0.02: score += 4
    # D1 slope — aligned with trend direction
    if (trend == "LONG"  and d1_slope > 0.1) or \
       (trend == "SHORT" and d1_slope < -0.1):
        score += 5
    # MACD momentum confirmation
    if macd_aligned:
        score += 8
    # ADX strength bonus
    if cur_adx >= 40:   score += 8   # very strong trend
    elif cur_adx >= 30: score += 5
    elif cur_adx >= 25: score += 3
    # RSI divergence on H4: weakening momentum → penalty
    h4_rsi_v = rsi(h4["close"], 14)
    if detect_rsi_divergence(h4["close"], h4_rsi_v, trend):
        score -= 12
    # D1 level proximity: entering near major daily obstacle → penalty
    d1_lv = d1_levels if d1_levels is not None \
            else find_levels(d1["high"], d1["low"], lookback=min(120, len(d1["high"])))
    d1_obstacles = d1_lv["resistance"] if trend == "LONG" else d1_lv["support"]
    is_near_d1, _ = near_level(price, d1_obstacles, tol=2.0)
    if is_near_d1:
        score -= 10
    score = min(score, 100)
    if score < cfg.MIN_SCORE:
        _reject("score ниже MIN_SCORE")
        return None

    rsi_str  = f"{cur_rsi:.0f}"
    atr_str  = f"{cur_atr:.4f}"
    macd_str = "🟢" if macd_aligned else "⚪"
    funding_line = (f"💱 Funding: <code>{funding:.4f}%</code>\n"
                    if funding is not None else "💱 Funding: <code>н/д</code>\n")
    reason  = (
        f"📊 <b>{symbol}</b> | {trend}\n"
        f"🕯 H1: {pname} | H4: {h4p if h4ok else '—'}\n"
        f"📈 D1: {'🟢' if d1_up else '🔴'} EMA200 slope {d1_slope:+.2f}%\n"
        f"H4: {'🟢' if h4_up else '🔴'} EMA50 | ADX: <code>{cur_adx:.1f}</code>\n"
        f"🎯 Уровень: <code>{level:.4f}</code> ({touches} кас., свежесть: {touch_age} св.)\n"
        f"📦 Объём: <code>{vrat:.2f}×</code> | RSI: <code>{rsi_str}</code> | "
        f"MACD: {macd_str} | ATR: <code>{atr_str}</code>\n"
        + funding_line
        + f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
        f"🟢 TP2: <code>{tp2:.4f}</code> | TP3: <code>{tp3:.4f}</code>\n"
        f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100"
    )
    return Signal(
        symbol=symbol, side=trend,
        entry=price, sl=sl,
        tp1=tp1, tp2=tp2, tp3=tp3,
        rr=rr, pattern=pname, tf="H1+H4",
        score=score, reason=reason,
    )


def analyze_false_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=None):
    """
    Ложный пробой — сетап №1 по Герчику.

    Цена тенью пробивает ключевой уровень, но закрывается обратно на
    «правильной» стороне.  Это означает, что крупный игрок (лимитная
    заявка) поглотил агрессоров и защитил уровень.  Чем больше объём
    на ложном пробое — тем сильнее подтверждение.
    SL ставится за экстремум ложного пробоя (хвост свечи).
    """
    if not d1 or not h4 or not h1:
        return None
    if len(d1["close"]) < cfg.TREND_EMA_D1 or len(h4["close"]) < cfg.TREND_EMA_H4 + 5 or len(h1["close"]) < 40:
        return None

    # ── D1 trend ──
    ema200   = ema(d1["close"], cfg.TREND_EMA_D1)
    d1_up    = d1["close"][-1] > ema200[-1]
    trend    = "LONG" if d1_up else "SHORT"
    d1_slope = trend_slope(d1["close"], 10)

    # Require minimum distance from EMA200 — too close = trend unclear
    _fb_d1_dist = abs(d1["close"][-1] - ema200[-1]) / ema200[-1] * 100 if ema200[-1] > 0 else 0
    if _fb_d1_dist < 0.3:
        _reject("ложный пробой: D1 у EMA200 (зона неопределённости)")
        return None

    # Slope filter — same threshold as analyze()
    if trend == "LONG"  and d1_slope < -cfg.D1_SLOPE_MAX_DECLINE:
        _reject("ложный пробой: D1 разворот вниз")
        return None
    if trend == "SHORT" and d1_slope > cfg.D1_SLOPE_MAX_DECLINE:
        _reject("ложный пробой: D1 разворот вверх")
        return None

    # ── H4 filter — must be aligned with D1 trend (tolerance 2%) ──
    ema50_h4  = ema(h4["close"], cfg.TREND_EMA_H4)
    h4_up     = h4["close"][-1] > ema50_h4[-1]
    h4_aligned = (trend == "LONG" and h4_up) or (trend == "SHORT" and not h4_up)
    h4_near_fb = abs(h4["close"][-1] - ema50_h4[-1]) / ema50_h4[-1] * 100 < cfg.H4_NEAR_PCT
    if not h4_aligned and not h4_near_fb:
        _reject("ложный пробой: H4 против тренда")
        return None
    if h4_near_fb and not h4_aligned:
        lookback = min(cfg.H4_WAS_ABOVE_LOOKBACK, len(h4["close"]) - 1)
        if trend == "LONG":
            if not any(h4["close"][-lookback-1:-1] > ema50_h4[-lookback-1:-1]):
                _reject("ложный пробой: H4 у EMA снизу (не откат — сопротивление)")
                return None
        else:
            if not any(h4["close"][-lookback-1:-1] < ema50_h4[-lookback-1:-1]):
                _reject("ложный пробой: H4 у EMA сверху (не откат — поддержка)")
                return None

    # H4 impulse guard: 4 of last 4 candles against signal direction → skip
    h4_bear_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] < h4["open"][i])
    h4_bull_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] > h4["open"][i])
    if trend == "LONG"  and h4_bear_cnt >= 4:
        _reject("ложный пробой: H4 медвежий импульс")
        return None
    if trend == "SHORT" and h4_bull_cnt >= 4:
        _reject("ложный пробой: H4 бычий импульс")
        return None

    # ── ADX — must be trending ──
    h4_adx_v = adx(h4["high"], h4["low"], h4["close"], 14)
    cur_adx  = h4_adx_v[-1]
    if cur_adx < cfg.ADX_MIN:
        _reject("ложный пробой: ADX низкий (боковик)")
        return None

    price = h1["close"][-1]

    # ── S/R levels ──
    lv4 = find_levels(h4["high"], h4["low"], lookback=120)
    lv1 = find_levels(h1["high"], h1["low"], lookback=80)
    if trend == "LONG":
        levels = lv4["support"] + lv1["support"]
    else:
        levels = lv4["resistance"] + lv1["resistance"]
    if not levels:
        _reject("ложный пробой: нет уровней")
        return None

    # ── ATR for wick quality check ──
    h1_atr_v = atr(h1["high"], h1["low"], h1["close"], 14)
    cur_atr  = h1_atr_v[-1]

    # ── Scan last 11 H1 candles for a false breakout ──
    fb_candle = None
    fb_level  = None
    fb_vrat   = 1.0
    vm = vol_ma(h1["volume"], cfg.VOLUME_MA_PERIOD)

    n = len(h1["close"])
    for back in range(2, 8):          # candles [-7 .. -2], skip current (-1); fresh breakouts only
        idx = n - back
        if idx < 1:
            break
        h_ = h1["high"][idx]
        l_ = h1["low"][idx]
        c  = h1["close"][idx]
        v_ref = vm[idx - 1] if vm[idx - 1] > 0 else 1.0
        v_rat = h1["volume"][idx] / v_ref

        for lvl in levels:
            if trend == "LONG":
                wick_depth = lvl - l_
                # Wick pierced below support (min 0.5%) + close CLEARLY back above (≥0.3%)
                # + wick depth ≥ 0.3× ATR (meaningful rejection, not noise)
                if (l_ < lvl * 0.995 and c > lvl * 1.003
                        and (cur_atr <= 0 or wick_depth >= cur_atr * 0.3)):
                    fb_candle = {"h": h_, "l": l_, "c": c}
                    fb_level  = lvl
                    fb_vrat   = v_rat
                    break
            else:
                wick_depth = h_ - lvl
                # Wick pierced above resistance (min 0.5%) + close CLEARLY back below (≥0.3%)
                # + wick depth ≥ 0.3× ATR
                if (h_ > lvl * 1.005 and c < lvl * 0.997
                        and (cur_atr <= 0 or wick_depth >= cur_atr * 0.3)):
                    fb_candle = {"h": h_, "l": l_, "c": c}
                    fb_level  = lvl
                    fb_vrat   = v_rat
                    break
        if fb_candle:
            break

    if fb_candle is None:
        _reject("ложный пробой: не найден")
        return None

    # Current price must still be on the correct side of the level
    if trend == "LONG":
        # Price must be at or above support (allow 0.3% tolerance for retest)
        if price < fb_level * 0.997:
            _reject("ложный пробой: цена ушла ниже уровня")
            return None
        # Don't enter if price bounced more than 3% away (bad R/R)
        if price > fb_level * 1.03:
            _reject("ложный пробой: цена далеко от уровня")
            return None
    else:
        # Price must be at or below resistance (allow 0.3% tolerance for retest)
        if price > fb_level * 1.003:
            _reject("ложный пробой: цена ушла выше уровня")
            return None
        # Don't enter if price dropped more than 3% away (bad R/R)
        if price < fb_level * 0.97:
            _reject("ложный пробой: цена далеко от уровня")
            return None

    # ── RSI ──
    h1_rsi  = rsi(h1["close"], 14)
    cur_rsi = h1_rsi[-1]
    if trend == "LONG"  and cur_rsi > 75:
        _reject("ложный пробой: RSI перекуплен")
        return None
    if trend == "SHORT" and cur_rsi < 25:
        _reject("ложный пробой: RSI перепродан")
        return None

    # ── Volume minimum ──
    if fb_vrat < cfg.FB_VOLUME_MULT:
        _reject(f"ложный пробой: объём < {cfg.FB_VOLUME_MULT}x")
        return None

    # ── Funding ──
    if funding is not None:
        if trend == "LONG"  and funding > cfg.FUNDING_MAX_LONG:
            _reject("фандинг высокий")
            return None
        if trend == "SHORT" and funding < cfg.FUNDING_MAX_SHORT:
            _reject("фандинг низкий")
            return None

    # ── SL: beyond the false breakout wick extreme ──
    buf = price * cfg.SL_BUFFER_PCT / 100
    if trend == "LONG":
        sl = fb_candle["l"] - buf
    else:
        sl = fb_candle["h"] + buf

    sld = abs(price - sl)
    if price <= 0 or sld <= 0 or sld / price < 0.004:
        _reject("ложный пробой: SL слишком узкий (шум)")
        return None
    if sld / price > 0.05:
        _reject("ложный пробой: SL слишком широкий")
        return None

    sl  = _px(sl)
    tp1 = _px(price + sld * cfg.TP1_RR if trend == "LONG" else price - sld * cfg.TP1_RR)
    tp2 = _px(price + sld * cfg.TP2_RR if trend == "LONG" else price - sld * cfg.TP2_RR)
    tp3 = _px(price + sld * cfg.TP3_RR if trend == "LONG" else price - sld * cfg.TP3_RR)
    rr  = cfg.TP2_RR
    if rr < cfg.MIN_RR:
        _reject(f"ложный пробой: R/R {rr:.1f} < MIN_RR {cfg.MIN_RR}")
        return None

    # wick_pct must be computed BEFORE score section
    if trend == "LONG":
        wick_pct = (fb_level - fb_candle["l"]) / fb_level * 100
    else:
        wick_pct = (fb_candle["h"] - fb_level) / fb_level * 100

    touches = level_touches(fb_level, h4["high"][-120:], h4["low"][-120:])
    if touches == 0:
        # Level may be H1-specific — fall back to H1 confirmation
        touches = level_touches(fb_level, h1["high"][-80:], h1["low"][-80:])
    if touches == 0:
        _reject("ложный пробой: уровень не подтверждён на H4/H1 (0 касаний)")
        return None
    if touches > 6:
        _reject("ложный пробой: уровень пробит (>6 касаний H4)")
        return None

    # ── Score (base 58 — strong setup) ──
    score = 58
    if fb_vrat >= 2.5:   score += 12
    elif fb_vrat >= 2.0: score += 8
    elif fb_vrat >= 1.5: score += 5
    # Level quality — Gerchik: 2–3 touches optimal
    if touches == 2:    score += 10
    elif touches == 3:  score += 8
    elif touches == 1:  score += 2
    elif touches == 4:  score += 4
    # 5+ touches: no bonus
    # Level freshness — recently defended = strong (check H1 + H4, use freshest)
    fb_age_h1 = level_last_touch_age(fb_level, h1["high"][-60:], h1["low"][-60:])
    fb_age_h4 = level_last_touch_age(fb_level, h4["high"][-120:], h4["low"][-120:]) * 4
    fb_touch_age = min(fb_age_h1, fb_age_h4)
    if fb_touch_age <= 5:    score += 8
    elif fb_touch_age <= 15: score += 5
    elif fb_touch_age <= 30: score += 2
    if cur_adx >= 40:    score += 10  # very strong trend
    elif cur_adx >= 30:  score += 8
    elif cur_adx >= 25:  score += 4
    if (trend == "LONG"  and d1_slope > 0.1) or \
       (trend == "SHORT" and d1_slope < -0.1):
        score += 5
    rsi_ok    = (trend == "LONG"  and 35 <= cur_rsi <= 60) or \
                (trend == "SHORT" and 40 <= cur_rsi <= 65)
    rsi_ideal = (trend == "LONG"  and 42 <= cur_rsi <= 55) or \
                (trend == "SHORT" and 45 <= cur_rsi <= 58)
    if rsi_ok:    score += 5
    if rsi_ideal: score += 3   # perfect momentum window
    # Wick depth bonus: deeper rejection = stronger institutional defense
    if wick_pct >= 1.5:   score += 7
    elif wick_pct >= 1.0: score += 5
    elif wick_pct >= 0.5: score += 3
    # RSI divergence on H4: weakening momentum → penalty
    h4_rsi_fb = rsi(h4["close"], 14)
    if detect_rsi_divergence(h4["close"], h4_rsi_fb, trend):
        score -= 12
    # D1 level proximity: entering near major daily obstacle → penalty
    d1_lv_fb = d1_levels if d1_levels is not None \
               else find_levels(d1["high"], d1["low"], lookback=min(120, len(d1["high"])))
    d1_obs_fb = d1_lv_fb["resistance"] if trend == "LONG" else d1_lv_fb["support"]
    is_near_d1_fb, _ = near_level(price, d1_obs_fb, tol=2.0)
    if is_near_d1_fb:
        score -= 10
    score = min(score, 100)

    if score < cfg.MIN_SCORE:
        _reject("ложный пробой: score ниже MIN_SCORE")
        return None

    reason = (
        f"🪤 <b>{symbol}</b> | {trend} ЛОЖНЫЙ ПРОБОЙ\n"
        f"🎯 Уровень: <code>{fb_level:.4f}</code> ({touches} кас., свежесть: {fb_touch_age} св.)\n"
        f"⚡ Тень пробила на {wick_pct:.2f}% | Объём: <code>{fb_vrat:.2f}×</code>\n"
        f"📊 ADX: <code>{cur_adx:.1f}</code> | RSI: <code>{cur_rsi:.0f}</code>\n"
        f"📈 D1: {'🟢' if d1_up else '🔴'} slope <code>{d1_slope:+.2f}%</code>\n"
        f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
        f"🟢 TP2: <code>{tp2:.4f}</code> | TP3: <code>{tp3:.4f}</code>\n"
        f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100"
    )
    return Signal(
        symbol=symbol, side=trend,
        entry=price, sl=sl,
        tp1=tp1, tp2=tp2, tp3=tp3,
        rr=rr, pattern=f"Ложный пробой {fb_level:.4f}", tf="H1+H4",
        score=score, reason=reason,
    )


def analyze_range_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=None):
    """
    Accumulation range breakout — Gerchik's core concept.

    Smart money accumulates in a tight H4 range (3+ bounces off boundary).
    When the institutional limit order is absorbed, price breaks out with
    strong volume.  TP3 is set at 1.5× the normal distance because
    these moves tend to travel much further than regular setups.
    """
    if not d1 or not h4 or not h1:
        return None
    n4 = len(h4["close"])
    if len(d1["close"]) < cfg.TREND_EMA_D1 or n4 < cfg.TREND_EMA_H4 + 5 or len(h1["close"]) < 40:
        return None

    # ── D1 trend ──
    ema200   = ema(d1["close"], cfg.TREND_EMA_D1)
    d1_up    = d1["close"][-1] > ema200[-1]
    trend    = "LONG" if d1_up else "SHORT"
    d1_slope = trend_slope(d1["close"], 10)

    # Require minimum distance from EMA200 — too close = trend unclear
    _rb_d1_dist = abs(d1["close"][-1] - ema200[-1]) / ema200[-1] * 100 if ema200[-1] > 0 else 0
    if _rb_d1_dist < 0.3:
        _reject("накопление: D1 у EMA200 (зона неопределённости)")
        return None

    # Slope filter — same threshold as analyze() and analyze_false_breakout()
    if trend == "LONG"  and d1_slope < -cfg.D1_SLOPE_MAX_DECLINE:
        _reject("накопление: D1 разворот вниз")
        return None
    if trend == "SHORT" and d1_slope > cfg.D1_SLOPE_MAX_DECLINE:
        _reject("накопление: D1 разворот вверх")
        return None

    # H4 EMA50 alignment — must be in trend direction (tolerance 2%)
    ema50_h4   = ema(h4["close"], cfg.TREND_EMA_H4)
    h4_up      = h4["close"][-1] > ema50_h4[-1]
    h4_near_rb = abs(h4["close"][-1] - ema50_h4[-1]) / ema50_h4[-1] * 100 < cfg.H4_NEAR_PCT
    if trend == "LONG" and not h4_up and not h4_near_rb:
        _reject("накопление: H4 против тренда")
        return None
    if trend == "SHORT" and h4_up and not h4_near_rb:
        _reject("накопление: H4 против тренда")
        return None
    if h4_near_rb and ((trend == "LONG" and not h4_up) or (trend == "SHORT" and h4_up)):
        lookback = min(cfg.H4_WAS_ABOVE_LOOKBACK, len(h4["close"]) - 1)
        if trend == "LONG":
            if not any(h4["close"][-lookback-1:-1] > ema50_h4[-lookback-1:-1]):
                _reject("накопление: H4 у EMA снизу (не откат — сопротивление)")
                return None
        else:
            if not any(h4["close"][-lookback-1:-1] < ema50_h4[-lookback-1:-1]):
                _reject("накопление: H4 у EMA сверху (не откат — поддержка)")
                return None

    # H4 impulse guard
    h4_bear_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] < h4["open"][i])
    h4_bull_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] > h4["open"][i])
    if trend == "LONG"  and h4_bear_cnt >= 4:
        _reject("накопление: H4 медвежий импульс")
        return None
    if trend == "SHORT" and h4_bull_cnt >= 4:
        _reject("накопление: H4 бычий импульс")
        return None

    price = h1["close"][-1]

    # ── Consolidation range: H4[-47 : -3] — exclude breakout candles ──
    rng_end   = n4 - 3
    rng_start = max(0, n4 - 47)
    rng_len   = rng_end - rng_start
    if rng_len < 15:
        return None

    r_highs = h4["high"][rng_start:rng_end]
    r_lows  = h4["low"][rng_start:rng_end]
    r_high  = float(np.max(r_highs))
    r_low   = float(np.min(r_lows))

    if r_low <= 0:
        return None
    width_pct = (r_high - r_low) / r_low * 100

    if width_pct > 9.0:
        _reject("накопление: диапазон широкий")
        return None
    if width_pct < 1.5:
        return None

    # ≥65% of range candles must stay inside — confirms real consolidation
    in_range = sum(
        1 for h, l in zip(r_highs, r_lows)
        if h <= r_high * 1.01 and l >= r_low * 0.99
    )
    if in_range < rng_len * 0.65:
        _reject("накопление: не настоящий диапазон")
        return None

    boundary = r_high if trend == "LONG" else r_low

    # ── Find H4 breakout candle among last 2 complete candles ([-2] or [-3]) ──
    # Checking [-3] catches pullback-to-breakout entries (best R/R for this setup)
    brk_idx = None
    for bi in (-2, -3):
        b_o = h4["open"][bi];   b_c = h4["close"][bi]
        b_h = h4["high"][bi];   b_l = h4["low"][bi]
        b_body = abs(b_c - b_o); b_full = b_h - b_l
        if b_full <= 0 or b_body / b_full < 0.50:
            continue
        if trend == "LONG"  and (b_c <= b_o or b_c < boundary):
            continue
        if trend == "SHORT" and (b_c >= b_o or b_c > boundary):
            continue
        brk_idx = bi
        break

    if brk_idx is None:
        _reject("накопление: нет чёткой свечи пробоя")
        return None

    # H1 price must still be outside the range (not reversed deep inside)
    if trend == "LONG"  and price < boundary * 0.995:
        _reject("накопление: цена вернулась в диапазон")
        return None
    if trend == "SHORT" and price > boundary * 1.005:
        _reject("накопление: цена вернулась в диапазон")
        return None

    # ── Count touches of the boundary (accumulation evidence) ──
    tol_abs = r_high * 0.005
    if trend == "LONG":
        touches = sum(1 for h in r_highs if h >= r_high - tol_abs)
    else:
        touches = sum(1 for l in r_lows  if l <= r_low  + tol_abs)
    if touches < 3:
        _reject("накопление: мало касаний границы (<3)")
        return None

    # ── Volume on H4 breakout candle ──
    h4_vm   = vol_ma(h4["volume"], cfg.VOLUME_MA_PERIOD)
    vm_ref  = h4_vm[brk_idx - 1]
    h4_vrat = h4["volume"][brk_idx] / vm_ref if vm_ref > 0 else 0
    if h4_vrat < cfg.BRK_VOLUME_MULT:
        _reject(f"накопление: объём H4 < {cfg.BRK_VOLUME_MULT}x")
        return None

    # ── ADX ──
    h4_adx_v = adx(h4["high"], h4["low"], h4["close"], 14)
    cur_adx  = h4_adx_v[-1]
    adx_rising = len(h4_adx_v) >= 8 and cur_adx > h4_adx_v[-8]

    # Range breakout requires RISING ADX — confirms trend is starting from accumulation
    if not adx_rising:
        _reject("накопление: ADX не растёт")
        return None
    # Hard floor — range breakouts start with ADX 15-19 (rising from low base),
    # so use a lower floor than the trend-following strategies.
    if cur_adx < 15:
        _reject("накопление: ADX < 15 (слишком плоский)")
        return None

    # ── RSI ──
    h1_rsi_v = rsi(h1["close"], 14)
    cur_rsi  = h1_rsi_v[-1]
    if trend == "LONG"  and cur_rsi > 80:
        _reject("накопление: RSI > 80")
        return None
    if trend == "SHORT" and cur_rsi < 20:
        _reject("накопление: RSI < 20")
        return None

    # ── Funding ──
    if funding is not None:
        if trend == "LONG"  and funding > cfg.FUNDING_MAX_LONG:
            _reject("фандинг высокий")
            return None
        if trend == "SHORT" and funding < cfg.FUNDING_MAX_SHORT:
            _reject("фандинг низкий")
            return None

    # ── SL: just inside the broken range boundary ──
    h1_atr_v = atr(h1["high"], h1["low"], h1["close"], 14)
    cur_atr  = h1_atr_v[-1]
    buf = price * cfg.SL_BUFFER_PCT / 100
    if trend == "LONG":
        sl = boundary - cur_atr * 0.7 - buf
    else:
        sl = boundary + cur_atr * 0.7 + buf

    sld = abs(price - sl)
    if price <= 0 or sld <= 0 or sld / price < 0.004:
        _reject("накопление: SL слишком узкий (шум)")
        return None
    if sld / price > 0.07:
        _reject("накопление: SL слишком широкий")
        return None

    sl      = _px(sl)
    tp1     = _px(price + sld * cfg.TP1_RR if trend == "LONG" else price - sld * cfg.TP1_RR)
    tp2     = _px(price + sld * cfg.TP2_RR if trend == "LONG" else price - sld * cfg.TP2_RR)
    tp3_rr  = cfg.TP3_RR * cfg.RANGE_TP3_MULT   # accumulation breakouts go further
    tp3     = _px(price + sld * tp3_rr if trend == "LONG" else price - sld * tp3_rr)
    rr      = cfg.TP2_RR
    if rr < cfg.MIN_RR:
        _reject(f"накопление: R/R {rr:.1f} < MIN_RR {cfg.MIN_RR}")
        return None

    # ── H1 candle pattern — optional confirmation bonus ──
    h1_pname, h1_pside = detect_pattern(h1, -2)
    h1_pat_ok = h1_pname != "" and (h1_pside == trend or h1_pside == "DOJI")

    # ── Score ──
    score = 60
    if touches >= 6:     score += 15
    elif touches >= 4:   score += 10
    else:                score += 5   # 3 touches
    if h4_vrat >= 4.0:   score += 15
    elif h4_vrat >= 3.0: score += 10
    elif h4_vrat >= 2.5: score += 6
    else:                score += 3   # 2.0–2.5×
    if width_pct < 4.0:  score += 8
    elif width_pct < 6.0: score += 4
    if adx_rising:       score += 7
    if cur_adx >= 25:    score += 5
    elif cur_adx >= 20:  score += 2
    if (trend == "LONG"  and d1_slope > 0.1) or \
       (trend == "SHORT" and d1_slope < -0.1):
        score += 5
    if h1_pat_ok:
        score += 8  # H1 candle confirms the breakout direction
    # RSI divergence on H4: weakening momentum into a breakout = warning signal
    h4_rsi_rb = rsi(h4["close"], 14)
    if detect_rsi_divergence(h4["close"], h4_rsi_rb, trend):
        score -= 10
    # D1 level proximity: entering near major daily obstacle → penalty
    d1_lv_rb = d1_levels if d1_levels is not None \
               else find_levels(d1["high"], d1["low"], lookback=min(120, len(d1["high"])))
    d1_obs_rb = d1_lv_rb["resistance"] if trend == "LONG" else d1_lv_rb["support"]
    is_near_d1_rb, _ = near_level(price, d1_obs_rb, tol=2.0)
    if is_near_d1_rb:
        score -= 10
    score = min(score, 100)

    if score < cfg.MIN_SCORE:
        _reject("накопление: score ниже MIN_SCORE")
        return None

    reason = (
        f"🎯 <b>{symbol}</b> | {trend} НАКОПЛЕНИЕ\n"
        f"📦 Диапазон H4: <code>{r_low:.4f}</code> — <code>{r_high:.4f}</code> "
        f"({width_pct:.1f}%, {rng_len} свечей)\n"
        f"🔄 Касаний уровня: <b>{touches}</b> | Объём пробоя: <code>{h4_vrat:.2f}×</code>\n"
        f"📊 ADX: <code>{cur_adx:.1f}</code>"
        + (" ↑" if adx_rising else "")
        + f" | RSI: <code>{cur_rsi:.0f}</code>\n"
        f"📈 D1: {'🟢' if d1_up else '🔴'} slope <code>{d1_slope:+.2f}%</code>\n"
        f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
        f"🟢 TP2: <code>{tp2:.4f}</code> | TP3 (×{tp3_rr:.1f}R): <code>{tp3:.4f}</code>\n"
        f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100"
    )
    return Signal(
        symbol=symbol, side=trend,
        entry=price, sl=sl,
        tp1=tp1, tp2=tp2, tp3=tp3,
        rr=rr, pattern=f"Накопление {width_pct:.1f}% ({touches} кас.)", tf="H4",
        score=score, reason=reason,
    )


def analyze_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=None):
    """
    Breakout signal: price closes through a key S/R level with conviction.
    Complements the pullback strategy — catches strong directional moves.
    """
    if not d1 or not h4 or not h1:
        return None
    if len(d1["close"]) < cfg.TREND_EMA_D1 or len(h4["close"]) < cfg.TREND_EMA_H4 + 5 or len(h1["close"]) < 40:
        return None

    # ── D1 trend ──
    ema200 = ema(d1["close"], cfg.TREND_EMA_D1)
    d1_up  = d1["close"][-1] > ema200[-1]
    trend  = "LONG" if d1_up else "SHORT"
    d1_slope = trend_slope(d1["close"], 10)

    # Breakout requires D1 momentum aligned with direction
    if trend == "LONG"  and d1_slope < cfg.D1_SLOPE_MIN:
        _reject("пробой: D1 нет роста")
        return None
    if trend == "SHORT" and d1_slope > -cfg.D1_SLOPE_MIN:
        _reject("пробой: D1 нет падения")
        return None

    # ── H4 filter — breakout must not happen against H4 trend ──
    ema50_h4 = ema(h4["close"], cfg.TREND_EMA_H4)
    h4_up    = h4["close"][-1] > ema50_h4[-1]
    h4_near  = abs(h4["close"][-1] - ema50_h4[-1]) / ema50_h4[-1] * 100 < cfg.H4_NEAR_PCT
    # For breakout: allow if H4 aligned OR price is crossing EMA50 from below (bullish)
    if trend == "LONG"  and not h4_up and not h4_near:
        _reject("пробой: H4 против тренда")
        return None
    if trend == "SHORT" and h4_up and not h4_near:
        _reject("пробой: H4 против тренда")
        return None
    # H4 impulse guard: 3+ of last 4 candles against direction
    h4_bear_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] < h4["open"][i])
    h4_bull_cnt = sum(1 for i in (-1,-2,-3,-4) if h4["close"][i] > h4["open"][i])
    if trend == "LONG"  and h4_bear_cnt >= 4:
        _reject("пробой: H4 медвежий импульс")
        return None
    if trend == "SHORT" and h4_bull_cnt >= 4:
        _reject("пробой: H4 бычий импульс")
        return None

    price = h1["close"][-1]
    h1_atr = atr(h1["high"], h1["low"], h1["close"], 14)
    cur_atr = h1_atr[-1]

    # ── Breakout candle: must be strong (body > 55% of range, min size 0.25× ATR) ──
    o2, c2 = h1["open"][-2], h1["close"][-2]
    h2, l2 = h1["high"][-2], h1["low"][-2]
    body  = abs(c2 - o2)
    rng   = h2 - l2
    if cur_atr > 0 and rng < cur_atr * 0.25:
        _reject("пробой: свеча слишком мала (шум)")
        return None
    if rng <= 0 or body / rng < 0.55:
        _reject("пробой: слабая свеча")
        return None
    # Candle must be in trend direction
    if trend == "LONG"  and c2 <= o2:
        _reject("пробой: медвежья свеча в LONG")
        return None
    if trend == "SHORT" and c2 >= o2:
        _reject("пробой: бычья свеча в SHORT")
        return None

    # ── Price must have broken through a key level ──
    lv4 = find_levels(h4["high"], h4["low"], lookback=120)
    lv1 = find_levels(h1["high"], h1["low"], lookback=80)
    if trend == "LONG":
        levels = lv4["resistance"] + lv1["resistance"]
    else:
        levels = lv4["support"] + lv1["support"]

    # Find a level the candle just broke through (level was between candle open and close)
    broken_level = None
    for lvl in levels:
        if trend == "LONG"  and o2 <= lvl <= c2:
            broken_level = lvl
            break
        if trend == "SHORT" and c2 <= lvl <= o2:
            broken_level = lvl
            break
    if broken_level is None:
        _reject("пробой: нет пробитого уровня")
        return None

    # Don't chase breakouts that already ran far from the broken level
    if trend == "LONG"  and price > broken_level * 1.025:
        _reject("пробой: цена ушла далеко от уровня (>2.5%)")
        return None
    if trend == "SHORT" and price < broken_level * 0.975:
        _reject("пробой: цена ушла далеко от уровня (>2.5%)")
        return None

    touches = level_touches(broken_level, h4["high"][-120:], h4["low"][-120:])
    if touches < 2:
        # Level may be H1-specific — fall back to H1 confirmation
        touches = level_touches(broken_level, h1["high"][-80:], h1["low"][-80:])
    if touches < 2:
        _reject("пробой: уровень не подтверждён (<2 касаний H4/H1)")
        return None

    # ── Volume: breakout must have 2x+ volume ──
    vm   = vol_ma(h1["volume"], cfg.VOLUME_MA_PERIOD)
    vrat = h1["volume"][-2] / vm[-2] if vm[-2] > 0 else 0
    if vrat < cfg.BRK_VOLUME_MULT:
        _reject(f"пробой: объём < {cfg.BRK_VOLUME_MULT}x")
        return None

    # ── ADX: must be trending ──
    h4_adx  = adx(h4["high"], h4["low"], h4["close"], 14)
    cur_adx = h4_adx[-1]
    if cur_adx < cfg.ADX_MIN:
        _reject(f"пробой: ADX < {cfg.ADX_MIN} (нет тренда)")
        return None

    # ── RSI: not extreme ──
    h1_rsi  = rsi(h1["close"], 14)
    cur_rsi = h1_rsi[-1]
    if trend == "LONG"  and cur_rsi > 75:
        _reject("пробой: RSI > 75")
        return None
    if trend == "SHORT" and cur_rsi < 25:
        _reject("пробой: RSI < 25")
        return None

    # ── Funding ──
    if funding is not None:
        if trend == "LONG"  and funding > cfg.FUNDING_MAX_LONG:
            _reject("фандинг высокий")
            return None
        if trend == "SHORT" and funding < cfg.FUNDING_MAX_SHORT:
            _reject("фандинг низкий")
            return None

    # ── SL: just below broken level ──
    buf = price * cfg.SL_BUFFER_PCT / 100
    if trend == "LONG":
        sl = broken_level - cur_atr * 0.5 - buf
    else:
        sl = broken_level + cur_atr * 0.5 + buf

    sld = abs(price - sl)
    if price <= 0 or sld <= 0 or sld / price < 0.004:
        _reject("пробой: SL слишком узкий (шум)")
        return None
    if sld / price > 0.05:
        _reject("пробой: SL слишком широкий")
        return None

    sl  = _px(sl)
    tp1 = _px(price + sld * cfg.TP1_RR if trend == "LONG" else price - sld * cfg.TP1_RR)
    tp2 = _px(price + sld * cfg.TP2_RR if trend == "LONG" else price - sld * cfg.TP2_RR)
    tp3 = _px(price + sld * cfg.TP3_RR if trend == "LONG" else price - sld * cfg.TP3_RR)
    rr  = cfg.TP2_RR
    if rr < cfg.MIN_RR:
        _reject(f"пробой: R/R {rr:.1f} < MIN_RR {cfg.MIN_RR}")
        return None

    # Score breakout signals
    score = 55  # base higher than pullback (50) — breakout is higher conviction
    if vrat >= 3.0:   score += 15
    elif vrat >= 2.5: score += 10
    else:             score += 6
    if cur_adx >= 35: score += 10
    elif cur_adx >= 30: score += 6
    elif cur_adx >= 25: score += 3
    if touches >= 3:  score += 8   # well-tested level = stronger breakout
    if (trend == "LONG" and d1_slope > 0.3) or (trend == "SHORT" and d1_slope < -0.3):
        score += 7
    # D1 level proximity: entering near major daily obstacle → penalty
    d1_lv_br = d1_levels if d1_levels is not None \
               else find_levels(d1["high"], d1["low"], lookback=min(120, len(d1["high"])))
    d1_obs_br = d1_lv_br["resistance"] if trend == "LONG" else d1_lv_br["support"]
    is_near_d1_br, _ = near_level(price, d1_obs_br, tol=2.0)
    if is_near_d1_br:
        score -= 10
    score = min(score, 100)

    if score < cfg.MIN_SCORE:
        _reject(f"пробой: score {score} < MIN_SCORE {cfg.MIN_SCORE}")
        return None

    reason = (
        f"💥 <b>{symbol}</b> | {trend} ПРОБОЙ\n"
        f"🕯 Пробита зона: <code>{broken_level:.4f}</code> ({touches} кас.)\n"
        f"📈 D1 slope: <code>{d1_slope:+.2f}%</code> | ADX: <code>{cur_adx:.1f}</code>\n"
        f"📦 Объём: <code>{vrat:.2f}×</code> | RSI: <code>{cur_rsi:.0f}</code>\n"
        f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
        f"🟢 TP2: <code>{tp2:.4f}</code> | TP3: <code>{tp3:.4f}</code>\n"
        f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100"
    )
    return Signal(
        symbol=symbol, side=trend,
        entry=price, sl=sl,
        tp1=tp1, tp2=tp2, tp3=tp3,
        rr=rr, pattern=f"Пробой {broken_level:.4f}", tf="H1+H4",
        score=score, reason=reason,
    )
