import logging
from dataclasses import dataclass
from typing import Optional
import numpy as np

log = logging.getLogger("strategy")

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

def parse_klines(raw):
    if not raw:
        return {}
    arr = np.array([[float(c) for c in k[:6]] for k in raw])
    return {"ts":arr[:,0],"open":arr[:,1],"high":arr[:,2],"low":arr[:,3],"close":arr[:,4],"volume":arr[:,5]}

def ema(values, period):
    result = np.zeros_like(values)
    k = 2/(period+1)
    result[0] = values[0]
    for i in range(1,len(values)):
        result[i] = values[i]*k + result[i-1]*(1-k)
    return result

def vol_ma(volumes, period=20):
    result = np.zeros_like(volumes)
    for i in range(period-1, len(volumes)):
        result[i] = volumes[i-period+1:i+1].mean()
    return result

def find_levels(highs, lows, lookback=60):
    rh, rl = highs[-lookback:], lows[-lookback:]
    res, sup = [], []
    for i in range(2, len(rh)-2):
        if rh[i]>rh[i-1] and rh[i]>rh[i-2] and rh[i]>rh[i+1] and rh[i]>rh[i+2]:
            res.append(float(rh[i]))
    for i in range(2, len(rl)-2):
        if rl[i]<rl[i-1] and rl[i]<rl[i-2] and rl[i]<rl[i+1] and rl[i]<rl[i+2]:
            sup.append(float(rl[i]))
    return {"resistance":res,"support":sup}

def near_level(price, levels, tol=0.4):
    for lvl in levels:
        if abs(price-lvl)/price*100 <= tol:
            return True, lvl
    return False, 0.0

def level_touches(level, highs, lows, tol=0.25):
    t = level*tol/100
    return sum(1 for h,l in zip(highs,lows) if abs(h-level)<=t or abs(l-level)<=t)

def hammer(o,h,l,c):
    body=abs(c-o);full=h-l
    if full==0: return False
    return body/full<=0.35 and (min(o,c)-l)>=body*2 and (h-max(o,c))<=body*0.5 and c>o

def shooting_star(o,h,l,c):
    body=abs(c-o);full=h-l
    if full==0: return False
    return body/full<=0.35 and (h-max(o,c))>=body*2 and (min(o,c)-l)<=body*0.5 and c<o

def bull_engulf(o1,c1,o2,c2):
    return c1<o1 and c2>o2 and o2<c1 and c2>o1

def bear_engulf(o1,c1,o2,c2):
    return c1>o1 and c2<o2 and o2>c1 and c2<o1

def bull_pin(o,h,l,c):
    body=abs(c-o);full=h-l
    if full==0: return False
    return (min(o,c)-l)>full*0.6 and body<full*0.25

def bear_pin(o,h,l,c):
    body=abs(c-o);full=h-l
    if full==0: return False
    return (h-max(o,c))>full*0.6 and body<full*0.25

def detect_pattern(candles, idx=-1):
    i = idx if idx>=0 else len(candles["open"])+idx
    o=candles["open"][i];h=candles["high"][i];l=candles["low"][i];c=candles["close"][i]
    o1=candles["open"][i-1];c1=candles["close"][i-1]
    if hammer(o,h,l,c):         return "Молот","LONG"
    if bull_pin(o,h,l,c):       return "Пин-бар (бычий)","LONG"
    if bull_engulf(o1,c1,o,c):  return "Бычье поглощение","LONG"
    if shooting_star(o,h,l,c):  return "Падающая звезда","SHORT"
    if bear_pin(o,h,l,c):       return "Пин-бар (медвежий)","SHORT"
    if bear_engulf(o1,c1,o,c):  return "Медвежье поглощение","SHORT"
    return "",""

def analyze(symbol, d1, h4, h1, funding, cfg):
    if not d1 or not h4 or not h1: return None
    if len(d1["close"])<cfg.TREND_EMA_D1: return None
    if len(h4["close"])<cfg.TREND_EMA_H4: return None
    ema200=ema(d1["close"],cfg.TREND_EMA_D1)
    d1_up=d1["close"][-1]>ema200[-1]; d1_dn=d1["close"][-1]<ema200[-1]
    if not d1_up and not d1_dn: return None
    trend="LONG" if d1_up else "SHORT"
    ema50=ema(h4["close"],cfg.TREND_EMA_H4)
    h4_up=h4["close"][-1]>ema50[-1]; h4_dn=h4["close"][-1]<ema50[-1]
    if trend=="LONG" and not h4_up: return None
    if trend=="SHORT" and not h4_dn: return None
    price=h1["close"][-1]
    lv4=find_levels(h4["high"],h4["low"],lookback=80)
    near,level=near_level(price,lv4["support"] if trend=="LONG" else lv4["resistance"],tol=0.5)
    if not near:
        lv1=find_levels(h1["high"],h1["low"],lookback=50)
        near,level=near_level(price,lv1["support"] if trend=="LONG" else lv1["resistance"],tol=0.3)
    if not near: return None
    touches=level_touches(level,h4["high"][-80:],h4["low"][-80:])
    if touches>3: return None
    pname,pside=detect_pattern(h1)
    if not pname or pside!=trend: return None
    h4p,h4s=detect_pattern(h4)
    h4ok=h4p!="" and h4s==trend
    vm=vol_ma(h1["volume"],cfg.VOLUME_MA_PERIOD)
    vrat=h1["volume"][-1]/vm[-1] if vm[-1]>0 else 0
    if vrat<cfg.VOLUME_MULT: return None
    if trend=="LONG" and funding>cfg.FUNDING_MAX_LONG: return None
    if trend=="SHORT" and funding<cfg.FUNDING_MAX_SHORT: return None
    buf=price*cfg.SL_BUFFER_PCT/100
    sl=(h1["low"][-1]-buf) if trend=="LONG" else (h1["high"][-1]+buf)
    sld=abs(price-sl)
    if sld<=0: return None
    tp1=price+sld*cfg.TP1_RR if trend=="LONG" else price-sld*cfg.TP1_RR
    tp2=price+sld*cfg.TP2_RR if trend=="LONG" else price-sld*cfg.TP2_RR
    tp3=price+sld*cfg.TP3_RR if trend=="LONG" else price-sld*cfg.TP3_RR
    rr=cfg.TP2_RR
    if rr<cfg.MIN_RR: return None
    score=55
    score+=10 if vrat>=2.0 else 5
    score+=10 if touches<=2 else 3
    score+=10 if h4ok else 0
    score+=10 if abs(funding)<0.02 else 5
    score=min(score,100)
    reason=(f"📊 <b>{symbol}</b> | {trend}\n"
            f"🕯 H1: {pname} | H4: {h4p if h4ok else '—'}\n"
            f"📈 D1: {'🟢' if d1_up else '🔴'} EMA200 | H4: {'🟢' if h4_up else '🔴'} EMA50\n"
            f"🎯 Уровень: <code>{level:.4f}</code> ({touches} кас.)\n"
            f"📦 Объём: <code>{vrat:.2f}×</code> | Funding: <code>{funding:.4f}%</code>\n"
            f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
            f"🟢 TP2: <code>{tp2:.4f}</code> | TP3: <code>{tp3:.4f}</code>\n"
            f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100")
    return Signal(symbol=symbol,side=trend,entry=price,sl=sl,tp1=tp1,tp2=tp2,tp3=tp3,rr=rr,pattern=pname,tf="H1+H4",score=score,reason=reason)
