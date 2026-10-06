"""Бумажная стратегия «низкая волатильность». Главное: она исполняет ТО ЖЕ
правило, что проверялось на истории, — иначе форвард-тест проверял бы
другую стратегию. Поэтому первым идёт сравнение с tools/lowvol.sides_at на
одних и тех же данных."""
import math

import numpy as np
import pytest

import strategy.lowvol_paper as lp

DAY = lp.DAY_MS
MONDAY = 20 * 7 * DAY + 4 * DAY          # понедельник 00:00 UTC (эпоха — четверг)


def _bars(closes, end_ms, turnover=1e6, forming=True):
    """Дневные бары, последний ЗАКРЫТ ровно к end_ms, плюс формирующийся."""
    n = len(closes)
    bars = [{"ts": end_ms - (n - i) * DAY, "open": c, "high": c * 1.01,
             "low": c * 0.99, "close": c, "volume": 1.0,
             "turnover": turnover(i) if callable(turnover) else turnover}
            for i, c in enumerate(closes)]
    if forming:
        bars.append({"ts": end_ms, "open": 1e9, "high": 1e9, "low": 1e9,
                     "close": 1e9, "volume": 1.0, "turnover": 1e15})
    return bars


def _walk(rng, n, sd):
    return list(100 * np.exp(np.cumsum(rng.normal(0, sd, n))))


# ── Совпадение с правилом, проверенным на истории ──────────────────────────

def test_selection_matches_the_backtested_rule_on_identical_data():
    """Отбор живой стратегии обязан совпасть с tools/lowvol.sides_at на тех
    же данных. Вселенная нарочно неудобная: свежие листинги 21–30 дней
    (на истории — 10% шортовой ноги), монеты без 20-дневной истории внутри
    топ-60, протухшая цена, монета без цены 14 дней назад. Каждое из
    расхождений, найденных ревью, проявилось бы здесь."""
    from tools import lowvol as hist
    rng = np.random.default_rng(11)
    live_bars, hist_coins = {}, {}
    for i in range(90):
        sym = f"S{i:03d}USDT"
        age = 70 if i % 7 else 22 + (i % 9)          # часть — свежие листинги
        if i % 23 == 0:
            age = 18                                   # без 20-дн. волатильности
        closes = _walk(rng, age, 0.01 + (i % 13) * 0.004)
        end = MONDAY if i % 31 else MONDAY - 4 * DAY   # часть — протухшие
        bars = _bars(closes, end, turnover=lambda k, i=i: 1e6 * (1 + i) * (1 + 0.1 * (k % 3)),
                     forming=False)
        live_bars[sym] = bars
        hist_coins[sym] = {"daily": [{"ts": b["ts"], "high": b["high"],
                                      "low": b["low"], "close": b["close"],
                                      "quote": b["turnover"]} for b in bars],
                           "funding": []}
    live, _ = lp.select(live_bars, MONDAY)
    expected = hist.sides_at(hist_coins, MONDAY)
    assert expected, "эталон пуст — тест ничего не сравнивает"
    assert live == expected, (
        f"живой отбор расходится с проверенным правилом:\n"
        f"  только в живом: {sorted(set(live.items()) - set(expected.items()))}\n"
        f"  только в истории: {sorted(set(expected.items()) - set(live.items()))}")


def test_young_listings_are_eligible_like_in_the_backtest():
    """На истории монете хватает 15 дней оборота, цены 14 дней назад и 21
    бара на волатильность. Прежнее требование «31 закрытый день» исключало
    свежие листинги, а на истории это 10% шортовой ноги."""
    closes = [100.0 + i for i in range(25)]
    closed = lp.closed_bars(_bars(closes, MONDAY), MONDAY)
    assert lp.eligible(closed, MONDAY)
    assert lp.liquidity(closed, MONDAY) is not None
    assert lp.realized_vol(closed) is not None


def test_universe_is_cut_before_coins_without_volatility_are_dropped():
    """Как на истории: сначала топ-60 по ликвидности, потом отсев монет без
    волатильности. Обратный порядок добирал бы вселенную до 60 следующими
    по ликвидности."""
    cands = {f"L{i:03d}USDT": (0.01 + i * 1e-4, 1e6 - i) for i in range(70)}
    for i in range(5):
        cands[f"L{i:03d}USDT"] = (None, 1e6 - i)       # самые ликвидные без волы
    sides = lp.pick_sides(cands)
    assert "L065USDT" not in sides and "L060USDT" not in sides, \
        "вселенная добрана за пределы топ-60"
    assert len(sides) == 2 * int(55 * lp.TOP_FRACTION)


