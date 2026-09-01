"""Кросс-секционный моментум: ранжирование монет друг против друга.

Спецификация зафиксирована в docs/PREREGISTRATION.md (замер V) ДО написания
этого файла. Ни один параметр здесь не подбирается: перебор окон, размеров
корзины и частот ребалансировки — тот самый путь, который уже дал 80
неудачных комбинаций в замере I.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ОСТАЛЬНЫХ ЗАМЕРОВ. Там момент входа задавало
СОБЫТИЕ (климакс, подход к круглому числу), и замер IV показал, что
события выбирают неудачные минуты: до цели 2R не доходит ни одно
направление. Здесь момент задаёт КАЛЕНДАРЬ, а решение принимается по
относительной силе монет, а не по абсолютному признаку на символе.

ЗАГЛЯДЫВАНИЕ В БУДУЩЕЕ. Ранжирование в момент T использует только свечи,
ЗАКРЫТЫЕ к T; доходность считается по следующей неделе. Отсечка
структурная — та же функция _closed_upto, что и в остальном прогоне, и на
неё есть тест с отравленным будущим.

Запуск:
    python3 -m tools.xsec --hist data/history --half explore
"""
import argparse
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import ROUND_TRIP_FEE_PCT      # noqa: E402
from tools.replay import _H4_MS, half_window  # noqa: E402

# --- Спецификация (docs/PREREGISTRATION.md, замер V). НЕ ПОДБИРАЕТСЯ. ---
LOOKBACK_DAYS = 14        # окно ранжирования
HOLD_DAYS = 7             # держим до следующей ребалансировки
TOP_FRACTION = 0.20       # лонг верхние 20%, шорт нижние 20%
MIN_NAMES_PER_LEG = 3     # меньше — нога не собирается, неделя пропускается
MIN_WEEKS = 20            # ниже этого числа вывод не делается

_DAY_MS = 24 * 3600 * 1000
_WEEK_MS = 7 * _DAY_MS


def _closed_upto(k4: List[Dict], now_ms: int) -> List[Dict]:
    """Свечи, ЗАВЕРШЁННЫЕ к моменту now_ms.

    ts — время НАЧАЛА свечи, поэтому она закрыта при ts + длительность <=
    now_ms. Сравнение по одному ts пустило бы свечу, начавшуюся минуту
    назад: она содержит будущее целиком.
    """
    return [k for k in k4 if k["ts"] + _H4_MS <= now_ms]


def _price_at(k4: List[Dict], now_ms: int) -> Optional[float]:
    closed = _closed_upto(k4, now_ms)
    if not closed:
        return None
    px = closed[-1]["close"]
    return px if px > 0 else None


def _return_over(k4: List[Dict], start_ms: int, end_ms: int) -> Optional[float]:
    a = _price_at(k4, start_ms)
    b = _price_at(k4, end_ms)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def _funding_between(rows: List[Dict], start_ms: int, end_ms: int) -> float:
    """Сумма ставок фандинга за период, в долях (не процентах).

    Лонг платит при положительной ставке, шорт получает — знак применяет
    вызывающий. Учитывать обязательно: позиция держится неделю и проходит
    два десятка выплат.
    """
    return sum(float(r.get("rate") or 0.0)
               for r in rows if start_ms < r["ts"] <= end_ms)


def rebalance_dates(start_ms: int, end_ms: int) -> List[int]:
    """Понедельники 00:00 UTC внутри окна.

    Момент задаёт КАЛЕНДАРЬ, а не рынок: в этом весь смысл замера. Первая
    дата отодвинута на окно ранжирования, иначе ранг считался бы по
    неполной истории.
    """
    # 1970-01-01 — четверг, поэтому ближайший понедельник = +4 дня
    first_monday = 4 * _DAY_MS
    lo = start_ms + LOOKBACK_DAYS * _DAY_MS
    n = math.ceil((lo - first_monday) / _WEEK_MS)
    out = []
    t = first_monday + n * _WEEK_MS
    while t + HOLD_DAYS * _DAY_MS <= end_ms:
        out.append(t)
        t += _WEEK_MS
    return out


