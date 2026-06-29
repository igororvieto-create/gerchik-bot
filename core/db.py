import asyncio
import os
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("db")
DB_PATH = Path(os.getenv("DB_PATH", "data/gerchik.db"))
_BACKUP_PATH = DB_PATH.parent / "positions_backup.json"


def _connect():
    """Open a SQLite connection with WAL mode and a generous write timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _update_backup():
    """Write current open positions from DB to JSON backup file (DB-only, no fallback)."""
    try:
        positions = []
        with _connect() as conn:
            cur = conn.execute("SELECT value FROM kv WHERE key LIKE 'pos:%'")
            for (val,) in cur.fetchall():
                try:
                    positions.append(json.loads(val))
                except Exception:
                    pass
        _BACKUP_PATH.write_text(json.dumps(positions, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"_update_backup: {e}")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            side        TEXT,
            entry       REAL,
            exit_price  REAL,
            sl          REAL,
            tp3         REAL,
            qty         REAL,
            pnl         REAL,
            pattern     TEXT,
            tf          TEXT,
            score       INTEGER,
            rr          REAL,
            opened_at   TEXT,
            closed_at   TEXT,
            result      TEXT,
            is_partial  INTEGER DEFAULT 0
        )""")
        # Migrations for existing DBs
        for col, defn in [("tf", "TEXT DEFAULT ''"), ("is_partial", "INTEGER DEFAULT 0")]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    log.error(f"init_db: ALTER TABLE trades ADD COLUMN {col} failed: {e}")
            except Exception as e:
                log.error(f"init_db: unexpected migration error for column {col}: {e}")
        conn.execute("""CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.commit()


def save_trade(pos, exit_price: float, pnl: float, result: str, is_partial: bool = False):
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO trades
                   (symbol,side,entry,exit_price,sl,tp3,qty,pnl,
                    pattern,tf,score,rr,opened_at,closed_at,result,is_partial)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pos.symbol, pos.side, pos.entry, exit_price,
                 pos.sl, pos.tp3, pos.qty, round(pnl, 4),
                 pos.pattern, getattr(pos, "tf", ""),
                 pos.score, pos.rr,
                 pos.opened_at.isoformat(),
                 datetime.utcnow().isoformat(),
                 result, int(is_partial)),
            )
            conn.commit()
    except Exception as e:
        log.error(f"save_trade: {e}")


def get_history(limit: int = 15):
    try:
        with _connect() as conn:
            cur = conn.execute(
                """SELECT symbol,side,entry,exit_price,pnl,result,closed_at
                   FROM trades WHERE is_partial = 0 ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
            return cur.fetchall()
    except Exception as e:
        log.error(f"get_history: {e}")
        return []


def get_stats(days: Optional[int] = None):
    """Aggregate stats. days=None → all time.
    Trade counts/wins exclude partial closes; PnL sums include all records."""
    try:
        with _connect() as conn:
            if days:
                since = (datetime.utcnow() - timedelta(days=days)).isoformat()
                cur = conn.execute(
                    """SELECT
                          SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                          SUM(pnl),
                          SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END)
                       FROM trades WHERE closed_at >= ?""",
                    (since,),
                )
            else:
                cur = conn.execute(
                    """SELECT
                          SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                          SUM(pnl),
                          SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END)
                       FROM trades"""
                )
            row = cur.fetchone()
        total = row[0] or 0
        pnl   = row[1] or 0.0
        wins  = row[2] or 0
        wr    = round(wins / total * 100) if total else 0
        return {"total": total, "pnl": round(pnl, 2), "wins": wins, "wr": wr}
    except Exception as e:
        log.error(f"get_stats: {e}")
        return {"total": 0, "pnl": 0.0, "wins": 0, "wr": 0}


def load_total_pnl() -> float:
    return get_stats()["pnl"]


def get_stats_by_pattern(days: Optional[int] = None) -> list:
    """Returns list of (pattern, total, wins, pnl) sorted by total trades.
    Counts only final closes; PnL sums all records for each pattern."""
    try:
        with _connect() as conn:
            if days:
                since = (datetime.utcnow() - timedelta(days=days)).isoformat()
                cur = conn.execute(
                    """SELECT pattern,
                              SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END),
                              SUM(pnl)
                       FROM trades WHERE closed_at >= ? AND pattern != ''
                       GROUP BY pattern
                       ORDER BY SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END) DESC""",
                    (since,),
                )
            else:
                cur = conn.execute(
                    """SELECT pattern,
                              SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END),
                              SUM(pnl)
                       FROM trades WHERE pattern != ''
                       GROUP BY pattern
                       ORDER BY SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END) DESC"""
                )
            rows = cur.fetchall()
        return [(r[0], r[1] or 0, r[2] or 0, round(r[3] or 0, 2)) for r in rows]
    except Exception as e:
        log.error(f"get_stats_by_pattern: {e}")
        return []


def get_yesterday_stats() -> dict:
    """Stats for yesterday (UTC calendar day) — used by daily_report at 09:00 UTC."""
    try:
        from datetime import timedelta
        today = datetime.utcnow().date()
        since = (today - timedelta(days=1)).isoformat() + "T00:00:00"
        until = today.isoformat() + "T00:00:00"
        with _connect() as conn:
            cur = conn.execute(
                """SELECT
                      SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                      SUM(pnl),
                      SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END)
                   FROM trades WHERE closed_at >= ? AND closed_at < ?""",
                (since, until),
            )
            row = cur.fetchone()
        total = row[0] or 0
        pnl   = row[1] or 0.0
        wins  = row[2] or 0
        return {
            "total": total, "pnl": round(pnl, 2),
            "wins": wins, "losses": total - wins,
            "date": (datetime.utcnow().date() - timedelta(days=1)).isoformat(),
        }
    except Exception as e:
        log.error(f"get_yesterday_stats: {e}")
        return {"total": 0, "pnl": 0.0, "wins": 0, "losses": 0, "date": ""}


def get_today_stats() -> dict:
    """Stats for today (UTC) — used to restore state.day after restart.
    Counts only final closes; PnL includes all records (partials + final)."""
    try:
        today = datetime.utcnow().date().isoformat() + "T00:00:00"
        with _connect() as conn:
            cur = conn.execute(
                """SELECT
                      SUM(CASE WHEN is_partial=0 THEN 1 ELSE 0 END),
                      SUM(pnl),
                      SUM(CASE WHEN is_partial=0 AND pnl>0 THEN 1 ELSE 0 END)
                   FROM trades WHERE closed_at >= ?""",
                (today,),
            )
            row = cur.fetchone()
        total = row[0] or 0
        pnl   = row[1] or 0.0
        wins  = row[2] or 0
        return {"total": total, "pnl": round(pnl, 2), "wins": wins, "losses": total - wins}
    except Exception as e:
        log.error(f"get_today_stats: {e}")
        return {"total": 0, "pnl": 0.0, "wins": 0, "losses": 0}


def load_all_cooldowns() -> dict:
    """Load all sl_cd:* cooldown entries at once (used on scanner startup)."""
    result = {}
    try:
        with _connect() as conn:
            cur = conn.execute("SELECT key, value FROM kv WHERE key LIKE 'sl_cd:%'")
            for key, val in cur.fetchall():
                symbol = key[len("sl_cd:"):]
                try:
                    result[symbol] = datetime.fromisoformat(val)
                except Exception:
                    pass
    except Exception as e:
        log.error(f"load_all_cooldowns: {e}")
    return result


def load_all_loss_streaks() -> dict:
    """Load all sl_streak:* entries at once (used on scanner startup)."""
    result = {}
    try:
        with _connect() as conn:
            cur = conn.execute("SELECT key, value FROM kv WHERE key LIKE 'sl_streak:%'")
            for key, val in cur.fetchall():
                symbol = key[len("sl_streak:"):]
                try:
                    result[symbol] = int(val)
                except Exception:
                    pass
    except Exception as e:
        log.error(f"load_all_loss_streaks: {e}")
    return result


def save_kv(key: str, value):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)",
                (key, str(value)),
            )
            conn.commit()
    except Exception as e:
        log.error(f"save_kv: {e}")


