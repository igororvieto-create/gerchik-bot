"""База и конфиг: миграции на старой схеме, дедуп, разбор env, клампы.

Форвард-тест — единственное основание включать реальные деньги, поэтому
целостность этих данных проверяется отдельно от торговой логики.
"""
import importlib
import os
import sqlite3
import sys
from datetime import datetime

import pytest


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """База СТАРОЙ схемы с накопленными данными — как на боевом томе."""
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        signal_type TEXT NOT NULL, direction TEXT NOT NULL, score INTEGER NOT NULL,
        price REAL NOT NULL, oi_change REAL, vol_ratio REAL, funding REAL,
        ob_bias TEXT, atr_pct REAL, details TEXT, ts TEXT NOT NULL)""")
    c.execute("CREATE INDEX idx_signals_symbol ON signals(symbol)")
    c.execute("CREATE INDEX idx_signals_symbol_ts ON signals(symbol, ts)")
    c.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL,
        entry REAL, exit_price REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        qty REAL, pnl REAL, score INTEGER, signal_type TEXT, order_id TEXT,
        status TEXT DEFAULT 'open', opened_at TEXT NOT NULL, closed_at TEXT)""")
    now = datetime.utcnow().isoformat()
    c.executemany(
        "INSERT INTO trades(symbol,side,order_id,status,pnl,opened_at) VALUES(?,?,?,?,?,?)",
        [("ETHUSDT", "Buy", "ord1", "open", None, now),      # дубль: open + closed
         ("ETHUSDT", "Buy", "ord1", "closed", -12.5, now),
         ("BTCUSDT", "Buy", "ord2", "closed", 7.0, now),     # обратный порядок id
         ("BTCUSDT", "Buy", "ord2", "open", None, now),
         ("SOLUSDT", "Buy", "", "open", None, now)])         # пустой order_id
    c.execute("INSERT INTO signals(symbol,signal_type,direction,score,price,ts) "
              "VALUES('X','MOMENTUM','LONG',50,1.0,?)", (now,))
    c.commit()
    c.close()
    monkeypatch.setenv("DB_PATH", str(path))
    for mod in [m for m in sys.modules if m.startswith("core.db")]:
        del sys.modules[mod]
    import core.db as db
    importlib.reload(db)
    return db, path


async def test_migration_on_legacy_schema_adds_all_columns(legacy_db):
    db, path = legacy_db
    await db.init_db()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(signals)")]
    for required in ("entry", "sl", "tp2", "outcome", "headroom", "mfe_r"):
        assert required in cols, f"миграция не добавила колонку {required}"


async def test_dedup_keeps_closed_row_with_pnl(legacy_db):
    """MIN(id) выбрасывал закрытую строку с реализованным убытком:
    он исчезал из дневного предохранителя, а вечная open-строка
    заставляла бота считать чужую позицию своей."""
    db, path = legacy_db
    await db.init_db()
    c = sqlite3.connect(path)
    rows = c.execute("SELECT order_id, status, pnl FROM trades "
                     "WHERE order_id != '' ORDER BY order_id").fetchall()
    assert rows == [("ord1", "closed", -12.5), ("ord2", "closed", 7.0)]
    total = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades "
                      "WHERE status='closed'").fetchone()[0]
    assert total == pytest.approx(-5.5), "PnL потерян при дедупе"


async def test_unique_index_created_after_dedup(legacy_db):
    db, path = legacy_db
    await db.init_db()
    idx = [r[0] for r in sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]
    assert "idx_trades_order" in idx
    assert "idx_signals_symbol" not in idx, "мёртвый индекс не удалён"


async def test_open_trades_raises_instead_of_returning_empty(legacy_db, monkeypatch):
    """[] читается как «своих сделок нет» → позиции бота получают ярлык
    MANUAL и теряют защиту стопа. Ошибка БД обязана быть исключением."""
    db, _ = legacy_db
    monkeypatch.setattr(db, "DB_PATH", "/nonexistent/dir/x.db")
    with pytest.raises(Exception):
        await db.get_open_trades()


