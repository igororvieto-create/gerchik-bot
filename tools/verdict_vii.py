"""Замер VII: вердикт по фильтру режима рынка. Планка — docs/PREREGISTRATION.md.

Сохранён, чтобы результат можно было ВОСПРОИЗВЕСТИ, а не принимать на
слово. Вход — выгрузка /api/signals в JSON:

    curl -H "X-Dashboard-Token: ..." "$URL/api/signals?hours=2000&limit=5000" > sig.json
    python3 -m tools.verdict_vii sig.json

Матожидание считается ПОСТРОЧНО в R (победа = кратность цели строки,
поражение = −1, безубыток = 0, минус комиссия и плюс фандинг в R этой же
строки) и только потом усредняется: комиссия в R зависит от ширины стопа,
и усреднение стопов занижало бы её (неравенство Йенсена).

Интервалы — по эффективному размеру выборки с поправкой на перекрытие
меток (tools.replay.avg_concurrency), как во всех замерах проекта.
"""
import json
import math
import os
import statistics
import sys
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import (ROUND_TRIP_FEE_PCT, VII_DRIFT_MIN_PCT,  # noqa: E402
                     VII_MIN_PER_GROUP, VII_START, VII_TARGET_N, funding_r)
from tools.replay import avg_concurrency                       # noqa: E402

Z95 = 1.96


def _ms(t: str) -> int:
    return int(datetime.fromisoformat(t.rstrip("Z")).timestamp() * 1000)


def row_r(r: Dict) -> Optional[float]:
    """Результат строки в R, нетто."""
    o = r.get("outcome")
    if o == "WIN":
        gross = float(r.get("rr") or 2.0)
    elif o == "LOSS":
        gross = -1.0
    elif o == "BE":
        gross = 0.0
    else:
        return None
    sl = r.get("sl_pct")
    fee = ROUND_TRIP_FEE_PCT / sl if sl and sl > 0 else 0.0
    fnd = funding_r(r.get("funding"), r.get("direction"), sl) or 0.0
    return gross - fee + fnd


def group_of(r: Dict) -> Optional[str]:
    p = r.get("mkt_prior_pct")
    if p is None or abs(p) < VII_DRIFT_MIN_PCT:
        return None                           # боковик — в проверку не входит
    up = p > 0
    agrees = (r["direction"] == "LONG") == up
    return "по дрейфу" if agrees else "против дрейфа"


def population(rows: List[Dict]) -> List[Dict]:
    """Сигналы, созданные ПОСЛЕ рождения гипотезы, с решённым исходом."""
    return [r for r in rows
            if r.get("outcome") in ("WIN", "LOSS", "BE")
            and (r.get("ts") or "").rstrip("Z") >= VII_START]


def stats(rs: List[Dict]) -> Optional[Dict]:
    vals = [v for v in (row_r(r) for r in rs) if v is not None]
    n = len(vals)
    if n < 2:
        return None
    mean = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    conc = avg_concurrency([{"symbol": r["symbol"], "ts": _ms(r["ts"])}
                            for r in rs])
    n_eff = max(2, n / conc)
    se = sd / math.sqrt(n_eff)
    w = sum(1 for r in rs if r["outcome"] == "WIN")
    lo_ = sum(1 for r in rs if r["outcome"] == "LOSS")
    return {"n": n, "w": w, "l": lo_, "ev_r": mean, "sd": sd, "conc": conc,
            "n_eff": n_eff, "se": se,
            "ci_lo": mean - Z95 * se, "ci_hi": mean + Z95 * se,
            "winrate": w / (w + lo_) * 100 if (w + lo_) else 0.0}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "sig.json"
    rows = json.load(open(path, encoding="utf-8"))["signals"]
    pop = population(rows)
    groups: Dict[str, List[Dict]] = {"по дрейфу": [], "против дрейфа": []}
    side = 0
    for r in pop:
        g = group_of(r)
        if g is None:
            side += 1
            continue
        groups[g].append(r)

    print("=" * 76)
    print("ЗАМЕР VII — РЕЖИМ РЫНКА КАК ФИЛЬТР НАПРАВЛЕНИЯ. ЕДИНСТВЕННЫЙ ВЗГЛЯД")
    print("=" * 76)
    print(f"Сигналы после {VII_START}, решённые: {len(pop)}   "
          f"боковик (|дрейф| < {VII_DRIFT_MIN_PCT}%) исключён: {side}")
    res: Dict[str, Optional[Dict]] = {}
    for g in ("по дрейфу", "против дрейфа"):
        s = stats(groups[g])
        res[g] = s
        if s is None:
            print(f"\n{g}: исходов меньше двух")
            continue
        print(f"\n{g:14s} {s['w']:3d}W/{s['l']:3d}L  винрейт {s['winrate']:5.1f}%  "
              f"ev_r {s['ev_r']:+.3f}")
        print(f"{'':14s} перекрытие ×{s['conc']:.2f} → эфф. n = {s['n_eff']:.0f}, "
              f"CI95 ev_r [{s['ci_lo']:+.3f} .. {s['ci_hi']:+.3f}]")

    a, b = res["по дрейфу"], res["против дрейфа"]
    diff = diff_lo = diff_hi = None
    if a is not None and b is not None:
        diff = a["ev_r"] - b["ev_r"]
        se = math.sqrt(a["se"] ** 2 + b["se"] ** 2)
        diff_lo, diff_hi = diff - Z95 * se, diff + Z95 * se
        print(f"\nразница (по − против): {diff:+.3f}   "
              f"CI95 [{diff_lo:+.3f} .. {diff_hi:+.3f}]")

    n_total = (a["n"] if a else 0) + (b["n"] if b else 0)
    print("\n" + "-" * 76)
    print("ПЛАНКА (docs/PREREGISTRATION.md, замер VII — не смягчается):")
    checks = [
        (f"исходов >= {VII_TARGET_N}, в каждой группе >= {VII_MIN_PER_GROUP}",
         n_total >= VII_TARGET_N and a is not None and b is not None
         and a["n"] >= VII_MIN_PER_GROUP and b["n"] >= VII_MIN_PER_GROUP,
         f"{n_total}: {a['n'] if a else 0} / {b['n'] if b else 0}"),
        ("ev_r группы «по дрейфу» > 0",
         a is not None and a["ev_r"] > 0,
         f"{a['ev_r']:+.3f}" if a else "—"),
        ("нижняя граница CI95 этой группы > 0",
         a is not None and a["ci_lo"] > 0,
         f"{a['ci_lo']:+.3f}" if a else "—"),
        ("разница с «против» > 0 и её CI95 без нуля",
         diff is not None and diff_lo is not None and diff > 0 and diff_lo > 0,
         f"{diff:+.3f}, нижняя {diff_lo:+.3f}" if diff is not None
         and diff_lo is not None else "—"),
    ]
    for name, ok, val in checks:
        print(f"  {'✓' if ok else '✗'} {name}: {val}")
    passed = all(ok for _, ok, _ in checks)
    print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