def week_result(hist_by_sym: Dict[str, Dict], t: int) -> Optional[Dict]:
    """Одна неделя портфеля: ранжирование на t, доходность за следующую."""
    ranked: List[Tuple[str, float]] = []
    for sym, h in hist_by_sym.items():
        r = _return_over(h["k4"], t - LOOKBACK_DAYS * _DAY_MS, t)
        if r is not None:
            ranked.append((sym, r))
    if len(ranked) < MIN_NAMES_PER_LEG * 2:
        return None
    ranked.sort(key=lambda x: x[1], reverse=True)
    k = max(MIN_NAMES_PER_LEG, int(len(ranked) * TOP_FRACTION))
    if k * 2 > len(ranked):
        return None
    longs = [s for s, _ in ranked[:k]]
    shorts = [s for s, _ in ranked[-k:]]

    end = t + HOLD_DAYS * _DAY_MS
    legs: List[float] = []
    for side, names in (("L", longs), ("S", shorts)):
        sign = 1.0 if side == "L" else -1.0
        for sym in names:
            h = hist_by_sym[sym]
            fwd = _return_over(h["k4"], t, end)
            if fwd is None:
                continue
            # Фандинг: лонг платит при положительной ставке, шорт получает.
            fnd = _funding_between(h.get("funding") or [], t, end)
            # Комиссии: вход и выход по каждой позиции за неделю.
            legs.append(sign * fwd - sign * fnd - ROUND_TRIP_FEE_PCT / 100.0)
    if len(legs) < MIN_NAMES_PER_LEG * 2:
        return None
    return {"ts": t, "names": len(legs), "ret": statistics.fmean(legs)}


def summarize(weeks: List[Dict]) -> Dict:
    rets = [w["ret"] for w in weeks]
    n = len(rets)
    out: Dict[str, Any] = {"weeks": n}
    if n < 2:
        return out
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets)
    se = sd / math.sqrt(n)
    out.update({
        "mean": mean,
        "median": statistics.median(rets),
        "sd": sd,
        # Недельные доходности НЕ перекрываются, поэтому наблюдения
        # независимы и поправка на перекрытие здесь не нужна — в отличие от
        # сигнальных замеров, где метки делят одни и те же свечи.
        "ci_lo": mean - 1.96 * se,
        "ci_hi": mean + 1.96 * se,
        "t": mean / se if se > 0 else 0.0,
        "positive_weeks": sum(1 for r in rets if r > 0),
        "best": max(rets),
        "worst": min(rets),
    })
    return out


def verdict(s: Dict) -> Tuple[bool, List[str]]:
    """Планка из PREREGISTRATION, замер V. Не смягчается."""
    checks = [
        ("средняя недельная нетто > 0", s.get("mean", 0) > 0),
        ("нижняя граница CI95 > 0", s.get("ci_lo", 0) > 0),
        (f"недель >= {MIN_WEEKS}", s.get("weeks", 0) >= MIN_WEEKS),
        ("медиана недельной > 0", s.get("median", 0) > 0),
    ]
    lines = [f"  {'✓' if ok else '✗'} {name}" for name, ok in checks]
    return all(ok for _, ok in checks), lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "history"))
    ap.add_argument("--half", default="explore",
                    choices=["", "explore", "holdout"])
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    meta_path = os.path.join(args.hist, "_meta.json")
    if not os.path.isfile(meta_path):
        print(f"Нет {meta_path}", file=sys.stderr)
        return 1
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    lo, hi = half_window(int(meta["start_ms"]), int(meta["end_ms"]), args.half)
    if not args.half:
        lo, hi = int(meta["start_ms"]), int(meta["end_ms"])

    hist_by_sym: Dict[str, Dict] = {}
    for sym in meta["symbols"]:
        p = os.path.join(args.hist, f"{sym}.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                hist_by_sym[sym] = json.load(f)

    weeks = [w for w in (week_result(hist_by_sym, t)
                         for t in rebalance_dates(lo, hi)) if w]
    s = summarize(weeks)
    passed, lines = verdict(s)

    print("=" * 72)
    print("КРОСС-СЕКЦИОННЫЙ МОМЕНТУМ (docs/PREREGISTRATION.md, замер V)")
    print("=" * 72)
    print(f"Монет: {len(hist_by_sym)}   половина: {args.half or 'вся'}")
    print(f"Окно ранжирования: {LOOKBACK_DAYS} дн.   держим: {HOLD_DAYS} дн.   "
          f"ноги: по {int(TOP_FRACTION * 100)}%")
    print(f"Недель: {s.get('weeks', 0)}")
    if s.get("weeks", 0) >= 2:
        print(f"\nСредняя недельная НЕТТО: {s['mean'] * 100:+.3f}%   "
              f"медиана: {s['median'] * 100:+.3f}%")
        print(f"CI95 средней: [{s['ci_lo'] * 100:+.3f}%..{s['ci_hi'] * 100:+.3f}%]"
              f"   t={s['t']:+.2f}")
        print(f"Прибыльных недель: {s['positive_weeks']}/{s['weeks']}   "
              f"лучшая {s['best'] * 100:+.2f}%   худшая {s['worst'] * 100:+.2f}%")
    print("\nПЛАНКА:")
    print("\n".join(lines))
    print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    if passed:
        print("\nВНИМАНИЕ: вселенная выбрана по СЕГОДНЯШНЕМУ объёму среди монет,")
        print("торгуемых сейчас. Умершие монеты отсутствуют, и для моментума это")
        print("смещение ОПТИМИСТИЧЕСКОЕ — шорт слабейших выглядит лучше, чем был")
        print("бы с делистингами. Это обязано стоять первым в любом выводе.")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "weeks": weeks}, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