def get_kv(key: str, default=None):
    try:
        with _connect() as conn:
            cur = conn.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception as e:
        log.error(f"get_kv: {e}")
        return default


# ── Open position persistence ─────────────────────────────────────────────────

def save_open_position(pos) -> None:
    """Persist open position to KV store so it survives bot restarts."""
    data = {
        "symbol": pos.symbol, "side": pos.side,
        "entry": pos.entry, "sl": pos.sl,
        "tp1": pos.tp1, "tp2": pos.tp2, "tp3": pos.tp3,
        "qty": pos.qty, "risk_usdt": pos.risk_usdt,
        "order_id": pos.order_id, "sl_order_id": pos.sl_order_id,
        "tp_order_id": pos.tp_order_id,
        "be_moved": pos.be_moved, "tp1_hit": pos.tp1_hit, "tp2_hit": pos.tp2_hit,
        "trail_price": pos.trail_price,
        "partial_pnl_taken": pos.partial_pnl_taken,
        "opened_at": pos.opened_at.isoformat(),
        "pattern": pos.pattern, "tf": pos.tf,
        "rr": pos.rr, "score": pos.score,
    }
    save_kv(f"pos:{pos.symbol}", json.dumps(data))
    _update_backup()


def delete_open_position(symbol: str) -> None:
    """Remove persisted position when it closes."""
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?", (f"pos:{symbol}",))
            conn.commit()
        _update_backup()
    except Exception as e:
        log.error(f"delete_open_position {symbol}: {e}")


