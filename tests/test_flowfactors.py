"""Замер XI. Как и в IX, главное — пара сквозных тестов: на шуме конвейер
обязан давать ноль (иначе подсматривает), на данных с сигналом,
заложенным именно в новые признаки, — находить его (иначе слеп)."""
import copy
import io
import zipfile

import numpy as np
import pytest

from tools import flowfactors as ff
from tools.fetch_metrics_binance import parse_snapshot
from tools.multifactor import walk_forward, summarize as mf_summarize
from tools.xsec import _DAY_MS, rebalance_dates


def _synth(n=40, days=560, planted=0.0, seed=0):
    """Цены — случайное блуждание. При planted > 0 доходность следующей
    недели зависит от изменения открытого интереса в момент t."""
    rng = np.random.default_rng(seed)
    coins, metrics = {}, {}
    for i in range(n):
        sym = f"C{i:02d}USDT"
        oi = 1e6
        closes, snaps = [100.0], {}
        lr = rng.normal(0, 0.02, days)
        oi_path = [oi]
        for d in range(1, days):
            oi_path.append(oi_path[-1] * float(np.exp(rng.normal(0, 0.03))))
        if planted:
            for d in range(7, days - 7):
                if d % 7 == 4:                       # понедельник (эпоха — четверг)
                    chg = oi_path[d] / oi_path[d - 7] - 1
                    lr[d:d + 7] += planted * chg / 7
        for d in range(1, days):
            closes.append(closes[-1] * float(np.exp(lr[d])))
        daily = [{"ts": d * _DAY_MS, "high": c * 1.01, "low": c * 0.99, "close": c,
                  "quote": 1e6 * (1 + i), "taker_quote": 0.5e6 * (1 + i)
                  * (1 + 0.1 * rng.normal())} for d, c in enumerate(closes)]
        for d in range(days):
            snaps[str(d * _DAY_MS)] = {"oi": oi_path[d - 1] if d else oi_path[0],
                                       "top_pos": float(np.exp(rng.normal(0, 0.2))),
                                       "acc": float(np.exp(rng.normal(0, 0.2)))}
        coins[sym] = {"daily": daily, "funding": []}
        metrics[sym] = snaps
    coins["BTCUSDT"] = {"daily": [{"ts": d * _DAY_MS, "high": 101, "low": 99,
                                   "close": 100.0, "quote": 1e9, "taker_quote": 5e8}
                                  for d in range(days)], "funding": []}
    return coins, metrics, days * _DAY_MS


def _run(coins, metrics, end):
    dates = rebalance_dates(0, end)
    weeks, _ = walk_forward(coins, dates, dates, cross_fn=ff.make_cross(metrics))
    return mf_summarize(weeks)


def test_pure_noise_produces_no_edge(monkeypatch):
    """ДЕТЕКТОР УТЕЧКИ: на шуме предсказывать нечего.

    Комиссия обнулена: тест проверяет конвейер, а не издержки — с ними на
    шуме средняя закономерно минус 0.13%/нед, и тест путал бы одно с другим
    (первая версия так и упала: t от −0.9 до −4.1). Решение — по
    объединённой статистике 12 прогонов, а не по каждому: отдельный прогон
    на 50 неделях даёт |t| > 2 примерно в одном случае из двадцати.
    Утечка будущего дала бы большой ПОЛОЖИТЕЛЬНЫЙ t во всех прогонах сразу."""
    import statistics
    import tools.multifactor as mf
    monkeypatch.setattr(mf, "ROUND_TRIP_FEE_PCT", 0.0)
    means, ts = [], []
    for seed in range(21, 33):
        s = _run(*_synth(seed=seed))
        assert s.get("weeks", 0) >= 20, "прогон пуст"
        means.append(s["mean"])
        ts.append(s["t"])
    z = statistics.fmean(means) / (statistics.stdev(means) / len(means) ** 0.5)
    assert abs(z) < 2.5, f"шум дал систематический результат z={z:+.2f}, t={ts}"
    assert max(ts) < 3.5, f"отдельный прогон на шуме дал t={max(ts):+.2f} — утечка"


