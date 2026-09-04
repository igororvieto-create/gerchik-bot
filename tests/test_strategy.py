"""Скоринг, направление и судейство форвард-теста.

Рецидивирующий баг №2 (CLAUDE.md): score и direction считались независимо.
Ловился трижды — VSA, уровень, затем стакан+фандинг через abs(). Тесты ниже
фиксируют правило: фактор, противоречащий сделке, очков НЕ даёт.
"""
import pytest

import strategy.scanner as s
from strategy.evaluator import _judge
from core.config import cfg

FLIP = {"LONG": "SHORT", "SHORT": "LONG", "NEUTRAL": "NEUTRAL"}


def k(high, low):
    return {"high": high, "low": low, "close": (high + low) / 2, "ts": 0}


# ── Рецидивирующий баг №2 ────────────────────────────────────────────────────

def test_contradicting_orderbook_gives_no_points():
    base = dict(oi_change=5.0, vol_ratio=2.2, funding=-0.02, price_change=2.0,
                vsa_type="NEUTRAL", vsa_bias="NEUTRAL", level_dist_atr=0.9)
    supports, _ = s._score_signal(ob_ratio=0.32, direction="LONG", **base)
    against, _ = s._score_signal(ob_ratio=-0.32, direction="LONG", **base)
    assert against < supports, "стакан ПРОТИВ сделки начислил те же очки"


def test_contradicting_funding_gives_no_points():
    base = dict(oi_change=5.0, vol_ratio=2.2, ob_ratio=0.0, price_change=2.0,
                vsa_type="NEUTRAL", vsa_bias="NEUTRAL", level_dist_atr=0.9)
    supports, _ = s._score_signal(funding=-0.08, direction="LONG", **base)
    against, _ = s._score_signal(funding=0.08, direction="LONG", **base)
    assert against < supports, "фандинг ПРОТИВ сделки начислил те же очки"


def test_neutral_bybit_funding_casts_no_vote():
    """0.01% — базовая ставка Bybit. Порог на ней задавал направление
    почти всем символам: 2/3 сделок были шортами из-за константы.

    price_change=0 обязателен: при ненулевом движении голос цены
    перевешивает и маскирует голос фандинга — тест проходил бы при ЛЮБОМ
    пороге (мутационная проверка это и вскрыла)."""
    # нейтральная ставка не должна давать голоса вовсе -> направления нет
    assert s._direction("SQUEEZE", 0.0, "NEUTRAL", -0.01)[0] == "NEUTRAL"
    assert s._direction("SQUEEZE", 0.0, "NEUTRAL", 0.01)[0] == "NEUTRAL"
    # а действительно экстремальный фандинг голос подаёт, и он контрарен
    assert s._direction("SQUEEZE", 0.0, "NEUTRAL", -0.05)[0] == "LONG"
    assert s._direction("SQUEEZE", 0.0, "NEUTRAL", 0.05)[0] == "SHORT"


def test_funding_points_start_at_extreme_threshold():
    """Очки за фандинг тоже обязаны начинаться с FUNDING_EXTREME, иначе
    базовая ставка биржи подкармливает скор почти каждому символу."""
    base = dict(oi_change=5.0, vol_ratio=2.2, ob_ratio=0.0, price_change=2.0,
                vsa_type="NEUTRAL", vsa_bias="NEUTRAL", level_dist_atr=0.9,
                direction="SHORT")
    neutral_rate, _ = s._score_signal(funding=0.01, **base)
    no_funding, _ = s._score_signal(funding=0.0, **base)
    assert neutral_rate == no_funding, "нейтральная ставка Bybit начислила очки"
    extreme, _ = s._score_signal(funding=0.05, **base)
    assert extreme > no_funding, "экстремальный фандинг очков не дал"


@pytest.mark.parametrize("sig_type,pchg,fund", [
    ("ACCUMULATION", 2.0, 0.0),
    ("DISTRIBUTION", -2.0, 0.0),
    ("FUNDING_EXTREME", 0.0, -0.12),
])
def test_tautological_vote_does_not_confirm_itself(sig_type, pchg, fund):
    """Голос, которым выбрано направление, не может его же подтверждать."""
    _, confidence = s._direction(sig_type, pchg, "NEUTRAL", fund)
    assert confidence < 1.0, f"{sig_type}: одинокий тавтологичный голос дал полную уверенность"


