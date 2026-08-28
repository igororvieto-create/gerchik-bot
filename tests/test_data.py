"""База и конфиг: миграции на старой схеме, дедуп, разбор env, клампы.

Форвард-тест — единственное основание включать реальные деньги, поэтому
целостность этих данных проверяется отдельно от торговой логики.
"""
import importlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta

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

@pytest.mark.parametrize("price", [111340.0, 2345.6, 0.02345, 9.87, 1.02])
def test_round_metric_is_independent_of_leading_digit(price):
    """Первая версия делила расстояние на ATR, и метрика мерила НЕ близость к
    круглому числу, а ВЕДУЩУЮ ЦИФРУ цены: максимум равен 5/(d*atr_pct).
    Монета за 9.x не могла попасть в дальнюю корзину физически, монета за 1.x
    попадала туда в 55% случаев — срез сравнивал бы монеты по первой цифре.

    Инвариант: диапазон метрики одинаков при любой цене."""
    import math
    from strategy.scanner import _round_number_pos
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    on_round = round(price / step) * step
    assert _round_number_pos(on_round) == pytest.approx(0.0, abs=1e-9)
    # Точка ровно посередине между круглыми берётся ВВЕРХ: шаг сетки
    # считается от самой цены, и вниз через границу декады он меняется
    # в 10 раз (см. отдельный тест ниже).
    assert abs(_round_number_pos(on_round + step / 2)) == pytest.approx(1.0, abs=1e-6)


def test_round_metric_grid_shrinks_below_a_power_of_ten():
    """Явная фиксация свойства, а не умолчание о нём: сетка всегда даёт два
    значащих разряда, поэтому под степенью десятки шаг в 10 раз мельче.
    Для 1.05 шаг 0.1, для 0.95 — 0.01. Тест стоит здесь, чтобы смена
    поведения была видна как падение, а не как молчаливый сдвиг среза."""
    import math
    from strategy.scanner import _round_number_pos
    assert math.isclose(10.0 ** (math.floor(math.log10(1.05)) - 1), 0.1)
    assert math.isclose(10.0 ** (math.floor(math.log10(0.95)) - 1), 0.01)
    assert _round_number_pos(0.95) == pytest.approx(0.0, abs=1e-9)   # ровно на сетке
    assert abs(_round_number_pos(1.05)) == pytest.approx(1.0, abs=1e-6)


def test_round_metric_distribution_does_not_depend_on_price_scale():
    """Прямая проверка того, что было сломано: доля попаданий в корзину
    «на круглом» обязана совпадать для монет с разной ведущей цифрой."""
    import random
    from strategy.scanner import _round_number_pos
    random.seed(11)
    shares = []
    for lead in (1, 2, 5, 9):
        near = 0
        for _ in range(4000):
            price = (lead + random.random()) * 1000.0
            if abs(_round_number_pos(price)) < 0.2:
                near += 1
        shares.append(near / 4000)
    assert max(shares) - min(shares) < 0.05, (
        f"доля «на круглом» зависит от ведущей цифры: {shares}")


def test_round_metric_keeps_the_sign_osler_needs():
    """У Osler (2003) круглое ВЫШЕ цены — чужие тейки (тормоз), НИЖЕ — чужие
    стопы (ускорение). Беззнаковая метрика складывала два противоположных
    эффекта в одну корзину, где они гасили друг друга."""
    from strategy.scanner import _round_number_pos
    # шаг сетки при цене ~1000 равен 100, круглые: 1000, 1100
    assert _round_number_pos(1005.0) < 0, "ближайшее круглое 1000 — НИЖЕ цены"
    assert _round_number_pos(1090.0) > 0, "ближайшее круглое 1100 — ВЫШЕ цены"
    assert abs(_round_number_pos(1050.0)) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("price", [0.0, -5.0])
