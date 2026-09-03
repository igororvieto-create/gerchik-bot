import logging
import os
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional

import aiosqlite

from core.state import Signal, Position

log = logging.getLogger("db")
# Где живёт база. Приоритет: DB_PATH > том Railway ГДЕ БЫ ОН НИ БЫЛ
# смонтирован > <корень проекта>/data/signals.db (эфемерно, стирается при
# каждом деплое).
#
# Про том спрашиваем саму платформу: Railway выставляет
# RAILWAY_VOLUME_MOUNT_PATH с фактической точкой монтирования. Раньше здесь
# был зашит путь /data, и это стоило нам всей истории форвард-теста: тома
# не было вовсе, ветка уходила в запасной путь /app/data/signals.db, а он
# живёт внутри контейнера и пропадает при каждом рестарте. На дашборде это
# выглядело как «статистика стала хуже», а не как потеря данных.
#
# Зашитый /data оставлен запасным вариантом — на случай тома, смонтированного
# туда до появления этой переменной.
def _resolve_default_db() -> str:
    vol = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    for cand in (vol, "/data"):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK):
            return os.path.join(cand, "signals.db")
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "data", "signals.db")
    )


_DEFAULT_DB = _resolve_default_db()
# `or` вместо дефолта getenv: у getenv дефолт подставляется только для
# ОТСУТСТВУЮЩЕЙ переменной, а заданная пустой даёт "". sqlite3.connect("")
# открывает анонимную ВРЕМЕННУЮ базу, которая уничтожается при закрытии
# соединения, — а мы открываем новое соединение на каждый вызов. Итог:
# каждая операция уходила в свежую пустую базу, init_db рапортовал успех,
# и торговля шла без записи вообще. Тот же класс уже обезврежен для
# STRATEGY_ID; здесь урок применён не был.
DB_PATH = (os.getenv("DB_PATH") or "").strip() or _DEFAULT_DB


def _strategy_id() -> str:
    from core.config import cfg
    return cfg.STRATEGY_ID


# Условие «только текущая стратегия» для запросов статистики.
#
# Зачем: при переходе на другую стратегию её исходы нельзя складывать со
# старыми — получится среднее по двум разным вещам, и ни одно из них не
# будет измерено. Раньше единственным способом разделить их было СТЕРЕТЬ
# базу, то есть потерять и то, и другое.
#
# NULL считается своим для строк, записанных до появления ярлыка: миграция
# проставляет им текущий ярлык, но на всякий случай запрос это переживает.
_CUR_STRAT = "(strategy = ? OR strategy IS NULL)"


def is_ephemeral() -> bool:
    """True — база не переживёт деплой.

    Раньше об этом сообщала ОДНА строка в логе при старте. Её никто не
    видит: на дашборде статистика просто обнулялась после пуша, и это
    выглядело как «стратегия стала хуже», а не как потеря данных.
    Признак отдаётся в API, чтобы предупреждение висело на экране.

    Судим по ФАКТУ монтирования, а не по виду пути. Прежняя проверка
    «путь начинается с /data» врала в обе стороны: том, смонтированный в
    другое место, объявлялся эфемерным, а заданный вручную DB_PATH
    считался надёжным, даже когда указывал внутрь контейнера — ровно этот
    случай и стёр историю.
    """
    vol = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if vol:
        root = os.path.abspath(vol).rstrip(os.sep) + os.sep
        return not os.path.abspath(DB_PATH).startswith(root)
    if os.getenv("RAILWAY_ENVIRONMENT"):
        # Мы на Railway, а тома нет ни одного: что бы ни стояло в DB_PATH,
        # запись идёт внутрь контейнера и не переживёт рестарт.
        return True
    # Не на Railway: обычный диск, деплоев нет, терять нечего.
    return False


