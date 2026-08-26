"""Прогон по истории: харнесс обязан мерить БОЕВУЮ стратегию, а не свою.

Два класса ошибок, ради которых написан этот файл:

1. ЗАГЛЯДЫВАНИЕ В БУДУЩЕЕ — кардинальная ошибка бэктеста. Любая утечка
   данных «после момента T» превращает результат в самообман, причём
   красивый: цифры становятся ЛУЧШЕ, а не хуже, поэтому по самому отчёту
   такую ошибку не заметить.
2. ПОДМЕНА СТРАТЕГИИ — если харнесс повторяет логику отбора вместо вызова
   боевой, он меряет другую стратегию под тем же именем.

ВАЖНО про синтетику (docs/REVIEW.md §0-А п.4): данные здесь нужны ТОЛЬКО
для проверки механики харнесса. Делать по ним выводы о прибыльности
стратегии нельзя — ряд нарисован, а не наблюдён.
"""
import math
import random

import pytest

import strategy.scanner as scanner
from core.config import cfg
from tools.replay import (ReplayClient, build_ticker, judge_signal,
                          replay_symbol, _acc, _finish, wilson, _H4_MS)

_T0 = 1_700_000_000_000


def make_hist(symbol="TSTUSDT", n=120, seed=2, drift=-0.002, depth=0.985,
              vol_mult=4.0, climax_at=110, launch_offset=400):
    """Ряд с климаксом, на котором боевой отбор реально срабатывает.

    Параметры подобраны так, чтобы сигнал появился: тест, в котором отбор
    ничего не выдаёт, проверял бы пустоту (§0-Б п.2).
    """
    random.seed(seed)
    k4 = []
    px = 100.0
    for i in range(n):
        px2 = px * (1 + drift + random.gauss(0, 0.003))
        hi = max(px, px2) * (1 + abs(random.gauss(0, 0.002)))
        lo = min(px, px2) * (1 - abs(random.gauss(0, 0.002)))
        vol = 600_000 * (1 + random.random() * 0.2)
        if i == climax_at:
            lo = min(px, px2) * depth
            px2 = px * 0.998
            hi = max(px, px2) * 1.001
            vol = 600_000 * vol_mult
        k4.append({"ts": _T0 + i * _H4_MS, "open": px, "high": hi, "low": lo,
                   "close": px2, "volume": vol, "turnover": vol * px2})
        px = px2
    k1, k15 = [], []
    for c in k4:
        for j in range(4):
            k1.append({"ts": c["ts"] + j * 3600_000, "open": c["open"],
                       "high": c["high"], "low": c["low"], "close": c["close"],
                       "volume": c["volume"] / 4})
        for j in range(16):
            k15.append({"ts": c["ts"] + j * 900_000, "open": c["open"],
                        "high": c["high"], "low": c["low"], "close": c["close"],
                        "volume": c["volume"] / 16})
    oi = [{"ts": c["ts"], "oi": 50000 * (1 + 0.03 * math.sin(i / 5))}
          for i, c in enumerate(k4)]
    fund = [{"ts": c["ts"], "rate": 0.0008} for c in k4]
    return {"symbol": symbol, "launch_ms": _T0 - launch_offset * _H4_MS,
            "k4": k4, "k1": k1, "k15": k15, "oi": oi, "funding": fund}


def poison_future(hist, now_ms):
    """Всё, что не завершено к now_ms, заменяется абсурдом.

    Сильнее простого усечения: усечение поймало бы только чтение за конец
    списка, а подмена ловит ЛЮБОЕ использование будущего — цифры сразу
    уедут, если хоть один гейт заглянул вперёд.
    """
    dur = {"k4": _H4_MS, "k1": 3600_000, "k15": 900_000}
    out = {k: v for k, v in hist.items()}
    for key in ("k4", "k1", "k15"):
        rows = []
        for c in hist[key]:
            if c["ts"] + dur[key] <= now_ms:
                rows.append(c)
            else:
                rows.append({**c, "open": c["open"] * 10, "high": c["high"] * 10,
                             "low": c["low"] * 10, "close": c["close"] * 10,
                             "volume": c["volume"] * 1000})
        out[key] = rows
    out["oi"] = [r if r["ts"] < now_ms else {**r, "oi": r["oi"] * 1000}
                 for r in hist["oi"]]
    out["funding"] = [r if r["ts"] < now_ms else {**r, "rate": 0.5}
                      for r in hist["funding"]]
    return out


