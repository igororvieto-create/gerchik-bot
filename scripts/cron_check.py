"""
Railway cron health check — runs every 3 hours.
Connects to BingX + reads SQLite DB, sends a status report to Telegram.

Railway setup:
  Service type : Cron
  Schedule     : 0 */3 * * *
  Command      : python scripts/cron_check.py
  Environment  : same vars as the main bot service (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                 BINGX_API_KEY, BINGX_SECRET, DB_PATH)
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cron_check")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET     = os.getenv("BINGX_SECRET", "").strip()
DB_PATH          = Path(os.getenv("DB_PATH", "data/gerchik.db"))

BINGX_BASE = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com")


# ── Telegram ──────────────────────────────────────────────────────────────────

async def tg_send(session: aiohttp.ClientSession, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.error(f"Telegram error {r.status}: {await r.text()}")
    except Exception as e:
        log.error(f"tg_send: {e}")


# ── BingX ─────────────────────────────────────────────────────────────────────

import hashlib
import hmac
import time
from urllib.parse import urlencode


def _sign(params: dict) -> str:
    qs = urlencode(sorted(params.items()))
    sig = hmac.new(BINGX_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig


async def bx_get(session: aiohttp.ClientSession, path: str, params: dict = None, signed=False):
    params = params or {}
    if signed:
        params["timestamp"] = str(int(time.time() * 1000))
        url = f"{BINGX_BASE}{path}?{_sign(params)}"
    else:
        url = f"{BINGX_BASE}{path}" + ("?" + urlencode(params) if params else "")
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 429:
                log.warning(f"Rate limited: {path}")
                return {}
            return await r.json()
    except Exception as e:
        log.error(f"bx_get {path}: {e}")
        return {}


async def get_balance(session) -> float:
    data = await bx_get(session, "/openApi/swap/v2/user/balance", {}, signed=True)
    try:
        d = data.get("data", {})
        if isinstance(d, dict) and "balance" in d:
            bal = d["balance"]
            if isinstance(bal, dict):
                for field in ("equity", "balance", "availableMargin", "available"):
                    if field in bal and float(bal[field]) > 0:
                        return float(bal[field])
            if isinstance(bal, list):
                for a in bal:
                    if a.get("asset") in ("USDT", "usdt"):
                        for field in ("equity", "balance", "availableMargin"):
                            if field in a:
                                return float(a[field])
        if isinstance(d, dict):
            for field in ("equity", "balance", "availableMargin"):
                if field in d:
                    return float(d[field])
    except Exception as e:
        log.error(f"get_balance parse: {e}")
    return 0.0


async def get_open_positions(session) -> list:
    data = await bx_get(session, "/openApi/swap/v2/user/positions", {}, signed=True)
    positions = data.get("data", []) or []
    return [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]


# ── SQLite ────────────────────────────────────────────────────────────────────

def db_today_stats() -> dict:
    """Read today's closed trades from the DB."""
    result = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    if not DB_PATH.exists():
        return result
    today = datetime.utcnow().date().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT result, pnl FROM trades WHERE closed_at >= ?",
                (today,)
            ).fetchall()
        for result_str, pnl in rows:
            result["trades"] += 1
            result["pnl"] += pnl or 0
            if result_str == "WIN":
                result["wins"] += 1
            else:
                result["losses"] += 1
    except Exception as e:
        log.error(f"db_today_stats: {e}")
    return result


def db_total_pnl() -> float:
    if not DB_PATH.exists():
        return 0.0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='total_pnl'").fetchone()
            return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def db_open_positions() -> list:
    """Read open positions tracked in the DB (KV store)."""
    if not DB_PATH.exists():
        return []
    import json
    positions = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT value FROM kv WHERE key LIKE 'pos:%'").fetchall()
        for (val,) in rows:
            try:
                positions.append(json.loads(val))
            except Exception:
                pass
    except Exception as e:
        log.error(f"db_open_positions: {e}")
    return positions


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set")
        return
    if not BINGX_API_KEY or not BINGX_SECRET:
        log.error("BINGX_API_KEY or BINGX_SECRET not set")
        return

    async with aiohttp.ClientSession() as session:
        balance, live_positions = await asyncio.gather(
            get_balance(session),
            get_open_positions(session),
        )

        stats     = db_today_stats()
        total_pnl = db_total_pnl()
        db_pos    = db_open_positions()

        # Build message
        now = datetime.utcnow().strftime("%H:%M UTC")
        lines = [f"🤖 <b>Статус бота</b> | {now}"]

        # Balance
        bal_sign = "+" if balance > 0 else ""
        lines.append(f"\n💰 <b>Баланс:</b> <code>{balance:.2f} USDT</code>")
        total_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"📊 <b>Всего PnL:</b> <code>{total_sign}{total_pnl:.2f} USDT</code>")

        # Today stats
        if stats["trades"] > 0:
            wr = stats["wins"] / stats["trades"] * 100
            day_sign = "+" if stats["pnl"] >= 0 else ""
            lines.append(
                f"\n📅 <b>Сегодня:</b> {stats['trades']} сделок | "
                f"✅{stats['wins']} ❌{stats['losses']} | "
                f"WR {wr:.0f}% | <code>{day_sign}{stats['pnl']:.2f} USDT</code>"
            )
        else:
            lines.append("\n📅 <b>Сегодня:</b> сделок нет")

        # Open positions
        if live_positions:
            lines.append(f"\n📌 <b>Открытых позиций на бирже:</b> {len(live_positions)}")
            for p in live_positions[:5]:
                sym  = p.get("symbol", "?")
                side = p.get("positionSide", "?")
                upnl = float(p.get("unrealizedProfit", 0))
                sign = "+" if upnl >= 0 else ""
                lines.append(f"  • {sym} {side} | uPnL: <code>{sign}{upnl:.2f}</code>")
        else:
            lines.append("\n📌 <b>Открытых позиций:</b> нет")

        # Mismatch warning
        if len(live_positions) != len(db_pos):
            lines.append(
                f"\n⚠️ Расхождение: биржа={len(live_positions)}, "
                f"бот отслеживает={len(db_pos)}"
            )

        # Balance zero warning
        if balance <= 0:
            lines.append("\n🚨 <b>Баланс равен 0!</b> Проверь фьючерсный счёт на BingX")

        await tg_send(session, "\n".join(lines))
        log.info("Health check sent to Telegram")


if __name__ == "__main__":
    asyncio.run(main())
