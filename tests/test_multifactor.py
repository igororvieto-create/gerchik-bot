"""Замер IX: многофакторная модель. Главное — модель не видит будущего.

Два сквозных теста образуют пару: на чистом шуме конвейер обязан давать
НОЛЬ (иначе он подсматривает), на данных с заложенным сигналом — находить
его (иначе он слеп). Один без другого ничего не доказывает: слепой конвейер
проходит первый, подглядывающий — второй.
"""
import copy
import math

import numpy as np
import pytest

from tools import multifactor as mf
from tools.xsec import _DAY_MS, rebalance_dates


def _synth(n_coins=40, days=560, drift_sd=0.0, seed=0):
    """Случайные блуждания. drift_sd > 0 — у каждой монеты свой постоянный
    снос: прошлая доходность тогда предсказывает будущую (моментум)."""
    rng = np.random.default_rng(seed)
    coins = {}
    names = [f"C{i:02d}USDT" for i in range(n_coins)] + ["BTCUSDT"]
    for s in names:
        mu = rng.normal(0.0, drift_sd) if drift_sd else 0.0
        lr = mu + rng.normal(0.0, 0.02, days)
        close = 100.0 * np.exp(np.cumsum(lr))
        daily = []
        for d in range(days):
            c = float(close[d])
            daily.append({"ts": d * _DAY_MS, "high": c * 1.01, "low": c * 0.99,
                          "close": c, "quote": 1e6 * (1.0 + rng.random())})
        coins[s] = {"daily": daily, "funding": []}
    return coins, days * _DAY_MS


def _run(coins, end_ms):
    dates = rebalance_dates(0, end_ms)
    weeks, _ = mf.walk_forward(coins, dates, dates)
    return mf.summarize(weeks)


# ── Обучение только на прошлом ─────────────────────────────────────────────

def test_training_never_includes_the_week_being_predicted():
    week = 7 * _DAY_MS
    dates = [i * week for i in range(10)]
    t = dates[5]
    train = mf.training_dates(dates, t)
    assert t not in train, "дата прогноза попала в обучение — это её будущее"
    assert dates[4] in train, \
        "прошлая неделя исключена, хотя её метка уже известна к t"
    assert all(tp + week <= t for tp in train)


def test_features_never_see_the_future():
    coins, _ = _synth(n_coins=3, days=80)
    t = 60 * _DAY_MS
    h = coins["C00USDT"]
    mkt = coins["BTCUSDT"]["daily"]
    before = mf.features_at(h, t, mkt)
    assert before is not None and any(v is not None for v in before.values())

    poisoned = copy.deepcopy(h)
    pm = copy.deepcopy(mkt)
    for d in poisoned["daily"] + pm:
        if d["ts"] + _DAY_MS > t:
            d.update(close=1e9, high=1e9, low=1e-9, quote=1e15)
    poisoned["funding"] = [{"ts": t + 1, "rate": 99.0}]
    assert mf.features_at(poisoned, t, pm) == before, \
        "признаки изменились от данных ПОСЛЕ момента t"


def test_rank_normalize_is_bounded_and_treats_missing_as_middle():
    out = mf.rank_normalize([3.0, None, 1.0, 2.0, float("nan")])
    assert out[2] == pytest.approx(-0.5) and out[0] == pytest.approx(0.5)
    assert out[3] == pytest.approx(0.0)
    assert out[1] == 0.0 and out[4] == 0.0, "пропуск не в середине"


# ── Пара сквозных тестов: шум даёт ноль, сигнал находится ──────────────────

def test_pure_noise_produces_no_edge():
    """ДЕТЕКТОР УТЕЧКИ. На случайном блуждании без сноса предсказывать
    нечего. Если конвейер показывает значимую прибыль — он подсматривает.
    Проверяется по нескольким сидам, чтобы не поймать удачу одного."""
    ts = []
    for seed in (1, 2, 3):
        s = _run(*_synth(drift_sd=0.0, seed=seed))
        assert s.get("weeks", 0) >= 20, "прогон пуст — тест ничего не проверяет"
        ts.append(s["t"])
    assert max(ts) < 2.5 and abs(sum(ts) / len(ts)) < 1.5, \
        f"шум дал «преимущество» t={ts} — конвейер заглядывает в будущее"


def test_planted_momentum_is_found():
    """У каждой монеты свой постоянный снос — прошлое предсказывает
    будущее. Модель обязана это найти, иначе она слепа, и провал на
    реальных данных ничего бы не значил."""
    s = _run(*_synth(drift_sd=0.004, seed=7))
    assert s.get("weeks", 0) >= 20
    assert s["mean"] > 0 and s["t"] > 3.0, \
        f"заложенный сигнал не найден: средняя {s['mean']:+.4f}, t={s['t']:+.2f}"


def test_no_forecast_before_the_minimum_training_history():
    """Спецификация: первые 26 недель — только обучение. Порог определяет,
    КАКИЕ недели входят в вердикт; молча изменённый, он дал бы вердикт по
    другому набору недель, чем записано. На синтетике модель справляется и
    с одной неделей обучения, поэтому качество его не выдаёт — только
    прямая проверка."""
    coins, end_ms = _synth(n_coins=20, days=400, seed=4)
    dates = rebalance_dates(0, end_ms)
    weeks, _ = mf.walk_forward(coins, dates, dates)
    assert weeks, "прогнозов нет вовсе — тест пуст"
    first = min(w["ts"] for w in weeks)
    assert first >= dates[mf.MIN_TRAIN_WEEKS], \
        "прогноз сделан раньше, чем набралась минимальная история"
    assert mf.MIN_TRAIN_WEEKS == 26, "порог отличается от записанного в спецификации"
