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
    # OI травится СТРОГО ПОСЛЕ now_ms, а не с включением границы.
    # Ряд OI — это «последний замер не позже границы», и границы совпадают
    # с моментами анализа: запись со ts == now_ms содержит настоящее, а не
    # будущее. Прежнее `< now_ms` объявляло её будущим и тем самым
    # ЗАКРЕПЛЯЛО дефект — починка кода покраснела бы ложно.
    out["oi"] = [r if r["ts"] <= now_ms else {**r, "oi": r["oi"] * 1000}
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
    assert set(VARIANTS) >= {"baseline", "nofunding", "nospike", "both",
                            "strict", "strict_nf"}
    # baseline обязан оставлять ВСЕ три освобождения включёнными
    apply_variant("strict")
    assert (cfg.VSA_SPIKE_EXEMPT, cfg.VSA_MTF_EXEMPT,
            cfg.VSA_LEVEL_EXEMPT) == (False, False, False)
    cfg.VSA_SPIKE_EXEMPT = cfg.VSA_MTF_EXEMPT = cfg.VSA_LEVEL_EXEMPT = True


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

    no_oi = {**hist, "oi": [r for r in hist["oi"] if r["ts"] > now]}
    assert build_ticker(no_oi, now) is None, "open interest подставлен нулём"


async def test_round_entry_is_free_of_lookahead():
    """Новый вход обязан пройти тот же тест, что и боевой: отравленное
    будущее не должно менять сигнал. Отдельная стратегия — отдельная
    возможность заглянуть вперёд."""
    from tools.round_strategy import analyze_round
    hist = make_hist(n=200)
    scanner._LISTING_AGE_CACHE.clear()
    found = None
    for i in range(30, 199):
        now = hist["k4"][i]["ts"] + _H4_MS
        t = build_ticker(hist, now)
        if t is None:
            continue
        scanner._LISTING_AGE_CACHE.pop(hist["symbol"], None)
        for mode in ("fade", "break"):
            s = await analyze_round(ReplayClient(hist, now), t, mode)
            if s:
                found = (now, mode, s)
                break
        if found:
            break
    assert found, "на этом ряду вход по круглым числам обязан сработать"
    now, mode, clean = found
    ph = poison_future(hist, now)
    scanner._LISTING_AGE_CACHE.pop(hist["symbol"], None)
    poisoned = await analyze_round(ReplayClient(ph, now), build_ticker(ph, now), mode)
    assert poisoned is not None
    for f in ("direction", "entry", "sl", "tp2", "sl_pct", "atr_pct"):
        assert getattr(clean, f) == pytest.approx(getattr(poisoned, f)), \
            f"{f} изменилось от будущих данных — заглядывание вперёд"


def test_round_grid_is_scale_invariant():
    from tools.round_strategy import round_grid
    for price, want_step in ((111340.0, 10000.0), (2345.6, 100.0), (0.02345, 0.001)):
        step, nearest = round_grid(price)
        assert step == pytest.approx(want_step)
        assert abs(nearest - price) <= step / 2 + 1e-12
    assert round_grid(0.0) is None
    assert round_grid(-1.0) is None


async def test_round_stop_sits_beyond_the_round_number():
    """Стоп обязан стоять ЗА круглым числом, а не вплотную: вплотную — это
    зона чужих стоп-кластеров (LITERATURE §1), там нас и выбьет каскадом."""
    from tools.round_strategy import analyze_round, round_grid
    hist = make_hist(n=200)
    scanner._LISTING_AGE_CACHE.clear()
    checked = 0
    for i in range(30, 199):
        now = hist["k4"][i]["ts"] + _H4_MS
        t = build_ticker(hist, now)
        if t is None:
            continue
        scanner._LISTING_AGE_CACHE.pop(hist["symbol"], None)
        s = await analyze_round(ReplayClient(hist, now), t, "fade")
        if not s:
            continue
        step, nearest = round_grid(s.entry)
        if s.direction == "SHORT":
            assert s.sl > nearest, "стоп не за круглым числом"
        else:
            assert s.sl < nearest, "стоп не за круглым числом"
        checked += 1
        if checked >= 3:
            break
    assert checked, "не нашлось ни одного сигнала fade для проверки стопа"


async def test_open_interest_uses_the_value_available_at_the_moment():
    """Ряд OI строится как «последний замер НЕ ПОЗЖЕ границы», а границы
    совпадают с моментами анализа. Значит запись со ts == now_ms — это
    настоящее, и выбрасывать её нельзя: строгое '<' подставляло значение
    ПРЕДЫДУЩЕЙ свечи, а ΔOI даёт до 30 очков из ~64 и задаёт тип сигнала."""
    hist = make_hist()
    i = 60
    now = hist["k4"][i]["ts"] + _H4_MS
    # в синтетике сетка OI лежит на ts свечей; добавим замер ровно в now
    hist = {**hist, "oi": hist["oi"] + [{"ts": now, "oi": 123456.0}]}
    rows = await ReplayClient(hist, now).get_open_interest("TSTUSDT", "4h", 2)
    assert rows[-1]["ts"] == now, "текущее значение OI выброшено"
    assert rows[-1]["oi"] == pytest.approx(123456.0)
    # а вот замер ПОСЛЕ момента анализа обязан быть отброшен
    hist2 = {**hist, "oi": hist["oi"] + [{"ts": now + 1, "oi": 999999.0}]}
    rows2 = await ReplayClient(hist2, now).get_open_interest("TSTUSDT", "4h", 2)
    assert all(r["ts"] <= now for r in rows2), "взято значение из будущего"


def test_ticker_open_interest_uses_the_current_value_not_the_previous():
    """Второй канал того же дефекта: openInterestValue в тикере. Боевой бот
    читает АКТУАЛЬНОЕ значение, а строгое '<' подставляло замер предыдущей
    свечи — ΔOI даёт до 30 очков из ~64 и задаёт тип сигнала."""
    hist = make_hist()
    i = 60
    now = hist["k4"][i]["ts"] + _H4_MS
    price = [k for k in hist["k4"] if k["ts"] + _H4_MS <= now][-1]["close"]
    hist = {**hist, "oi": hist["oi"] + [{"ts": now, "oi": 777.0}]}
    t = build_ticker(hist, now)
    assert t is not None
    assert float(t["openInterestValue"]) == pytest.approx(777.0 * price), \
        "тикер взял OI предыдущей свечи вместо текущей"
    # замер ПОСЛЕ момента анализа по-прежнему не берётся
    hist2 = {**hist, "oi": hist["oi"] + [{"ts": now + 1, "oi": 111.0}]}
    t2 = build_ticker(hist2, now)
    assert t2 is not None
    assert float(t2["openInterestValue"]) == pytest.approx(777.0 * price), \
        "тикер заглянул в будущее"


def test_baseline_variant_restores_all_switches():
    """VARIANTS['baseline'] был пустым словарём и ничего не сбрасывал: в
    одном процессе nospike после nofunding давал фактически both, а
    повторный baseline отчитывался чужой конфигурацией."""
    from tools.replay import apply_variant, _SWITCHES
    apply_variant("strict_nf")
    assert not any(getattr(cfg, k) for k in _SWITCHES)
    apply_variant("baseline")
    assert all(getattr(cfg, k) for k in _SWITCHES), "baseline не восстановил флаги"
    apply_variant("nofunding")
    assert cfg.FUNDING_VOTE is False and cfg.VSA_SPIKE_EXEMPT is True
    apply_variant("nospike")
    assert cfg.FUNDING_VOTE is True, "вариант унаследовал флаг предыдущего"
    apply_variant("baseline")


def test_explore_half_is_embargoed_from_the_holdout():
    """Разрез половин идёт по моменту ВХОДА, а исход тянется 48ч вперёд:
    сигнал за час до границы судился бы свечами из закрытой трети. Проверочная
    треть обязана быть неприкосновенной, поэтому explore теряет последние 48ч."""
    from tools.replay import half_window
    from strategy.evaluator import _MAX_AGE_HOURS
    a, b = _T0, _T0 + 365 * 24 * 3600 * 1000
    e_lo, e_hi = half_window(a, b, "explore")
    h_lo, h_hi = half_window(a, b, "holdout")
    span = _MAX_AGE_HOURS * 3600 * 1000
    assert e_lo == a and h_hi == b, "окно выборки урезано не с той стороны"
    assert e_hi + span <= h_lo, (
        "исход последнего explore-сигнала дотягивается до закрытой трети")
    assert h_lo - e_hi >= span, "эмбарго короче окна исхода"
    # закрытая треть не должна ужиматься — она и так меньшая
    assert half_window(a, b, "") == (0, 0), "без --half окно обязано быть полным"


def test_overlapping_labels_widen_the_interval():
    """Уилсон считает наблюдения независимыми. Исход тянется 48ч, и сигналы
    по одному символу внутри окна судятся общими свечами — заявлять по ним
    точность как по независимым значит завышать её вдвое-втрое."""
    from tools.replay import avg_concurrency, wilson, wilson_overlap
    hour = 3600 * 1000
    # десять сигналов подряд с шагом 4ч по одному символу: окно 48ч,
    # значит каждый делит его почти со всеми остальными
    dense = [{"symbol": "A", "ts": _T0 + i * 4 * hour} for i in range(10)]
    assert avg_concurrency(dense) > 5, "плотное перекрытие не обнаружено"
    # те же десять, но раз в неделю — окна не пересекаются вовсе
    sparse = [{"symbol": "A", "ts": _T0 + i * 168 * hour} for i in range(10)]
    assert avg_concurrency(sparse) == pytest.approx(1.0)
    # и разные символы друг друга не занимают
    cross = [{"symbol": f"S{i}", "ts": _T0} for i in range(10)]
    assert avg_concurrency(cross) == pytest.approx(1.0)

    raw = wilson(300, 900)
    adj = wilson_overlap(300, 900, dense)
    assert (adj[1] - adj[0]) > (raw[1] - raw[0]) * 1.5, \
        "интервал не расширился при перекрытии меток"
