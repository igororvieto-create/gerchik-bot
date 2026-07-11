import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict

import aiosqlite

from core.state import Signal, Position

log = logging.getLogger("db")
DB_PATH = os.getenv("DB_PATH", "data/signals.db")


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                direction   TEXT NOT NULL,
                score       INTEGER NOT NULL,
                price       REAL NOT NULL,
                oi_change   REAL,
                vol_ratio   REAL,
                funding     REAL,
                ob_bias     TEXT,
                atr_pct     REAL,
                details     TEXT,
                entry       REAL,
                sl          REAL,
                tp1         REAL,
                tp2         REAL,
                tp3         REAL,
                rr          REAL,
                sl_pct      REAL,
                ts          TEXT NOT NULL
            )
        """)
        for col in ["entry REAL", "sl REAL", "tp1 REAL", "tp2 REAL",
                    "tp3 REAL", "rr REAL", "sl_pct REAL"]:
            try:
                await db.execute(f"ALTER TABLE signals ADD COLUMN {col}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL,
                entry       REAL,
                exit_price  REAL,
                sl          REAL,
                tp1         REAL,
                tp2         REAL,
                tp3         REAL,
                qty         REAL,
                pnl         REAL,
                score       INTEGER,
                signal_type TEXT,
                order_id    TEXT,
                status      TEXT DEFAULT 'open',
                opened_at   TEXT NOT NULL,
                closed_at   TEXT
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        await db.commit()
    log.info(f"DB initialised at {DB_PATH}")


async def save_signal(sig: Signal) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO signals
                   (symbol, signal_type, direction, score, price,
                    oi_change, vol_ratio, funding, ob_bias, atr_pct, details,
                    entry, sl, tp1, tp2, tp3, rr, sl_pct, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.symbol, sig.signal_type, sig.direction, sig.score, sig.price,
                 sig.oi_change, sig.vol_ratio, sig.funding, sig.ob_bias, sig.atr_pct,
                 sig.details,
                 sig.entry, sig.sl, sig.tp1, sig.tp2, sig.tp3, sig.rr, sig.sl_pct,
                 sig.ts.isoformat()),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_signal error: {e}")


async def save_trade_open(pos: Position) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR IGNORE INTO trades
                   (symbol, side, entry, sl, tp1, tp2, tp3, qty,
                    score, signal_type, order_id, status, opened_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
                (pos.symbol, pos.side, pos.entry, pos.sl,
                 pos.tp1, pos.tp2, pos.tp3, pos.qty,
                 pos.score, pos.signal_type, pos.order_id,
                 pos.ts.isoformat()),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_trade_open error: {e}")


async def save_trade_close(pos: Position, exit_price: float = 0.0, pnl: float = 0.0) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
                   WHERE symbol=? AND status='open'""",
                (exit_price, pnl, datetime.utcnow().isoformat(), pos.symbol),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_trade_close error: {e}")


async def get_recent_signals(hours: int = 24, limit: int = 200) -> List[Dict]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM signals WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_recent_signals error: {e}")
        return []


async def get_trades(limit: int = 50) -> List[Dict]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_trades error: {e}")
        return []


async def cleanup_old_signals(keep_hours: int = 48) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=keep_hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
            await db.commit()
            return cur.rowcount
    except Exception as e:
        log.error(f"cleanup error: {e}")
        return 0