async def test_realized_pnl_raises_instead_of_zero(legacy_db, monkeypatch):
    """0.0 означало «сегодня потерь нет» и тихо обнуляло предохранитель."""
    db, _ = legacy_db
    monkeypatch.setattr(db, "DB_PATH", "/nonexistent/dir/x.db")
    with pytest.raises(Exception):
        await db.get_realized_pnl_since("2026-01-01T00:00:00")


async def test_expectancy_counts_breakeven_outcomes(legacy_db):
    db, path = legacy_db
    await db.init_db()
    ev = db._ev({"win": 2, "loss": 2, "be": 1})
    assert ev["winrate"] == pytest.approx(50.0)
    # (2*2 + 1*0 - 2*1) / 5
    assert ev["ev_r"] == pytest.approx(0.4)


async def test_expired_excluded_from_winrate(legacy_db):
    db, _ = legacy_db
    ev = db._ev({"win": 1, "loss": 1, "be": 0, "expired": 98})
    assert ev["winrate"] == pytest.approx(50.0), "EXPIRED попал в знаменатель винрейта"


# ── Конфиг ───────────────────────────────────────────────────────────────────
#
# Проверяется в ОТДЕЛЬНОМ ПРОЦЕССЕ, а не через importlib.reload.
# reload создаёт новый объект cfg, но модули, уже сделавшие
# `from core.config import cfg`, продолжают держать старый — а те, что
# импортируются ПОСЛЕ, получают новый, собранный с подставленными env.
# Из-за этого набор падал плавающе, в зависимости от порядка тестов.
# Подпроцесс исключает протечку полностью.

def _cfg_value(env: dict, expr: str) -> str:
    import subprocess
    import os as _os
    code = (
        "import json\n"
        "from core.config import cfg\n"
        f"print(json.dumps({expr}))\n"
    )
    envs = dict(_os.environ)
    envs.update({k: str(v) for k, v in env.items()})
    envs["PYTHONPATH"] = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=envs, timeout=60)
    assert r.returncode == 0, f"импорт конфига упал: {r.stderr[-500:]}"
    import json as _json
    return _json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("env,expr,expected", [
    ({"LEVERAGE": "inf"}, "cfg.LEVERAGE", 5),              # OverflowError ронял импорт
    ({"RISK_PER_TRADE": "nan"}, "cfg.RISK_PER_TRADE", 1.0),  # nan проходил мимо клампов
    ({"LEVERAGE": "99"}, "cfg.LEVERAGE", 5),               # инвариант: плечо <= 5
    ({"RISK_PER_TRADE": "10"}, "cfg.RISK_PER_TRADE", 3.0),   # инвариант: риск <= 3%
    ({"SCAN_INTERVAL_MIN": "0.5"}, "cfg.SCAN_INTERVAL_MIN", 1),  # 0 -> интервал 1 секунда
    ({"ABORT_ON_LEVERAGE_FAIL": "enabled"}, "cfg.ABORT_ON_LEVERAGE_FAIL", True),  # мусор -> дефолт
    ({"REQUIRE_MTF_ALIGN": "off"}, "cfg.REQUIRE_MTF_ALIGN", False),
    ({"TRADE_FLOW_LIMIT": "99999"}, "cfg.TRADE_FLOW_LIMIT", 1000),
])
def test_env_parsing_is_hardened(env, expr, expected):
    assert _cfg_value(env, expr) == expected


def test_risk_times_positions_cannot_exceed_daily_limit():
    """Полный набор позиций не должен пробивать дневной лимит разом."""
    worst, limit = _cfg_value(
        {"RISK_PER_TRADE": "3.0", "MAX_POSITIONS": "20"},
        "[cfg.RISK_PER_TRADE * cfg.MAX_POSITIONS, cfg.DAILY_LOSS_LIMIT_PCT]")
    assert worst <= limit


