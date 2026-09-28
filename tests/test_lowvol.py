"""Замер X: инструмент проверяется на синтетике ДО единственного прогона
на holdout. Ошибка в нём там — испорченная последняя чистая проверка."""
import subprocess
import os

import numpy as np
import pytest

from tools import lowvol as lv
from tools.xsec import _DAY_MS


def _daily(closes, quote=1e6):
    return [{"ts": d * _DAY_MS, "high": c * 1.01, "low": c * 0.99,
             "close": c, "quote": quote} for d, c in enumerate(closes)]


def test_calm_names_go_long_and_wild_names_go_short():
    rng = np.random.default_rng(0)
    coins = {}
    days = 60
    for i in range(20):
        sd = 0.005 + i * 0.004            # волатильность растёт с номером
        lr = rng.normal(0, sd, days)
        coins[f"V{i:02d}USDT"] = {"daily": _daily(list(100 * np.exp(np.cumsum(lr)))),
                                  "funding": []}
    sides = lv.sides_at(coins, days * _DAY_MS)
    assert sides.get("V00USDT") == 1, "самая спокойная не в лонге"
    assert sides.get("V19USDT") == -1, "самая бурная не в шорте"


def test_alpha_regression_recovers_known_values():
    xs = [0.01 * ((i * 7) % 11 - 5) for i in range(40)]
    ys = [0.003 + 1.5 * x for x in xs]            # альфа 0.3%, бета 1.5, без шума
    a = lv.alpha(ys, xs)
    assert a["alpha"] == pytest.approx(0.003, abs=1e-12)
    assert a["beta"] == pytest.approx(1.5, abs=1e-12)


def test_profit_explained_by_market_does_not_pass():
    """УРОК ЗАМЕРА VII. Портфель, чья прибыль целиком — бета к падавшему
    рынку: средняя положительна, интервал выше нуля, медиана положительна —
    но альфа ноль. Обязан НЕ пройти."""
    rng = np.random.default_rng(1)
    mk = list(rng.normal(-0.02, 0.03, 50))        # падающий рынок
    ys = [-1.0 * m for m in mk]                   # чистая короткая бета
    mean = sum(ys) / len(ys)
    s = {"weeks": 50, "mean": mean, "median": sorted(ys)[25],
         "ci_lo": mean - 0.001, "alpha": lv.alpha(ys, mk)}
    passed, lines = lv.verdict(s)
    assert s["mean"] > 0 and s["ci_lo"] > 0, "сценарий не тот: прибыли нет"
    assert not passed, "прибыль одной беты прошла планку"
    assert any("✗ альфа" in ln for ln in lines)


def test_genuine_alpha_passes():
    rng = np.random.default_rng(2)
    mk = list(rng.normal(0.0, 0.03, 60))
    ys = [0.01 + rng.normal(0, 0.01) for _ in mk]  # доход не зависит от рынка
    mean = sum(ys) / len(ys)
    sd = float(np.std(ys, ddof=1))
    s = {"weeks": 60, "mean": mean, "median": sorted(ys)[30],
         "ci_lo": mean - 1.96 * sd / 60 ** 0.5, "alpha": lv.alpha(ys, mk)}
    passed, _ = lv.verdict(s)
    assert passed


def test_cli_refuses_explore():
    """Спецификация исключает «предварительную проверку» на explore:
    гипотеза оттуда родилась."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(["python3", "-m", "tools.lowvol", "--half", "explore"],
                         capture_output=True, text=True, cwd=root, timeout=60)
    assert out.returncode == 2, "запуск на explore не запрещён"
    assert "запрещён" in out.stderr