def _signal_moment(hist):
    """Момент, в который боевой отбор выдаёт сигнал на этом ряду."""
    return hist["k4"][110]["ts"] + _H4_MS   # закрытие климакса


# ── Заглядывание в будущее ───────────────────────────────────────────────────

async def test_analysis_is_identical_when_the_future_is_poisoned():
    """ГЛАВНЫЙ тест файла. Отбор в момент T обязан дать ровно тот же сигнал
    независимо от того, что лежит в истории ПОСЛЕ T."""
    hist = make_hist()
    now = _signal_moment(hist)

    scanner._LISTING_AGE_CACHE.clear()
    clean = await scanner._analyze_symbol(
        ReplayClient(hist, now), build_ticker(hist, now))
    assert clean is not None, "на этом ряду отбор обязан выдать сигнал"

    scanner._LISTING_AGE_CACHE.clear()
    poisoned_hist = poison_future(hist, now)
    poisoned = await scanner._analyze_symbol(
        ReplayClient(poisoned_hist, now),
        build_ticker(poisoned_hist, now))
    assert poisoned is not None

    for field in ("signal_type", "direction", "score", "price", "entry", "sl",
                  "tp1", "tp2", "tp3", "rr", "headroom", "sl_pct", "atr_pct",
                  "oi_change", "vol_ratio", "funding", "confidence", "candle_ts"):
        assert getattr(clean, field) == pytest.approx(getattr(poisoned, field)), (
            f"поле {field} изменилось от данных ПОСЛЕ момента анализа — "
            f"заглядывание в будущее")


async def test_ticker_is_built_only_from_completed_candles():
    """Тикер — второй канал утечки: цена, изменение за 24ч, объём и OI
    берутся не из klines, а собираются отдельно."""
    hist = make_hist()
    now = _signal_moment(hist)
    a = build_ticker(hist, now)
    b = build_ticker(poison_future(hist, now), now)
    assert a == b, "тикер собран из данных после момента анализа"