def test_round_metric_returns_none_on_degenerate_input(price):
    """None означает «не измерено» и в срез не попадает. Ноль на его месте
    означал бы «точно на круглом числе» — сбой замера подмешивался бы в
    самую интересную корзину."""
    from strategy.scanner import _round_number_pos
    assert _round_number_pos(price) is None


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
        ob_ratio=0.37, confidence=0.66, round_pos=-0.19,
    )
    sig.candle_ts = 1755600000000
    await db.save_signal(sig)
    row = sqlite3.connect(path).execute(
        "SELECT ob_ratio, confidence, round_pos, candle_ts, headroom, sl_pct, funding "
        "FROM signals WHERE symbol='ZZZUSDT'").fetchone()
    assert row == pytest.approx(
        (0.37, 0.66, -0.19, 1755600000000, 3.4, 4.4, -0.044), abs=1e-9)


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
        # (direction, ob_bias, round_pos, outcome)
        ("LONG",  "BUY",     0.05, "WIN"),    # стакан за, вход на круглом
        ("SHORT", "SELL",    None, "WIN"),    # стакан за, замера круглых нет
        ("LONG",  "SELL",    0.80, "LOSS"),   # стакан против, круглое ВЫШЕ
        ("SHORT", "BUY",     None, "LOSS"),   # стакан против, замера нет
        ("LONG",  "NEUTRAL", -0.80, "LOSS"),  # стакан молчит, круглое НИЖЕ
        ("LONG",  None,      None, "WIN"),    # совсем старая строка
    ]
    c = sqlite3.connect(path)
    for direction, ob, rd, outcome in rows:
        c.execute(
            "INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
            "outcome,ob_bias,round_pos) VALUES('S','MOMENTUM',?,60,1.0,?,?,?,?)",
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
    assert rnd["на круглом"]["win"] == 1
    assert rnd["круглое выше"]["loss"] == 1
    assert rnd["круглое ниже"]["loss"] == 1
    assert b["_order"]["by_ob"][0] == "стакан за"
    # знак обязан разделять корзины: обе проигравшие строки лежат в РАЗНЫХ
    # корзинах, хотя по модулю расстояние у них одинаковое
    assert rnd["круглое выше"] is not rnd["круглое ниже"]


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


async def test_save_trade_open_reports_failure(legacy_db):
    """Строка в trades — ЕДИНСТВЕННЫЙ признак «своя позиция» после рестарта.
    Пока провал записи был не виден вызывающему, живая позиция бота после
    ближайшего деплоя усыновлялась как MANUAL, а ручным позициям монитор
    принципиально не досылает стоп — рецидив бага №1 через слой данных."""
    db, path = legacy_db
    await db.init_db()
    from core.state import Position
    pos = Position(symbol="AAAUSDT", side="Buy", entry=1.0, sl=0.9, tp1=0, tp2=1.2,
                   tp3=0, qty=1.0, score=60, signal_type="VSA_CLIMAX", order_id="o1")
    assert await db.save_trade_open(pos) is True
    # повторный вызов идемпотентен и тоже успех
    assert await db.save_trade_open(pos) is True
    # недоступная база обязана вернуть False, а не None
    db.DB_PATH = "/nonexistent-dir/x.db"
    assert await db.save_trade_open(pos) is False


async def test_close_distinguishes_write_failure_from_nothing_to_close(legacy_db):
    """Булева мало: «не удалось записать» требует ПОВТОРА, «переводить
    нечего» — ровно наоборот. Раньше оба случая давали False, а докстрока
    обещала проверку rowcount, которой не было: True возвращался при любом
    успешном commit, включая UPDATE на 0 строк."""
    db, path = legacy_db
    await db.init_db()
    from core.state import Position
    pos = Position(symbol="BBBUSDT", side="Buy", entry=1.0, sl=0.9, tp1=0, tp2=1.2,
                   tp3=0, qty=1.0, score=60, signal_type="VSA_CLIMAX", order_id="o2")
    await db.save_trade_open(pos)
    assert await db.save_trade_close(pos, exit_price=1.1, pnl=5.0) == db.CLOSE_OK
    # второй раз переводить уже нечего — и это НЕ провал записи
    assert await db.save_trade_close(pos, exit_price=1.1, pnl=5.0) == db.CLOSE_ABSENT
    db.DB_PATH = "/nonexistent-dir/x.db"
    assert await db.save_trade_close(pos, exit_price=1.1, pnl=5.0) == db.CLOSE_FAILED


async def test_candle_dedup_survives_a_restart(legacy_db):
    """Дедуп «один сетап = один сигнал» жил только в памяти сканера, и каждый
    деплой Railway его обнулял: та же закрытая 4h-свеча сигналила заново.
    Три деплоя внутри одной свечи — четыре строки на ОДИН сетап, и все
    четыре считались независимыми исходами (LITERATURE §5)."""
    db, path = legacy_db
    await db.init_db()
    now = datetime.utcnow().isoformat()
    c = sqlite3.connect(path)
    for sym, cts in [("AAAUSDT", 1755600000000), ("AAAUSDT", 1755614400000),
                     ("BBBUSDT", 1755600000000), ("CCCUSDT", None)]:
        c.execute("INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
                  "candle_ts) VALUES(?,'MOMENTUM','LONG',60,1.0,?,?)", (sym, now, cts))
    c.commit(); c.close()

    marks = await db.get_recent_candle_marks(hours=8)
    assert marks["AAAUSDT"] == 1755614400000, "нужна САМАЯ СВЕЖАЯ свеча символа"
    assert marks["BBBUSDT"] == 1755600000000
    assert "CCCUSDT" not in marks, "строки без метки не создают ложный дедуп"

    # провал чтения обязан БРОСАТЬ: пустой словарь означал бы «дедупа нет»,
    # то есть тихое возвращение дублей
    db.DB_PATH = "/nonexistent-dir/x.db"
    with pytest.raises(Exception):
        await db.get_recent_candle_marks(hours=8)


@pytest.mark.parametrize("env,expr,floor", [
    # Инвариант «не торговать листинги/новостные спайки» держится ровно на
    # этих параметрах, а клампов у них не было: одна env-переменная молча
    # отключала его целиком. MIN_LISTING_AGE_DAYS=0 делает age_days >= 0
    # истиной всегда; MAX_LAST_CANDLE_ATR=1e9 отключает анти-спайк;
    # MIN_RR=0 отключает гейт R:R и через связь обнуляет торговый порог запаса.
    ({"MIN_LISTING_AGE_DAYS": "0"}, "cfg.MIN_LISTING_AGE_DAYS", 1),
    ({"MAX_LAST_CANDLE_ATR": "1e9"}, "cfg.MAX_LAST_CANDLE_ATR", None),
    ({"MIN_RR": "0"}, "cfg.MIN_RR", 1.0),
    ({"MAX_SL_ATR": "999"}, "cfg.MAX_SL_ATR", None),
])
def test_invariant_guards_cannot_be_switched_off_by_env(env, expr, floor):
    got = _cfg_value(env, expr)
    if floor is not None:
        assert got >= floor, f"{expr} = {got}: инвариант отключается через env"
    else:
        assert got <= 10.0, f"{expr} = {got}: гейт фактически отключён"


def test_zero_min_rr_no_longer_drags_trade_headroom_to_zero():
    """MIN_TRADE_HEADROOM_R клампится СНИЗУ по MIN_RR, поэтому обнулённый
    MIN_RR обнулял и торговый порог запаса — два инварианта падали от одной
    переменной."""
    hr = _cfg_value({"MIN_RR": "0", "MIN_TRADE_HEADROOM_R": "0"},
                    "cfg.MIN_TRADE_HEADROOM_R")
    assert hr >= 1.0, f"торговый порог запаса обнулён через MIN_RR: {hr}"


# ── Класс «молчаливый отказ»: функция глотает провал, система врёт ───────────

async def test_pending_signals_raises_instead_of_reporting_nothing_to_do(legacy_db):
    """[] читается вызывающим как «оценивать нечего», и форвард-тест —
    единственное основание включать реальные деньги — вставал бы полностью и
    БЕСШУМНО, пока дашборд показывает старую статистику за 7 дней.
    get_open_trades и get_realized_pnl_since этот урок уже усвоили."""
    db, path = legacy_db
    await db.init_db()
    db.DB_PATH = "/nonexistent-dir/x.db"
    with pytest.raises(Exception):
        await db.get_pending_signals()


async def test_outcome_write_reports_failure(legacy_db):
    """Оценщик считал decided += 1 безусловно: при полном диске в лог уходило
    «5 outcome(s) recorded» при нуле строк в базе, и решение о реальных
    деньгах принималось по этой цифре."""
    db, path = legacy_db
    await db.init_db()
    now = datetime.utcnow().isoformat()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO signals(symbol,signal_type,direction,score,price,ts,"
              "entry,sl,tp2) VALUES('S','MOMENTUM','LONG',60,1.0,?,1.0,0.9,1.2)", (now,))
    c.commit()
    sid = c.execute("SELECT id FROM signals WHERE symbol='S'").fetchone()[0]
    c.close()
    assert await db.set_signal_outcome(sid, "WIN", 1.2, mfe_r=2.0) is True
    db.DB_PATH = "/nonexistent-dir/x.db"
    assert await db.set_signal_outcome(sid, "WIN", 1.2, mfe_r=2.0) is False


async def test_close_writes_a_row_when_none_exists(legacy_db):
    """Самый дорогой из класса. Если строки в trades нет (запись при входе
    провалилась) или её запечатал сторож зависших, save_trade_close раньше
    возвращала CLOSE_ABSENT — а он означает «PnL учёл кто-то другой».
    Не учёл НИКТО: убыток исчезал из дневного предохранителя навсегда."""
    db, path = legacy_db
    await db.init_db()
    from core.state import Position
    pos = Position(symbol="NOROWUSDT", side="Buy", entry=100.0, sl=98.0, tp1=0.0,
                   tp2=104.0, tp3=0.0, qty=1.0, score=60,
                   signal_type="VSA_CLIMAX", order_id="orphan-1")
    # строки нет вовсе -> функция обязана дописать её и вернуть CLOSE_OK
    assert await db.save_trade_close(pos, exit_price=98.0, pnl=-9.0) == db.CLOSE_OK
    row = sqlite3.connect(path).execute(
        "SELECT status, pnl FROM trades WHERE symbol='NOROWUSDT'").fetchone()
    assert row == ("closed", -9.0), "терминальная строка не дописана"
    # повторный вызов уже НЕ учитывается: PnL зафиксирован
    assert await db.save_trade_close(pos, exit_price=98.0, pnl=-9.0) == db.CLOSE_ABSENT


async def test_stale_sealed_row_still_gets_its_pnl(legacy_db):
    """Учёт научился откладываться на следующий тик, а сторож зависших
    строк — нет. Строка успевала стать stale между попытками, и следующий
    save_trade_close её не находил. Порядок «stale ПОСЛЕ закрытий» в
    мониторе этот случай больше не покрывает."""
    db, path = legacy_db
    await db.init_db()
    from core.state import Position
    pos = Position(symbol="STALEUSDT", side="Buy", entry=100.0, sl=98.0, tp1=0.0,
                   tp2=104.0, tp3=0.0, qty=1.0, score=60,
                   signal_type="VSA_CLIMAX", order_id="st-1")
    await db.save_trade_open(pos)
    c = sqlite3.connect(path)
    c.execute("UPDATE trades SET status='stale' WHERE order_id='st-1'")
    c.commit(); c.close()
    assert await db.save_trade_close(pos, exit_price=98.0, pnl=-9.0) == db.CLOSE_OK
    row = sqlite3.connect(path).execute(
        "SELECT status, pnl FROM trades WHERE order_id='st-1'").fetchone()
    assert row == ("closed", -9.0), "stale-строка осталась без PnL"


async def test_history_span_reports_facts_not_guesses(legacy_db):
    """Признак is_ephemeral смотрит на ПУТЬ и может ошибаться: том бывает
    смонтирован не в /data. Факт — сколько истории реально в базе.

    Нужно потому, что потеря базы выглядит на дашборде как «стратегия
    испортилась»: статистика за ночь превратилась из 0W/13L в 1W/3L, и
    понять по экрану, что это стёртые данные, было нельзя."""
    db, path = legacy_db
    await db.init_db()
    span = await db.history_span()
    assert span["rows"] >= 1, "фикстура содержит сигнал — он обязан считаться"
    assert span["age_hours"] is not None and span["age_hours"] >= 0

    now = datetime.utcnow().isoformat()
    old = (datetime.utcnow() - timedelta(hours=50)).isoformat()
    c = sqlite3.connect(path)
    for ts in (now, old):
        c.execute("INSERT INTO signals(symbol,signal_type,direction,score,price,ts)"
                  " VALUES('S','MOMENTUM','LONG',60,1.0,?)", (ts,))
    c.commit(); c.close()
    span = await db.history_span()
    assert span["age_hours"] >= 49, "возраст берётся у САМОГО СТАРОГО сигнала"

    # недоступная база не роняет дашборд, но и не притворяется пустой
    db.DB_PATH = "/nonexistent-dir/x.db"
    bad = await db.history_span()
    assert bad["rows"] == -1, "сбой чтения выдан за пустую базу"


@pytest.mark.parametrize("value", ["0", "3", "10"])
def test_signal_limit_cannot_be_set_low_enough_to_destroy_history(value):
    """MAX_SIGNALS_DB — единственный параметр, который НАПРЯМУЮ УДАЛЯЕТ
    данные: cleanup_old_signals режет решённые сигналы до этого числа, не
    глядя на возраст, и ходит по крону каждые 6 часов.

    Клампа у него не было. Опечатка MAX_SIGNALS_DB=3 уничтожает всю
    статистику форвард-теста за один прогон, оставляя в логе только
    «Cleanup: removed N old signals» — и это неотличимо от стёртой базы."""
    got = _cfg_value({"MAX_SIGNALS_DB": value}, "cfg.MAX_SIGNALS_DB")
    assert got >= 1000, f"MAX_SIGNALS_DB={value} прошёл без кламп — история под угрозой"


def test_signal_limit_keeps_a_sane_value_untouched():
    """Кламп не должен ломать рабочую настройку."""
    assert _cfg_value({"MAX_SIGNALS_DB": "5000"}, "cfg.MAX_SIGNALS_DB") == 5000


# ── Фандинг в расчёте ожидания ──────────────────────────────────────────────

def test_funding_is_income_for_the_receiving_side():
    """Ставка положительна — лонги платят шортам. Знак обязан следовать за
    направлением: иначе учёт превратит доход в издержку и наоборот."""
    from core.db import funding_r, FUNDING_SETTLEMENTS
    # +0.05% при стопе 5%: лонг платит, шорт получает
    assert funding_r(0.05, "LONG", 5.0) < 0, "лонг при плюсовой ставке платит"
    assert funding_r(0.05, "SHORT", 5.0) > 0, "шорт при плюсовой ставке получает"
    # и наоборот при отрицательной ставке
    assert funding_r(-0.05, "LONG", 5.0) > 0
    assert funding_r(-0.05, "SHORT", 5.0) < 0
    # величина: ставка × выплаты / ширина стопа
    assert funding_r(0.05, "SHORT", 5.0) == pytest.approx(
        0.05 * FUNDING_SETTLEMENTS / 5.0)


def test_funding_in_r_scales_with_stop_width():
    """Как и комиссия, в R фандинг зависит от ширины стопа: при узком стопе
    та же ставка весит В РАЗЫ больше. Плоская константа сместила бы срез
    по ширине стопа."""
    from core.db import funding_r
    narrow = funding_r(0.05, "LONG", 0.7)
    wide = funding_r(0.05, "LONG", 7.0)
    assert abs(narrow) > abs(wide) * 9, "фандинг не масштабируется стопом"


def test_missing_funding_data_is_not_treated_as_income():
    from core.db import funding_r
    assert funding_r(None, "LONG", 5.0) is None
    assert funding_r(0.05, None, 5.0) is None
    assert funding_r(0.05, "LONG", 0.0) is None
    assert funding_r(0.05, "LONG", None) is None


def test_expectancy_subtracts_funding_cost_and_adds_funding_income():
    """Главное: ev_r обязан двигаться от фандинга. Раньше он вычитал только
    комиссии, и карточка на дашборде была оптимистичнее реальности."""
    from core.db import _ev
    slot = {"win": 10, "loss": 20, "be": 0}
    base = _ev(dict(slot), fee_r=0.04)["ev_r"]
    cost = _ev(dict(slot), fee_r=0.04, fund_r=-0.02)["ev_r"]
    income = _ev(dict(slot), fee_r=0.04, fund_r=+0.02)["ev_r"]
    assert cost == pytest.approx(base - 0.02), "издержка фандинга не вычтена"
    assert income == pytest.approx(base + 0.02), "доход от фандинга не учтён"
    assert _ev(dict(slot), fee_r=0.04, fund_r=None)["ev_r"] == pytest.approx(base)
    # брутто фандинг НЕ трогает: это издержка/доход, а не исход
    assert _ev(dict(slot), fee_r=0.04, fund_r=-0.02)["ev_gross_r"] == \
        _ev(dict(slot), fee_r=0.04)["ev_gross_r"]


# ── Где живёт база: том Railway ищется по факту, а не по виду пути ──────────
#
# Каждая проверка — отдельный процесс: DB_PATH вычисляется на импорте
# модуля, и подмена os.environ внутри теста мерила бы не то, что делает
# боевой старт (docs/REVIEW.md §0-Б п.7).

def _db_probe(env: dict, tmp_path) -> dict:
    import json as _json
    import os as _os
    import subprocess
    import sys as _sys
    code = ("import json, core.db as d; "
            "print(json.dumps({'path': d.DB_PATH, 'eph': d.is_ephemeral()}))")
    e = {**_os.environ, **env}
    for k, v in list(e.items()):
        if v is None:
            e.pop(k, None)
    out = subprocess.run([_sys.executable, "-c", code], env=e, capture_output=True,
                         text=True, cwd=_os.path.dirname(_os.path.dirname(
                             _os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    return _json.loads(out.stdout.strip().splitlines()[-1])


def test_db_follows_the_volume_wherever_it_is_mounted(tmp_path):
    """Раньше путь /data был зашит. Том, смонтированный в другое место,
    не использовался, и база уходила внутрь контейнера."""
    vol = tmp_path / "mnt"
    vol.mkdir()
    r = _db_probe({"RAILWAY_VOLUME_MOUNT_PATH": str(vol),
                   "RAILWAY_ENVIRONMENT": "production", "DB_PATH": None}, tmp_path)
    assert r["path"] == str(vol / "signals.db"), "том не найден"
    assert r["eph"] is False, "база на томе объявлена эфемерной"


def test_railway_without_a_volume_is_always_ephemeral(tmp_path):
    """Ровно наш случай: тома нет, путь уходит в /app/data внутри
    контейнера и стирается при каждом рестарте."""
    r = _db_probe({"RAILWAY_ENVIRONMENT": "production",
                   "RAILWAY_VOLUME_MOUNT_PATH": None, "DB_PATH": None}, tmp_path)
    assert r["eph"] is True, "отсутствие тома не распознано"


def test_db_path_pointing_outside_the_volume_is_ephemeral(tmp_path):
    """Заданный вручную DB_PATH раньше СЧИТАЛСЯ надёжным сам по себе.
    Он может указывать внутрь контейнера — тогда история так же пропадёт."""
    vol = tmp_path / "mnt"
    vol.mkdir()
    r = _db_probe({"RAILWAY_VOLUME_MOUNT_PATH": str(vol),
                   "RAILWAY_ENVIRONMENT": "production",
                   "DB_PATH": "/app/data/signals.db"}, tmp_path)
    assert r["path"] == "/app/data/signals.db"
    assert r["eph"] is True, "путь мимо тома объявлен надёжным"


def test_db_path_inside_the_volume_is_persistent(tmp_path):
    vol = tmp_path / "mnt"
    (vol / "sub").mkdir(parents=True)
    r = _db_probe({"RAILWAY_VOLUME_MOUNT_PATH": str(vol),
                   "RAILWAY_ENVIRONMENT": "production",
                   "DB_PATH": str(vol / "sub" / "s.db")}, tmp_path)
    assert r["eph"] is False


def test_local_disk_is_not_reported_as_ephemeral(tmp_path):
    """Не на Railway деплоев нет — пугать нечем."""
    r = _db_probe({"RAILWAY_ENVIRONMENT": None,
                   "RAILWAY_VOLUME_MOUNT_PATH": None, "DB_PATH": None}, tmp_path)
    assert r["eph"] is False


async def test_reopening_a_symbol_overwrites_the_stale_open_row(tmp_path,
                                                                monkeypatch):
    """Строка предыдущей сделки, чья запись закрытия провалилась, остаётся
    status='open'. Раньше новая позиция без order_id видела её и молча
    считала записанной себя — а при усыновлении после рестарта цели, score
    и order_id берутся ИЗ СТРОКИ, то есть позиция получала ЧУЖОЙ TP2."""
    import core.db as d
    from core.state import Position
    monkeypatch.setattr(d, "DB_PATH", str(tmp_path / "t.db"))
    await d.init_db()
    old = Position(symbol="AAAUSDT", side="Buy", entry=100.0, sl=95.0,
                   tp1=105.0, tp2=110.0, tp3=115.0, qty=1.0, score=50,
                   signal_type="OLD_TRADE")
    assert await d.save_trade_open(old) is True
    new = Position(symbol="AAAUSDT", side="Sell", entry=200.0, sl=210.0,
                   tp1=195.0, tp2=180.0, tp3=170.0, qty=2.0, score=70,
                   signal_type="NEW_TRADE")
    assert await d.save_trade_open(new) is True
    rows = await d.get_open_trades()
    rows = [r for r in rows if r["symbol"] == "AAAUSDT"]
    assert len(rows) == 1, "на символ должна остаться ровно одна открытая строка"
    r = rows[0]
    assert r["signal_type"] == "NEW_TRADE", "строка описывает ПРЕДЫДУЩУЮ сделку"
    assert r["tp2"] == 180.0, "цель осталась от чужой сделки"
    assert r["side"] == "Sell" and r["qty"] == 2.0 and r["score"] == 70
