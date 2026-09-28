"""Замер X: аномалия низкой волатильности — подтверждение на holdout.

Спецификация — docs/PREREGISTRATION.md (замер X), записана ДО расчётов.
Гипотезу породил замер IX на explore; проверяется она ТОЛЬКО на holdout,
не тронутом ни одним замером, и один раз. Запуск на explore запрещён в
самом коде: спецификация исключает «предварительную проверку», а запрет
в коде надёжнее памяти исполнителя.

Альфа к рынку — обязательное условие: портфель «лонг спокойных, шорт
бурных» по построению ставит против рынка, и на падавшем годе покажет
прибыль одной бетой (урок замера VII).

    python3 -m tools.lowvol --hist data/universe
"""
import argparse
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.battery import _realized_vol                      # noqa: E402
from tools.multifactor import MARKET, load, portfolio_week    # noqa: E402
from tools.replay import half_window                          # noqa: E402
from tools.xsec import (HOLD_DAYS, MIN_NAMES_PER_LEG, MIN_WEEKS,  # noqa: E402
                        TOP_FRACTION, _DAY_MS, rebalance_dates)
from tools.xsec_universe import _price_at, universe_at        # noqa: E402

VOL_DAYS = 20
Z95 = 1.96


def sides_at(coins: Dict[str, Dict], t: int) -> Dict[str, int]:
    """Лонг — нижние 20% по волатильности, шорт — верхние 20%."""
    vols: List[Tuple[str, float]] = []
    for s in universe_at(coins, t):
        v = _realized_vol(coins[s]["daily"], t, VOL_DAYS)
        if v is not None:
            vols.append((s, v))
    if len(vols) < MIN_NAMES_PER_LEG * 2:
        return {}
    vols.sort(key=lambda x: x[1])
    k = max(MIN_NAMES_PER_LEG, int(len(vols) * TOP_FRACTION))
    if k * 2 > len(vols):
        return {}
    out = {s: 1 for s, _ in vols[:k]}
    out.update({s: -1 for s, _ in vols[-k:]})
    return out


def market_return(coins: Dict[str, Dict], t: int) -> Optional[float]:
    mkt = coins.get(MARKET)
    if not mkt:
        return None
    a = _price_at(mkt["daily"], t, fresh=True)
    b = _price_at(mkt["daily"], t + HOLD_DAYS * _DAY_MS)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def alpha(ys: List[float], xs: List[float]) -> Optional[Dict]:
    """МНК y = a + b·x; альфа a и её стандартная ошибка."""
    n = len(ys)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - a - b * x for x, y in zip(xs, ys)]
    s2 = sum(r * r for r in resid) / (n - 2)
    se_a = math.sqrt(s2 * (1.0 / n + mx * mx / sxx))
    return {"alpha": a, "beta": b, "se": se_a,
            "lo": a - Z95 * se_a, "hi": a + Z95 * se_a}


def evaluate(coins: Dict[str, Dict], dates: List[int]) -> Dict:
    weeks, mk = [], []
    for t in dates:
        sides = sides_at(coins, t)
        if not sides:
            continue
        w = portfolio_week(coins, t, sides)
        m = market_return(coins, t)
        if w is None or m is None:
            continue
        weeks.append(w["ret"])
        mk.append(m)
    out: Dict = {"weeks": len(weeks)}
    if len(weeks) >= 2:
        mean, sd = statistics.fmean(weeks), statistics.stdev(weeks)
        se = sd / math.sqrt(len(weeks))
        out.update({"mean": mean, "median": statistics.median(weeks),
                    "ci_lo": mean - Z95 * se, "ci_hi": mean + Z95 * se,
                    "t": mean / se if se > 0 else 0.0,
                    "positive_weeks": sum(1 for r in weeks if r > 0),
                    "mkt_mean": statistics.fmean(mk)})
        out["alpha"] = alpha(weeks, mk)
    return out


def verdict(s: Dict) -> Tuple[bool, List[str]]:
    a = s.get("alpha") or {}
    checks = [
        ("средняя недельная нетто > 0", s.get("mean", 0) > 0),
        ("нижняя граница CI95 > 0", s.get("ci_lo", 0) > 0),
        (f"недель >= {MIN_WEEKS}", s.get("weeks", 0) >= MIN_WEEKS),
        ("медиана недельной > 0", s.get("median", 0) > 0),
        ("альфа к рынку > 0, нижняя граница CI95 > 0",
         a.get("alpha", 0) > 0 and a.get("lo", 0) > 0),
    ]
    return (all(ok for _, ok in checks),
            [f"  {'✓' if ok else '✗'} {name}" for name, ok in checks])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "universe"))
    ap.add_argument("--half", default="holdout")
    args = ap.parse_args()
    if args.half != "holdout":
        print("Замер X проверяется ТОЛЬКО на holdout (docs/PREREGISTRATION.md). "
              "Запуск на explore запрещён: гипотеза оттуда родилась.",
              file=sys.stderr)
        return 2
    meta, coins = load(args.hist)
    lo, hi = half_window(int(meta["start_ms"]), int(meta["end_ms"]), "holdout")
    s = evaluate(coins, rebalance_dates(lo, hi))
    passed, lines = verdict(s)
    print("=" * 76)
    print("ЗАМЕР X — АНОМАЛИЯ НИЗКОЙ ВОЛАТИЛЬНОСТИ   половина: holdout (один прогон)")
    print("=" * 76)
    print(f"Символов: {len(coins)}   лонг нижние 20% по волатильности {VOL_DAYS}д, "
          f"шорт верхние 20%")
    if s.get("weeks", 0) >= 2:
        print(f"\nнедель: {s['weeks']}   средняя {s['mean'] * 100:+.3f}%   "
              f"медиана {s['median'] * 100:+.3f}%   t={s['t']:+.2f}")
        print(f"CI95 [{s['ci_lo'] * 100:+.3f}% .. {s['ci_hi'] * 100:+.3f}%]   "
              f"прибыльных недель {s['positive_weeks']}/{s['weeks']}")
        print(f"рынок (BTC) за те же недели: в среднем {s['mkt_mean'] * 100:+.3f}%")
        a = s.get("alpha")
        if a:
            print(f"альфа {a['alpha'] * 100:+.3f}%/нед  CI95 [{a['lo'] * 100:+.3f}% .. "
                  f"{a['hi'] * 100:+.3f}%]   бета к BTC {a['beta']:+.2f}")
    print("\nПЛАНКА (замер X — не смягчается):")
    print("\n".join(lines))
    print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
