"""Замер VIII: батарея классических направленных правил.

Спецификация зафиксирована в docs/PREREGISTRATION.md (замер VIII) ДО
загрузки данных. Ни один параметр здесь не подбирается: окна взяты из
литературы и из уже проведённых замеров, и перечислены в таблице
спецификации. Любое отклонение — новая пре-регистрация, а не правка здесь.

Общая инфраструктура — та же, что в замере V-бис (tools/xsec_universe.py):
честная вселенная на каждую дату, свежесть цены, выход при делистинге,
фандинг и комиссия. Отличается только правило выбора стороны.

ЗАГЛЯДЫВАНИЕ В БУДУЩЕЕ. Каждое правило видит только дни, ЗАВЕРШЁННЫЕ к
моменту ребалансировки t (xsec_universe._closed_days). На это есть тест с
отравленным будущим: подмена всех данных после t не обязана менять ни одно
решение.

Запуск (сначала ТОЛЬКО explore — holdout не трогается до вердикта):
    python3 -m tools.battery --hist data/universe --half explore
"""
import argparse
import json
import math
import os
import statistics
import sys
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import ROUND_TRIP_FEE_PCT                       # noqa: E402
from tools.replay import half_window                          # noqa: E402
from tools.xsec import (HOLD_DAYS, MIN_NAMES_PER_LEG, MIN_WEEKS,  # noqa: E402
                        TOP_FRACTION, _DAY_MS, rebalance_dates)
from tools.xsec_universe import (_closed_days, _exit_price,   # noqa: E402
                                 _funding_between, _price_at, universe_at)

# --- Спецификация (docs/PREREGISTRATION.md, замер VIII). НЕ ПОДБИРАЕТСЯ. ---
TSMOM_DAYS = 14        # VIII-A и VIII-D: то же окно, что в замерах V и V-бис
DONCHIAN_DAYS = 20     # VIII-B: канонический параметр правила «черепах»
REVERSAL_DAYS = 3      # VIII-C: стандарт литературы по short-term reversal
VOL_DAYS = 20          # VIII-D: окно реализованной волатильности

# Поправка Бонферрони на четыре правила: вместо CI95 — CI99.
Z_CI99 = 2.576
# Меньше позиций — неделя не собирается. То же число, что в V-бис
# (MIN_NAMES_PER_LEG × 2), чтобы не вводить новую степень свободы.
MIN_POSITIONS = MIN_NAMES_PER_LEG * 2


def _ret(daily: List[Dict], t: int, days: int) -> Optional[float]:
    """Доходность за `days` дней, закончившихся к t. Только прошлое."""
    a = _price_at(daily, t - days * _DAY_MS)
    b = _price_at(daily, t, fresh=True)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def _realized_vol(daily: List[Dict], t: int, days: int) -> Optional[float]:
    """Стандартное отклонение дневных лог-доходностей за окно ДО t."""
    closed = _closed_days(daily, t)[-(days + 1):]
    if len(closed) < days + 1:
        return None
    rets = []
    for prev, cur in zip(closed, closed[1:]):
        if prev["close"] <= 0 or cur["close"] <= 0:
            return None
        rets.append(math.log(cur["close"] / prev["close"]))
    return statistics.stdev(rets) if len(rets) >= 2 else None


# ---- Правила. Каждое возвращает {символ: +1 лонг / -1 шорт}. ----

def rule_tsmom(coins: Dict[str, Dict], names: List[str], t: int) -> Dict[str, int]:
    """VIII-A. Знак собственной доходности за 14 дней."""
    out: Dict[str, int] = {}
    for s in names:
        r = _ret(coins[s]["daily"], t, TSMOM_DAYS)
        if r is None or r == 0:
            continue
        out[s] = 1 if r > 0 else -1
    return out


