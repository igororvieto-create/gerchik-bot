import logging
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import aiosqlite

from core.state import Signal, Position

log = logging.getLogger("db")
# Priority: DB_PATH env var > Railway Volume at /data (survives deploys!)
# > <project-root>/data/signals.db (ephemeral — wiped on every redeploy).
# Without a Volume the forward-test statistics reset with each git push.
if os.path.isdir("/data") and os.access("/data", os.W_OK):
    _DEFAULT_DB = "/data/signals.db"
else:
    _DEFAULT_DB = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "signals.db")
    )
DB_PATH = os.getenv("DB_PATH", _DEFAULT_DB)


async def init_db() -> None:
    # Предупреждение здесь, а не на уровне модуля: на импорте оно (а) уходило
    # в stderr мимо настроенного в main.py формата, потому что basicConfig
    # ещё не отработал, и (б) срабатывало ложно, когда том смонтирован не в
    # /data, а путь задан через DB_PATH.
    if not os.getenv("DB_PATH") and not DB_PATH.startswith("/data"):
        # Без тома вся история сделок и форвард-теста исчезает при каждом
        # деплое, а get_open_trades() после рестарта не находит своих позиций —
        # живые позиции бота получают ярлык MANUAL и лишаются защиты стопа.
        log.error("Railway Volume (/data) НЕ подключён — база эфемерная, "
                  "история и открытые сделки не переживут деплой")
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
                    "outcome TEXT", "outcome_price REAL", "outcome_at TEXT",
                    # headroom нужен, чтобы ПРОВЕРИТЬ по истории гипотезу, ради
                    # которой введён MIN_TRADE_HEADROOM_R: без него нельзя
                    # срезать винрейт по запасу до цели и убедиться, что полоса
                    # 1.5-2.0R действительно проигрышная.
                    "headroom REAL",
                    # mfe_r — максимальный ход в плюс в единицах R до исхода.
                    # Позволяет подбирать порог переноса стопа в безубыток по
                    # фактическим данным, а не по модели случайного блуждания.
                    "mfe_r REAL",
                    # Лента исполненных сделок: направленное «усилие» VSA.
                    # Пишется, но на решения пока не влияет — сначала
                    # проверяем на исходах, потом (и только потом) включаем.
                    "flow_delta REAL", "flow_span_min REAL", "flow_absorb INTEGER"]:
            try:
                await db.execute(f"ALTER TABLE signals ADD COLUMN {col}")
            except Exception as e:
                # "duplicate column name" — норма (колонка уже есть).
                # Всё остальное (БД заблокирована, диск полон, read-only)
                # раньше глушилось молча, и весь форвард-тест тихо не работал:
                # запросы падали на "no such column", а каждый вызывающий
                # проглатывал ошибку и возвращал пустоту.
                if "duplicate column" not in str(e).lower():
                    log.error(f"init_db: не удалось добавить колонку {col} — {e}")

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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome, ts)")
        # Индексов по signals(symbol) больше нет: ни один запрос не фильтрует
        # и не сортирует по symbol (кулдаун живёт в памяти, в state.signal_seen),
        # а два лишних индекса обновлялись на КАЖДОЙ вставке сигнала.
        await db.execute("DROP INDEX IF EXISTS idx_signals_symbol")
        await db.execute("DROP INDEX IF EXISTS idx_signals_symbol_ts")
        # Уникальный индекс может упасть на СУЩЕСТВУЮЩЕЙ базе, где дубли
        # уже накопились (он появился позже самой таблицы). Раньше это
        # роняло весь init_db: остальные индексы не создавались, commit не
        # вызывался. Сначала убираем дубли, затем создаём — и в любом
        # случае не даём упасть остальным DDL.
        try:
            # Оставляем не MIN(id), а ТЕРМИНАЛЬНУЮ строку: у дубля по одному
            # order_id одна строка обычно 'open' (запись при входе), вторая
            # 'closed' с реализованным PnL. MIN(id) сохранял первую и удалял
            # вторую — убыток исчезал из дневного предохранителя, а вечная
            # open-строка заставляла бота считать чужую позицию своей.
            # ROW_NUMBER, а не коррелированный подзапрос: тот сканировал
            # таблицу целиком на КАЖДУЮ группу (частичный индекс во вложенном
            # запросе неприменим) и рос квадратично — 5.6 с на 10 000 строк,
            # ~22 с на 20 000. init_db ждётся синхронно до старта HTTP, то
            # есть это была прямая задержка деплоя и риск провалить healthcheck.
            await db.execute(
                "DELETE FROM trades WHERE order_id IS NOT NULL AND order_id != '' "
                "AND id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY order_id"
                "      ORDER BY (status='closed') DESC, (pnl IS NOT NULL) DESC, id ASC"
                "    ) rn FROM trades WHERE order_id IS NOT NULL AND order_id != ''"
                "  ) WHERE rn = 1"
                ")"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_order "
                "ON trades(order_id) WHERE order_id IS NOT NULL AND order_id != ''"
            )
        except Exception as e:
            log.error(f"init_db: не удалось создать idx_trades_order — {e}")
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
                    entry, sl, tp1, tp2, tp3, rr, sl_pct, headroom,
                    flow_delta, flow_span_min, flow_absorb, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.symbol, sig.signal_type, sig.direction, sig.score, sig.price,
                 sig.oi_change, sig.vol_ratio, sig.funding, sig.ob_bias, sig.atr_pct,
                 sig.details,
                 sig.entry, sig.sl, sig.tp1, sig.tp2, sig.tp3, sig.rr, sig.sl_pct,
                 sig.headroom,
                 sig.flow_delta, sig.flow_span_min, int(sig.flow_absorb),
                 sig.ts.isoformat()),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_signal error: {e}")