def test_display_threshold_cannot_exceed_trade_threshold():
    """MIN_SCORE выше TRADE_MIN_SCORE делал торговый порог фиктивным."""
    mn, trade = _cfg_value({"MIN_SCORE": "60", "TRADE_MIN_SCORE": "45"},
                           "[cfg.MIN_SCORE, cfg.TRADE_MIN_SCORE]")
    assert trade >= mn


# ── Сериализация цен для биржи ───────────────────────────────────────────────

@pytest.mark.parametrize("value", [0.00001234, 0.000098, 0.0000073, 7.0565e-05])
def test_prices_never_use_scientific_notation(value):
    """str(round(x,8)) давал '7.3e-06' — биржа такую строку не принимает,
    то есть ордер уходил бы без стопа."""
    from exchange.bybit import _fmt_price
    assert "e" not in _fmt_price(value).lower()
    assert float(_fmt_price(value)) == pytest.approx(value, rel=1e-6)


@pytest.mark.parametrize("tick,value,mode", [
    (0.00001, 0.136654321, "down"), (0.00001, 0.136654321, "up"),
    (0.01, 100.837, "down"), (1e-8, 7.0565e-05, "up"),
])
def test_quantized_prices_land_on_exchange_grid(tick, value, mode):
    from strategy.trader import _quantize
    q = _quantize(value, tick, mode)
    n = round(q / tick)
    assert abs(n * tick - q) < tick * 1e-6, f"{q} не кратно шагу {tick}"
    if mode == "down":
        assert q <= value + tick * 1e-9
    else:
        assert q >= value - tick * 1e-9


async def test_flow_buckets_separate_missing_short_and_real(legacy_db):
    """Срез по ленте не должен смешивать три разные вещи: «данных нет»,
    «лента короче минуты» и содержательный перекос агрессора. Иначе он
    меряет долю сбоев запроса, а не предсказательную силу потока."""
    db, path = legacy_db
    await db.init_db()
    now = datetime.utcnow().isoformat()
    rows = [
        # (flow_delta, flow_span_min, flow_absorb, outcome)
        (None,  0.0,  0, "WIN"),    # ленты не было
        (None,  0.0,  0, "LOSS"),
        (0.5,   0.2,  0, "WIN"),    # лента слишком короткая
        (-0.5,  0.3,  0, "LOSS"),
        (0.45,  30.0, 0, "WIN"),    # содержательный перекос покупателей
        (-0.45, 30.0, 0, "LOSS"),   # содержательный перекос продавцов
        (0.02,  30.0, 0, "WIN"),    # реально сбалансированная лента
        (0.60,  30.0, 1, "LOSS"),   # поглощение
    ]
    c = sqlite3.connect(path)
    for delta, span, absorb, outcome in rows:
        c.execute(
            "INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
            "outcome,flow_delta,flow_span_min,flow_absorb) "
            "VALUES('S','VSA_CLIMAX','LONG',60,1.0,?,?,?,?,?)",
            (now, outcome, delta, span, absorb))
    c.commit()
    c.close()

    b = await db.get_outcome_breakdown(days=7)
    flow = b["by_flow"]
    # строки без ленты в срез вообще не попадают
    assert sum(v["win"] + v["loss"] for v in flow.values()) == 6, \
        "сигналы без ленты не должны попадать в срез по потоку"
    assert flow["лента <1 мин"]["win"] == 1 and flow["лента <1 мин"]["loss"] == 1
    assert flow["покупатели >+0.2"]["win"] == 1
    assert flow["продавцы <-0.2"]["loss"] == 1
    assert flow["нейтрально"]["win"] == 1
    assert flow["поглощение"]["loss"] == 1
    # порядок корзин задаётся бэкендом и монотонен
    assert b["_order"]["by_flow"][0] == "продавцы <-0.2"


