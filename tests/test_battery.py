"""Замер VIII: правила батареи. Каждое правило видит только прошлое, и
каждое делает ровно то, что записано в спецификации."""
import copy

import pytest

from tools import battery as b
from tools.xsec import _DAY_MS


def _daily(closes, highs=None, lows=None, start=0, quote=1e6):
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return [{"ts": start + i * _DAY_MS, "high": h, "low": lo,
             "close": c, "quote": quote}
            for i, (c, h, lo) in enumerate(zip(closes, highs, lows))]


def _coins(n_days=30):
    """Разнородная вселенная: растущие, падающие, пробивающие, шумные —
    чтобы каждое правило приняло хоть одно решение и тест не был пустым."""
    coins = {}
    for i in range(6):
        up = [100 * (1 + 0.01 * (i + 1)) ** d for d in range(n_days)]
        coins[f"UP{i}USDT"] = {"daily": _daily(up), "funding": []}
        dn = [100 * (1 - 0.01 * (i + 1)) ** d for d in range(n_days)]
        coins[f"DN{i}USDT"] = {"daily": _daily(dn), "funding": []}
    for i in range(4):
        noisy = [100 + (5 if d % 2 else -5) * (i + 1) for d in range(n_days)]
        coins[f"NZ{i}USDT"] = {"daily": _daily(noisy), "funding": []}
    return coins


def test_rules_never_see_the_future():
    """Отравленное будущее не обязано менять ни одного решения.

    Все данные ПОСЛЕ момента t заменяются абсурдными значениями. Если хоть
    одно правило изменило решение — оно смотрело вперёд, и весь замер
    меряет не стратегию, а утечку."""
    n = 30
    coins = _coins(n)
    t = n * _DAY_MS
    names = list(coins)
    before = {name: rule(coins, names, t) for name, rule in b.RULES.items()}
    assert any(before.values()), "ни одно правило не приняло решения — тест пуст"

    poisoned = copy.deepcopy(coins)
    for h in poisoned.values():
        for k in range(10):
            h["daily"].append({"ts": t + k * _DAY_MS, "high": 1e9, "low": 1e-9,
                               "close": 1e9 if k % 2 else 1e-9, "quote": 1e12})
    after = {name: rule(poisoned, names, t) for name, rule in b.RULES.items()}
    for name in b.RULES:
        assert before[name] == after[name], f"{name} заглядывает в будущее"


def test_donchian_channel_is_built_from_highs_not_closes():
    """Канал «черепах» строится по МАКСИМУМАМ дня. Закрытие выше прежних
    закрытий, но ниже прежнего максимума — это НЕ пробой.

    Если канал молча перейти на закрытия, правило станет другим, а
    записанная гипотеза останется непроверенной."""
    n = 22
    closes = [100.0] * (n - 1) + [104.0]           # последнее закрытие выше всех закрытий
    highs = [110.0] * (n - 1) + [105.0]            # но ниже прежних максимумов
    coins = {"XUSDT": {"daily": _daily(closes, highs=highs), "funding": []}}
    assert b.rule_donchian(coins, ["XUSDT"], n * _DAY_MS) == {}, \
        "канал построен по закрытиям — пробоем посчитано то, что им не является"

    highs2 = [101.0] * (n - 1) + [105.0]           # теперь прежний максимум ниже
    coins2 = {"XUSDT": {"daily": _daily(closes, highs=highs2), "funding": []}}
    assert b.rule_donchian(coins2, ["XUSDT"], n * _DAY_MS) == {"XUSDT": 1}


def test_donchian_does_not_include_the_breakout_day_in_its_own_channel():
    """Если последний день входит в канал, закрытие никогда не превысит
    максимум, в который оно же и входит, — правило не сработает ни разу."""
    n = 22
    closes = [100.0] * (n - 1) + [120.0]
    highs = [101.0] * (n - 1) + [121.0]
    coins = {"XUSDT": {"daily": _daily(closes, highs=highs), "funding": []}}
    assert b.rule_donchian(coins, ["XUSDT"], n * _DAY_MS) == {"XUSDT": 1}


