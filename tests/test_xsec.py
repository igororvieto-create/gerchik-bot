"""Кросс-секционный моментум: харнесс не имеет права видеть будущее.

Тот же класс ошибок, что и в tests/test_replay.py, но здесь он опаснее:
ранжирование по доходности — самая естественная точка утечки. Достаточно
включить в окно ранжирования свечу, которая ещё не закрылась, и «моментум»
начнёт предсказывать сам себя.

Данные здесь синтетические и годятся ТОЛЬКО для проверки механики
(docs/REVIEW.md §0-А п.4). Выводов о прибыльности по ним не делается.
"""
import math

import pytest

from tools.xsec import (LOOKBACK_DAYS, HOLD_DAYS, MIN_WEEKS, _closed_upto,
                        _funding_between, _price_at, _return_over,
                        rebalance_dates, summarize, verdict, week_result,
                        _DAY_MS, _WEEK_MS)
from tools.replay import _H4_MS

_T0 = 1_700_000_000_000 // _DAY_MS * _DAY_MS   # ровные сутки


def mk(symbol, n=400, drift=0.0, start=100.0, funding_rate=0.0):
    """Ряд 4h-свечей с заданным дрейфом за свечу."""
    k4, px = [], start
    for i in range(n):
        nxt = px * (1 + drift)
        k4.append({"ts": _T0 + i * _H4_MS, "open": px, "high": max(px, nxt),
                   "low": min(px, nxt), "close": nxt, "volume": 1000.0})
        px = nxt
    fund = [{"ts": c["ts"], "rate": funding_rate} for c in k4]
    return {"symbol": symbol, "k4": k4, "funding": fund}


# ── Заглядывание в будущее ──────────────────────────────────────────────────

