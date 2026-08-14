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
    почти всем символам: 2/3 сделок были шортами из-за константы."""
    long_side, _ = s._direction("SQUEEZE", 0.5, "NEUTRAL", -0.01)
    short_side, _ = s._direction("SQUEEZE", 0.5, "NEUTRAL", 0.01)
    assert long_side == short_side, "нейтральная ставка фандинга задаёт направление"


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


def test_flow_handles_empty_and_broken_input():
    assert s._trade_flow([])["delta"] == 0.0
    # нулевые объёмы не должны давать деления на ноль
    assert s._trade_flow([_tr(0, 100, 0, "Buy")])["delta"] == 0.0


def test_absorption_requires_effort_without_result():
    """Ядро VSA: сильный односторонний агрессор, а цена стоит."""
    flat = [_tr(i * 100, 100.0, 5, "Buy") for i in range(10)]
    assert s._trade_flow(flat)["absorb"] is True
    moving = [_tr(i * 100, 100.0 + i * 0.5, 5, "Buy") for i in range(10)]
    assert s._trade_flow(moving)["absorb"] is False
    balanced = [_tr(i * 100, 100.0, 5, "Buy" if i % 2 else "Sell") for i in range(10)]
    assert s._trade_flow(balanced)["absorb"] is False