async def init_db() -> None:
    # Предупреждение здесь, а не на уровне модуля: на импорте оно (а) уходило
    # в stderr мимо настроенного в main.py формата, потому что basicConfig
    # ещё не отработал, и (б) срабатывало ложно, когда том смонтирован не в
    # /data, а путь задан через DB_PATH.
    if is_ephemeral():
        # Без тома вся история сделок и форвард-теста исчезает при каждом
        # деплое, а get_open_trades() после рестарта не находит своих позиций —
        # живые позиции бота получают ярлык MANUAL и лишаются защиты стопа.
        log.error(f"Том Railway НЕ подключён — база {DB_PATH} эфемерная, "
                  f"история и открытые сделки не переживут рестарт")
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
                    # Ярлык стратегии: статистика считается только по
                    # текущему, поэтому переход на новую стратегию
                    # разделяет результаты САМ, без стирания истории.
                    "strategy TEXT",
                    # mfe_r — максимальный ход в плюс в единицах R до исхода.
                    # Позволяет подбирать порог переноса стопа в безубыток по
                    # фактическим данным, а не по модели случайного блуждания.
                    "mfe_r REAL",
                    # Лента исполненных сделок: направленное «усилие» VSA.
                    # Пишется, но на решения пока не влияет — сначала
                    # проверяем на исходах, потом (и только потом) включаем.
                    "flow_delta REAL", "flow_span_min REAL", "flow_absorb INTEGER",
                    # Замеры из docs/LITERATURE.md §1 и §3. Как и лента:
                    # пишутся, но на решения НЕ влияют, пока срез по исходам
                    # не покажет разницу в ev_r.
                    # ob_ratio — числовой перекос стакана (в ob_bias лежала
                    # только корзина); confidence — доля согласных голосов,
                    # ею ограничивается score, но сам кап на исходах никогда
                    # не проверялся; round_dist_atr — дистанция до круглого
                    # числа, механизм Osler (2003), которого нет в нашем
                    # фрактальном поиске уровней.
                    "ob_ratio REAL", "confidence REAL",
                    # round_dist_atr остаётся в старых базах как мёртвая
                    # колонка: она хранит заведомо испорченный замер (мерила
                    # ведущую цифру цены, разбор в scanner._round_number_pos).
                    # Новый замер пишется в round_pos, старую не читаем.
                    "round_dist_atr REAL", "round_pos REAL",
                    # candle_ts — метка 4h-свечи сетапа. Дедуп «один сетап =
                    # один сигнал» жил ТОЛЬКО в памяти (_SIGNALLED_CANDLE), и
                    # каждый деплой обнулял его: та же свеча сигналила заново.
                    # Три деплоя внутри одной свечи — четыре строки на один
                    # сетап, и все четыре считались независимыми исходами.
                    "candle_ts INTEGER"]:
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
    # Строки, записанные до появления ярлыка, принадлежат текущей
    # стратегии — она с тех пор не менялась. Без этого первый же запрос
    # статистики после обновления показал бы ноль.
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE signals SET strategy = ? WHERE strategy IS NULL",
                (_strategy_id(),))
            await db.commit()
    except Exception as e:
        log.error(f"strategy backfill error: {e}")
    # САМОПРОВЕРКА. Создание таблиц могло «удаться» в базу, которая не
    # переживает закрытие соединения (пустой путь, /dev/null, каталог без
    # права записи). Открываем НОВОЕ соединение и убеждаемся, что таблицы
    # видны из него: только это доказывает, что данные где-то лежат.
    #
    # Бросаем, а не логируем: main.py на исключении init_db останавливает
    # торговлю. Молчаливый успех здесь означал бы торговлю без записи
    # сигналов и без усыновления живых позиций — то есть без стопов.
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('signals','trades')") as cur:
            found = {r[0] for r in await cur.fetchall()}
    missing = {"signals", "trades"} - found
    if missing:
        raise RuntimeError(
            f"база по пути {DB_PATH!r} не сохраняет данные: после создания "
            f"таблиц из нового соединения не видны {sorted(missing)}. "
            f"Проверь DB_PATH и права на каталог.")
    log.info(f"DB initialised at {DB_PATH} (стратегия {_strategy_id()})")


async def save_signal(sig: Signal) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO signals
                   (symbol, signal_type, direction, score, price,
                    oi_change, vol_ratio, funding, ob_bias, atr_pct, details,
                    entry, sl, tp1, tp2, tp3, rr, sl_pct, headroom,
                    flow_delta, flow_span_min, flow_absorb,
                    ob_ratio, confidence, round_pos, candle_ts, strategy, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.symbol, sig.signal_type, sig.direction, sig.score, sig.price,
                 sig.oi_change, sig.vol_ratio, sig.funding, sig.ob_bias, sig.atr_pct,
                 sig.details,
                 sig.entry, sig.sl, sig.tp1, sig.tp2, sig.tp3, sig.rr, sig.sl_pct,
                 sig.headroom,
                 sig.flow_delta, sig.flow_span_min, int(sig.flow_absorb),
                 sig.ob_ratio, sig.confidence, sig.round_pos, sig.candle_ts,
                 _strategy_id(), sig.ts.isoformat()),
            )
            await db.commit()
    except Exception as e:
        log.error(f"save_signal error: {e}")