def test_decision_ignores_bars_after_monday_midnight():
    """Расчёт может идти днём в понедельник, но решение — только по барам,
    закрытым к 00:00: бар понедельника — будущее."""
    rng = np.random.default_rng(5)
    bars = {f"D{i:02d}USDT": _bars(_walk(rng, 60, 0.01 + i * 0.002), MONDAY,
                                   forming=False) for i in range(30)}
    before, e_before = lp.select(bars, MONDAY)
    for b in bars.values():
        b.append({"ts": MONDAY, "open": 1, "high": 1e9, "low": 1e-9,
                  "close": 1e9, "volume": 1, "turnover": 1e18})
    after, e_after = lp.select(bars, MONDAY)
    assert before == after and e_before == e_after


def test_monday_of():
    assert lp.monday_of(MONDAY) == MONDAY
    assert lp.monday_of(MONDAY + 6 * DAY + DAY - 1) == MONDAY
    assert lp.monday_of(MONDAY + 7 * DAY) == MONDAY + 7 * DAY


def test_leg_return_signs_funding_and_fee():
    fee = lp.db.ROUND_TRIP_FEE_PCT / 100.0
    assert lp.leg_return(1, 100.0, 110.0, 0.0) == pytest.approx(0.10 - fee)
    assert lp.leg_return(-1, 100.0, 110.0, 0.0) == pytest.approx(-0.10 - fee)
    assert lp.leg_return(1, 100.0, 100.0, 0.003) == pytest.approx(-0.003 - fee)
    assert lp.leg_return(-1, 100.0, 100.0, 0.003) == pytest.approx(0.003 - fee)


def test_exit_is_the_bar_seven_days_after_entry_even_when_computed_later():
    """Поздний расчёт не имеет права удлинить удержание: пропущенный
    понедельник превращал 14 дней в одну «неделю»."""
    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes, MONDAY + 14 * DAY, forming=False)
    px, ts = lp.exit_price(bars, MONDAY)
    assert ts == MONDAY + 7 * DAY, "выход не по бару через 7 суток"
    assert px == closes[-1 - 7]


def test_delisted_mid_week_exits_at_last_price_after_entry():
    closes = [100.0] * 20 + [90.0, 80.0]          # торги прекратились в среду
    bars = _bars(closes, MONDAY + 2 * DAY, forming=False)
    px, ts = lp.exit_price(bars, MONDAY)
    assert px == 80.0 and ts == MONDAY + 2 * DAY


# ── Неделя на фейковой бирже ───────────────────────────────────────────────

class FakeClient:
    def __init__(self, n=40, seed=3):
        rng = np.random.default_rng(seed)
        self.series = {f"C{i:02d}USDT": _walk(rng, 80, 0.005 + i * 0.002)
                       for i in range(n)}
        self.series["BTCUSDT"] = [100.0 + 0.1 * i for i in range(80)]
        self.end = MONDAY            # последний закрытый бар ряда
        self.fail = set()            # символы, по которым запрос «падает»
        self.no_funding = False

    async def get_tickers(self):
        return [{"symbol": s, "turnover24h": str(1e6 + i)}
                for i, s in enumerate(self.series)]

    async def get_klines(self, symbol, interval="D", limit=25):
        if symbol in self.fail:
            return []
        days = (self.end - MONDAY) // DAY
        closes = self.series[symbol][: 60 + days]
        return _bars(closes, self.end, turnover=1e6)[-limit:]

    async def get_funding_history(self, symbol, start_ms, end_ms):
        if self.no_funding:
            return []
        return [{"ts": start_ms + k * 8 * 3600 * 1000, "rate": 0.0001}
                for k in range(1, 22) if start_ms + k * 8 * 3600 * 1000 <= end_ms]


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv.db"))

    async def no_sleep(_s):
        return None
    # паузы между запросами берегут лимит биржи, в тестах они только тормозят
    monkeypatch.setattr(lp.asyncio, "sleep", no_sleep)
    return tmp_path


