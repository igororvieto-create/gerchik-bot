import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("db")
DB_PATH = Path("data/gerchik.db")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
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
            score       INTEGER,
            rr          REAL,
            opened_at   TEXT,
            closed_at   TEXT,
            result      TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.commit()


def save_trade(pos, exit_price: float, pnl: float, result: str):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """INSERT INTO trades
                   (symbol,side,entry,exit_price,sl,tp3,qty,pnl,
                    pattern,score,rr,opened_at,closed_at,result)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pos.symbol, pos.side, pos.entry, exit_price,
                 pos.sl, pos.tp3, pos.qty, round(pnl, 4),
                 pos.pattern, pos.score, pos.rr,
                 pos.opened_at.isoformat(),
                 datetime.utcnow().isoformat(),
                 result),
            )
            conn.commit()
    except Exception as e:
        log.error(f"save_trade: {e}")


def get_history(limit: int = 15):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                """SELECT symbol,side,entry,exit_price,pnl,result,closed_at
                   FROM trades ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
            return cur.fetchall()
    except Exception as e:
        log.error(f"get_history: {e}")
        return []


def get_stats(days: int = None):
    """Aggregate stats. days=None → all time."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if days:
                since = (datetime.utcnow() - timedelta(days=days)).isoformat()
                cur = conn.execute(
                    """SELECT COUNT(*), SUM(pnl),
                              SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)
                       FROM trades WHERE closed_at >= ?""",
                    (since,),
                )
            else:
                cur = conn.execute(
                    """SELECT COUNT(*), SUM(pnl),
                              SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)
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


def save_kv(key: str, value):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)",
                (key, str(value)),
            )
            conn.commit()
    except Exception as e:
        log.error(f"save_kv: {e}")


def get_kv(key: str, default=None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception as e:
        log.error(f"get_kv: {e}")
        return default