# ── Симметрия движка ─────────────────────────────────────────────────────────

def test_engine_is_mirror_symmetric():
    """Зеркальный рынок обязан давать зеркальную сделку с тем же скором."""
    bad = []
    for oi in (-6, -3, 0, 3, 6):
        for vol in (1.3, 2.0, 3.0):
            for pchg in (-3, -1, 0, 1, 3):
                for ob in (-0.3, 0, 0.3):
                    for f in (-0.05, 0, 0.05):
                        for vsa, bias in (("NEUTRAL", "NEUTRAL"), ("CLIMAX", "LONG"),
                                          ("ABSORPTION", "SHORT")):
                            mb = FLIP[bias]
                            t1 = s._classify_type(oi, vol, f, pchg, vsa, bias)
                            t2 = s._classify_type(oi, vol, -f, -pchg, vsa, mb)
                            ob1 = "BUY" if ob > 0.1 else "SELL" if ob < -0.1 else "NEUTRAL"
                            ob2 = "SELL" if ob > 0.1 else "BUY" if ob < -0.1 else "NEUTRAL"
                            d1, c1 = s._direction(t1, pchg, ob1, f, vsa_bias=bias)
                            d2, c2 = s._direction(t2, -pchg, ob2, -f, vsa_bias=mb)
                            s1, _ = s._score_signal(oi, vol, f, ob, pchg, vsa_type=vsa,
                                                    level_dist_atr=0.6, vsa_bias=bias,
                                                    direction=d1)
                            s2, _ = s._score_signal(oi, vol, -f, -ob, -pchg, vsa_type=vsa,
                                                    level_dist_atr=0.6, vsa_bias=mb,
                                                    direction=d2)
                            if d1 != FLIP[d2] or s1 != s2 or abs(c1 - c2) > 1e-9:
                                bad.append((oi, vol, pchg, ob, f, vsa, bias, d1, d2, s1, s2))
    assert not bad, f"несимметричных точек: {len(bad)}, первая: {bad[0]}"


def test_zero_price_change_does_not_pick_a_side():
    """`LONG if pc > 0 else SHORT` отдавал SHORT и при ровно нуле:
    направление задавала форма записи условия, а не рынок."""
    assert s._tie_break(0.0) == "NEUTRAL"
    assert s._tie_break(0.5) == "LONG"
    assert s._tie_break(-0.5) == "SHORT"


def test_classification_is_symmetric_around_dead_zone():
    up = s._classify_type(3.0, 1.2, 0.0, 0.5, "NEUTRAL", "NEUTRAL")
    down = s._classify_type(3.0, 1.2, 0.0, -0.5, "NEUTRAL", "NEUTRAL")
    assert (up, down) == ("ACCUMULATION", "DISTRIBUTION")
    # мёртвая зона не должна форсировать LONG
    flat_up = s._classify_type(3.0, 1.2, 0.0, 0.2, "NEUTRAL", "NEUTRAL")
    flat_dn = s._classify_type(3.0, 1.2, 0.0, -0.2, "NEUTRAL", "NEUTRAL")
    assert flat_up == flat_dn


def test_score_scale_is_not_degenerate():
    """Шкала обязана различать сетапы, а не схлопываться в пару значений."""
    seen = set()
    for oi in (-8, -4, 0, 4, 8):
        for vol in (1.2, 2.0, 3.5):
            for ld in (0.3, 0.8, 1.5):
                for vsa, bias in (("NEUTRAL", "NEUTRAL"), ("CLIMAX", "LONG")):
                    t = s._classify_type(oi, vol, 0.0, 2.0, vsa, bias)
                    d, c = s._direction(t, 2.0, "BUY", 0.0, vsa_bias=bias)
                    if d == "NEUTRAL":
                        continue
                    v, _ = s._score_signal(oi, vol, 0.0, 0.2, 2.0, vsa_type=vsa,
                                           level_dist_atr=ld, vsa_bias=bias, direction=d)
                    seen.add(s._apply_confluence_cap(v, c))
    assert len(seen) >= 10, f"шкала выродилась: всего {len(seen)} значений"


