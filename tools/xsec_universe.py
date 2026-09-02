"""Кросс-секционный моментум на ЧЕСТНОЙ вселенной (замер V-бис).

Спецификация та же, что в tools/xsec.py, и меняться ей нельзя: замер V дал
t = 1.74, и любая правка параметров после этого была бы подгонкой под
недостающие 0.22. Меняются ТОЛЬКО данные.

Что здесь иначе:
  * вселенная строится НА КАЖДУЮ ДАТУ из всех 1018 символов USDT-M за всю
    историю, включая делистнутые. В замере V она была выбрана по
    сегодняшнему объёму среди монет, торгуемых сейчас, — то есть отобрана
    по факту выживания;
  * ликвидность считается по обороту в КОТИРУЕМОЙ валюте: объём в базовой
    монете не сравним между монетами разной цены;
  * делистинг внутри удерживаемой недели обрабатывается явно (см.
    _exit_price) — выбрасывать такие позиции нельзя.

Запуск:
    python3 -m tools.xsec_universe --hist data/universe --half explore
"""
import argparse
import json
import math
import os
import statistics
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import ROUND_TRIP_FEE_PCT          # noqa: E402
from tools.replay import half_window            # noqa: E402
from tools.xsec import (LOOKBACK_DAYS, HOLD_DAYS, TOP_FRACTION,   # noqa: E402
                        MIN_NAMES_PER_LEG, _DAY_MS, rebalance_dates,
                        summarize, verdict)

# Размер вселенной сохранён от замера V ради сопоставимости, а не выбран.
UNIVERSE_N = 60
# Окно, по которому оценивается ликвидность на дату. Медиана, а не среднее:
# один всплеск оборота не должен заводить монету в топ.
LIQ_WINDOW_DAYS = 30


def _closed_days(daily: List[Dict], now_ms: int) -> List[Dict]:
    """Дни, ЗАВЕРШЁННЫЕ к моменту now_ms.

    ts — время НАЧАЛА дня, поэтому день закрыт при ts + сутки <= now_ms.
    Сравнение по одному ts пустило бы текущий день, а он содержит будущее.
    """
    return [d for d in daily if d["ts"] + _DAY_MS <= now_ms]


# Насколько старой может быть цена, чтобы считаться действующей. Монета,
# не торговавшаяся два дня, не торгуется.
_MAX_STALE_DAYS = 2


def _price_at(daily: List[Dict], now_ms: int,
              fresh: bool = False) -> Optional[float]:
    """Цена на момент now_ms.

    fresh=True требует, чтобы цена была СВЕЖЕЙ. Без этого возвращалась
    последняя известная цена независимо от её возраста, и монета, умершая
    неделю назад, входила в портфель по протухшей цене — с нулевой
    доходностью, которой в реальности не было бы, потому что купить её было
    нельзя. Поймано тестом: монета, делистнутая за 5 дней до отбора,
    проходила в топ.
    """
    c = _closed_days(daily, now_ms)
    if not c:
        return None
    last = c[-1]
    if fresh and (now_ms - last["ts"]) > _MAX_STALE_DAYS * _DAY_MS:
        return None
    px = last["close"]
    return px if px > 0 else None


def _exit_price(daily: List[Dict], t: int, end: int) -> Optional[float]:
    """Цена выхода с учётом ДЕЛИСТИНГА внутри недели.

    Здесь прячется смещение, зеркальное исходному. Если монета перестала
    торговаться посреди недели, у неё нет цены на конец периода. Просто
    выбросить позицию нельзя: делистятся преимущественно слабейшие, то есть
    ровно нога ШОРТА, и выбрасывание тихо удалило бы её лучшие исходы.

    Правило (записано в PREREGISTRATION до данных): выходим по ПОСЛЕДНЕЙ
    доступной цене внутри недели — это то, что произошло бы в реальности.
    """
    at_end = _price_at(daily, end)
    if at_end is not None:
        last_ts = _closed_days(daily, end)[-1]["ts"]
        # Ряд может кончиться задолго до end: тогда _price_at вернёт цену
        # последнего дня жизни монеты, и это и есть цена выхода.
        if last_ts + _DAY_MS >= t:
            return at_end
    return None


def liquidity(daily: List[Dict], now_ms: int) -> Optional[float]:
    """Медианный дневной оборот в USDT за окно ДО now_ms."""
    lo = now_ms - LIQ_WINDOW_DAYS * _DAY_MS
    vals = [d["quote"] for d in _closed_days(daily, now_ms)
            if d["ts"] >= lo and d["quote"] > 0]
    if len(vals) < LIQ_WINDOW_DAYS // 2:
        return None
    return statistics.median(vals)


