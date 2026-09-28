"""Бумажная стратегия «низкая волатильность». Главное: она исполняет ТО ЖЕ
правило, что проверялось на истории, — иначе форвард-тест проверял бы
другую стратегию."""
import math

import numpy as np
import pytest

import strategy.lowvol_paper as lp

DAY = lp.DAY_MS
MONDAY = 20 * 7 * DAY + 4 * DAY          # понедельник 00:00 UTC (эпоха — четверг)


def _bars(closes, end_ms, turnover=1e6):
    """Дневные бары, последний ЗАКРЫТ ровно к end_ms, плюс формирующийся."""
    n = len(closes)
    bars = [{"ts": end_ms - (n - i) * DAY, "open": c, "high": c, "low": c,
             "close": c, "volume": 1.0, "turnover": turnover}
            for i, c in enumerate(closes)]
    bars.append({"ts": end_ms, "open": 1e9, "high": 1e9, "low": 1e9,
                 "close": 1e9, "volume": 1.0, "turnover": 1e15})   # формируется
    return bars


def test_forming_bar_is_never_used():
    """Формирующийся бар содержит будущее: его цена не смеет попасть ни в
    волатильность, ни во вход."""
    closed = lp.closed_bars(_bars([100.0] * 40, MONDAY), MONDAY)
    assert all(k["close"] < 1e9 for k in closed)
    assert closed[-1]["ts"] + DAY == MONDAY


def test_vol_uses_twenty_closed_days_like_the_backtest():
    rng = np.random.default_rng(0)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0, 0.03, 40))))
    closed = lp.closed_bars(_bars(closes, MONDAY), MONDAY)
    lr = [math.log(closes[i] / closes[i - 1]) for i in range(40 - 20, 40)]
    expect = float(np.std(lr, ddof=1))
    assert lp.realized_vol(closed) == pytest.approx(expect, rel=1e-12)


def test_sides_follow_the_tested_rule():
    """Ликвидность отбирает вселенную, волатильность — стороны."""
    cands = {f"S{i:02d}USDT": (0.01 + i * 0.001, 1e6 + i) for i in range(30)}
    cands["ILLIQUIDUSDT"] = (0.0001, 1.0)      # самая спокойная, но неликвидна
    sides = lp.pick_sides(cands)
    assert sides["S00USDT"] == 1, "самая спокойная ликвидная не в лонге"
    assert sides["S29USDT"] == -1, "самая бурная не в шорте"
    assert "ILLIQUIDUSDT" not in sides or len(cands) <= lp.UNIVERSE_N


def test_liquidity_cut_is_top_sixty():
    cands = {f"L{i:03d}USDT": (0.01 + i * 1e-5, float(i)) for i in range(100)}
    cands["CALMLOWLIQUSDT"] = (1e-6, 0.5)      # спокойнее всех, но вне топ-60
    sides = lp.pick_sides(cands)
    assert "CALMLOWLIQUSDT" not in sides, "монета вне топ-60 по ликвидности вошла"
    assert len(sides) == 2 * int(60 * lp.TOP_FRACTION)


def test_leg_return_signs_funding_and_fee():
    fee = lp.db.ROUND_TRIP_FEE_PCT / 100.0
    assert lp.leg_return(1, 100.0, 110.0, 0.0) == pytest.approx(0.10 - fee)
    assert lp.leg_return(-1, 100.0, 110.0, 0.0) == pytest.approx(-0.10 - fee)
    assert lp.leg_return(1, 100.0, 100.0, 0.003) == pytest.approx(-0.003 - fee)
    assert lp.leg_return(-1, 100.0, 100.0, 0.003) == pytest.approx(0.003 - fee)


class FakeClient:
    """Биржа с управляемым временем: цены у каждой монеты свои и растут
    на заданный процент к следующему понедельнику."""

    def __init__(self, n=40):
        self.now = MONDAY
        self.n = n
        rng = np.random.default_rng(3)
        self.series = {}
        for i in range(n):
            sd = 0.005 + i * 0.002
            self.series[f"C{i:02d}USDT"] = list(100 * np.exp(np.cumsum(rng.normal(0, sd, 60))))
        self.series["BTCUSDT"] = [100.0] * 60
        self.funding = []

    async def get_tickers(self):
        return [{"symbol": s, "turnover24h": str(1e6 + i)}
                for i, s in enumerate(self.series)]

    async def get_klines(self, symbol, interval="D", limit=25):
        closes = self.series[symbol][:-7] if self.now == MONDAY else self.series[symbol]
        return _bars(closes[-limit:], self.now)

    async def get_funding_history(self, symbol, start_ms, end_ms):
        return self.funding


async def test_week_opens_then_closes_after_seven_days(tmp_path, monkeypatch):
    import core.db as d
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv.db"))
    await lp.db.init_db()
    c = FakeClient()

    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    week = await lp.db.lowvol_open_legs()
    assert week and week["week_start"] == MONDAY, "неделя не открыта от понедельника"
    n_legs = len(week["legs"])
    assert n_legs == 2 * max(lp.MIN_NAMES_PER_LEG, int(min(40, lp.UNIVERSE_N) * lp.TOP_FRACTION))

    # середина недели — рестарт бота: ничего не закрывается и не открывается
    await lp.rebalance(c, now_ms=MONDAY + 3 * DAY)
    assert (await lp.db.lowvol_open_legs())["week_start"] == MONDAY
    assert not [w for w in await lp.db.lowvol_weeks() if w["closed_at"]]

    # следующий понедельник: неделя закрывается, открывается новая
    c.now = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 10 * 60 * 1000)
    weeks = await lp.db.lowvol_weeks()
    closed = [w for w in weeks if w["closed_at"]]
    assert len(closed) == 1 and closed[0]["n"] == n_legs
    assert closed[0]["ret"] is not None
    assert (await lp.db.lowvol_open_legs())["week_start"] == MONDAY + 7 * DAY