# ── Геометрия уровней ────────────────────────────────────────────────────────

def test_levels_grid_is_exactly_1r_2r_3r():
    lv = s._calc_levels(100.0, 2.0, "LONG", support=97.5, resistance=112.0)
    assert lv is not None
    risk = 100.0 - lv["sl"]
    assert abs(lv["tp1"] - (100.0 + risk)) < 1e-9
    assert abs(lv["tp2"] - (100.0 + 2 * risk)) < 1e-9
    assert abs(lv["tp3"] - (100.0 + 3 * risk)) < 1e-9


def test_open_sky_is_refused():
    """Нет встречного уровня = покупка хая. Раньше гейт MIN_RR тут пропускался."""
    assert s._calc_levels(100.0, 2.0, "LONG", support=97.5, resistance=None) is None


def test_absurdly_wide_stop_is_refused():
    assert s._calc_levels(100.0, 1.0, "LONG", support=90.0, resistance=200.0) is None


# ── Судейство форвард-теста ──────────────────────────────────────────────────

def test_stop_checked_before_target_in_same_candle():
    """Обе цели в одной свече — засчитываем худшее: порядок внутри неизвестен."""
    verdict = _judge("LONG", 98.0, 104.0, [k(105.0, 97.0)], entry=100.0)
    assert verdict[0] == "LOSS"


def test_mfe_is_recorded_regardless_of_outcome():
    _, _, mfe = _judge("LONG", 98.0, 104.0, [k(103.0, 99.0), k(100.0, 97.5)], entry=100.0)
    assert mfe == pytest.approx(1.5, abs=0.01)


def test_breakeven_disabled_by_default_behaves_as_before():
    """По умолчанию механизм выключен — исходы должны быть как до его появления."""
    assert cfg.BREAKEVEN_AT_R == 0.0
    verdict = _judge("LONG", 98.0, 104.0, [k(102.1, 101.0), k(101.0, 97.5)], entry=100.0)
    assert verdict[0] == "LOSS"


def test_breakeven_converts_loss_when_enabled():
    saved = cfg.BREAKEVEN_AT_R
    cfg.BREAKEVEN_AT_R = 1.0
    try:
        verdict = _judge("LONG", 98.0, 104.0, [k(102.1, 101.0), k(101.0, 97.5)], entry=100.0)
        assert verdict[0] == "BE"
    finally:
        cfg.BREAKEVEN_AT_R = saved


def test_undecided_returns_none():
    assert _judge("LONG", 98.0, 104.0, [k(101.0, 99.0)], entry=100.0) is None


# ── Лента исполненных сделок ─────────────────────────────────────────────────

def _tr(ts, price, qty, side):
    return {"ts": ts, "price": price, "qty": qty, "side": side}


def test_flow_delta_sign_matches_aggressor():
    buys = [_tr(0, 100, 1, "Buy"), _tr(1000, 100, 3, "Buy"), _tr(2000, 100, 1, "Sell")]
    f = s._trade_flow(buys)
    assert f["delta"] > 0, "перевес покупателей должен давать положительную дельту"
    sells = [_tr(0, 100, 1, "Sell"), _tr(1000, 100, 3, "Sell"), _tr(2000, 100, 1, "Buy")]
    assert s._trade_flow(sells)["delta"] < 0


def test_flow_is_symmetric():
    buys = [_tr(0, 100, 2, "Buy"), _tr(1000, 100, 1, "Sell")]
    sells = [_tr(0, 100, 2, "Sell"), _tr(1000, 100, 1, "Buy")]
    assert s._trade_flow(buys)["delta"] == pytest.approx(-s._trade_flow(sells)["delta"])


def test_flow_reports_time_span():
    """Без охвата по времени перекос несопоставим между символами:
    500 сделок у BTC — это секунды, у неликвида — часы."""
    f = s._trade_flow([_tr(0, 100, 1, "Buy"), _tr(600_000, 100, 1, "Sell")])
    assert f["span_min"] == pytest.approx(10.0)


