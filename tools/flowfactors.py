"""Замер XI: позиционирование и поток — открытый интерес, лонги/шорты, агрессор.

Спецификация — docs/PREREGISTRATION.md (замер XI), записана ДО загрузки
данных. Шесть признаков закрыты; модель, вселенная, метки и портфель —
ровно те же, что в замере IX (tools/multifactor), меняется только набор
признаков. Знаки признаков выучивает модель на прошлом, а не выбираю я.

    python3 -m tools.flowfactors
"""
import json
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.lowvol import alpha                                  # noqa: E402
from tools.multifactor import (MIN_TRAIN_WEEKS, RIDGE_LAMBDA,    # noqa: E402
                               load, rank_normalize, walk_forward)
from tools.replay import half_window                            # noqa: E402
from tools.xsec import _DAY_MS, rebalance_dates                  # noqa: E402
from tools.xsec_universe import _closed_days, universe_at         # noqa: E402

FEATURES = ["oi_chg7", "top_pos_ls", "acc_ls", "acc_ls_chg7", "taker7", "taker_chg"]
MARKET = "BTCUSDT"
Z99 = 2.576
Z95 = 1.96
MIN_WEEKS_XI = 40
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snap(metrics: Dict[str, Dict], sym: str, t: int) -> Optional[Dict]:
    return (metrics.get(sym) or {}).get(str(t))


def _taker7(daily: List[Dict], t: int) -> Optional[float]:
    """Доля агрессивных покупок в обороте за 7 дней, закрытых к t."""
    w = [d for d in _closed_days(daily, t) if d["ts"] >= t - 7 * _DAY_MS]
    if len(w) < 5:
        return None
    q = sum(d.get("quote") or 0 for d in w)
    tq = [d.get("taker_quote") for d in w]
    if q <= 0 or any(v is None for v in tq):
        return None
    return sum(tq) / q          # type: ignore[arg-type]


def _log(v: Optional[float]) -> Optional[float]:
    return math.log(v) if v and v > 0 else None


def features_at(sym: str, h: Dict, metrics: Dict[str, Dict], t: int) -> Dict[str, Optional[float]]:
    now, prev = _snap(metrics, sym, t), _snap(metrics, sym, t - 7 * _DAY_MS)
    f: Dict[str, Optional[float]] = {k: None for k in FEATURES}
    if now and prev and now.get("oi") and prev.get("oi"):
        f["oi_chg7"] = now["oi"] / prev["oi"] - 1.0
    if now:
        f["top_pos_ls"] = _log(now.get("top_pos"))
        f["acc_ls"] = _log(now.get("acc"))
    if now and prev and f["acc_ls"] is not None and _log(prev.get("acc")) is not None:
        f["acc_ls_chg7"] = f["acc_ls"] - _log(prev.get("acc"))  # type: ignore[operator]
    tk, tk_prev = _taker7(h["daily"], t), _taker7(h["daily"], t - 7 * _DAY_MS)
    f["taker7"] = tk
    if tk is not None and tk_prev is not None:
        f["taker_chg"] = tk - tk_prev
    return f


def make_cross(metrics: Dict[str, Dict]):
    def cross(coins: Dict[str, Dict], t: int) -> Tuple[List[str], np.ndarray]:
        names = universe_at(coins, t)
        if not names:
            return [], np.zeros((0, len(FEATURES)))
        raw = {s: features_at(s, coins[s], metrics, t) for s in names}
        cols = [rank_normalize([raw[s][fn] for s in names]) for fn in FEATURES]
        return names, np.array(cols).T * math.sqrt(12.0)
    return cross


def _mkt_week(coins: Dict[str, Dict], t: int) -> Optional[float]:
    from tools.lowvol import market_return
    return market_return(coins, t)