def test_donchian_refuses_files_without_highs_and_lows():
    """Файлы первой версии загрузчика не содержат high/low. Подставить
    канал по закрытиям — значит проверить другое правило молча."""
    n = 22
    daily = [{"ts": i * _DAY_MS, "close": 100.0 + i, "quote": 1e6}
             for i in range(n)]
    coins = {"XUSDT": {"daily": daily, "funding": []}}
    assert b.rule_donchian(coins, ["XUSDT"], n * _DAY_MS) == {}


def test_reversal_buys_the_worst_and_sells_the_best():
    n = 10
    coins = {}
    for i in range(10):
        # за последние 3 дня: от -9% до +9%
        closes = [100.0] * (n - 3) + [100 * (1 + (i - 5) * 0.006 * k)
                                      for k in (1, 2, 3)]
        coins[f"R{i}USDT"] = {"daily": _daily(closes), "funding": []}
    sides = b.rule_reversal(coins, list(coins), n * _DAY_MS)
    assert sides["R0USDT"] == 1, "худший за 3 дня обязан идти в лонг"
    assert sides["R9USDT"] == -1, "лучший за 3 дня обязан идти в шорт"


def test_low_vol_filter_excludes_the_noisy_half():
    """Фильтр обязан отсекать шумные имена, которые моментум БЕЗ фильтра
    взял бы.

    Прежняя версия теста проходила побочным путём: у шумных монет в
    фикстуре доходность за 14 дней была ровно нулевой, и моментум не брал
    их сам по себе — фильтр можно было удалить целиком, и тест оставался
    зелёным (мутация M54 выжила). Теперь шумные монеты ТРЕНДОВЫЕ: без
    фильтра они получили бы позицию, и отсечь их может только он."""
    n = 30
    coins = {}
    for i in range(12):
        # гладкие: тренд + крошечная неровность, волатильность мала, но не 0
        closes = [100 * 1.01 ** d * (1 + 0.001 * (((d * 7 + i) % 3) - 1))
                  for d in range(n)]
        coins[f"SM{i}USDT"] = {"daily": _daily(closes), "funding": []}
    for i in range(4):
        # шумные: тот же тренд, но скачки ±8% день ко дню
        closes = [100 * 1.01 ** d * (1.08 if d % 2 == 0 else 0.92)
                  for d in range(n)]
        coins[f"NZ{i}USDT"] = {"daily": _daily(closes), "funding": []}
    names, t = list(coins), n * _DAY_MS

    unfiltered = b.rule_tsmom(coins, names, t)
    assert any(s.startswith("NZ") for s in unfiltered), \
        "моментум без фильтра не берёт шумные — тест снова ничего не проверяет"
    filtered = b.rule_tsmom_lowvol(coins, names, t)
    assert filtered, "фильтр отсёк всё — пустой результат проверяет ничто"
    assert not any(s.startswith("NZ") for s in filtered), \
        "шумные имена прошли фильтр низкой волатильности"


def test_verdict_uses_ci99_not_ci95():
    """Бонферрони на четыре правила: планка — CI99. На выборке, где CI95
    уже выше нуля, а CI99 ещё нет, правило обязано НЕ пройти."""
    # среднее 0.004, разброс ±0.0098 на 30 неделях → t ≈ 2.2: между
    # порогом CI95 (1.96) и CI99 (2.576). Прежняя фикстура давала t ≈ 2.7,
    # выше ОБОИХ порогов, и тест ничего не различал.
    weeks = [{"ret": r} for r in ([0.0138, -0.0058] * 15)]
    s = b.summarize99(weeks)
    se = s["sd"] / (s["weeks"] ** 0.5)
    ci95_lo = s["mean"] - 1.96 * se
    assert ci95_lo > 0 > s["ci99_lo"], "выборка не разделяет CI95 и CI99"
    passed, _ = b.verdict99(s)
    assert not passed, "планка пропустила по CI95 — поправка не применена"


@pytest.mark.parametrize("name", list(b.RULES))
def test_every_registered_rule_is_wired(name):
    """Четыре правила записаны в спецификации — четыре и считаются."""
    assert callable(b.RULES[name])
    assert len(b.RULES) == 4