async def save_trade_open(pos: Position) -> bool:
    """True — строка «позиция бота» существует в trades после этого вызова.

    Возврат обязателен: строка в trades — ЕДИНСТВЕННЫЙ признак «своя
    позиция» после рестарта. Когда провал записи был не виден вызывающему,
    живая позиция бота после ближайшего деплоя усыновлялась как MANUAL, а
    ручным позициям монитор принципиально не досылает стоп — то есть
    рецидивирующий баг №1 возвращался через слой данных.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Частичный уникальный индекс покрывает только непустой order_id.
            # Для позиций без него (реконсиляция дубликата 110072,
            # восстановленные) проверяем вручную, иначе повторный вызов
            # плодит строку, и "лишняя" open-строка потом заставляет бота
            # считать чужую позицию своей.
            if not pos.order_id:
                async with db.execute(
                    "SELECT id FROM trades WHERE symbol=? AND status='open' "
                    "ORDER BY opened_at DESC LIMIT 1",
                    (pos.symbol,),
                ) as cur:
                    hit = await cur.fetchone()
                if hit:
                    # Строка уже есть — но она может описывать ПРЕДЫДУЩУЮ
                    # сделку по этому символу, чья запись закрытия провалилась
                    # и оставила status='open'. Раньше здесь стоял голый
                    # `return True`, и такая строка доживала до рестарта: при
                    # усыновлении вход, стоп и объём подтягиваются с биржи, а
                    # ЦЕЛИ, score и order_id берутся из строки — то есть
                    # позиция получала чужой TP2 и чужой order_id, по которому
                    # потом искался PnL закрытия.
                    #
                    # Переписываем строку под живую позицию: одна открытая
                    # строка на символ, и она описывает то, что есть сейчас.
                    await db.execute(
                        """UPDATE trades SET side=?, entry=?, sl=?, tp1=?, tp2=?,
                                             tp3=?, qty=?, score=?, signal_type=?,
                                             opened_at=?
                           WHERE id=?""",
                        (pos.side, pos.entry, pos.sl, pos.tp1, pos.tp2, pos.tp3,
                         pos.qty, pos.score, pos.signal_type,
                         pos.ts.isoformat(), hit[0]),
                    )
                    await db.commit()
                    return True
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
        return True
    except Exception as e:
        log.error(f"save_trade_open error: {e}")
        return False


# Три исхода записи закрытия. Булева мало: «не удалось записать» требует
# ПОВТОРА, а «переводить нечего» повтора требует ровно наоборот — иначе
# позиция навсегда остаётся под наблюдением и держит слот. Раньше оба
# случая сливались в False, а докстрока обещала проверку, которой не было:
# True возвращался при любом успешном commit, включая UPDATE на 0 строк.
CLOSE_OK = "closed"       # строка переведена — PnL учитывать
CLOSE_FAILED = "failed"   # запись не удалась — повторить на следующем тике
CLOSE_ABSENT = "absent"   # открытой строки нет — PnL НЕ учитывать, НЕ повторять


async def save_trade_close(pos: Position, exit_price: float = 0.0,
                           pnl: float = 0.0) -> str:
    """CLOSE_OK — PnL этой сделки ЗАПИСАН и его обязан учесть вызывающий.

    CLOSE_ABSENT возвращается ТОЛЬКО когда PnL уже учтён кем-то другим, то
    есть существует терминальная строка с непустым pnl. Раньше сюда же
    попадали «строки вообще нет» и «строка запечатана как stale с pnl
    IS NULL» — в обоих случаях не учёл НИКТО, и убыток исчезал из дневного
    предохранителя навсегда. Оба случая теперь самоисцеляются: строка
    дописывается, и функция возвращает CLOSE_OK.

    Цель — убрать класс целиком, а не отдельные его проявления:
      * запись при входе провалилась (строки нет);
      * close_stale_open_trades запечатал строку, пока учёт был отложен;
      * позиция восстановлена и своей строки не имела.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            now = datetime.utcnow().isoformat()
            # status IN ('open','stale') И pnl IS NULL: stale-строка ещё не
            # учтена (её запечатал сторож зависших), и терять её нельзя.
            # Порядок «stale ПОСЛЕ закрытий» в мониторе перестал спасать,
            # когда учёт научился откладываться на следующий тик.
            if pos.order_id:
                cur = await db.execute(
                    """UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
                       WHERE symbol=? AND order_id=?
                         AND status IN ('open','stale') AND pnl IS NULL""",
                    (exit_price, pnl, now, pos.symbol, pos.order_id),
                )
            else:
                # Без order_id закрываем только САМУЮ СВЕЖУЮ подходящую
                # строку: общий WHERE проштамповал бы этим pnl все зависшие.
                cur = await db.execute(
                    """UPDATE trades SET status='closed', exit_price=?, pnl=?, closed_at=?
                       WHERE id = (SELECT id FROM trades
                                   WHERE symbol=? AND status IN ('open','stale')
                                     AND pnl IS NULL
                                   ORDER BY opened_at DESC LIMIT 1)""",
                    (exit_price, pnl, now, pos.symbol),
                )
            moved = cur.rowcount
            if moved < 1:
                # Обновлять нечего. Различаем два РАЗНЫХ случая.
                if pos.order_id:
                    q = ("SELECT 1 FROM trades WHERE symbol=? AND order_id=? "
                         "AND pnl IS NOT NULL LIMIT 1")
                    args: tuple = (pos.symbol, pos.order_id)
                else:
                    q = ("SELECT 1 FROM trades WHERE symbol=? AND pnl IS NOT NULL "
                         "AND closed_at >= ? LIMIT 1")
                    args = (pos.symbol, pos.ts.isoformat())
                async with db.execute(q, args) as c2:
                    already = await c2.fetchone()
                if already:
                    await db.commit()
                    log.info(f"{pos.symbol}: закрытие уже учтено другим путём")
                    return CLOSE_ABSENT
                # Строки нет вовсе — дописываем терминальную. Иначе PnL
                # исчезал молча: вызывающий трактовал ABSENT как «учтено».
                await db.execute(
                    """INSERT INTO trades
                       (symbol, side, entry, exit_price, sl, tp1, tp2, tp3, qty,
                        pnl, score, signal_type, order_id, status, opened_at, closed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?,?)""",
                    (pos.symbol, pos.side, pos.entry, exit_price, pos.sl,
                     pos.tp1, pos.tp2, pos.tp3, pos.qty, pnl, pos.score,
                     pos.signal_type, pos.order_id, pos.ts.isoformat(), now),
                )
                await db.commit()
                log.warning(f"{pos.symbol}: строки сделки не было — записана "
                            f"терминальной, pnl={pnl:+.2f} учтён")
                return CLOSE_OK
            await db.commit()
        return CLOSE_OK
    except Exception as e:
        log.error(f"save_trade_close error: {e}")
        return CLOSE_FAILED