def rule_donchian(coins: Dict[str, Dict], names: List[str], t: int) -> Dict[str, int]:
    """VIII-B. Пробой канала по МАКСИМУМАМ и МИНИМУМАМ 20 предыдущих дней.

    Последний закрытый день сравнивается с каналом, построенным по 20
    дням ДО него — сам день в канал не входит, иначе закрытие никогда не
    превысило бы максимум, в который оно же и входит.
    """
    out: Dict[str, int] = {}
    for s in names:
        closed = _closed_days(coins[s]["daily"], t)
        if len(closed) < DONCHIAN_DAYS + 1:
            continue
        last, window = closed[-1], closed[-(DONCHIAN_DAYS + 1):-1]
        if any("high" not in d or "low" not in d for d in window):
            # Файл без high/low — первая версия загрузчика. Канал по
            # закрытиям подставлять НЕЛЬЗЯ: это другое правило.
            continue
        if _price_at(coins[s]["daily"], t, fresh=True) is None:
            continue
        hi = max(d["high"] for d in window)
        lo = min(d["low"] for d in window)
        if last["close"] > hi:
            out[s] = 1
        elif last["close"] < lo:
            out[s] = -1
    return out


def rule_reversal(coins: Dict[str, Dict], names: List[str], t: int) -> Dict[str, int]:
    """VIII-C. Лонг худшие 20% за 3 дня, шорт лучшие 20%."""
    ranked: List[Tuple[str, float]] = []
    for s in names:
        r = _ret(coins[s]["daily"], t, REVERSAL_DAYS)
        if r is not None:
            ranked.append((s, r))
    if len(ranked) < MIN_POSITIONS:
        return {}
    ranked.sort(key=lambda x: x[1])
    k = max(MIN_NAMES_PER_LEG, int(len(ranked) * TOP_FRACTION))
    if k * 2 > len(ranked):
        return {}
    out = {s: 1 for s, _ in ranked[:k]}
    out.update({s: -1 for s, _ in ranked[-k:]})
    return out


def rule_tsmom_lowvol(coins: Dict[str, Dict], names: List[str], t: int) -> Dict[str, int]:
    """VIII-D. VIII-A, но только для имён с волатильностью НИЖЕ медианы
    вселенной на эту дату."""
    vols: Dict[str, float] = {}
    for s in names:
        v = _realized_vol(coins[s]["daily"], t, VOL_DAYS)
        if v is not None:
            vols[s] = v
    if len(vols) < MIN_POSITIONS:
        return {}
    med = statistics.median(vols.values())
    calm = [s for s, v in vols.items() if v < med]
    return rule_tsmom(coins, calm, t)


RULES: Dict[str, Callable[[Dict[str, Dict], List[str], int], Dict[str, int]]] = {
    "A  моментум временного ряда 14д": rule_tsmom,
    "B  пробой канала Дончиана 20д":   rule_donchian,
    "C  краткосрочный возврат 3д":     rule_reversal,
    "D  моментум при низкой волат.":    rule_tsmom_lowvol,
}


def week_result(coins: Dict[str, Dict], t: int,
                rule: Callable[[Dict[str, Dict], List[str], int], Dict[str, int]]
                ) -> Optional[Dict]:
    """Одна неделя одного правила: решение на t, результат за 7 дней."""
    names = universe_at(coins, t)
    sides = rule(coins, names, t)
    end = t + HOLD_DAYS * _DAY_MS
    legs: List[float] = []
    for s, sign in sides.items():
        h = coins[s]
        entry = _price_at(h["daily"], t, fresh=True)
        exit_px = _exit_price(h["daily"], t, end)
        if entry is None or exit_px is None or entry <= 0:
            continue
        fwd = exit_px / entry - 1.0
        fnd = _funding_between(h.get("funding") or [], t, end)
        legs.append(sign * fwd - sign * fnd - ROUND_TRIP_FEE_PCT / 100.0)
    if len(legs) < MIN_POSITIONS:
        return None
    return {"ts": t, "positions": len(legs),
            "longs": sum(1 for v in sides.values() if v > 0),
            "shorts": sum(1 for v in sides.values() if v < 0),
            "ret": statistics.fmean(legs)}


