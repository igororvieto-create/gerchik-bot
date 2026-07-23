import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict

import aiosqlite

from core.state import Signal, Position

log = logging.getLogger("db")
# Use an absolute path anchored to this file so the DB is always found
# at <project-root>/data/signals.db regardless of the process CWD.
# The env-var override still works for Railway Volumes or custom mounts.
_DEFAULT_DB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "signals.db")
)
DB_PATH = os.getenv("DB_PATH", _DEFAULT_DB)


async def init_db() -> None:
    dirpath = os.path.dirname(DB_PATH)
    if dirpath:
        try:
            os.makedirs(dirpath, exist_ok=True)
        except OSError as e:
            log.warning(f"Could not create DB directory {dirpath!r}: {e}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
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
                    "tp3 REAL", "rr REAL", "sl_pct REAL",
                    "outcome TEXT", "outcome_price REAL", "outcome_at TEXT"]:
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, opened_at)")
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
            # Filter by order_id to avoid closing the wrong row on re-entry
            if pos.order_id:
                await db.execute(
                    """UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
                       WHERE symbol=? AND order_id=? AND status='open'""",
                    (exit_price, pnl, datetime.utcnow().isoformat(), pos.symbol, pos.order_id),
                )
            else:
                # No order_id: close only the MOST RECENT open row — a blanket
                # WHERE symbol+status would stamp every stale open row (e.g.
                # left over from a crash) with this trade's exit/pnl
                await db.execute(
                    """UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
                       WHERE id = (SELECT id FROM trades
                                   WHERE symbol=? AND status='open'
                                   ORDER BY opened_at DESC LIMIT 1)""",
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


async def get_pending_signals(max_age_hours: int = 48) -> List[Dict]:
    """Signals without a recorded outcome, young enough to still evaluate."""
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, symbol, direction, entry, sl, tp2, ts FROM signals
                   WHERE outcome IS NULL AND ts >= ?
                     AND entry > 0 AND sl > 0 AND tp2 > 0""",
                (cutoff,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_pending_signals error: {e}")
        return []


async def set_signal_outcome(signal_id: int, outcome: str, price: float) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE signals SET outcome=?, outcome_price=?, outcome_at=? WHERE id=?",
                (outcome, price, datetime.utcnow().isoformat(), signal_id),
            )
            await db.commit()
    except Exception as e:
        log.error(f"set_signal_outcome error: {e}")


async def get_outcome_stats(days: int = 7) -> Dict:
    """Forward-test scoreboard: how many signals hit TP2 before SL and vice versa."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    stats = {"win": 0, "loss": 0, "expired": 0, "open": 0, "winrate": None}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT COALESCE(outcome, 'OPEN') o, COUNT(*) c FROM signals
                   WHERE ts >= ? GROUP BY o""",
                (cutoff,),
            ) as cur:
                for o, c in await cur.fetchall():
                    if o == "WIN":       stats["win"] = c
                    elif o == "LOSS":    stats["loss"] = c
                    elif o == "EXPIRED": stats["expired"] = c
                    else:                stats["open"] = c
        decided = stats["win"] + stats["loss"]
        if decided:
            stats["winrate"] = round(stats["win"] / decided * 100, 1)
        return stats
    except Exception as e:
        log.error(f"get_outcome_stats error: {e}")
        return stats


async def get_realized_pnl_since(closed_after_iso: str) -> float:
    """Sum of realized PnL for trades closed at/after the given ISO timestamp.
    Used to rebuild the daily circuit-breaker counter after a process restart —
    in-memory-only accounting would reset the halt on every deploy/crash."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE status='closed' AND closed_at >= ?",
                (closed_after_iso,),
            ) as cur:
                row = await cur.fetchone()
        return float(row[0] or 0.0)
    except Exception as e:
        log.error(f"get_realized_pnl_since error: {e}")
        return 0.0


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
    from core.config import cfg
    cutoff = (datetime.utcnow() - timedelta(hours=keep_hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
            removed = cur.rowcount
            # Row-count cap (MAX_SIGNALS_DB) on top of the time-based retention —
            # a noisy market can write thousands of rows inside 48h
            cur2 = await db.execute(
                """DELETE FROM signals WHERE id NOT IN
                   (SELECT id FROM signals ORDER BY ts DESC LIMIT ?)""",
                (max(cfg.MAX_SIGNALS_DB, 1),),
            )
            removed += cur2.rowcount
            # Also purge closed trades older than 90 days
            old_trades = (datetime.utcnow() - timedelta(days=90)).isoformat()
            await db.execute(
                "DELETE FROM trades WHERE status='closed' AND closed_at < ?", (old_trades,)
            )
            await db.commit()

        # Evict stale entries from in-memory cooldown dict to prevent unbounded growth
        from core.state import state
        cutoff_dt = datetime.utcnow() - timedelta(hours=keep_hours)
        stale = [sym for sym, ts in state.signal_seen.items() if ts < cutoff_dt]
        for sym in stale:
            state.signal_seen.pop(sym, None)
        if stale:
            log.info(f"cleanup: evicted {len(stale)} stale signal_seen entries")

        return removed
    except Exception as e:
        log.error(f"cleanup error: {e}")
        return 0