def test_planted_open_interest_signal_is_found():
    s = _run(*_synth(planted=2.0, seed=7))
    assert s.get("weeks", 0) >= 20
    assert s["mean"] > 0 and s["t"] > 3.0, \
        f"сигнал в открытом интересе не найден: t={s['t']:+.2f}"


def test_features_never_see_the_future():
    coins, metrics, _ = _synth(n=3, days=80)
    t = 60 * _DAY_MS
    sym = "C00USDT"
    before = ff.features_at(sym, coins[sym], metrics, t)
    assert all(v is not None for v in before.values()), before
    pc, pm = copy.deepcopy(coins), copy.deepcopy(metrics)
    for d in pc[sym]["daily"]:
        if d["ts"] + _DAY_MS > t:
            d.update(close=1e9, quote=1e15, taker_quote=1e15)
    for k in list(pm[sym]):
        if int(k) > t:
            pm[sym][k] = {"oi": 1e15, "top_pos": 99.0, "acc": 99.0}
    assert ff.features_at(sym, pc[sym], pm, t) == before, \
        "признаки изменились от данных после момента t"


def test_open_interest_change_uses_contracts_and_the_week_before():
    metrics = {"X": {str(14 * _DAY_MS): {"oi": 110.0, "top_pos": 1.0, "acc": 2.0},
                     str(7 * _DAY_MS): {"oi": 100.0, "top_pos": 1.0, "acc": 1.0}}}
    h = {"daily": []}
    f = ff.features_at("X", h, metrics, 14 * _DAY_MS)
    assert f["oi_chg7"] == pytest.approx(0.10)
    assert f["acc_ls_chg7"] == pytest.approx(np.log(2.0))
    assert f["taker7"] is None, "без свечей поток не должен выдумываться"


def test_snapshot_parser_takes_the_last_row_not_after_t():
    rows = ["create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio",
            "2024-01-07 23:50:00,X,100,1,1,1.5,2.0,1",
            "2024-01-07 23:55:00,X,101,1,1,1.6,2.1,1",
            "2024-01-08 00:00:00,X,999,1,1,9.9,9.9,1"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(rows))
    from datetime import datetime, timezone
    t = int(datetime(2024, 1, 7, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    snap = parse_snapshot(buf.getvalue(), t)
    assert snap == {"oi": 101.0, "top_pos": 1.6, "acc": 2.1}


def test_verdict_requires_both_halves_and_ci99():
    s = {"mean": 0.01, "median": 0.01, "ci99_lo": 0.001, "weeks": 60,
         "alpha": {"alpha": 0.01, "lo": 0.001}}
    assert ff.verdict(s, (0.01, 0.01))[0]
    assert not ff.verdict(s, (0.02, -0.001))[0], "прошло с минусом в одной половине"
    assert not ff.verdict({**s, "ci99_lo": -0.001}, (0.01, 0.01))[0]
    assert not ff.verdict({**s, "alpha": {"alpha": 0.01, "lo": -0.001}}, (0.01, 0.01))[0]


def test_summary_computes_a_99_percent_interval():
    """Порог CI99 записан в спецификации из-за множественности (одиннадцатая
    гипотеза). Прежний тест подавал границу готовой, и подмена 2.576 на
    1.96 его не роняла."""
    import statistics
    weeks = [{"ts": i, "ret": r} for i, r in enumerate([0.02, -0.01, 0.015, 0.0, 0.01] * 6)]
    s = ff.summarize(weeks, {})
    se = statistics.stdev([w["ret"] for w in weeks]) / len(weeks) ** 0.5
    assert s["ci99_lo"] == pytest.approx(s["mean"] - 2.576 * se)