async def save_trade_open(pos: Position) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Частичный уникальный индекс покрывает только непустой order_id.
            # Для позиций без него (реконсиляция дубликата 110072,
            # восстановленные) проверяем вручную, иначе повторный вызов
            # плодит строку, и "лишняя" open-строка потом заставляет бота
            # считать чужую позицию своей.
            if not pos.order_id:
                async with db.execute(
                    "SELECT 1 FROM trades WHERE symbol=? AND status='open' LIMIT 1",
                    (pos.symbol,),
                ) as cur:
                    if await cur.fetchone():
                        return
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


async def set_signal_outcome(signal_id: int, outcome: str, price: float,
                             mfe_r: float = 0.0) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE signals SET outcome=?, outcome_price=?, outcome_at=?, mfe_r=? "
                "WHERE id=?",
                (outcome, price, datetime.utcnow().isoformat(), mfe_r, signal_id),
            )
            await db.commit()
    except Exception as e:
        log.error(f"set_signal_outcome error: {e}")


def _ev(slot: Dict) -> Dict:
    """Винрейт и матожидание в R.

    С переносом стопа в безубыток одного винрейта мало: исход BE даёт ~0R и
    в знаменатель винрейта не входит, но на матожидание влияет — он забирает
    сделки, которые раньше были полным убытком. Решение о реальных деньгах
    принимается по ev_r, а не по проценту побед.

    Цель — TP2 = 2R, стоп — 1R, безубыток — 0R.
    """
    win, loss, be = slot.get("win", 0), slot.get("loss", 0), slot.get("be", 0)
    decided = win + loss
    out: Dict = {"winrate": round(win / decided * 100, 1) if decided else None}
    total = win + loss + be
    out["ev_r"] = round((win * 2.0 + be * 0.0 + loss * -1.0) / total, 3) if total else None
    return out


