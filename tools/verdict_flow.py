"""Замер III: вердикт по потоку ордеров. Планка — docs/PREREGISTRATION.md.

Сохранён, чтобы результат из docs/FLOW.md можно было ВОСПРОИЗВЕСТИ, а не
принимать на слово. Вход — выгрузка /api/signals в JSON:

    curl -H "X-Dashboard-Token: ..." "$URL/api/signals?hours=1000&limit=2000" > sig.json
    python3 -m tools.verdict_flow sig.json
"""
import json, math, sys, os
from typing import Dict, List
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import ROUND_TRIP_FEE_PCT, funding_r
from tools.replay import avg_concurrency, wilson

rows = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "sig.json"))["signals"]

def ms(t):
    return int(datetime.fromisoformat(t.rstrip("Z")).timestamp() * 1000)

# ПОПУЛЯЦИЯ ЗАМЕРА, как записано: решённые, лента >= 1 мин, score >= 45
# (мерить на неторгуемых сигналах — другой вопрос), поглощение отдельно.
pop = [r for r in rows
       if r.get("outcome") in ("WIN", "LOSS", "BE")
       and r.get("flow_delta") is not None
       and (r.get("flow_span_min") or 0) >= 1.0
       and (r.get("score") or 0) >= 45
       and not r.get("flow_absorb")]

def side(r):
    d, dr = r["flow_delta"], r["direction"]
    if abs(d) < 0.2:
        return "нейтрально"
    agrees = (dr == "LONG" and d >= 0.2) or (dr == "SHORT" and d <= -0.2)
    return "поток ЗА" if agrees else "поток ПРОТИВ"

groups: Dict[str, List[Dict]] = {}
for r in pop:
    groups.setdefault(side(r), []).append(r)

def stats(rs):
    w = sum(1 for r in rs if r["outcome"] == "WIN")
    l = sum(1 for r in rs if r["outcome"] == "LOSS")
    b = sum(1 for r in rs if r["outcome"] == "BE")
    n = w + l + b
    if not n:
        return None
    win_rr = [r["rr"] for r in rs if r["outcome"] == "WIN" and r.get("rr")]
    avg_rr = sum(win_rr) / len(win_rr) if win_rr else 2.0
    gross = (w * avg_rr + b * 0.0 - l) / n
    # издержки построчно (неравенство Йенсена: 1/sl усредняем, а не sl)
    fees = [ROUND_TRIP_FEE_PCT / r["sl_pct"] for r in rs if r.get("sl_pct")]
    fee = sum(fees) / len(fees) if fees else 0.0
    fnds = [funding_r(r.get("funding"), r["direction"], r.get("sl_pct"))
            for r in rs]
    fnds = [f for f in fnds if f is not None]
    fnd = sum(fnds) / len(fnds) if fnds else 0.0
    conc = avg_concurrency([{"symbol": r["symbol"], "ts": ms(r["ts"])}
                            for r in rs])
    eff_n = max(1, int(round((w + l) / conc)))
    eff_w = int(round(w / conc))
    lo, hi = wilson(eff_w, eff_n)
    return {"w": w, "l": l, "be": b, "n": n, "decided": w + l,
            "winrate": w / (w + l) * 100 if (w + l) else 0.0,
            "avg_rr": avg_rr, "ev_gross": gross, "fee": fee, "fund": fnd,
            "ev_r": gross - fee + fnd, "conc": conc,
            "eff_n": eff_n, "ci_lo": lo * 100, "ci_hi": hi * 100,
            "breakeven": 100.0 / (1.0 + avg_rr)}

print("=" * 74)
print("ЗАМЕР III — ПОТОК ОРДЕРОВ. ЕДИНСТВЕННЫЙ ВЗГЛЯД")
print("=" * 74)
print(f"Популяция: решённые, лента >= 1 мин, score >= 45, без поглощения — {len(pop)}")
print()
res = {}
for k in ("поток ЗА", "поток ПРОТИВ", "нейтрально"):
    s = stats(groups.get(k, []))
    res[k] = s
    if not s:
        print(f"{k:14s} — пусто"); continue
    print(f"{k:14s} {s['w']:3d}W/{s['l']:3d}L  винрейт {s['winrate']:5.1f}%  "
          f"ev_r {s['ev_r']:+.3f}  (брутто {s['ev_gross']:+.3f}, "
          f"комиссия {s['fee']:.3f}, фандинг {s['fund']:+.4f})")
    print(f"{'':14s} перекрытие ×{s['conc']:.2f} → эфф. n = {s['eff_n']}, "
          f"CI95 винрейта [{s['ci_lo']:.1f}%..{s['ci_hi']:.1f}%], "
          f"безубыток {s['breakeven']:.1f}%")

# Контроль: те же сделки без разделения
ctrl = stats(pop)
if ctrl is None:
    print("популяция пуста — сравнивать нечего")
    raise SystemExit(1)
print(f"\n{'КОНТРОЛЬ':14s} {ctrl['w']:3d}W/{ctrl['l']:3d}L  "
      f"винрейт {ctrl['winrate']:5.1f}%  ev_r {ctrl['ev_r']:+.3f}")

a, b = res.get("поток ЗА"), res.get("поток ПРОТИВ")
print("\n" + "-" * 74)
print("ПЛАНКА (docs/PREREGISTRATION.md, замер III — не смягчается):")
checks = []
checks.append(("ev_r корзины «поток ЗА» > 0",
               a is not None and a["ev_r"] > 0,
               f"{a['ev_r']:+.3f}" if a else "нет данных"))
checks.append(("нижняя граница Уилсона выше безубытка",
               a is not None and a["ci_lo"] > a["breakeven"],
               f"{a['ci_lo']:.1f}% vs {a['breakeven']:.1f}%" if a else "—"))
z = None
if a is not None and b is not None and a["eff_n"] and b["eff_n"]:
    p1, n1 = a["w"] / a["conc"] / a["eff_n"], a["eff_n"]
    p2, n2 = b["w"] / b["conc"] / b["eff_n"], b["eff_n"]
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    z = (p1 - p2) / se if se > 0 else 0.0
checks.append(("|z| разницы с «поток ПРОТИВ» > 1.96",
               z is not None and abs(z) > 1.96,
               f"z = {z:+.2f}" if z is not None else "—"))
checks.append(("n >= 50 в каждой корзине",
               a is not None and b is not None and a["decided"] >= 50 and b["decided"] >= 50,
               f"{a['decided'] if a else 0} и {b['decided'] if b else 0}"))
for name, ok, val in checks:
    print(f"  {'✓' if ok else '✗'} {name}: {val}")
passed = all(ok for _, ok, _ in checks)
print(f"\nВЕРДИКТ: {'ПРОШЛА' if passed else 'НЕ ПРОШЛА'}")