def test_expectancy_subtracts_round_trip_fees():
    """В единицах R издержки тем тяжелее, чем уже стоп. Без поправки срез
    by_sl_atr смещён в пользу узких стопов больше, чем любой реальный
    эффект, и сравнивать корзины нельзя."""
    import core.db as db
    slot = {"win": 33, "loss": 67, "be": 0}
    narrow = db._ev(dict(slot), sl_pct=0.7)
    wide = db._ev(dict(slot), sl_pct=7.0)
    assert narrow["ev_gross_r"] == wide["ev_gross_r"], "брутто обязано совпадать"
    assert narrow["ev_r"] < wide["ev_r"], "издержки не учтены по ширине стопа"
    assert narrow["fee_r"] == pytest.approx(0.186, abs=0.005)
    assert wide["fee_r"] == pytest.approx(0.019, abs=0.005)
    # без sl_pct поправку применить не к чему — брутто и нетто совпадают
    unknown = db._ev(dict(slot))
    assert unknown["ev_r"] == unknown["ev_gross_r"]


# ── Замеры из docs/LITERATURE.md (пишутся, на решения не влияют) ─────────────

@pytest.mark.parametrize("price,expected_step", [
    (111340.0, 10000.0),   # BTC: круглые — 110000, 120000
    (2345.6,   100.0),     # ETH
    (0.02345,  0.001),     # альт
    (1.0,      0.1),       # ровно степень десятки
])
def test_round_number_grid_is_scale_invariant(price, expected_step):
    """Osler (2003) про «00-уровни»: сетка обязана масштабироваться вместе с
    ценой. Фиксированный шаг сделал бы метрику для BTC и для альта разными
    величинами под одним именем — срез мерял бы цену монеты, а не близость
    к круглому числу."""
    from strategy.scanner import _round_number_dist_atr
    # ATR = шаг сетки → расстояние в ATR обязано лежать в [0, 0.5]
    d = _round_number_dist_atr(price, expected_step)
    assert d is not None and 0.0 <= d <= 0.5, \
        f"{price}: сетка не совпала с ожидаемым шагом {expected_step}"
    # цена ровно НА круглом числе даёт ноль
    on_round = round(price / expected_step) * expected_step
    assert _round_number_dist_atr(on_round, expected_step) == pytest.approx(0.0, abs=1e-9)
    # ровно посередине между круглыми — максимум 0.5
    mid = on_round + expected_step / 2
    assert _round_number_dist_atr(mid, expected_step) == pytest.approx(0.5, abs=1e-6)


@pytest.mark.parametrize("price,atr", [(0.0, 1.0), (-5.0, 1.0), (100.0, 0.0), (100.0, -1.0)])
def test_round_number_returns_none_on_degenerate_input(price, atr):
    """None означает «не измерено» и в срез не попадает. Ноль на его месте
    означал бы «вплотную к круглому числу» — то есть сбой замера
    подмешивался бы в самую интересную корзину."""
    from strategy.scanner import _round_number_dist_atr
    assert _round_number_dist_atr(price, atr) is None


async def test_measurement_columns_are_written_in_the_right_order(legacy_db):
    """INSERT перечисляет 26 колонок и 26 значений. Перепутанный ПОРЯДОК не
    падает — он молча пишет confidence в ob_ratio, и срез потом меряет не то,
    что называется. Поэтому проверяются именно значения, а не факт записи."""
    db, path = legacy_db
    await db.init_db()
    from core.state import Signal
    sig = Signal(
        symbol="ZZZUSDT", signal_type="VSA_CLIMAX", direction="LONG", score=61,
        price=2.5, oi_change=3.3, vol_ratio=2.2, funding=-0.044, ob_bias="BUY",
        atr_pct=1.75, details="d", entry=2.51, sl=2.4, tp1=2.6, tp2=2.73,
        tp3=2.9, rr=2.1, headroom=3.4, sl_pct=4.4,
        # три величины намеренно РАЗНЫЕ и не равны соседям по INSERT,
        # иначе перестановка осталась бы незамеченной
        ob_ratio=0.37, confidence=0.66, round_dist_atr=0.19,
    )
    await db.save_signal(sig)
    row = sqlite3.connect(path).execute(
        "SELECT ob_ratio, confidence, round_dist_atr, headroom, sl_pct, funding "
        "FROM signals WHERE symbol='ZZZUSDT'").fetchone()
    assert row == pytest.approx((0.37, 0.66, 0.19, 3.4, 4.4, -0.044), abs=1e-9)