def test_missing_flow_is_none_not_zero():
    """delta=None означает «ленты не было» и ОБЯЗАНА отличаться от 0.0
    («поток сбалансирован»). Иначе символы с недоступной лентой попадают
    в корзину «нейтрально», и срез меряет не поток, а долю сбоев."""
    assert s._trade_flow([])["delta"] is None
    # нулевые объёмы — тоже отсутствие данных, а не баланс
    assert s._trade_flow([_tr(0, 100, 0, "Buy")])["delta"] is None
    # а вот реально сбалансированная лента даёт именно 0.0
    balanced = s._trade_flow([_tr(0, 100, 1, "Buy"), _tr(1000, 100, 1, "Sell")])
    assert balanced["delta"] == pytest.approx(0.0)





def test_absorption_requires_effort_without_result():
    """Ядро VSA: сильный односторонний агрессор, а цена стоит."""
    flat = [_tr(i * 100, 100.0, 5, "Buy") for i in range(10)]
    assert s._trade_flow(flat)["absorb"] is True
    moving = [_tr(i * 100, 100.0 + i * 0.5, 5, "Buy") for i in range(10)]
    assert s._trade_flow(moving)["absorb"] is False
    balanced = [_tr(i * 100, 100.0, 5, "Buy" if i % 2 else "Sell") for i in range(10)]
    assert s._trade_flow(balanced)["absorb"] is False


def test_mfe_recorded_for_undecided_signals():
    """У просроченного сигнала вердикта нет, но MFE обязан считаться:
    `_judge(...) or (None,None,0.0)` подставлял ноль КАЖДОМУ EXPIRED, а
    доля EXPIRED доходит до 90% при широких стопах."""
    from strategy.evaluator import _mfe
    ks = [k(101.0, 99.5), k(103.4, 100.0), k(101.0, 99.8)]
    assert _judge("LONG", 98.0, 110.0, ks, entry=100.0) is None
    assert _mfe("LONG", 100.0, 98.0, ks) == pytest.approx(1.7, abs=0.01)


def test_breakeven_arms_on_close_not_wick():
    """Свеча, дотянувшаяся до порога ХВОСТОМ и закрывшаяся ниже безубытка,
    ставила стоп ВЫШЕ рынка — исход BE по цене, которой не было."""
    saved = cfg.BREAKEVEN_AT_R
    cfg.BREAKEVEN_AT_R = 1.0
    try:
        wick = [k(101.0, 99.5), k(99.9, 99.5)]
        for c in wick:
            c["close"] = 99.6
        assert _judge("LONG", 99.0, 104.0, wick, entry=100.0) is None
    finally:
        cfg.BREAKEVEN_AT_R = saved


def test_unknown_direction_is_refused_not_treated_as_short():
    assert _judge("", 98.0, 104.0, [k(105.0, 97.0)], entry=100.0) is None
    assert _judge("NEUTRAL", 98.0, 104.0, [k(105.0, 97.0)], entry=100.0) is None


def test_vsa_contribution_cannot_contradict_direction():
    """Инвариант вместо мёртвого кода.

    В _analyze_symbol стоял блок «снять вклад VSA, если он противоречит
    direction». Перебор всех достижимых состояний даёт НОЛЬ срабатываний:
    vsa_bias != NEUTRAL влечёт sig_type='VSA_*', а тогда _direction ставит
    primary = vsa_bias. Блок был украшением и дублировал константы 20/15.

    Тест охраняет сам инвариант: если цепочка когда-нибудь разойдётся,
    он покраснеет — в отличие от удалённого блока, который молчал."""
    import strategy.scanner as s
    contradictions = []
    for vt, vb in [("CLIMAX", "LONG"), ("CLIMAX", "SHORT"),
                   ("ABSORPTION", "LONG"), ("ABSORPTION", "SHORT"),
                   ("NO_DEMAND_SUPPLY", "NEUTRAL"), ("NEUTRAL", "NEUTRAL"),
                   ("CLIMAX", "NEUTRAL")]:
        for oi in (-10, -3, 0, 3, 10):
            for pc in (-2, -0.05, 0, 0.05, 2):
                for f in (-0.2, 0, 0.2):
                    for ob in ("BUY", "SELL", "NEUTRAL"):
                        st = s._classify_type(oi, 3.0, f, pc, vt, vb)
                        d, _ = s._direction(st, pc, ob, f, vsa_bias=vb)
                        if vb != "NEUTRAL" and vb != d:
                            contradictions.append((vt, vb, st, d, oi, pc, f, ob))
    assert not contradictions, (
        f"VSA даёт очки против направления в {len(contradictions)} состояниях, "
        f"первое: {contradictions[0]}")