async def test_restart_on_monday_does_not_open_a_second_week(tmp_path, monkeypatch):
    """Повторный запуск в тот же понедельник (рестарт, окно задержки
    планировщика) не должен создать вторую неделю или задвоить позиции."""
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv2.db"))
    await lp.db.init_db()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    first = await lp.db.lowvol_open_legs()
    await lp.rebalance(c, now_ms=MONDAY + 2 * 3600 * 1000)
    assert len(await lp.db.lowvol_weeks()) == 1
    assert len((await lp.db.lowvol_open_legs())["legs"]) == len(first["legs"])


def test_summary_needs_closed_weeks():
    assert lp.summarize([{"ret": None}])["weeks"] == 0
    s = lp.summarize([{"ret": 0.01}, {"ret": 0.02}, {"ret": 0.0}])
    assert s["weeks"] == 3 and s["mean"] == pytest.approx(0.01)


async def test_restart_with_a_different_selection_adds_nothing(tmp_path, monkeypatch):
    """При рестарте в тот же понедельник отбор может выйти ДРУГИМ: оборот
    за сутки меняется, и монеты на границе предотбора меняются местами.
    Уникальные ключи в базе не пускают только ТЕ ЖЕ позиции — новые монеты
    дописались бы к уже открытой неделе. Прежний тест подавал одинаковые
    данные и этого не видел (мутация выжила)."""
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv3.db"))
    await lp.db.init_db()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    first = {lg["symbol"] for lg in (await lp.db.lowvol_open_legs())["legs"]}

    # второй запуск видит другую вселенную: половина прежних монет пропала
    keep = sorted(c.series)[::2] + ["BTCUSDT"]
    c.series = {s: c.series[s] for s in keep}
    await lp.rebalance(c, now_ms=MONDAY + 2 * 3600 * 1000)
    after = {lg["symbol"] for lg in (await lp.db.lowvol_open_legs())["legs"]}
    assert after == first, f"к открытой неделе дописаны позиции: {after - first}"


async def test_stale_price_is_not_an_entry(tmp_path, monkeypatch):
    """Монета, переставшая торговаться неделю назад, не должна войти по
    протухшей цене — даже если она «самая спокойная» (застывшая цена даёт
    нулевую волатильность и первое место в лонге)."""
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv4.db"))
    await lp.db.init_db()
    c = FakeClient()
    c.series["DEADUSDT"] = [100.0] * 60
    real = c.get_klines

    async def klines(symbol, interval="D", limit=25):
        if symbol == "DEADUSDT":
            return _bars([100.0] * 40, MONDAY - 7 * DAY)   # оборвался неделю назад
        return await real(symbol, interval, limit)
    c.get_klines = klines

    async def tickers():
        base = [{"symbol": s, "turnover24h": str(1e6 + i)}
                for i, s in enumerate(c.series)]
        return base
    c.get_tickers = tickers

    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)
    syms = {lg["symbol"] for lg in (await lp.db.lowvol_open_legs())["legs"]}
    assert "DEADUSDT" not in syms, "вход по цене недельной давности"


async def test_failed_close_does_not_open_a_second_week(tmp_path, monkeypatch):
    """Провал записи закрытия — событие, а не «ничего не произошло». Если
    при этом открыть новую неделю, останутся две открытые, и старая не
    закроется никогда: дальше всегда берётся самая свежая."""
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv5.db"))
    await lp.db.init_db()
    c = FakeClient()
    await lp.rebalance(c, now_ms=MONDAY + 10 * 60 * 1000)

    async def broken_close(*a, **k):
        return False
    monkeypatch.setattr(lp.db, "lowvol_close_week", broken_close)
    c.now = MONDAY + 7 * DAY
    await lp.rebalance(c, now_ms=MONDAY + 7 * DAY + 10 * 60 * 1000)
    assert len(await lp.db.lowvol_weeks()) == 1, "открыта вторая неделя поверх незакрытой"
    assert (await lp.db.lowvol_open_legs())["week_start"] == MONDAY


async def test_reopening_the_same_week_adds_no_legs(tmp_path, monkeypatch):
    """Защита на уровне базы: повторное открытие той же недели с ДРУГИМ
    набором монет не дописывает позиции. Уникальный ключ (неделя, монета)
    пропустил бы новые монеты — стоп обязан ставить флаг открытия."""
    monkeypatch.setattr(lp.db, "DB_PATH", str(tmp_path / "lv6.db"))
    await lp.db.init_db()
    assert await lp.db.lowvol_open_week(MONDAY, [{"symbol": "AUSDT", "side": 1, "entry": 1.0}], 100.0)
    assert not await lp.db.lowvol_open_week(MONDAY, [{"symbol": "BUSDT", "side": -1, "entry": 2.0}], 100.0)
    legs = {lg["symbol"] for lg in (await lp.db.lowvol_open_legs())["legs"]}
    assert legs == {"AUSDT"}, f"к открытой неделе дописано: {legs}"
