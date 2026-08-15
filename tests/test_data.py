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