async def get_recent_candle_marks(hours: int = 8) -> Dict[str, int]:
    """symbol -> метка последней отсигналенной 4h-свечи.

    Дедуп «один сетап = один сигнал» жил только в памяти сканера, поэтому
    каждый деплой Railway (то есть каждый пуш) обнулял его: та же закрытая
    свеча сигналила заново и писалась ещё одной строкой. Соседние деплои
    внутри одной 4-часовой свечи давали несколько строк на ОДИН сетап, и
    get_outcome_breakdown считал их независимыми наблюдениями — прямо
    против LITERATURE §5 и §0-А п.6, где n означает независимые исходы.

    Окно 8 часов: свеча 4h плюс запас. БРОСАЕТ при ошибке — пустой словарь
    здесь означал бы «дедупа нет», то есть тихое возвращение дублей.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT symbol, MAX(candle_ts) FROM signals
               WHERE ts >= ? AND candle_ts IS NOT NULL AND candle_ts > 0
               GROUP BY symbol""",
            (cutoff,),
        ) as cur:
            return {row[0]: int(row[1]) for row in await cur.fetchall()}


async def history_span() -> Dict:
    """Сколько истории реально лежит в базе прямо сейчас.

    Признак is_ephemeral() смотрит на ПУТЬ и может ошибаться: том бывает
    смонтирован не в /data, а DB_PATH может указывать куда угодно. Здесь —
    факт вместо догадки: сколько сигналов и насколько стар самый старый.

    Нужно потому, что потеря базы выглядит на дашборде как «стратегия
    испортилась»: у владельца статистика за ночь превратилась из 0W/13L в
    1W/3L, и понять по экрану, что это стёртые данные, а не новые сделки,
    было нельзя.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT COUNT(*), MIN(ts) FROM signals") as cur:
                row = await cur.fetchone()
        if row is None:            # COUNT всегда даёт строку, но не полагаемся
            return {"rows": 0, "oldest_ts": None, "age_hours": None}
        rows = int(row[0] or 0)
        oldest = row[1]
        age_h = None
        if oldest:
            try:
                age_h = (datetime.utcnow()
                         - datetime.fromisoformat(oldest)).total_seconds() / 3600
            except ValueError:
                age_h = None
        return {"rows": rows, "oldest_ts": oldest, "age_hours": age_h}
    except Exception as e:
        log.error(f"history_span error: {e}")
        return {"rows": -1, "oldest_ts": None, "age_hours": None}


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
        # БРОСАЕМ, а не возвращаем []: пустой список читается вызывающим как
        # «оценивать нечего», и форвард-тест — единственное основание
        # включать реальные деньги — вставал бы полностью и БЕСШУМНО, пока
        # дашборд показывает старую статистику. get_open_trades и
        # get_realized_pnl_since этот урок уже усвоили (рецидив №5 в
        # docs/REVIEW.md), get_pending_signals — нет.
        log.error(f"get_pending_signals error: {e}")
        raise


async def set_signal_outcome(signal_id: int, outcome: str, price: float,
                             mfe_r: float = 0.0) -> bool:
    """True — вердикт ДЕЙСТВИТЕЛЬНО записан.

    Возврат обязателен: оценщик считал `decided += 1` безусловно, и при
    полном диске в лог уходило «5 outcome(s) recorded» при нуле строк в
    базе. Оператор принимает решение о реальных деньгах по этой цифре."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE signals SET outcome=?, outcome_price=?, outcome_at=?, mfe_r=? "
                "WHERE id=?",
                (outcome, price, datetime.utcnow().isoformat(), mfe_r, signal_id),
            )
            await db.commit()
        return True
    except Exception as e:
        log.error(f"set_signal_outcome error: {e}")
        return False


# Круговые издержки: тейкер 0.055% × 2 + типичное проскальзывание.
# В единицах R это FEE_PCT / sl_pct, то есть для узкого стопа они В РАЗЫ
# тяжелее: при стопе 0.7% это 0.19R, при 7% — 0.019R. Без этой поправки
# срез by_sl_atr смещён в пользу узких стопов на ~0.15R — больше любого
# правдоподобного реального эффекта, и сравнивать корзины нельзя.
ROUND_TRIP_FEE_PCT = 0.13

# Сколько выплат фандинга в среднем застаёт сделка.
#
# Замер по 2842 сделкам прогона: держим медианно 7.8 часа (полное окно 48ч
# доживает лишь 18%), выплаты идут каждые 8 часов, медиана — 2 выплаты.
# Берём медиану, а не подогнанное среднее 1.62: круглое число из структуры
# данных, а не десятичная дробь, подобранная на этой же выборке. Оценка
# получается слегка ПЕССИМИСТИЧНЕЕ факта (-0.0056R против -0.0046R), и это
# правильная сторона ошибки для оценки ожидания.
FUNDING_SETTLEMENTS = 2