def summarize(weeks: List[Dict], coins: Dict[str, Dict]) -> Dict:
    rets = [w["ret"] for w in weeks]
    out: Dict = {"weeks": len(rets)}
    if len(rets) < 3:
        return out
    mean, sd = statistics.fmean(rets), statistics.stdev(rets)
    se = sd / math.sqrt(len(rets))
    out.update({"mean": mean, "median": statistics.median(rets),
                "t": mean / se if se > 0 else 0.0,
                "ci99_lo": mean - Z99 * se, "ci99_hi": mean + Z99 * se,
                "positive": sum(1 for r in rets if r > 0)})
    pairs = [(w["ret"], m) for w in weeks
             if (m := _mkt_week(coins, w["ts"])) is not None]
    if len(pairs) >= 3:
        out["alpha"] = alpha([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def verdict(s: Dict, half_means: Tuple[Optional[float], Optional[float]]) -> Tuple[bool, List[str]]:
    a = s.get("alpha") or {}
    checks = [
        ("средняя недельная нетто > 0", s.get("mean", 0) > 0),
        ("нижняя граница CI99 > 0", s.get("ci99_lo", 0) > 0),
        (f"недель с прогнозом >= {MIN_WEEKS_XI}", s.get("weeks", 0) >= MIN_WEEKS_XI),
        ("медиана недельной > 0", s.get("median", 0) > 0),
        ("альфа к BTC > 0, нижняя граница CI95 > 0",
         a.get("alpha", 0) > 0 and a.get("lo", 0) > 0),
        ("средняя > 0 в каждой половине окна",
         all(m is not None and m > 0 for m in half_means)),
    ]
    return (all(ok for _, ok in checks),
            [f"  {'✓' if ok else '✗'} {name}" for name, ok in checks])


def load_metrics(path: str) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    if not os.path.isdir(path):
        return out
    for fn in os.listdir(path):
        if fn.endswith(".json") and not fn.startswith("_"):
            with open(os.path.join(path, fn), encoding="utf-8") as fh:
                d = json.load(fh)
            out[d["symbol"]] = d.get("snap") or {}
    return out


def main() -> int:
    hist = os.path.join(_ROOT, "data", "universe")
    mpath = os.path.join(_ROOT, "data", "metrics")
    if not os.path.isfile(os.path.join(mpath, "_meta.json")):
        print("Нет data/metrics/_meta.json — загрузка снимков не завершена",
              file=sys.stderr)
        return 1
    meta, coins = load(hist)
    metrics = load_metrics(mpath)
    start, end = int(meta["start_ms"]), int(meta["end_ms"])
    dates = rebalance_dates(start, end)
    weeks, betas = walk_forward(coins, dates, dates, cross_fn=make_cross(metrics))
    s = summarize(weeks, coins)
    _, ex_hi = half_window(start, end, "explore")
    ho_lo, _ = half_window(start, end, "holdout")
    ex = [w["ret"] for w in weeks if w["ts"] < ex_hi]
    ho = [w["ret"] for w in weeks if w["ts"] >= ho_lo]
    half_means = (statistics.fmean(ex) if ex else None,
                  statistics.fmean(ho) if ho else None)
    passed, lines = verdict(s, half_means)

    print("=" * 76)
    print("ЗАМЕР XI — ПОЗИЦИОНИРОВАНИЕ И ПОТОК (6 признаков), один прогон")
    print("=" * 76)
    have = sum(1 for v in metrics.values() for x in v.values() if x)
    print(f"символов: {len(coins)}   снимков позиционирования: {have}   "
          f"ridge λ={RIDGE_LAMBDA}, первые {MIN_TRAIN_WEEKS} нед. без прогноза")
    if s.get("weeks", 0) >= 3:
        print(f"\nнедель с прогнозом: {s['weeks']}")
        print(f"средняя {s['mean'] * 100:+.3f}%/нед   медиана {s['median'] * 100:+.3f}%   "
              f"t={s['t']:+.2f}   прибыльных {s['positive']}/{s['weeks']}")
        print(f"CI99 [{s['ci99_lo'] * 100:+.3f}% .. {s['ci99_hi'] * 100:+.3f}%]")
        a = s.get("alpha")
        if a:
            print(f"альфа к BTC {a['alpha'] * 100:+.3f}%/нед  CI95 "
                  f"[{a['lo'] * 100:+.3f}% .. {a['hi'] * 100:+.3f}%]  бета {a['beta']:+.2f}")
        print("средняя по половинам: explore "
              + (f"{half_means[0] * 100:+.3f}%" if half_means[0] is not None else "—")
              + f" ({len(ex)} нед.), holdout "
              + (f"{half_means[1] * 100:+.3f}%" if half_means[1] is not None else "—")
              + f" ({len(ho)} нед.)")
    if betas:
        avg = np.mean(np.array(betas), axis=0)
        print("\nсредние веса признаков:")
        for fn, b in sorted(zip(FEATURES, avg), key=lambda x: -abs(x[1])):
            print(f"  {fn:12s} {b:+.4f}")
    print("\nПЛАНКА (замер XI — не смягчается):")
    print("\n".join(lines))
    print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