# Здесь стоял test_confidence_ignores_the_gap_between_vote_and_tie_break —
# он закреплял НЕВЕРНЫЙ инвариант «в зазоре confidence обязана совпадать с
# confidence при полноценном голосе цены». Тест охранял неполную правку: он
# был написан на примере, где стакан и фандинг ПРОТИВОРЕЧАТ друг другу, то
# есть на ничьей, и требовал для неё той же уверенности, что для честного
# большинства. Настоящий инвариант — ниже, в двух тестах: согласие голосов
# наказывать нельзя, а ничью нельзя выдавать за согласие.
# Урок в docs/REVIEW.md §0-Б: тест, написанный вместе с правкой, наследует
# её ошибку, если пример подобран под правку, а не под инвариант.


def test_tie_of_contradicting_votes_keeps_the_confluence_cap():
    """Ничья голосов в SQUEEZE достижима ТОЛЬКО когда стакан и фандинг
    смотрят в разные стороны — то есть это максимально противоречивый сетап,
    ровно тот, ради которого написан _apply_confluence_cap.

    Правка зазора |pc| <= 0.1 закрыла реальную ошибку (вычёркивался не тот
    голос), но подняла кап с 35 до 75 у такой популяции и сделала её
    торгуемой: сторону реальной сделки со score 70 выбирало суточное
    движение в 0.0001%. Кап снимать нельзя (CLAUDE.md, LITERATURE §4)."""
    import strategy.scanner as s
    # стакан SELL против фандинга LONG, цена в зазоре ниже порога голоса
    for pc in (-0.0001, 0.0001, 0.05, -0.09):
        direction, conf = s._direction("SQUEEZE", pc, "SELL", -0.08)
        assert direction == "NEUTRAL", (
            f"pc={pc}: сторону решает движение ниже порога голоса ({direction})")
        assert s._apply_confluence_cap(70, conf) <= 35, (
            f"pc={pc}: кап снят с противоречивого сетапа")
    # честное движение цены тай-брейк по-прежнему решает
    direction, conf = s._direction("SQUEEZE", -0.2, "SELL", -0.08)
    assert direction == "SHORT" and conf == pytest.approx(0.5)


def test_agreeing_votes_are_not_punished_by_the_tie_rule():
    """Обратная сторона: когда стакан и фандинг СОГЛАСНЫ, ничьей нет, и
    правило не должно ничего отбрасывать или занижать."""
    import strategy.scanner as s
    for pc in (0.05, 0.2):
        direction, conf = s._direction("SQUEEZE", pc, "BUY", -0.08)
        assert direction == "LONG"
        assert conf == pytest.approx(1.0), f"pc={pc}: согласие наказано ({conf})"
        assert s._apply_confluence_cap(62, conf) == 62


def test_weak_price_move_never_decides_direction_anywhere():
    """Принцип применён в ОБОИХ тай-брейках, а не в одном: движение слабее
    порога голоса (|pc| <= 0.1) сторону не выбирает нигде. Половинчатое
    правило вернулось бы находкой следующего ревью, а в фолбэке одинокий
    голос фандинга — фактор без литературной опоры — единолично снимал кап."""
    import strategy.scanner as s
    # фолбэк: стакан нейтрален, голосует только фандинг
    for sig_type in ("VOLUME_SPIKE", "MOMENTUM"):
        weak, _ = s._direction(sig_type, 0.05, "NEUTRAL", -0.08)
        assert weak == "NEUTRAL", f"{sig_type}: сторону решило движение 0.05%"
        strong, _ = s._direction(sig_type, 0.5, "NEUTRAL", -0.08)
        assert strong == "LONG", f"{sig_type}: честное движение перестало решать"


# ── Лента сделок запрашивается только под состоявшийся сигнал ──────────────

