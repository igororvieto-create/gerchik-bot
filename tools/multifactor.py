"""Замер IX: многофакторная модель на всех признаках из цены, объёма и фандинга.

Спецификация — docs/PREREGISTRATION.md (замер IX), записана ДО расчётов.
Признаки, форма модели и λ здесь закрыты: любое изменение — новая
пре-регистрация, а не правка.

ОБУЧЕНИЕ ТОЛЬКО НА ПРОШЛОМ. В неделю t модель видит лишь пары (t', y_t'),
чей горизонт доходности ЗАКОНЧИЛСЯ до t: t' + 7 дней <= t. Без этой
очистки (purging) метка обучения перекрывала бы момент прогноза, и модель
выучила бы будущее — ровно та утечка, которая даёт красивую историю и
нулевые живые деньги. На это есть тест: на чистом случайном блуждании
конвейер обязан давать ноль, а не прибыль.

Запуск (holdout — только если explore прошёл):
    python3 -m tools.multifactor --hist data/universe --half explore
"""
import argparse
import json
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import ROUND_TRIP_FEE_PCT                        # noqa: E402
from tools.replay import half_window                           # noqa: E402
from tools.xsec import (HOLD_DAYS, MIN_NAMES_PER_LEG, MIN_WEEKS,  # noqa: E402
                        TOP_FRACTION, _DAY_MS, rebalance_dates)
from tools.xsec_universe import (_closed_days, _exit_price,    # noqa: E402
                                 _funding_between, _price_at, universe_at)

# --- Спецификация (замер IX). НЕ ПОДБИРАЕТСЯ. ---
RIDGE_LAMBDA = 1.0
MIN_TRAIN_WEEKS = 26
Z95 = 1.96
MARKET = "BTCUSDT"

FEATURES = ["r1", "r3", "r7", "r14", "r30", "vol20", "atr14", "range_pos20",
            "dd30", "qv_ratio", "log_liq", "fund7", "fund_chg", "beta30",
            "resid7"]