def funding_r(funding_pct: Optional[float], direction: Optional[str],
              sl_pct: Optional[float]) -> Optional[float]:
    """Фандинг за сделку в единицах R. ПЛЮС = получаем, минус = платим.

    Учитывать его обязательно: позиция живёт часами и пересекает выплаты.
    Величина мала — в среднем -0.005R против -0.036R комиссий, то есть
    примерно восьмая часть, — но знак у неё не всегда отрицательный:
    контрарный голос по фандингу ставит нас на ПРИНИМАЮЩУЮ сторону в 48%
    сделок против 29% платящих.

    Ставка положительна, когда лонги платят шортам, поэтому лонг её платит,
    а шорт получает. Как и комиссия, в R она зависит от ширины стопа.

    Живой реализованный PnL это уже учитывает сам: у Bybit
    Closed P&L = P&L позиции - комиссия открытия - комиссия закрытия -
    сумма фандинга. Здесь считается ТЕОРЕТИЧЕСКОЕ ожидание по меткам
    исходов, где вычитались только комиссии.
    """
    if funding_pct is None or not sl_pct or sl_pct <= 0 or not direction:
        return None
    sign = 1.0 if direction == "LONG" else -1.0
    return -sign * funding_pct * FUNDING_SETTLEMENTS / sl_pct


def _ev(slot: Dict, sl_pct: Optional[float] = None,
        fee_r: Optional[float] = None, fund_r: Optional[float] = None) -> Dict:
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
    if not total:
        out["ev_r"] = None
        out["ev_gross_r"] = None
        return out
    gross = (win * 2.0 + be * 0.0 + loss * -1.0) / total
    out["ev_gross_r"] = round(gross, 3)
    # Комиссия вычитается по СРЕДНЕМУ стопу корзины: в R она зависит от
    # ширины стопа, поэтому одна константа для всех корзин снова дала бы
    # смещение.
    if fee_r is None:
        fee_r = (ROUND_TRIP_FEE_PCT / sl_pct) if (sl_pct and sl_pct > 0) else 0.0
    # Фандинг со СВОИМ знаком: он бывает и доходом, поэтому прибавляется,
    # а не вычитается. Отсутствие данных — это 0.0, а не «дохода нет»:
    # величина мала, и подставлять сюда пессимизм значило бы врать в
    # другую сторону.
    fund = fund_r or 0.0
    out["ev_r"] = round(gross - fee_r + fund, 3)
    out["fee_r"] = round(fee_r, 3)
    out["funding_r"] = round(fund, 4)
    return out