async def _init():
    await lp.db.init_db()


async def test_week_opens_on_monday_and_closes_after_seven_days(paper_db):
    await _init()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    weeks = await lp.db.lowvol_open_weeks()
    assert len(weeks) == 1 and weeks[0]["week_start"] == MONDAY
    n_legs = len(weeks[0]["legs"])
    assert n_legs == 2 * int(40 * lp.TOP_FRACTION)

    # среда: ничего не закрывается и не открывается
    c.end = MONDAY + 2 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 2 * DAY + 3600 * 1000)
    assert len(await lp.db.lowvol_weeks()) == 1

    # следующий понедельник: неделя закрыта, открыта новая
    c.end = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 20 * 60 * 1000)
    closed = [w for w in await lp.db.lowvol_weeks() if w["closed_at"]]
    assert len(closed) == 1 and closed[0]["n"] == n_legs and closed[0]["ret"] is not None
    assert [w["week_start"] for w in await lp.db.lowvol_open_weeks()] == [MONDAY + 7 * DAY]


async def test_network_failure_postpones_close_instead_of_dropping_legs(paper_db):
    """Пустой ответ биржи — сбой, а не делистинг. Раньше позиция молча
    исключалась, и неделя закрывалась по оставшимся — при отказе посреди
    цикла отваливались шорты, и записывалась бета вместо спреда."""
    await _init()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    legs = (await lp.db.lowvol_open_weeks())[0]["legs"]
    c.fail = {legs[-1]["symbol"]}
    c.end = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 20 * 60 * 1000)
    assert not [w for w in await lp.db.lowvol_weeks() if w["closed_at"]], \
        "неделя закрыта, хотя цену одной позиции не удалось получить"
    c.fail = set()
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 80 * 60 * 1000)
    closed = [w for w in await lp.db.lowvol_weeks() if w["closed_at"]]
    assert len(closed) == 1 and closed[0]["n"] == len(legs), "повтор не закрыл неделю"


async def test_empty_funding_history_postpones_close(paper_db):
    """За неделю у перпетуала не бывает ни одной выплаты фандинга — пустой
    ответ значит сбой. Раньше фандинг молча становился нулём."""
    await _init()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    c.no_funding = True
    c.end = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 20 * 60 * 1000)
    assert not [w for w in await lp.db.lowvol_weeks() if w["closed_at"]]


async def test_late_close_still_uses_the_seven_day_bar(paper_db):
    """Бот лежал весь следующий понедельник и поднялся через неделю: неделя
    закрывается по бару ws + 7 суток, а не по последнему бару."""
    await _init()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    leg = (await lp.db.lowvol_open_weeks())[0]["legs"][0]
    c.end = MONDAY + 14 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 14 * DAY + 20 * 60 * 1000)
    import aiosqlite
    async with aiosqlite.connect(lp.db.DB_PATH) as conn:
        async with conn.execute("SELECT exit FROM lowvol_legs WHERE week_start=? "
                                "AND symbol=?", (MONDAY, leg["symbol"])) as cur:
            exit_px = (await cur.fetchone())[0]
    assert exit_px == pytest.approx(c.series[leg["symbol"]][60 + 7 - 1]), \
        "выход не по бару через 7 суток — удержание удлинено"


async def test_missed_close_does_not_block_the_next_week(paper_db):
    """Если закрытие задержалось, следующая неделя всё равно открывается в
    свой понедельник, и закрываются ОБЕ. Раньше бралась только самая свежая
    открытая неделя, и старая повисала."""
    await _init()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    first = (await lp.db.lowvol_open_weeks())[0]["legs"]
    c.fail = {first[0]["symbol"]}
    c.end = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 20 * 60 * 1000)
    assert [w["week_start"] for w in await lp.db.lowvol_open_weeks()] == \
        [MONDAY, MONDAY + 7 * DAY]
    c.fail = set()
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 80 * 60 * 1000)
    assert [w["week_start"] for w in await lp.db.lowvol_open_weeks()] == [MONDAY + 7 * DAY]


