import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict

import aiosqlite

from core.state import Signal

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
                ts          TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        await db.commit()
    log.info(f"DB initialised at {DB_PATH}")


async def save_signal(sig: Signal) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO signals
                   (symbol, signal_type, direction, score, price,
                    oi_change, vol_ratio, funding, ob_bias, atr_pct, details, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sig.symbol, sig.signal_type, sig.direction, sig.score, sig.price,
                    sig.oi_change, sig.vol_ratio, sig.funding, sig.ob_bias, sig.atr_pct,
                    sig.details, sig.ts.isoformat(),
                ),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_signal error: {e}")


async def get_recent_signals(hours: int = 24, limit: int = 200) -> List[Dict]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM signals WHERE ts >= ? ORDER BY ts DESC LIMIT ?""",
                (cutoff, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_recent_signals error: {e}")
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