async def get_outcome_stats(days: int = 7) -> Dict:
    """Forward-test scoreboard: how many signals hit TP2 before SL and vice versa."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    stats = {"win": 0, "loss": 0, "be": 0, "expired": 0, "open": 0,
             "winrate": None, "ev_r": None}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Средний стоп по РЕШЁННЫМ сигналам: без него _ev не может
            # вычесть комиссии, и карточка показывала бы БРУТТО, тогда как
            # срезы показывают НЕТТО. Одно имя поля — две разные величины
            # на одном экране, вплоть до смены знака (+0.05R зелёным в
            # шапке против -0.14R красным в срезе по тем же сделкам).
            async with db.execute(
                f"""SELECT AVG(1.0 / sl_pct) FROM signals
                    WHERE ts >= ? AND outcome IN ('WIN','LOSS','BE')
                      AND sl_pct IS NOT NULL AND sl_pct > 0
                      AND {_CUR_STRAT}""",
                (cutoff, _strategy_id()),
            ) as cur_sl:
                row_sl = await cur_sl.fetchone()
            # AVG(1/sl), а не 1/AVG(sl): усреднение обратной величины —
            # единственный корректный способ, иначе издержки занижаются.
            # И только по РЕШЁННЫМ: просрочки в знаменатель EV не входят.
            avg_fee = (ROUND_TRIP_FEE_PCT * float(row_sl[0])
                       if (row_sl and row_sl[0]) else None)
            # Фандинг усредняется ПОСТРОЧНО и по тем же решённым сигналам:
            # в R он делится на свой стоп, поэтому 1/sl нельзя выносить за
            # среднее (то же неравенство Йенсена, что и с комиссией).
            async with db.execute(
                f"""SELECT AVG((CASE WHEN direction = 'LONG' THEN -funding
                                     ELSE funding END) * ? / sl_pct)
                    FROM signals
                    WHERE ts >= ? AND outcome IN ('WIN','LOSS','BE')
                      AND sl_pct IS NOT NULL AND sl_pct > 0
                      AND funding IS NOT NULL
                      AND {_CUR_STRAT}""",
                (FUNDING_SETTLEMENTS, cutoff, _strategy_id()),
            ) as cur_fd:
                row_fd = await cur_fd.fetchone()
            avg_fund = float(row_fd[0]) if (row_fd and row_fd[0] is not None) else None
            async with db.execute(
                f"""SELECT COALESCE(outcome, 'OPEN') o, COUNT(*) c
                    FROM signals
                    WHERE ts >= ? AND {_CUR_STRAT} GROUP BY o""",
                (cutoff, _strategy_id()),
            ) as cur:
                for o, c in await cur.fetchall():
                    if o == "WIN":       stats["win"] = c
                    elif o == "LOSS":    stats["loss"] = c
                    elif o == "BE":      stats["be"] = c
                    elif o == "EXPIRED": stats["expired"] = c
                    else:                stats["open"] = c
        stats.update(_ev(stats, fee_r=avg_fee, fund_r=avg_fund))
        return stats
    except Exception as e:
        log.error(f"get_outcome_stats error: {e}")
        return stats


async def get_outcome_breakdown(days: int = 7) -> Dict:
    """Winrate sliced by score bucket / direction / signal type + last decided.
    The go/no-go analysis tool: shows WHERE the strategy wins or loses."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    out: Dict = {"by_score": {}, "by_direction": {}, "by_type": {},
                 "by_sl_atr": {}, "by_headroom": {}, "by_flow": {},
                 "by_ob": {}, "by_round": {}, "recent": []}
    # Явный порядок корзин. Фронт сортировал ключи лексикографически, а '<'
    # (0x3C) и '>' (0x3E) больше цифр — крайние корзины уезжали в середину и
    # хвост, ось переставала быть монотонной по ширине стопа. Именно её
    # монотонность и есть весь смысл среза: растёт ли винрейт с шириной.
    out["_order"] = {
        "by_flow":     ["продавцы <-0.2", "нейтрально", "покупатели >+0.2",
                        "поглощение", "лента <1 мин"],
        "by_score":    ["30-44", "45-59", "60+"],
        "by_sl_atr":   ["<1.0 ATR", "1.0-1.5", "1.5-2.5", ">2.5 ATR"],
        "by_headroom": ["1.5-2.0R (не торгуется)", "2.0-3.0R", ">3.0R"],
        "by_ob":       ["стакан за", "стакан нейтр.", "стакан против"],
        "by_round":    ["круглое ниже", "на круглом", "круглое выше"],
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

    _FLOW_MIN_SPAN_MIN = 1.0   # лента короче минуты ничего не говорит о 48 часах

    def _flow_bucket(delta, absorb, span_min) -> Optional[str]:
        """Срез по направленному усилию из ленты сделок.

        Проверяет ровно один вопрос: предсказывает ли перевес агрессора
        исход сделки. Если срезы «покупатели» и «продавцы» дают одинаковое
        матожидание — метрика бесполезна и её не надо встраивать в скор.
        """
        if delta is None:
            return None
        # Охват обязан учитываться: 500 сделок у ликвидной монеты — это
        # секунды, у неликвида — часы. Слишком короткая лента попадает в
        # отдельную корзину, а не смешивается с содержательными.
        if not span_min or span_min < _FLOW_MIN_SPAN_MIN:
            return "лента <1 мин"
        if absorb:
            return "поглощение"
        if delta <= -0.2:
            return "продавцы <-0.2"
        if delta >= 0.2:
            return "покупатели >+0.2"
        return "нейтрально"

    def _ob_bucket(ob_bias, direction) -> Optional[str]:
        """Согласен ли перекос стакана с направлением сделки.

        Стакан — один из четырёх голосов в _direction, то есть при равенстве
        он способен ЗАДАТЬ направление. Литературной опоры под ним нет:
        Cont/Kukanov/Stoikov (2014) меряют поток событий на лучших котировках,
        а мы — статический снимок 20 уровней, это разные величины
        (docs/LITERATURE.md §3).

        Срез отвечает данными: если «стакан за» и «стакан против» дают
        одинаковое ev_r, голос не несёт информации. Работает и на СТАРЫХ
        строках — ob_bias пишется с самого начала.
        """
        if not ob_bias or ob_bias == "NEUTRAL":
            return "стакан нейтр."
        side = "LONG" if ob_bias == "BUY" else "SHORT"
        return "стакан за" if side == direction else "стакан против"

    def _round_bucket(p) -> Optional[str]:
        """Положение входа относительно круглого числа (Osler 2003).

        Знак существенен: круглое ВЫШЕ цены — это чужие тейк-профиты
        (тормоз), круглое НИЖЕ — чужие стопы (ускорение при пробое).
        Беззнаковая корзина складывала два противоположных эффекта и
        гасила их друг о друга.

        Величина нормирована на шаг сетки, а не на ATR: деление на ATR
        превращало метрику в измеритель ведущей цифры цены (разбор в
        scanner._round_number_pos).
        """
        if p is None:
            return None
        if abs(p) < 0.2:
            return "на круглом"
        return "круглое выше" if p > 0 else "круглое ниже"

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
                          sl_pct, atr_pct, headroom, funding,
                          flow_delta, flow_absorb, flow_span_min,
                          ob_bias, round_pos
                   FROM signals WHERE outcome IS NOT NULL AND ts >= ?
                     AND """ + _CUR_STRAT + """
                   ORDER BY ts DESC""",
                (cutoff, _strategy_id()),
            ) as cur:
                rows = await cur.fetchall()

        def _acc(d: Dict, key: str, outcome: str, sl_pct=None,
                 fund=None) -> None:
            slot = d.setdefault(key, {"win": 0, "loss": 0, "be": 0, "expired": 0,
                                      "_fee_sum": 0.0, "_fee_n": 0,
                                      "_fund_sum": 0.0, "_fund_n": 0})
            k = outcome.lower()
            if k in slot:
                slot[k] += 1
            # Комиссия копится ПОСТРОЧНО и только по РЕШЁННЫМ исходам.
            # Усреднение sl_pct занижало издержки: E[1/sl] > 1/E[sl]
            # (неравенство Йенсена), а EXPIRED-строки, которых нет в
            # знаменателе матожидания, тянули среднее вверх. На корзине
            # «10 решённых со стопом 0.7% + 90 просроченных со стопом 5%»
            # занижение доходило до 6.6 раза — больше самого эффекта.
            if sl_pct and sl_pct > 0 and k in ("win", "loss", "be"):
                slot["_fee_sum"] += ROUND_TRIP_FEE_PCT / sl_pct
                slot["_fee_n"] += 1
            if fund is not None and k in ("win", "loss", "be"):
                slot["_fund_sum"] += fund
                slot["_fund_n"] += 1

        for r in rows:
            _sl = r["sl_pct"]
            _fd = funding_r(r["funding"], r["direction"], _sl)
            _acc(out["by_score"], _bucket(r["score"]), r["outcome"], _sl, _fd)
            _acc(out["by_direction"], r["direction"], r["outcome"], _sl, _fd)
            _acc(out["by_type"], r["signal_type"], r["outcome"], _sl, _fd)
            slb = _sl_bucket(r["sl_pct"], r["atr_pct"])
            if slb:
                _acc(out["by_sl_atr"], slb, r["outcome"], _sl, _fd)
            hrb = _hr_bucket(r["headroom"])
            if hrb:
                _acc(out["by_headroom"], hrb, r["outcome"], _sl, _fd)
            fb = _flow_bucket(r["flow_delta"], r["flow_absorb"], r["flow_span_min"])
            if fb:
                _acc(out["by_flow"], fb, r["outcome"], _sl, _fd)
            obb = _ob_bucket(r["ob_bias"], r["direction"])
            if obb:
                _acc(out["by_ob"], obb, r["outcome"], _sl, _fd)
            rb = _round_bucket(r["round_pos"])
            if rb:
                _acc(out["by_round"], rb, r["outcome"], _sl, _fd)

        for d in (out["by_score"], out["by_direction"], out["by_type"],
                  out["by_sl_atr"], out["by_headroom"], out["by_flow"],
                  out["by_ob"], out["by_round"]):
            for slot in d.values():
                fee = (slot["_fee_sum"] / slot["_fee_n"]) if slot["_fee_n"] else None
                fnd = (slot["_fund_sum"] / slot["_fund_n"]) if slot["_fund_n"] else None
                slot.update(_ev(slot, fee_r=fee, fund_r=fnd))
                for k_tmp in ("_fee_sum", "_fee_n", "_fund_sum", "_fund_n"):
                    slot.pop(k_tmp, None)

        out["recent"] = [
            {"symbol": r["symbol"], "score": r["score"], "dir": r["direction"],
             "type": r["signal_type"], "outcome": r["outcome"], "ts": r["ts"]}
            for r in list(rows)[:25]
        ]
        return out
    except Exception as e:
        log.error(f"get_outcome_breakdown error: {e}")
        return out


async def update_trade_entry(order_id: str, symbol: str, entry: float) -> None:
    """Заменить цену входа фактической ценой залива.

    save_trade_open пишется ДО верификации (чтобы усыновление после
    рестарта опознало позицию), поэтому там лежит цена сигнала. Без этой
    поправки любой последующий разбор в R по таблице trades систематически
    смещён на величину проскальзывания."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            if order_id:
                cur = await db.execute(
                    "UPDATE trades SET entry=? WHERE order_id=? AND status='open'",
                    (entry, order_id))
            else:
                # Только САМАЯ СВЕЖАЯ открытая строка: без order_id (ветка
                # дубликата 110072 и потерянного ответа) общий WHERE
                # проставлял цену чужого залива всем открытым строкам
                # символа. save_trade_close рядом делает так же и объясняет
                # почему — новая функция этот урок не переняла.
                cur = await db.execute(
                    """UPDATE trades SET entry=?
                       WHERE id = (SELECT id FROM trades
                                   WHERE symbol=? AND status='open'
                                   ORDER BY opened_at DESC LIMIT 1)""",
                    (entry, symbol))
            touched = cur.rowcount
            await db.commit()
        if touched < 1:
            # Молчаливый no-op: в trades.entry навсегда остаётся цена
            # СИГНАЛА вместо цены залива, и весь последующий разбор в R
            # систематически смещён на величину проскальзывания — ровно то,
            # ради чего эта функция и написана.
            log.warning(f"{symbol}: фактическая цена входа не записана "
                        f"(нет открытой строки, order_id={order_id or '-'}) — "
                        f"разбор в R будет смещён на проскальзывание")
    except Exception as e:
        log.error(f"update_trade_entry error: {e}")


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
    # Агрегат всегда даёт ровно одну строку, но проверка явная: молчаливый
    # TypeError здесь ушёл бы в _ensure_daily_state и остановил торговлю
    # с невнятной причиной в логе.
    return float(row[0] or 0.0) if row else 0.0


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


# Докуда дотягивается оценщик: _MAX_AGE_HOURS(48) * 3. Нерешённый сигнал
# старше этого срока вердикта уже не получит никогда, и держать его незачем.
_EVAL_REACH_HOURS = 144


# Цель замера III (docs/PREREGISTRATION.md): 130 решённых исходов с
# ПРИГОДНОЙ лентой. Считать прогресс обязательно, и вот почему: правило
# остановки задано числом n, а не результатом, поэтому смотреть на счётчик
# можно и нужно — в отличие от самих исходов по потоку, на которые смотрят
# ОДИН раз в конце.
FLOW_TARGET_N = 130


async def flow_progress() -> Dict:
    """Сколько исходов с пригодной лентой уже набрано.

    Отдельно считается «лента была, но короткая»: если этот счётчик растёт,
    а нужный стоит на нуле, значит TRADE_FLOW_LIMIT слишком мал и замер
    копит непригодные строки. Без этой цифры мы узнали бы об этом через
    восемь недель.
    """
    from core.config import cfg
    # Явная аннотация: без неё mypy выводит Dict[str, int | None] по
    # значению None у usable_share и запрещает положить туда долю.
    out: Dict[str, Any] = {"usable": 0, "too_short": 0, "no_tape": 0,
                           "decided_total": 0, "target": FLOW_TARGET_N,
                           "usable_share": None}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Считаем ТОЛЬКО торгуемые сигналы. Решение, ради которого идёт
            # замер, — ставить ли поток гейтом на сделки, которые бот берёт.
            # Мерить его на сигналах ниже TRADE_MIN_SCORE значит отвечать на
            # другой вопрос: из 25 пригодных по ленте торгуемыми оказались
            # ПЯТЬ, то есть замер шёл почти целиком по неторгуемым.
            async with db.execute(
                """SELECT
                     COUNT(*),
                     SUM(CASE WHEN flow_delta IS NOT NULL
                               AND flow_span_min >= 1.0 THEN 1 ELSE 0 END),
                     SUM(CASE WHEN flow_delta IS NOT NULL
                               AND (flow_span_min IS NULL OR flow_span_min < 1.0)
                              THEN 1 ELSE 0 END),
                     SUM(CASE WHEN flow_delta IS NULL THEN 1 ELSE 0 END)
                   FROM signals
                   WHERE outcome IN ('WIN','LOSS','BE')
                     AND score >= ?
                     AND """ + _CUR_STRAT, (cfg.TRADE_MIN_SCORE, _strategy_id(),)
            ) as cur:
                row = await cur.fetchone()
        if row:
            out["decided_total"] = int(row[0] or 0)
            out["usable"] = int(row[1] or 0)
            out["too_short"] = int(row[2] or 0)
            out["no_tape"] = int(row[3] or 0)
            # Доля пригодных — главный признак СМЕЩЕНИЯ, а не просто
            # медленного накопления. Отбраковка по длине ленты не случайна:
            # она выбрасывает самые бурные сигналы. Пока доля высокая,
            # смещение мало; падает — замер меряет не ту популяцию.
            if out["decided_total"]:
                out["usable_share"] = out["usable"] / out["decided_total"]
    except Exception as e:
        log.error(f"flow_progress error: {e}")
    return out


async def cleanup_old_signals(keep_hours: int = 192) -> int:
    """Чистка. РЕШЁННЫЕ сигналы по возрасту НЕ удаляются — никогда.

    Здесь стояло `DELETE FROM signals WHERE ts < cutoff` без оговорок, и оно
    сносило исходы вместе со всем остальным. Форвард-тест от этого упирался
    в потолок: при 17 решённых в неделю и горизонте 8 дней в базе physически
    не могло накопиться больше ~19 исходов, сколько бы месяцев бот ни
    работал. Для вывода нужно ~130 — то есть замер не мог завершиться
    НИКОГДА, и по цифрам на экране это выглядело бы нормально: свежие
    исходы есть, счётчик живой.

    Решённый исход — это накопленный результат наблюдения, а не мусор. Его
    объём ограничивается ЧИСЛОМ строк (MAX_SIGNALS_DB), а не возрастом: при
    5000 строк и текущем темпе это годы истории.

    keep_hours теперь относится только к сигналам БЕЗ вердикта, и не может
    оказаться короче досягаемости оценщика.
    """
    from core.config import cfg
    stale_h = max(int(keep_hours), _EVAL_REACH_HOURS)
    cutoff = (datetime.utcnow() - timedelta(hours=stale_h)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            removed = 0
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
            # Нерешённые старше досягаемости оценщика вердикта уже не
            # получат. Порог не может быть короче 144 ч: оценщик запрашивает
            # нерешённые именно за это окно, и при более короткой чистке
            # половина запрошенного окна физически не существовала — после
            # его простоя строки исчезали БЕЗ вердикта, молча уменьшая n.
            cur3 = await db.execute(
                "DELETE FROM signals WHERE outcome IS NULL AND ts < ?",
                (cutoff,),
            )
            removed += cur3.rowcount
            # Also purge closed trades older than 90 days
            old_trades = (datetime.utcnow() - timedelta(days=90)).isoformat()
            await db.execute(
                # status IN ('closed','stale'): сторож зависших ставит
                # 'stale', и такие строки не удалялись НИКОГДА — таблица
                # росла без предела. Функционально безвредно (get_open_trades
                # фильтрует по 'open'), но это утечка на годы работы.
                "DELETE FROM trades WHERE status IN ('closed','stale') "
                "AND closed_at < ?", (old_trades,)
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