def summarize99(weeks: List[Dict]) -> Dict:
    """Сводка с интервалом CI99 (поправка на четыре правила).

    Недельные доходности НЕ перекрываются (ребалансировка раз в неделю,
    удержание неделя), поэтому поправка на перекрытие меток не нужна.
    """
    rets = [w["ret"] for w in weeks]
    n = len(rets)
    out: Dict = {"weeks": n}
    if n < 2:
        return out
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets)
    se = sd / math.sqrt(n)
    out.update({"mean": mean, "median": statistics.median(rets), "sd": sd,
                "ci99_lo": mean - Z_CI99 * se, "ci99_hi": mean + Z_CI99 * se,
                "t": mean / se if se > 0 else 0.0,
                "positive_weeks": sum(1 for r in rets if r > 0)})
    return out


def verdict99(s: Dict) -> Tuple[bool, List[str]]:
    """Планка замера VIII. Не смягчается."""
    checks = [
        ("средняя недельная нетто > 0", s.get("mean", 0) > 0),
        ("нижняя граница CI99 > 0", s.get("ci99_lo", 0) > 0),
        (f"недель >= {MIN_WEEKS}", s.get("weeks", 0) >= MIN_WEEKS),
        ("медиана недельной > 0", s.get("median", 0) > 0),
    ]
    lines = [f"    {'✓' if ok else '✗'} {name}" for name, ok in checks]
    return all(ok for _, ok in checks), lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "universe"))
    ap.add_argument("--half", default="explore", choices=["explore", "holdout"])
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    meta_path = os.path.join(args.hist, "_meta.json")
    if not os.path.isfile(meta_path):
        print(f"Нет {meta_path} — загрузка не завершена", file=sys.stderr)
        return 1
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    coins: Dict[str, Dict] = {}
    for sym in meta["symbols"]:
        p = os.path.join(args.hist, f"{sym}.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                coins[sym] = json.load(f)
    lo, hi = half_window(int(meta["start_ms"]), int(meta["end_ms"]), args.half)

    print("=" * 76)
    print(f"ЗАМЕР VIII — БАТАРЕЯ НАПРАВЛЕННЫХ ПРАВИЛ   половина: {args.half}")
    print("=" * 76)
    print(f"Символов: {len(coins)} (вкл. делистнутые)   "
          f"поправка: Бонферрони на 4 правила → CI99")
    report: Dict[str, Dict] = {}
    for name, rule in RULES.items():
        weeks = [w for w in (week_result(coins, t, rule)
                             for t in rebalance_dates(lo, hi)) if w]
        s = summarize99(weeks)
        passed, lines = verdict99(s)
        report[name] = {"summary": s, "passed": passed}
        print(f"\n{name}")
        if s.get("weeks", 0) >= 2:
            print(f"    недель {s['weeks']}   средняя {s['mean'] * 100:+.3f}%   "
                  f"медиана {s['median'] * 100:+.3f}%   t={s['t']:+.2f}")
            print(f"    CI99 [{s['ci99_lo'] * 100:+.3f}% .. "
                  f"{s['ci99_hi'] * 100:+.3f}%]   "
                  f"прибыльных недель {s['positive_weeks']}/{s['weeks']}")
        else:
            print(f"    недель {s.get('weeks', 0)} — сравнивать нечего")
        print("\n".join(lines))
        print(f"    → {'ПРОШЛО' if passed else 'НЕ ПРОШЛО'}")
    any_passed = any(r["passed"] for r in report.values())
    print("\n" + "-" * 76)
    print(f"ИТОГ по {args.half}: "
          + (", ".join(n.split()[0] for n, r in report.items() if r["passed"])
             + " прошли — обязательна проверка на holdout"
             if any_passed else "ни одно правило не прошло планку"))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