async def test_tape_is_not_fetched_for_symbols_without_a_signal():
    """Трафик, а не изящество. Ответ recent-trade весит ~141 байт на
    сделку; запрос на каждый из 100 символов каждые 4 минуты при глубине
    1000 давал бы ~5 ГБ в сутки. Прокси Webshare тарифицируются по
    трафику, и перерасход заканчивается рецидивирующим багом №1: 402 ->
    перебор -> прямое соединение -> гео-блок -> get_positions() = None ->
    проверка наличия стопа прекращается.

    Сигналов около 50 в сутки, поэтому лента под них стоит ~7 МБ."""
    import strategy.scanner as scanner
    calls = []

    class C:
        async def get_klines(self, symbol, interval="240", limit=25):
            return []          # пустые свечи -> анализ выйдет рано
        async def get_open_interest(self, symbol, interval="4h", limit=12):
            return []
        async def get_orderbook(self, symbol, limit=20):
            return {"bids": [], "asks": []}
        async def get_recent_trades(self, symbol, limit=500):
            calls.append(symbol)
            return []
        async def get_instrument_info(self, symbol):
            return {"launchTime": 0}

    scanner._LISTING_AGE_CACHE.clear()
    res = await scanner._analyze_symbol(C(), {
        "symbol": "NOSIGUSDT", "lastPrice": "1.0", "price24hPcnt": "0.0",
        "fundingRate": "0.0", "volume24h": "1000", "openInterestValue": "1000"})
    assert res is None, "заготовка обязана НЕ давать сигнала"
    assert calls == [], (
        f"лента запрошена для символа без сигнала ({calls}) — "
        f"это возвращает расход трафика на все 100 символов каждый скан")


def test_tape_depth_is_justified_by_the_deferred_fetch():
    """Глубина 1000 допустима ТОЛЬКО потому, что лента берётся под сигнал.
    Если запрос вернут в общий сбор, глубину придётся вернуть вместе с ним:
    иначе трафик вырастет в восемь раз и упрётся в квоту прокси."""
    src = open("strategy/scanner.py", encoding="utf-8").read()
    head = src[:src.index("sig = Signal(")]
    bulk = head[head.index("await asyncio.gather("):]
    bulk = bulk[:bulk.index(")\n")]
    assert "get_recent_trades" not in bulk, (
        "лента вернулась в общий сбор данных по каждому символу — "
        "при глубине 1000 это ~5 ГБ трафика в сутки")


def test_missing_tape_is_not_reported_as_balanced_flow():
    """Отсутствие ленты — это None, а НЕ 0.0. Ноль означал бы «поток
    сбалансирован», и срез по потоку мерил бы долю символов с недоступной
    лентой вместо самого потока."""
    from strategy.scanner import _EMPTY_FLOW, _trade_flow
    assert _EMPTY_FLOW["delta"] is None
    assert _trade_flow([])["delta"] is None


# ── Отсутствие данных не имеет права давать больше уверенности ─────────────

async def test_orderbook_failure_skips_the_symbol():
    """Отказ эндпоинта стакана и реально пустая книга раньше были
    неразличимы. Последствие серьёзнее потери фактора: без голоса стакана
    _direction уходит в запасную ветку, сторону задаёт знак изменения цены,
    а список независимых голосов пустеет — confidence РАСТЁТ с 0.0 до 0.4 и
    confluence-кап поднимается с 35 до 55. То есть отказ переворачивал
    сторону сделки И ослаблял защиту против рецидива №2."""
    import strategy.scanner as scanner
    calls = {"ob": 0}

    class C:
        async def get_klines(self, symbol, interval="240", limit=25):
            base = 100.0
            return [{"ts": i * 14400000, "open": base, "high": base * 1.01,
                     "low": base * 0.99, "close": base, "volume": 1000.0}
                    for i in range(30)]

        async def get_open_interest(self, symbol, interval="4h", limit=12):
            return [{"ts": 0, "oi": 100.0}, {"ts": 1, "oi": 110.0}]

        async def get_orderbook(self, symbol, limit=20):
            calls["ob"] += 1
            return None                     # эндпоинт НЕ ОТВЕТИЛ

        async def get_recent_trades(self, symbol, limit=500):
            return []

        async def get_instrument_info(self, symbol):
            return {"launchTime": 0}

    scanner._LISTING_AGE_CACHE.clear()
    scanner._SCAN_HEALTH["data_fail"] = 0
    res = await scanner._analyze_symbol(C(), {
        # изменение цены выше PRICE_CHANGE_MIN, иначе символ отсекается
        # ранним гейтом и до запроса стакана дело не доходит
        "symbol": "OBFAILUSDT", "lastPrice": "100", "price24hPcnt": "0.05",
        # объём выше MIN_VOL_24H (2 млн), иначе символ отсекается раньше
        "fundingRate": "0.0", "volume24h": "50000000",
        "openInterestValue": "11000"})
    assert calls["ob"] == 1, "заготовка не дошла до запроса стакана"
    assert res is None, (
        "символ с недоступным стаканом дал сигнал — сторона выбрана без "
        "данных, а confluence-кап при этом ослаблен")
    # None само по себе ничего не доказывает: без охраны анализ падает
    # дальше на AttributeError, общий except его глотает и тоже возвращает
    # None. Отличаем штатный выход от пойманного исключения по счётчику —
    # его увеличивает только охрана (REVIEW §0-Б п.2).
    assert scanner._SCAN_HEALTH["data_fail"] == 1, (
        "символ отсеян не охраной, а проглоченным исключением — "
        "и в счётчик здоровья скана это не попало")