def test_candle_is_closed_only_after_its_full_duration():
    """ts у Bybit — время НАЧАЛА свечи. Отсечка по одному ts пустила бы
    свечу, начавшуюся минуту назад, а она содержит будущее целиком."""
    hist = make_hist()
    c = hist["k4"][50]
    # момент внутри свечи: она НЕ завершена
    mid = ReplayClient(hist, c["ts"] + _H4_MS // 2)
    assert all(k["ts"] != c["ts"] for k in mid._closed("k4")), \
        "незавершённая свеча попала в закрытые"
    # ровно момент закрытия: свеча завершена
    at_close = ReplayClient(hist, c["ts"] + _H4_MS)
    assert any(k["ts"] == c["ts"] for k in at_close._closed("k4"))


async def test_forming_candle_carries_no_future_information():
    """Боевой анализ рассчитан на klines[-1] = формирующаяся свеча. Подставь
    сюда настоящую следующую — и весь прогон станет самообманом."""
    hist = make_hist()
    i = 60
    now = hist["k4"][i]["ts"] + _H4_MS
    kl = await ReplayClient(hist, now).get_klines("TSTUSDT", "240", 26)
    forming, last_closed = kl[-1], kl[-2]
    assert last_closed["ts"] == hist["k4"][i]["ts"]
    assert forming["ts"] == now
    assert forming["open"] == forming["high"] == forming["low"] == forming["close"]
    assert forming["close"] == last_closed["close"]
    assert forming["volume"] == 0.0
    # и главное — она не равна настоящей следующей свече
    assert forming["high"] != hist["k4"][i + 1]["high"]


async def test_klines_shape_matches_the_live_client():
    """Длина ответа и порядок обязаны совпадать с боевым клиентом, иначе
    индексы [-1]/[-2] в анализе означают другие свечи."""
    hist = make_hist()
    now = hist["k4"][60]["ts"] + _H4_MS
    c = ReplayClient(hist, now)
    for limit in (10, 26, 40):
        kl = await c.get_klines("TSTUSDT", "240", limit)
        assert len(kl) == limit
        assert kl == sorted(kl, key=lambda k: k["ts"]), "порядок не по возрастанию"


# ── Гейты боевой стратегии исполняются как есть ─────────────────────────────

async def test_listing_age_gate_runs_unmodified_against_simulated_time():
    """Возраст листинга боевая функция считает от РЕАЛЬНОГО «сейчас».
    Харнесс не подменяет сам гейт (он часть измеряемой стратегии), а сдвигает
    дату листинга. Проверяем, что гейт при этом реально работает."""
    fresh = make_hist(launch_offset=6)      # листинг за сутки до начала ряда
    now = fresh["k4"][30]["ts"] + _H4_MS
    scanner._LISTING_AGE_CACHE.clear()
    assert await scanner._is_listing_old_enough(
        ReplayClient(fresh, now), "TSTUSDT") is False, \
        f"свежий листинг прошёл гейт MIN_LISTING_AGE_DAYS={cfg.MIN_LISTING_AGE_DAYS}"

    old = make_hist(launch_offset=400)      # больше 60 дней
    scanner._LISTING_AGE_CACHE.clear()
    assert await scanner._is_listing_old_enough(
        ReplayClient(old, now), "TSTUSDT") is True


async def test_orderbook_and_tape_are_absent_not_faked():
    """Исторических стакана и ленты нет. Подсунуть сюда «сбалансированную
    книгу» значило бы измерять фактор, которого в данных не было."""
    hist = make_hist()
    c = ReplayClient(hist, _signal_moment(hist))
    ob = await c.get_orderbook("TSTUSDT", 20)
    assert scanner._ob_imbalance(ob) == (0.0, "NEUTRAL")
    assert await c.get_recent_trades("TSTUSDT", 500) == []
    flow = scanner._trade_flow(await c.get_recent_trades("TSTUSDT", 500))
    assert flow["delta"] is None, "отсутствие ленты выдано за нулевой поток"


# ── Судейство исходов ────────────────────────────────────────────────────────

async def test_verdict_is_not_invented_when_history_runs_out():
    """У сигнала в конце окна просто нет 48 часов будущего. Выдать ему
    EXPIRED значило бы добавить в выборку исход, которого не наблюдали."""
    hist = make_hist()
    now = _signal_moment(hist)
    scanner._LISTING_AGE_CACHE.clear()
    sig = await scanner._analyze_symbol(
        ReplayClient(hist, now), build_ticker(hist, now))
    assert sig is not None
    # обрезаем 15м-историю сразу после входа
    short = {**hist, "k15": [k for k in hist["k15"] if k["ts"] < now + 3600_000]}
    assert judge_signal(short, sig, now) is None
    # без единой свечи после входа — тоже None, а не выдуманный исход
    empty = {**hist, "k15": [k for k in hist["k15"] if k["ts"] < now]}
    assert judge_signal(empty, sig, now) is None


async def test_verdict_uses_only_candles_after_entry():
    """Судейство по свечам ДО входа — то же заглядывание, только наоборот:
    исход определялся бы движением, которого сделка не застала."""
    hist = make_hist()
    now = _signal_moment(hist)
    scanner._LISTING_AGE_CACHE.clear()
    sig = await scanner._analyze_symbol(
        ReplayClient(hist, now), build_ticker(hist, now))
    assert sig is not None
    # ломаем ПРОШЛОЕ до неузнаваемости — вердикт меняться не должен
    before = judge_signal(hist, sig, now)
    wrecked = {**hist, "k15": [
        k if k["ts"] >= now else {**k, "high": k["high"] * 50, "low": k["low"] / 50}
        for k in hist["k15"]]}
    assert judge_signal(wrecked, sig, now) == before


# ── Арифметика отчёта ────────────────────────────────────────────────────────

def test_report_expectancy_matches_the_dashboard_math():
    """Матожидание в отчёте обязано считаться ТЕМ ЖЕ кодом, что на дашборде,
    иначе прогон и боевая статистика окажутся несравнимы."""
    import core.db as db
    d = {}
    for outcome in ("WIN", "WIN", "LOSS", "LOSS", "LOSS", "EXPIRED"):
        _acc(d, "все", outcome, 2.0)
    _finish(d)
    slot = d["все"]
    expected = db._ev({"win": 2, "loss": 3, "be": 0},
                      fee_r=db.ROUND_TRIP_FEE_PCT / 2.0)
    assert slot["ev_r"] == expected["ev_r"]
    assert slot["ev_gross_r"] == expected["ev_gross_r"]
    assert slot["expired"] == 1, "просрочка обязана считаться отдельно"
    assert "_fee_sum" not in slot and "_fee_n" not in slot


def test_expired_outcomes_do_not_enter_the_fee_average():
    """Просрочки в знаменатель матожидания не входят, значит и комиссию по
    ним копить нельзя — иначе издержки смещаются (неравенство Йенсена)."""
    import core.db as db
    narrow = {}
    for _ in range(10):
        _acc(narrow, "к", "WIN", 0.7)
    for _ in range(90):
        _acc(narrow, "к", "EXPIRED", 5.0)
    _finish(narrow)
    assert narrow["к"]["fee_r"] == pytest.approx(db.ROUND_TRIP_FEE_PCT / 0.7, abs=1e-3)


@pytest.mark.parametrize("k,n,lo,hi", [(6, 16, 0.185, 0.614), (0, 0, 0.0, 1.0)])
def test_wilson_interval(k, n, lo, hi):
    got_lo, got_hi = wilson(k, n)
    assert got_lo == pytest.approx(lo, abs=0.01)
    assert got_hi == pytest.approx(hi, abs=0.01)


# ── Сквозной прогон ──────────────────────────────────────────────────────────

async def test_replay_end_to_end_produces_outcomes():
    """Сквозная проверка: харнесс доходит от ряда до строки с исходом."""
    hist = make_hist()
    scanner._LISTING_AGE_CACHE.clear()
    rows = await replay_symbol(hist)
    assert rows, "сквозной прогон не дал ни одного исхода"
    r = rows[0]
    assert r["outcome"] in ("WIN", "LOSS", "BE", "EXPIRED")
    assert r["symbol"] == "TSTUSDT"
    assert r["sl_pct"] > 0 and r["atr_pct"] > 0
    assert r["sl_atr"] == pytest.approx(r["sl_pct"] / r["atr_pct"])


def test_report_renders_without_crashing():
    """Отчёт — единственный выход всей работы. Падение в нём обесценивает
    прогон целиком, а поймать его без теста можно только после того, как
    данные скачаны и час потрачен."""
    from tools.replay import report
    rows = [
        {"symbol": "A", "ts": 1, "score": 61, "direction": "LONG",
         "type": "VSA_CLIMAX", "sl_pct": 2.0, "atr_pct": 2.5, "sl_atr": 0.8,
         "headroom": 2.5, "confidence": 1.0, "round_pos": 0.1,
         "outcome": "WIN", "mfe_r": 2.0},
        {"symbol": "B", "ts": 2, "score": 40, "direction": "SHORT",
         "type": "MOMENTUM", "sl_pct": 5.0, "atr_pct": 2.0, "sl_atr": 2.5,
         "headroom": 1.8, "confidence": 0.5, "round_pos": -0.9,
         "outcome": "LOSS", "mfe_r": 0.3},
        {"symbol": "C", "ts": 3, "score": 50, "direction": "LONG",
         "type": "SQUEEZE", "sl_pct": 3.0, "atr_pct": 3.0, "sl_atr": 1.0,
         "headroom": 4.0, "confidence": 0.7, "round_pos": None,
         "outcome": "EXPIRED", "mfe_r": 1.1},
    ]
    text = report(rows, {"symbols": ["A", "B", "C"], "days": 90})
    assert "ТОРГУЕМАЯ ПОПУЛЯЦИЯ" in text
    assert "CI95" in text, "интервал обязан быть рядом с процентом (LITERATURE §7)"
    # оговорки об ограничениях обязаны быть в отчёте, а не только в коде
    assert "стакан" in text and "ОДИН РАЗ" in text
    # пустой вход не роняет отчёт
    assert "Сигналов не найдено" in report([], {"symbols": [], "days": 1})


def test_variants_do_not_change_live_defaults():
    """Переключатели существуют ради ИЗМЕРЕНИЯ. Вариант baseline обязан
    оставлять боевое поведение нетронутым, иначе прогон меряет не то, что
    работает в бою."""
    from tools.replay import apply_variant, VARIANTS
    from core.config import cfg
    before = (cfg.FUNDING_VOTE, cfg.VSA_SPIKE_EXEMPT)
    assert before == (True, True), "боевые дефолты изменены"
    apply_variant("baseline")
    assert (cfg.FUNDING_VOTE, cfg.VSA_SPIKE_EXEMPT) == before
    apply_variant("both")
    assert (cfg.FUNDING_VOTE, cfg.VSA_SPIKE_EXEMPT) == (False, False)
    # возвращаем, чтобы не протекло в соседние тесты
    cfg.FUNDING_VOTE, cfg.VSA_SPIKE_EXEMPT = before
    assert set(VARIANTS) >= {"baseline", "nofunding", "nospike", "both"}


async def test_half_split_covers_the_window_without_overlap():
    """Закрытая половина обязана НЕ пересекаться с половиной для поиска:
    иначе гипотеза проверяется на тех же данных, где найдена."""
    hist = make_hist(n=120)
    a = hist["k4"][0]["ts"]
    b = hist["k4"][-1]["ts"] + _H4_MS
    cut = a + int((b - a) * 2 / 3)
    scanner._LISTING_AGE_CACHE.clear()
    explore = await replay_symbol(hist, lo_ms=a, hi_ms=cut)
    scanner._LISTING_AGE_CACHE.clear()
    holdout = await replay_symbol(hist, lo_ms=cut, hi_ms=b)
    ts_e = {r["ts"] for r in explore}
    ts_h = {r["ts"] for r in holdout}
    assert not (ts_e & ts_h), "половины пересекаются — проверка недействительна"
    assert all(t < cut for t in ts_e) and all(t >= cut for t in ts_h)


async def test_long_only_drops_shorts():
    from tools.replay import replay_symbol as rs
    hist = make_hist(n=120)
    scanner._LISTING_AGE_CACHE.clear()
    rows = await rs(hist, long_only=True)
    assert all(r["direction"] == "LONG" for r in rows)


def test_missing_funding_or_oi_skips_the_moment():
    """Пропуск данных НЕ должен подставляться нулём.

    Так и случилось на первом сборе: истории фандинга не хватало, каждый
    сигнал получал ровно 0.0000%, голос фандинга не срабатывал НИ РАЗУ, и
    прогон мерил обрубок стратегии. По отчёту это было не видно —
    единственным следом была confidence 0.4 у 100% сделок."""
    hist = make_hist()
    now = _signal_moment(hist)
    assert build_ticker(hist, now) is not None

    no_fund = {**hist, "funding": [f for f in hist["funding"] if f["ts"] >= now]}
    assert build_ticker(no_fund, now) is None, "фандинг подставлен нулём"

    no_oi = {**hist, "oi": [r for r in hist["oi"] if r["ts"] >= now]}
    assert build_ticker(no_oi, now) is None, "open interest подставлен нулём"