async def test_orderbook_slice_splits_agreement_and_survives_legacy_rows(legacy_db):
    """Голос стакана задаёт направление при равенстве голосов, а литературной
    опоры под ним нет (docs/LITERATURE.md §3). Срез обязан отделять «стакан
    за» от «стакан против» — иначе проверить голос нечем.

    Отдельно: старые строки (round_dist_atr = NULL) обязаны считаться в
    by_ob и НЕ считаться в by_round. Иначе срез по круглым числам молча
    получит корзину из строк, где замера не было."""
    db, path = legacy_db
    await db.init_db()
    now = datetime.utcnow().isoformat()
    rows = [
        # (direction, ob_bias, round_dist_atr, outcome)
        ("LONG",  "BUY",     0.10, "WIN"),    # стакан за
        ("SHORT", "SELL",    None, "WIN"),    # стакан за, замера круглых нет
        ("LONG",  "SELL",    0.50, "LOSS"),   # стакан против
        ("SHORT", "BUY",     None, "LOSS"),   # стакан против, замера нет
        ("LONG",  "NEUTRAL", 0.90, "LOSS"),   # стакан молчит
        ("LONG",  None,      None, "WIN"),    # совсем старая строка
    ]
    c = sqlite3.connect(path)
    for direction, ob, rd, outcome in rows:
        c.execute(
            "INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
            "outcome,ob_bias,round_dist_atr) VALUES('S','MOMENTUM',?,60,1.0,?,?,?,?)",
            (direction, now, outcome, ob, rd))
    c.commit()
    c.close()

    b = await db.get_outcome_breakdown(days=7)
    ob_s, rnd = b["by_ob"], b["by_round"]
    assert ob_s["стакан за"]["win"] == 2 and ob_s["стакан за"]["loss"] == 0
    assert ob_s["стакан против"]["loss"] == 2 and ob_s["стакан против"]["win"] == 0
    # и NEUTRAL, и отсутствующий ob_bias — это «стакан молчит», не «против»
    assert ob_s["стакан нейтр."]["loss"] == 1 and ob_s["стакан нейтр."]["win"] == 1
    # все шесть строк учтены срезом по стакану
    assert sum(v["win"] + v["loss"] for v in ob_s.values()) == 6
    # а срезом по круглым числам — только три, где замер есть
    assert sum(v["win"] + v["loss"] for v in rnd.values()) == 3
    assert rnd["<0.25 ATR"]["win"] == 1
    assert rnd["0.25-0.75"]["loss"] == 1
    assert rnd[">0.75 ATR"]["loss"] == 1
    assert b["_order"]["by_ob"][0] == "стакан за"


async def test_new_slices_report_expectancy_not_just_counts(legacy_db):
    """Решение принимается по ev_r (§0-А п.7). Новые срезы обязаны считать
    его так же, как старые, — с построчной комиссией."""
    db, path = legacy_db
    await db.init_db()
    now = datetime.utcnow().isoformat()
    c = sqlite3.connect(path)
    for outcome in ("WIN", "LOSS", "LOSS"):
        c.execute(
            "INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
            "outcome,ob_bias,sl_pct) VALUES('S','MOMENTUM','LONG',60,1.0,?,?,'BUY',2.0)",
            (now, outcome))
    c.commit()
    c.close()
    slot = (await db.get_outcome_breakdown(days=7))["by_ob"]["стакан за"]
    assert slot["ev_gross_r"] == pytest.approx((2.0 - 1.0 - 1.0) / 3, abs=1e-3)
    assert slot["ev_r"] < slot["ev_gross_r"], "комиссия не вычтена"
    assert "_fee_sum" not in slot and "_fee_n" not in slot, "служебные поля утекли в API"