async def test_empty_orderbook_is_still_allowed():
    """Обратная сторона: ЯВНО пустая книга — это не отказ. Прогон по
    истории отдаёт именно её (снапшотов Bybit не даёт), и запрет уронил бы
    весь замер."""
    from strategy.scanner import _ob_imbalance
    ratio, bias = _ob_imbalance({"bids": [], "asks": []})
    assert ratio == 0.0 and bias == "NEUTRAL"


async def test_scan_reports_data_failure_instead_of_looking_healthy():
    """Отказ ПОСИМВОЛЬНЫХ эндпоинтов (типичный rate-limit) не ловил никто:
    last_scan_error оставался пустым, и «скан прошёл, находок нет» было
    неотличимо от «данные не пришли ни по одному символу». Бот мог быть
    мёртв сутками при зелёном дашборде."""
    import strategy.scanner as scanner
    from core.state import state

    class Dead:
        async def get_tickers(self, symbol=None):
            return [{"symbol": f"S{i}USDT", "lastPrice": "1",
                     "price24hPcnt": "0.01", "fundingRate": "0",
                     "volume24h": "10000000", "openInterestValue": "100000",
                     "turnover24h": "10000000"} for i in range(20)]

        async def get_klines(self, symbol, interval="240", limit=25):
            return []                      # rate-limit: пусто, без исключения

        async def get_open_interest(self, symbol, interval="4h", limit=12):
            return []

        async def get_orderbook(self, symbol, limit=20):
            return {"bids": [], "asks": []}

        async def get_recent_trades(self, symbol, limit=500):
            return []

        async def get_instrument_info(self, symbol):
            return {"launchTime": 0}

    state.last_scan_error = ""
    scanner._LISTING_AGE_CACHE.clear()
    await scanner.scan_all(Dead())
    assert state.last_scan_error, (
        "скан не получил данных ни по одному символу, но отчитался как "
        "здоровый — на дашборде это зелёный пульс при мёртвом боте")
    assert "данные не получены" in state.last_scan_error


async def test_client_marks_a_failed_orderbook_request_as_no_data():
    """Сам клиент обязан различать отказ и пустую книгу: при отказе _get
    отдаёт {} или тело с retCode != 0, и оба раньше превращались в
    {"bids": [], "asks": []} — неотличимо от честно пустого стакана."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        async def dead(path, params=None, auth=False):
            return {}                       # три попытки исчерпаны
        c._get = dead
        assert await c.get_orderbook("XUSDT") is None, \
            "отказ эндпоинта выдан за пустую книгу"

        async def rate_limited(path, params=None, auth=False):
            return {"retCode": 10006, "retMsg": "too many visits"}
        c._get = rate_limited
        assert await c.get_orderbook("XUSDT") is None, \
            "rate-limit выдан за пустую книгу"

        async def alive(path, params=None, auth=False):
            return {"retCode": 0, "result": {"b": [["1", "2"]], "a": [["3", "4"]]}}
        c._get = alive
        ob = await c.get_orderbook("XUSDT")
        assert ob == {"bids": [[1.0, 2.0]], "asks": [[3.0, 4.0]]}
    finally:
        await c.close()