def universe_at(coins: Dict[str, Dict], t: int) -> List[str]:
    """Топ-N по обороту НА ДАТУ t. Монета, не торговавшаяся к t, не входит:
    её нельзя было купить."""
    scored: List[Tuple[str, float]] = []
    for sym, h in coins.items():
        liq = liquidity(h["daily"], t)
        if liq is None:
            continue
        # нужна история для ранжирования и живая цена на дату входа
        if _price_at(h["daily"], t - LOOKBACK_DAYS * _DAY_MS) is None:
            continue
        # Цена входа обязана быть СВЕЖЕЙ: по протухшей котировке позицию
        # не открыть.
        if _price_at(h["daily"], t, fresh=True) is None:
            continue
        scored.append((sym, liq))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in scored[:UNIVERSE_N]]


def _funding_between(rows: List[Dict], a: int, b: int) -> float:
    return sum(float(r.get("rate") or 0.0) for r in rows if a < r["ts"] <= b)


def week_result(coins: Dict[str, Dict], t: int) -> Optional[Dict]:
    names = universe_at(coins, t)
    if len(names) < MIN_NAMES_PER_LEG * 2:
        return None
    ranked: List[Tuple[str, float]] = []
    for sym in names:
        a = _price_at(coins[sym]["daily"], t - LOOKBACK_DAYS * _DAY_MS)
        b = _price_at(coins[sym]["daily"], t)
        if a and b:
            ranked.append((sym, b / a - 1.0))
    if len(ranked) < MIN_NAMES_PER_LEG * 2:
        return None
    ranked.sort(key=lambda x: x[1], reverse=True)
    k = max(MIN_NAMES_PER_LEG, int(len(ranked) * TOP_FRACTION))
    if k * 2 > len(ranked):
        return None

    end = t + HOLD_DAYS * _DAY_MS
    legs: List[float] = []
    delisted = 0
    for side, group in (("L", ranked[:k]), ("S", ranked[-k:])):
        sign = 1.0 if side == "L" else -1.0
        for sym, _ in group:
            h = coins[sym]
            entry = _price_at(h["daily"], t, fresh=True)
            exit_px = _exit_price(h["daily"], t, end)
            if entry is None or exit_px is None or entry <= 0:
                continue
            if _price_at(h["daily"], end) is None:
                delisted += 1
            fwd = exit_px / entry - 1.0
            fnd = _funding_between(h.get("funding") or [], t, end)
            legs.append(sign * fwd - sign * fnd - ROUND_TRIP_FEE_PCT / 100.0)
    if len(legs) < MIN_NAMES_PER_LEG * 2:
        return None
    return {"ts": t, "names": len(legs), "delisted": delisted,
            "universe": len(names), "ret": statistics.fmean(legs)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "universe"))
    ap.add_argument("--half", default="explore",
                    choices=["", "explore", "holdout"])
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    meta_path = os.path.join(args.hist, "_meta.json")
    if not os.path.isfile(meta_path):
        print(f"Нет {meta_path} — сначала tools/fetch_universe_binance.py",
              file=sys.stderr)
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
    if not args.half:
        lo, hi = int(meta["start_ms"]), int(meta["end_ms"])

    weeks = [w for w in (week_result(coins, t)
                         for t in rebalance_dates(lo, hi)) if w]
    s = summarize(weeks)
    passed, lines = verdict(s)

    print("=" * 72)
    print("КРОСС-СЕКЦИОННЫЙ МОМЕНТУМ, ЧЕСТНАЯ ВСЕЛЕННАЯ (замер V-бис)")
    print("=" * 72)
    print(f"Символов в пуле: {len(coins)} (включая делистнутые)")
    print(f"Вселенная на дату: топ-{UNIVERSE_N} по обороту в USDT")
    print(f"Окно ранжирования: {LOOKBACK_DAYS} дн.   держим: {HOLD_DAYS} дн.   "
          f"ноги: по {int(TOP_FRACTION * 100)}%")
    print(f"Половина: {args.half or 'вся'}   недель: {s.get('weeks', 0)}")
    if weeks:
        print(f"Позиций, закрытых по делистингу: "
              f"{sum(w['delisted'] for w in weeks)}")
    if s.get("weeks", 0) >= 2:
        print(f"\nСредняя недельная НЕТТО: {s['mean'] * 100:+.3f}%   "
              f"медиана: {s['median'] * 100:+.3f}%")
        print(f"CI95 средней: [{s['ci_lo'] * 100:+.3f}%..{s['ci_hi'] * 100:+.3f}%]"
              f"   t={s['t']:+.2f}")
        print(f"Прибыльных недель: {s['positive_weeks']}/{s['weeks']}   "
              f"лучшая {s['best'] * 100:+.2f}%   худшая {s['worst'] * 100:+.2f}%")
    print("\nПЛАНКА (не смягчается):")
    print("\n".join(lines))
    print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "weeks": weeks}, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