async def test_too_few_priced_legs_make_the_week_void(paper_db):
    """Как на истории: меньше 6 оценённых позиций — неделя не засчитывается.
    Раньше неделя считалась и по одной позиции."""
    await _init()
    await lp.db.lowvol_open_week(MONDAY, [{"symbol": f"V{i}USDT", "side": 1 if i < 4 else -1,
                                           "entry": 100.0} for i in range(8)], 100.0)
    c = FakeClient()
    c.series.update({f"V{i}USDT": [100.0] * 80 for i in range(8)})
    c.fail = {f"V{i}USDT" for i in range(5)}         # оценятся только 3
    c.end = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + lp.CLOSE_GIVE_UP_MS + 3600 * 1000)
    w = (await lp.db.lowvol_weeks())[0]
    assert w["closed_at"] and w["void"] == 1 and w["ret"] is None


async def test_closing_twice_does_not_overwrite(paper_db):
    await _init()
    await lp.db.lowvol_open_week(MONDAY, [{"symbol": "AUSDT", "side": 1, "entry": 1.0}], 100.0)
    assert await lp.db.lowvol_close_week(MONDAY, [{"symbol": "AUSDT", "exit": 1.1,
                                                   "funding": 0.0, "ret": 0.1}], 101.0)
    assert not await lp.db.lowvol_close_week(MONDAY, [], None), \
        "повторное закрытие прошло — итог был бы затёрт"
    assert not await lp.db.lowvol_close_week(MONDAY + 7 * DAY, [], None), \
        "закрытие несуществующей недели вернуло успех"
    w = (await lp.db.lowvol_weeks())[0]
    assert w["ret"] == pytest.approx(0.1) and w["btc_exit"] == 101.0


async def test_reopening_the_same_week_adds_no_legs(paper_db):
    await _init()
    assert await lp.db.lowvol_open_week(MONDAY, [{"symbol": "AUSDT", "side": 1, "entry": 1.0}], 100.0)
    assert not await lp.db.lowvol_open_week(MONDAY, [{"symbol": "BUSDT", "side": -1, "entry": 2.0}], 100.0)
    legs = {lg["symbol"] for lg in (await lp.db.lowvol_open_weeks())[0]["legs"]}
    assert legs == {"AUSDT"}


async def test_week_is_not_opened_outside_monday(paper_db):
    await _init()
    await lp.rebalance(FakeClient(), now_ms=MONDAY + DAY + 3600 * 1000)
    assert await lp.db.lowvol_weeks() == []


def test_weeks_before_the_corrected_rule_are_not_counted():
    """Недели 28.09 и 05.10 открыты отбором, расходившимся с проверенным, —
    это другая стратегия, и в форвард-тест они не входят."""
    old = [{"week_start": lp.FORWARD_START_MS - 7 * DAY * k, "ret": 0.05,
            "btc_entry": 1.0, "btc_exit": 1.0, "void": 0} for k in (1, 2)]
    s = lp.summarize(old)
    assert s["weeks"] == 0 and s["pre_rule"] == 2


def test_summary_reports_alpha_to_market():
    """Критерий форвард-теста — альфа к BTC, а не сырая доходность."""
    weeks = [{"ret": 0.01 + 1.5 * m, "btc_entry": 100.0, "btc_exit": 100.0 * (1 + m),
              "void": 0, "week_start": lp.FORWARD_START_MS + i * 7 * DAY}
             for i, m in enumerate((-0.02, 0.01, 0.03, -0.01, 0.0, 0.02))]
    s = lp.summarize(weeks)
    assert s["alpha"]["alpha"] == pytest.approx(0.01, abs=1e-12)
    assert s["alpha"]["beta"] == pytest.approx(1.5, abs=1e-12)


async def test_mass_fetch_failure_postpones_opening(paper_db):
    """Если свечи не пришли у заметной части списка, вселенная вышла бы
    случайно урезанной. Открытие переносится на следующий час — неделя всё
    равно стартует от бара на понедельник 00:00."""
    await _init()
    c = FakeClient()
    c.fail = {f"C{i:02d}USDT" for i in range(0, 40, 4)}        # 10 из 41
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    assert await lp.db.lowvol_weeks() == [], "неделя открыта по урезанной вселенной"
    c.fail = set()
    await lp.rebalance(c, now_ms=MONDAY + 70 * 60 * 1000)
    weeks = await lp.db.lowvol_open_weeks()
    assert len(weeks) == 1 and weeks[0]["week_start"] == MONDAY