def _ret(daily: List[Dict], t: int, days: int) -> Optional[float]:
    a = _price_at(daily, t - days * _DAY_MS)
    b = _price_at(daily, t, fresh=True)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def _log_rets(closed: List[Dict]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for prev, cur in zip(closed, closed[1:]):
        if prev["close"] > 0 and cur["close"] > 0:
            out[cur["ts"]] = math.log(cur["close"] / prev["close"])
    return out


def features_at(h: Dict, t: int, mkt_daily: Optional[List[Dict]]) -> Optional[Dict[str, Optional[float]]]:
    """Все 15 признаков символа на момент t — только по закрытым дням."""
    daily = h["daily"]
    if _price_at(daily, t, fresh=True) is None:
        return None
    closed = _closed_days(daily, t)
    if len(closed) < 31:
        return None
    last = closed[-1]
    f: Dict[str, Optional[float]] = {}
    for k in (1, 3, 7, 14, 30):
        f[f"r{k}"] = _ret(daily, t, k)

    lr = list(_log_rets(closed[-21:]).values())
    f["vol20"] = statistics.stdev(lr) if len(lr) >= 2 else None

    w14 = closed[-14:]
    f["atr14"] = (statistics.fmean((d["high"] - d["low"]) / d["close"]
                                   for d in w14 if d["close"] > 0)
                  if all("high" in d for d in w14) else None)

    w20 = closed[-20:]
    if all("high" in d for d in w20):
        hi, lo = max(d["high"] for d in w20), min(d["low"] for d in w20)
        f["range_pos20"] = (last["close"] - lo) / (hi - lo) if hi > lo else 0.5
    else:
        f["range_pos20"] = None

    w30 = closed[-30:]
    f["dd30"] = (last["close"] / max(d["high"] for d in w30) - 1.0
                 if all("high" in d for d in w30) else None)

    q30 = [d["quote"] for d in w30 if d.get("quote", 0) > 0]
    q7 = [d["quote"] for d in closed[-7:] if d.get("quote", 0) > 0]
    med30 = statistics.median(q30) if q30 else None
    f["qv_ratio"] = (sum(q7) / (7 * med30)) if (med30 and q7) else None
    f["log_liq"] = math.log(med30) if med30 else None

    fund = h.get("funding") or []
    f7 = _funding_between(fund, t - 7 * _DAY_MS, t)
    f["fund7"] = f7
    f["fund_chg"] = f7 - _funding_between(fund, t - 14 * _DAY_MS, t - 7 * _DAY_MS)

    f["beta30"] = f["resid7"] = None
    if mkt_daily is not None:
        mc = _closed_days(mkt_daily, t)
        own, mk = _log_rets(closed[-31:]), _log_rets(mc[-31:])
        common = [ts for ts in own if ts in mk]
        if len(common) >= 20:
            x = np.array([mk[ts] for ts in common])
            y = np.array([own[ts] for ts in common])
            vx = float(np.var(x))
            if vx > 0:
                beta = float(np.cov(x, y, bias=True)[0, 1] / vx)
                f["beta30"] = beta
                m7 = _ret(mkt_daily, t, 7)
                if f["r7"] is not None and m7 is not None:
                    f["resid7"] = f["r7"] - beta * m7
    return f


def rank_normalize(vals: List[Optional[float]]) -> List[float]:
    """Кросс-секционный ранг в [−0.5, 0.5]; пропуск — 0 (середина)."""
    known = [(v, i) for i, v in enumerate(vals) if v is not None and math.isfinite(v)]
    out = [0.0] * len(vals)
    n = len(known)
    if n < 2:
        return out
    known.sort()
    for r, (_, i) in enumerate(known):
        out[i] = r / (n - 1) - 0.5
    return out


def _net_forward(h: Dict, t: int) -> Optional[float]:
    """Нетто-доходность ЛОНГА за 7 дней после t — метка обучения."""
    end = t + HOLD_DAYS * _DAY_MS
    entry = _price_at(h["daily"], t, fresh=True)
    exit_px = _exit_price(h["daily"], t, end)
    if entry is None or exit_px is None or entry <= 0:
        return None
    return exit_px / entry - 1.0 - _funding_between(h.get("funding") or [], t, end)


def cross_section(coins: Dict[str, Dict], t: int) -> Tuple[List[str], np.ndarray]:
    """Матрица признаков вселенной на t: ранги × sqrt(12) ≈ единичная дисперсия."""
    names = universe_at(coins, t)
    mkt = coins.get(MARKET, {}).get("daily")
    raw = {s: features_at(coins[s], t, mkt) for s in names}
    names = [s for s in names if raw[s] is not None]
    if not names:
        return [], np.zeros((0, len(FEATURES)))
    cols = []
    for fname in FEATURES:
        cols.append(rank_normalize([raw[s][fname] for s in names]))  # type: ignore[index]
    X = np.array(cols).T * math.sqrt(12.0)
    return names, X


def ridge_fit(X: np.ndarray, y: np.ndarray, lam: float = RIDGE_LAMBDA) -> np.ndarray:
    n, k = X.shape
    return np.linalg.solve(X.T @ X / n + lam * np.eye(k), X.T @ y / n)


def portfolio_week(coins: Dict[str, Dict], t: int, sides: Dict[str, int]) -> Optional[Dict]:
    end = t + HOLD_DAYS * _DAY_MS
    legs = []
    for s, sign in sides.items():
        h = coins[s]
        entry = _price_at(h["daily"], t, fresh=True)
        exit_px = _exit_price(h["daily"], t, end)
        if entry is None or exit_px is None or entry <= 0:
            continue
        fwd = exit_px / entry - 1.0
        fnd = _funding_between(h.get("funding") or [], t, end)
        legs.append(sign * fwd - sign * fnd - ROUND_TRIP_FEE_PCT / 100.0)
    if len(legs) < MIN_NAMES_PER_LEG * 2:
        return None
    return {"ts": t, "positions": len(legs), "ret": statistics.fmean(legs)}


def training_dates(all_dates: List[int], t: int) -> List[int]:
    """Даты, чьи метки ИЗВЕСТНЫ к моменту t.

    Метка даты t' — доходность за [t', t' + 7 дней]. Она известна только
    когда эти 7 дней прошли. Дата t сама в обучение не входит никогда: её
    метка — ровно то будущее, которое прогнозируется.
    """
    return [tp for tp in all_dates if tp + HOLD_DAYS * _DAY_MS <= t]


def walk_forward(coins: Dict[str, Dict], all_dates: List[int],
                 predict_dates: List[int]) -> Tuple[List[Dict], List[np.ndarray]]:
    """Прогноз на каждую дату из predict_dates, обучение — только на прошлом.

    all_dates — все даты набора по порядку (обучающая история); прогнозы
    делаются только на predict_dates. Метка даты t' известна лишь в
    t' + HOLD_DAYS: в обучение она попадает, когда это время прошло.
    """
    cache: Dict[int, Tuple[List[str], np.ndarray, Optional[np.ndarray]]] = {}

    def get(t: int):
        if t not in cache:
            names, X = cross_section(coins, t)
            y = None
            if names:
                fwd = [_net_forward(coins[s], t) for s in names]
                y = np.array(rank_normalize(fwd))
            cache[t] = (names, X, y)
        return cache[t]

    weeks: List[Dict] = []
    betas: List[np.ndarray] = []
    pset = set(predict_dates)
    for t in all_dates:
        if t not in pset:
            continue
        train = training_dates(all_dates, t)
        if len(train) < MIN_TRAIN_WEEKS:
            continue
        Xs, ys = [], []
        for tp in train:
            names, X, y = get(tp)
            if names and y is not None:
                Xs.append(X)
                ys.append(y)
        if not Xs:
            continue
        beta = ridge_fit(np.vstack(Xs), np.concatenate(ys))
        betas.append(beta)
        names, X, _ = get(t)
        if len(names) < MIN_NAMES_PER_LEG * 2:
            continue
        score = X @ beta
        order = sorted(range(len(names)), key=lambda i: score[i])
        k = max(MIN_NAMES_PER_LEG, int(len(names) * TOP_FRACTION))
        if k * 2 > len(names):
            continue
        sides = {names[i]: -1 for i in order[:k]}
        sides.update({names[i]: 1 for i in order[-k:]})
        w = portfolio_week(coins, t, sides)
        if w:
            weeks.append(w)
    return weeks, betas


def summarize(weeks: List[Dict]) -> Dict:
    rets = [w["ret"] for w in weeks]
    n = len(rets)
    out: Dict = {"weeks": n}
    if n < 2:
        return out
    mean, sd = statistics.fmean(rets), statistics.stdev(rets)
    se = sd / math.sqrt(n)
    out.update({"mean": mean, "median": statistics.median(rets), "sd": sd,
                "ci_lo": mean - Z95 * se, "ci_hi": mean + Z95 * se,
                "t": mean / se if se > 0 else 0.0,
                "positive_weeks": sum(1 for r in rets if r > 0)})
    return out


def verdict(s: Dict) -> Tuple[bool, List[str]]:
    checks = [
        ("средняя недельная нетто > 0", s.get("mean", 0) > 0),
        ("нижняя граница CI95 > 0", s.get("ci_lo", 0) > 0),
        (f"недель с прогнозом >= {MIN_WEEKS}", s.get("weeks", 0) >= MIN_WEEKS),
        ("медиана недельной > 0", s.get("median", 0) > 0),
    ]
    return (all(ok for _, ok in checks),
            [f"  {'✓' if ok else '✗'} {name}" for name, ok in checks])


def load(hist: str) -> Tuple[Dict, Dict[str, Dict]]:
    with open(os.path.join(hist, "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    coins: Dict[str, Dict] = {}
    for sym in meta["symbols"]:
        p = os.path.join(hist, f"{sym}.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                coins[sym] = json.load(f)
    return meta, coins


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "universe"))
    ap.add_argument("--half", default="explore", choices=["explore", "holdout"])
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(args.hist, "_meta.json")):
        print("Нет _meta.json — загрузка не завершена", file=sys.stderr)
        return 1
    meta, coins = load(args.hist)
    start, end = int(meta["start_ms"]), int(meta["end_ms"])
    lo, hi = half_window(start, end, args.half)
    # Обучающая история — ВСЁ прошлое от начала набора; прогнозы — только
    # внутри выбранной половины. Для holdout прошлое включает explore, и это
    # честно: к моменту t оно уже случилось.
    all_dates = rebalance_dates(start, hi)
    predict = [t for t in all_dates if lo <= t]
    weeks, betas = walk_forward(coins, all_dates, predict)
    s = summarize(weeks)
    passed, lines = verdict(s)

    print("=" * 76)
    print(f"ЗАМЕР IX — МНОГОФАКТОРНАЯ МОДЕЛЬ ({len(FEATURES)} признаков)   "
          f"половина: {args.half}")
    print("=" * 76)
    print(f"Символов: {len(coins)}   ridge λ={RIDGE_LAMBDA}   "
          f"обучение: расширяющееся окно, первые {MIN_TRAIN_WEEKS} нед. без прогноза")
    if s.get("weeks", 0) >= 2:
        print(f"\nнедель с прогнозом: {s['weeks']}")
        print(f"средняя недельная нетто: {s['mean'] * 100:+.3f}%   "
              f"медиана: {s['median'] * 100:+.3f}%   t={s['t']:+.2f}")
        print(f"CI95 [{s['ci_lo'] * 100:+.3f}% .. {s['ci_hi'] * 100:+.3f}%]   "
              f"прибыльных недель {s['positive_weeks']}/{s['weeks']}")
    if betas:
        avg = np.mean(np.array(betas), axis=0)
        print("\nсредние веса признаков по всем переобучениям:")
        for fname, b in sorted(zip(FEATURES, avg), key=lambda x: -abs(x[1])):
            print(f"  {fname:12s} {b:+.4f}")
    print("\nПЛАНКА (замер IX — не смягчается):")
    print("\n".join(lines))
    print(f"\nВЕРДИКТ по {args.half}: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