def test_ranking_uses_only_closed_candles():
    """ts — время НАЧАЛА свечи. Отсечка по одному ts включила бы свечу,
    которая ещё формируется, и ранг стал бы предсказывать сам себя."""
    h = mk("A")
    c = h["k4"][50]
    mid = _closed_upto(h["k4"], c["ts"] + _H4_MS // 2)
    assert all(k["ts"] != c["ts"] for k in mid), \
        "незакрытая свеча попала в ранжирование"
    at_close = _closed_upto(h["k4"], c["ts"] + _H4_MS)
    assert any(k["ts"] == c["ts"] for k in at_close)


def test_ranking_is_unchanged_when_the_future_is_poisoned():
    """ГЛАВНЫЙ тест файла: ранг в момент T обязан не зависеть от того, что
    лежит в истории ПОСЛЕ T."""
    h = mk("A")
    t = _T0 + 100 * _H4_MS
    clean = _return_over(h["k4"], t - LOOKBACK_DAYS * _DAY_MS, t)
    poisoned = {**h, "k4": [k if k["ts"] + _H4_MS <= t
                            else {**k, "close": k["close"] * 1000,
                                  "high": k["high"] * 1000}
                            for k in h["k4"]]}
    assert _return_over(poisoned["k4"], t - LOOKBACK_DAYS * _DAY_MS, t) == \
        pytest.approx(clean), "ранг изменился от данных ПОСЛЕ момента отбора"


def test_forward_return_does_not_start_before_the_rebalance():
    """Доходность недели обязана считаться ОТ момента ребалансировки. Сдвиг
    назад хотя бы на свечу подмешал бы в результат то, по чему ранжировали."""
    h = mk("A", drift=0.01)
    t = _T0 + 100 * _H4_MS
    fwd = _return_over(h["k4"], t, t + HOLD_DAYS * _DAY_MS)
    back = _return_over(h["k4"], t - _H4_MS, t + HOLD_DAYS * _DAY_MS)
    assert fwd != pytest.approx(back), "окно доходности начинается не там"


# ── Механика портфеля ───────────────────────────────────────────────────────

def test_rebalance_dates_are_weekly_and_leave_room_for_the_lookback():
    """Первая дата отодвинута на окно ранжирования: иначе ранг считался бы
    по неполной истории и первые недели мерили бы длину ряда."""
    lo = _T0
    hi = _T0 + 200 * _DAY_MS
    d = rebalance_dates(lo, hi)
    assert d, "ни одной даты ребалансировки"
    assert d[0] >= lo + LOOKBACK_DAYS * _DAY_MS
    assert all(b - a == _WEEK_MS for a, b in zip(d, d[1:])), "шаг не недельный"
    assert d[-1] + HOLD_DAYS * _DAY_MS <= hi, "последняя неделя не помещается"
    # все даты — один и тот же день недели
    assert len({(x // _DAY_MS) % 7 for x in d}) == 1


def test_long_short_legs_are_taken_from_opposite_ends():
    """Сильные в лонг, слабые в шорт. Перепутанные ноги дали бы зеркальный
    результат, и по знаку это было бы не отличить от находки."""
    coins = {f"UP{i}": mk(f"UP{i}", drift=0.002) for i in range(6)}
    coins.update({f"DN{i}": mk(f"DN{i}", drift=-0.002) for i in range(6)})
    t = _T0 + 100 * _H4_MS
    w = week_result(coins, t)
    assert w is not None
    # растущие продолжают расти, падающие падать -> лонг сильных выигрывает
    assert w["ret"] > 0, "ноги перепутаны: моментум на трендовом ряду в минусе"


def test_costs_and_funding_are_subtracted():
    """Комиссии и фандинг обязаны уменьшать результат обеих ног. Без них
    замер меряет валовую доходность и врёт в плюс."""
    flat = {f"C{i}": mk(f"C{i}", drift=0.0) for i in range(12)}
    t = _T0 + 100 * _H4_MS
    w = week_result(flat, t)
    assert w is not None
    # на ровном ряду валовая доходность нулевая -> остаются одни издержки
    assert w["ret"] < 0, "издержки не вычтены"
    assert w["ret"] == pytest.approx(-0.0013, abs=1e-6), \
        "вычтено не то, что заявлено: 0.13% на оборот"


def test_funding_sign_follows_the_side():
    """Ставка положительна — лонги платят шортам. Знак обязан следовать за
    стороной, иначе издержка превратится в доход."""
    rows = [{"ts": _T0 + i * _H4_MS, "rate": 0.001} for i in range(20)]
    got = _funding_between(rows, _T0, _T0 + 10 * _H4_MS)
    assert got == pytest.approx(0.001 * 10)
    assert _funding_between(rows, _T0, _T0) == 0.0


def test_funding_changes_the_weekly_result_of_the_portfolio():
    """Одной проверки знака мало: важно, что фандинг реально доходит до
    результата недели. Ставку берём АСИММЕТРИЧНУЮ — при одинаковой у обеих
    ног платёж лонгов и доход шортов сокращаются, и пропущенный фандинг
    выглядел бы как ноль (мутация «фандинг не вычитается» так и выживала).

    Дорогой фандинг у растущих монет — не выдумка: именно там он и бывает
    высоким, потому что в них набивается толпа лонгов."""
    def universe(up_rate):
        c = {f"UP{i}": mk(f"UP{i}", drift=0.002, funding_rate=up_rate)
             for i in range(6)}
        c.update({f"DN{i}": mk(f"DN{i}", drift=-0.002, funding_rate=0.0)
                  for i in range(6)})
        return c
    t = _T0 + 100 * _H4_MS
    free = week_result(universe(0.0), t)
    paid = week_result(universe(0.001), t)
    assert free is not None and paid is not None
    assert paid["ret"] < free["ret"], "фандинг не дошёл до результата недели"
    # лонги держат 7 дней = 42 четырёхчасовых выплаты, шорты не платят;
    # результат усредняется по обеим ногам, поэтому эффект вдвое меньше
    expected = 0.001 * (HOLD_DAYS * 24 // 4) / 2
    assert (free["ret"] - paid["ret"]) == pytest.approx(expected, rel=0.02), \
        "вычтен не тот фандинг, что заявлен"


def test_missing_price_does_not_fabricate_a_return():
    h = mk("A", n=5)
    assert _price_at(h["k4"], _T0 - _DAY_MS) is None
    assert _return_over(h["k4"], _T0 - _DAY_MS, _T0 + _H4_MS) is None


# ── Планка ──────────────────────────────────────────────────────────────────

def test_bar_requires_median_not_just_mean():
    """У крипты тяжёлые хвосты: одна неделя способна вытянуть среднее в плюс
    при большинстве убыточных недель. Условие на медиану записано заранее
    именно против этого."""
    # 11 мелких убыточных недель против 9 крупных прибыльных: среднее в
    # плюсе, интервал ноль НЕ накрывает, недель хватает — блокирует ТОЛЬКО
    # медиана. С одной гигантской неделей тест был бы фиктивным: там его
    # заворачивал интервал, и условие на медиану не проверялось вовсе.
    rets = ([{"ts": 0, "names": 24, "ret": -0.001} for _ in range(11)]
            + [{"ts": 0, "names": 24, "ret": 0.05} for _ in range(9)])
    s = summarize(rets)
    assert s["mean"] > 0, "заготовка не воспроизводит случай, против которого правило"
    assert s["ci_lo"] > 0, "заготовку заворачивает интервал, а не медиана"
    assert s["weeks"] >= MIN_WEEKS
    assert s["median"] < 0, "медиана не отрицательная — проверять нечего"
    ok, _ = verdict(s)
    assert not ok, "планка пропустила результат, где большинство недель в минусе"


def test_bar_requires_enough_weeks():
    s = summarize([{"ts": 0, "names": 24, "ret": 0.05} for _ in range(5)])
    ok, _ = verdict(s)
    assert not ok, "вывод сделан по пяти неделям"


def test_bar_requires_the_interval_to_exclude_zero():
    """Средняя в плюсе при интервале, накрывающем ноль, — это шум."""
    rets = [0.10, -0.09, 0.11, -0.08, 0.09, -0.10] * 5
    s = summarize([{"ts": 0, "names": 24, "ret": r} for r in rets])
    assert s["mean"] > 0 and s["median"] > 0 and s["weeks"] >= MIN_WEEKS
    assert s["ci_lo"] < 0, "заготовка не воспроизводит шумный случай"
    ok, _ = verdict(s)
    assert not ok, "планка пропустила результат с интервалом через ноль"


def test_bar_passes_only_on_a_consistently_positive_result():
    s = summarize([{"ts": 0, "names": 24, "ret": 0.02 + 0.001 * (i % 3)}
                   for i in range(30)])
    ok, lines = verdict(s)
    assert ok, f"устойчиво прибыльный результат не прошёл: {lines}"