async def get_outcome_stats(days: int = 7) -> Dict:
    """Forward-test scoreboard: how many signals hit TP2 before SL and vice versa."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    stats = {"win": 0, "loss": 0, "be": 0, "expired": 0, "open": 0,
             "winrate": None, "ev_r": None}
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
                    elif o == "BE":      stats["be"] = c
                    elif o == "EXPIRED": stats["expired"] = c
                    else:                stats["open"] = c
        stats.update(_ev(stats))
        return stats
    except Exception as e:
        log.error(f"get_outcome_stats error: {e}")
        return stats


async def get_outcome_breakdown(days: int = 7) -> Dict:
    """Winrate sliced by score bucket / direction / signal type + last decided.
    The go/no-go analysis tool: shows WHERE the strategy wins or loses."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    out: Dict = {"by_score": {}, "by_direction": {}, "by_type": {},
                 "by_sl_atr": {}, "by_headroom": {}, "by_flow": {}, "recent": []}
    # Явный порядок корзин. Фронт сортировал ключи лексикографически, а '<'
    # (0x3C) и '>' (0x3E) больше цифр — крайние корзины уезжали в середину и
    # хвост, ось переставала быть монотонной по ширине стопа. Именно её
    # монотонность и есть весь смысл среза: растёт ли винрейт с шириной.
    out["_order"] = {
        "by_flow":     ["продавцы <-0.2", "нейтрально", "покупатели >+0.2", "поглощение"],
        "by_score":    ["30-44", "45-59", "60+"],
        "by_sl_atr":   ["<1.0 ATR", "1.0-1.5", "1.5-2.5", ">2.5 ATR"],
        "by_headroom": ["1.5-2.0R (не торгуется)", "2.0-3.0R", ">3.0R"],
    }

    def _bucket(score: int) -> str:
        if score >= 60: return "60+"
        if score >= 45: return "45-59"
        return "30-44"

    def _sl_bucket(sl_pct, atr_pct) -> Optional[str]:
        """Ширина стопа в ATR. Пол в _calc_levels при найденном уровне —
        0.75 ATR, то есть стоп может оказаться УЖЕ типичного хода одной
        4-часовой свечи и выбиваться шумом, а не сломом идеи сделки.
        Этот срез отвечает на вопрос данными, а не рассуждением."""
        if not sl_pct or not atr_pct or atr_pct <= 0:
            return None
        r = sl_pct / atr_pct
        if r < 1.0:  return "<1.0 ATR"
        if r < 1.5:  return "1.0-1.5"
        if r < 2.5:  return "1.5-2.5"
        return ">2.5 ATR"

    def _flow_bucket(delta, absorb) -> Optional[str]:
        """Срез по направленному усилию из ленты сделок.

        Проверяет ровно один вопрос: предсказывает ли перевес агрессора
        исход сделки. Если срезы «покупатели» и «продавцы» дают одинаковое
        матожидание — метрика бесполезна и её не надо встраивать в скор.
        """
        if delta is None:
            return None
        if absorb:
            return "поглощение"
        if delta <= -0.2:
            return "продавцы <-0.2"
        if delta >= 0.2:
            return "покупатели >+0.2"
        return "нейтрально"

    def _hr_bucket(hr) -> Optional[str]:
        """Запас до встречной цели. Торгуются только >=2R
        (MIN_TRADE_HEADROOM_R), полоса 1.5-2.0 показывается, но не торгуется —
        срез показывает, оправдано ли это отсечение."""
        if hr is None or hr <= 0:
            return None
        if hr < 2.0: return "1.5-2.0R (не торгуется)"
        if hr < 3.0: return "2.0-3.0R"
        return ">3.0R"

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT symbol, score, direction, signal_type, outcome, ts,
                          sl_pct, atr_pct, headroom, flow_delta, flow_absorb
                   FROM signals WHERE outcome IS NOT NULL AND ts >= ?
                   ORDER BY ts DESC""",
                (cutoff,),
            ) as cur:
                rows = await cur.fetchall()

        def _acc(d: Dict, key: str, outcome: str) -> None:
            slot = d.setdefault(key, {"win": 0, "loss": 0, "be": 0, "expired": 0})
            k = outcome.lower()
            if k in slot:
                slot[k] += 1

        for r in rows:
            _acc(out["by_score"], _bucket(r["score"]), r["outcome"])
            _acc(out["by_direction"], r["direction"], r["outcome"])
            _acc(out["by_type"], r["signal_type"], r["outcome"])
            slb = _sl_bucket(r["sl_pct"], r["atr_pct"])
            if slb:
                _acc(out["by_sl_atr"], slb, r["outcome"])
            hrb = _hr_bucket(r["headroom"])
            if hrb:
                _acc(out["by_headroom"], hrb, r["outcome"])
            fb = _flow_bucket(r["flow_delta"], r["flow_absorb"])
            if fb:
                _acc(out["by_flow"], fb, r["outcome"])

        for d in (out["by_score"], out["by_direction"], out["by_type"],
                  out["by_sl_atr"], out["by_headroom"], out["by_flow"]):
            for slot in d.values():
                slot.update(_ev(slot))

        out["recent"] = [
            {"symbol": r["symbol"], "score": r["score"], "dir": r["direction"],
             "type": r["signal_type"], "outcome": r["outcome"], "ts": r["ts"]}
            for r in rows[:25]
        ]
        return out
    except Exception as e:
        log.error(f"get_outcome_breakdown error: {e}")
        return out


async def get_open_trades() -> List[Dict]:
    """Открытые сделки БОТА. Нужны, чтобы после рестарта отличить свою
    позицию (её надо защищать и учитывать) от ручной сделки пользователя
    (её трогать нельзя и в дневной лимит она не входит).

    БРОСАЕТ при ошибке БД и НЕ возвращает []. Пустой список означает «своих
    сделок нет», и вызывающий помечал бы все живые позиции как MANUAL: без
    стопа, вне слотов и мимо дневного лимита. Отличить «БД недоступна» от
    «сделок нет» обязан вызывающий, а не эта функция."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def close_stale_open_trades(live_symbols: List[str], older_than_hours: int = 24) -> int:
    """Закрывает "зависшие" open-строки: сделка старше суток, а позиции с таким
    символом на бирже нет. Без этого строка оставалась open навсегда и при
    усыновлении заставляла бота считать ЧУЖУЮ позицию своей."""
    cutoff = (datetime.utcnow() - timedelta(hours=older_than_hours)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            if live_symbols:
                ph = ",".join("?" * len(live_symbols))
                sql = (f"UPDATE trades SET status='stale', closed_at=? "
                       f"WHERE status='open' AND opened_at < ? AND symbol NOT IN ({ph})")
                params = [datetime.utcnow().isoformat(), cutoff, *live_symbols]
            else:
                sql = ("UPDATE trades SET status='stale', closed_at=? "
                       "WHERE status='open' AND opened_at < ?")
                params = [datetime.utcnow().isoformat(), cutoff]
            cur = await db.execute(sql, params)
            await db.commit()
            if cur.rowcount:
                log.warning(f"reconcile: {cur.rowcount} зависших open-сделок помечены stale")
            return cur.rowcount
    except Exception as e:
        log.error(f"close_stale_open_trades error: {e}")
        return 0


async def get_realized_pnl_since(closed_after_iso: str) -> float:
    """Sum of realized PnL for trades closed at/after the given ISO timestamp.
    Used to rebuild the daily circuit-breaker counter after a process restart —
    in-memory-only accounting would reset the halt on every deploy/crash.

    БРОСАЕТ при ошибке БД. Возврат 0.0 означал «сегодня потерь нет»: при
    заблокированной базе (WAL + параллельная чистка) предохранитель тихо
    обнулялся, дата дня штамповалась, и бот торговал остаток суток с
    потерянным счётчиком убытка."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE status='closed' AND closed_at >= ?",
            (closed_after_iso,),
        ) as cur:
            row = await cur.fetchone()
    return float(row[0] or 0.0)


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
            # Лимит строк НЕ трогает сигналы, по которым оценщик ещё не вынес
            # вердикт: раньше при 100 парах лимит 500 выбирался за часы, и
            # сигналы удалялись прямо посреди 48-часового окна оценки. Стопы
            # (1R) разрешаются быстрее тейков (2R), поэтому удалялись
            # преимущественно будущие победы — винрейт систематически занижался.
            # Окно лимита считается ТОЛЬКО по решённым строкам. Раньше в
            # него входили и нерешённые (outcome IS NULL): они вытесняли
            # решённые, и первый же cleanup стирал всю статистику винрейта,
            # по которой принимается решение о запуске на реальные деньги.
            cur2 = await db.execute(
                """DELETE FROM signals
                   WHERE outcome IS NOT NULL
                     AND id NOT IN (SELECT id FROM signals
                                    WHERE outcome IS NOT NULL
                                    ORDER BY ts DESC LIMIT ?)""",
                (max(cfg.MAX_SIGNALS_DB, 1),),
            )
            removed += cur2.rowcount
            # Нерешённые старше окна оценки уже никогда не будут досуждены
            cur3 = await db.execute(
                "DELETE FROM signals WHERE outcome IS NULL AND ts < ?",
                ((datetime.utcnow() - timedelta(hours=72)).isoformat(),),
            )
            removed += cur3.rowcount
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