def save_cfg_value(key: str, value) -> None:
    """Persist a runtime-changed config value so it survives restarts."""
    save_kv(f"cfg:{key}", str(value))


def load_cfg_values() -> dict:
    """Load all previously saved config overrides (keys without 'cfg:' prefix)."""
    result = {}
    try:
        with _connect() as conn:
            cur = conn.execute("SELECT key, value FROM kv WHERE key LIKE 'cfg:%'")
            for k, v in cur.fetchall():
                result[k[4:]] = v
    except Exception as e:
        log.error(f"load_cfg_values: {e}")
    return result


def load_open_positions() -> list:
    """Load all persisted open positions at startup."""
    result = []
    try:
        with _connect() as conn:
            cur = conn.execute("SELECT value FROM kv WHERE key LIKE 'pos:%'")
            for (val,) in cur.fetchall():
                try:
                    result.append(json.loads(val))
                except Exception:
                    pass
    except Exception as e:
        log.error(f"load_open_positions: {e}")

    # Fall back to JSON backup if DB has no positions (e.g. fresh DB after redeploy)
    if not result and _BACKUP_PATH.exists():
        try:
            backup = json.loads(_BACKUP_PATH.read_text())
            if backup:
                log.warning(f"DB had no positions — restoring {len(backup)} from JSON backup")
                result = backup
        except Exception as e:
            log.error(f"load_open_positions backup: {e}")
    return result


# ── Async wrappers (non-blocking for asyncio event loop) ─────────────────────

async def async_save_trade(pos, exit_price: float, pnl: float, result: str,
                           is_partial: bool = False) -> None:
    await asyncio.to_thread(save_trade, pos, exit_price, pnl, result, is_partial)


async def async_save_kv(key: str, value) -> None:
    await asyncio.to_thread(save_kv, key, value)


async def async_save_cfg_value(key: str, value) -> None:
    await asyncio.to_thread(save_cfg_value, key, value)


async def async_save_open_position(pos) -> None:
    await asyncio.to_thread(save_open_position, pos)


async def async_delete_open_position(symbol: str) -> None:
    await asyncio.to_thread(delete_open_position, symbol)
