import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import List

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from core.config import cfg
from datetime import datetime

from core.state import state
from core import db
from exchange.bybit import BybitClient
from strategy.scanner import run_scan_and_broadcast
from strategy.trader import monitor_positions
from strategy.evaluator import evaluate_signal_outcomes
from api.routes import router

# stdout, не stderr: Railway помечает весь stderr как severity=error,
# из-за чего обычные INFO-строки выглядят в логах как ошибки
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,   # сносит хендлеры, поставленные библиотеками при импорте (apscheduler и др.)
)
log = logging.getLogger("main")

_client: BybitClient | None = None
_scheduler: AsyncIOScheduler | None = None


async def _fetch_webshare_proxies() -> List[str]:
    token = os.getenv("WEBSHARE_API_TOKEN", "").strip()
    if not token:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page_size=25",
                headers={"Authorization": f"Token {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                # HTTP-статус проверяется ОБЯЗАТЕЛЬНО. Без него 401 (токен
                # протух) и 402 (квота выжжена) давали пустой results, а в
                # логе стояло безмятежное «loaded 0 proxies». Отличить
                # «прокси не нужны» от «доступ к ним потерян» было нельзя, а
                # цепочка дальше жёсткая: нет прокси -> прямое соединение с
                # IP Railway -> гео-блок Bybit 403 -> get_positions() = None
                # -> монитор перестаёт проверять наличие стопа, то есть
                # рецидивирующий баг №1.
                if r.status != 200:
                    body = (await r.text())[:200]
                    log.error(f"Webshare API вернул {r.status}: {body} — "
                              f"прокси НЕ получены, соединение будет прямым")
                    return []
                data = await r.json()
        results = data.get("results", [])
        proxies = []
        for p in results:
            if p.get("valid"):
                url = (f"http://{p['username']}:{p['password']}"
                       f"@{p['proxy_address']}:{p['port']}")
                proxies.append(url)
        if not proxies:
            log.error(f"Webshare: получено {len(results)} записей, валидных 0 — "
                      f"соединение будет прямым, вероятен гео-блок Bybit")
        else:
            log.info(f"Webshare: loaded {len(proxies)} proxies")
        return proxies
    except Exception as e:
        log.warning(f"Webshare API fetch failed: {e}")
        return []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _scheduler

    # Провал init_db больше не «continuing anyway» без последствий: без
    # миграции save_signal падает на «no such column» и ГЛУШИТ ошибку, то
    # есть бот торгует, не записывая ни одного сигнала, а форвард-тест —
    # единственное основание включать реальные деньги — молча пуст.
    # Торговлю в этом случае держим остановленной до успешной инициализации.
    try:
        await db.init_db()
        log.info("DB ready")
    except Exception as e:
        state.trading_halted = True
        state.halt_reason = "db_init_failed"
        log.critical(f"init_db провалилась ({e}) — торговля остановлена: без "
                     f"миграции сигналы не пишутся и форвард-тест пуст")

    webshare_proxies: List[str] = []
    try:
        webshare_proxies = await _fetch_webshare_proxies()
    except Exception as e:
        log.warning(f"Webshare fetch failed: {e}")

    _client = BybitClient(cfg.BYBIT_API_KEY, cfg.BYBIT_SECRET,
                          extra_proxies=webshare_proxies)
    state.client = _client
    log.info(f"AUTO_TRADE={'ON' if cfg.AUTO_TRADE else 'OFF'} "
             f"api_key={'set' if cfg.BYBIT_API_KEY else 'not set'}")

    _scheduler = AsyncIOScheduler(timezone="UTC")
    # misfire_grace_time: по умолчанию APScheduler пропускает задачу, если
    # опоздал больше чем на 1 секунду. Тяжёлый скан блокирует цикл событий, и
    # тик монитора (проверка наличия SL у живых позиций!) молча терялся.
    _scheduler.add_job(_scan_job,    "interval", minutes=cfg.SCAN_INTERVAL_MIN, id="scan",
                       max_instances=1, misfire_grace_time=120)
    _scheduler.add_job(_monitor_job, "interval", seconds=30,                    id="monitor",
                       max_instances=1, misfire_grace_time=60)
    _scheduler.add_job(_cleanup_job, "cron",     hour="*/6",                    id="cleanup",
                       misfire_grace_time=3600)
    _scheduler.add_job(_outcome_job, "interval", minutes=30,                    id="outcomes",
                       max_instances=1, misfire_grace_time=600)
    _scheduler.start()
    log.info(f"Scheduler started — scan every {cfg.SCAN_INTERVAL_MIN} min")

    # Держим ссылку на таск: голый create_task() может быть собран GC до
    # завершения, а при shutdown его нужно отменить, чтобы он не проснулся
    # после закрытия aiohttp-сессии ("Session is closed" спам)
    initial_scan_task = asyncio.create_task(_delayed_initial_scan())

    yield

    # Корректное завершение: AsyncIOScheduler.shutdown(wait=True) НЕ ждёт —
    # его исполнитель отменяет запущенные корутины (в apscheduler так и
    # написано: "There is no way to honor wait=True"). Поэтому сначала
    # ставим планировщик на паузу, чтобы новые задачи не стартовали, затем
    # сами дожидаемся текущих: обрыв сессии посреди верификации SL оставил
    # бы позицию на бирже с неподтверждённым стопом.
    if _scheduler and _scheduler.running:
        try:
            _scheduler.pause()
        except Exception as pe:
            log.warning(f"scheduler pause: {pe}")

    import strategy.scanner as _sc
    import strategy.trader as _tr
    import strategy.evaluator as _ev
    deadline = 25.0
    # _ENTERING обязателен: enter_trade выполняется ПОСЛЕ того, как scan_all
    # отпустил _SCANNING, поэтому раньше цикл завершался на первой итерации,
    # и сессия закрывалась прямо между place_order и установкой стопа.
    # _EVALUATING в условии обязателен. Без него оценщик не ждали вовсе:
    # pause() уже запущенную задачу не трогает, флаги остальных нулевые,
    # цикл выходил мгновенно, и _client.close() срабатывал у оценщика под
    # руками. Сам он от этого не падает (CancelledError проходит мимо его
    # except Exception), но если отмена застаёт его внутри get_klines, то
    # bybit._get уходит в повтор и ПЕРЕСОЗДАЁТ закрытую сессию — ровно тот
    # осиротевший объект, от которого стартовый скан защитили отдельно.
    def _busy() -> bool:
        return bool(_sc._SCANNING or _tr._MONITORING or _tr._ENTERING > 0
                    or _ev._EVALUATING)

    while deadline > 0 and _busy():
        await asyncio.sleep(0.5)
        deadline -= 0.5
    if _busy():
        log.warning("shutdown: задачи не завершились за 25с — закрываю принудительно")

    # Отмена стартового скана — ПОСЛЕ ожидания, а не до него. Планировщику
    # хватает pause(), но эта задача ему не подчиняется, и cancel() до цикла
    # ожидания активно убивал единственный незащищённый путь: CancelledError
    # прилетал в enter_trade между принятым ордером и подтверждением стопа,
    # оставляя позицию на бирже голой.
    #
    # Оговорка: цикл выше не даёт полной гарантии. Между снятием _SCANNING и
    # взведением _ENTERING есть await'ы (save_signal, _ensure_daily_state), и
    # SIGTERM ровно в этот момент застанет все флаги нулевыми. Окно узкое, но
    # существует — поэтому здесь не «безопасно», а «настолько безопасно,
    # насколько возможно без отдельного флага у самой задачи».
    initial_scan_task.cancel()
    # Дать задаче доработать отмену ДО закрытия сессии: иначе её финализация
    # вызывает _get_session(), который ПЕРЕСОЗДАЁТ закрытую сессию — она
    # остаётся никем не закрытой, и сетевые запросы летят уже после
    # "Shutdown complete".
    try:
        await asyncio.wait_for(asyncio.shield(initial_scan_task), timeout=3)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception as ie:
        log.warning(f"initial scan finalisation: {ie}")

    if _scheduler and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as se:
            log.warning(f"scheduler shutdown: {se}")
    if _client:
        await _client.close()
    log.info("Shutdown complete")


async def _scan_job():
    if _client:
        await run_scan_and_broadcast(_client, cfg.NTFY_URL)


async def _monitor_job():
    """Монитор досылает и удерживает стоп — падать молча ему нельзя.

    Без этого блока исключение уходило в APScheduler: задача повторялась
    каждые 30 секунд и падала снова, живые позиции оставались без
    присмотра, а наружу не выходило ничего. Отмечаем ФАКТ успешного
    прохода: по нему дашборд отличает работу от холостого хода.
    """
    if not _client:
        return
    try:
        await monitor_positions(_client)
    except Exception as e:
        state.last_monitor_error = f"{type(e).__name__}: {e}"
        log.error(f"монитор позиций упал: {e}", exc_info=True)
        return
    state.last_monitor_error = ""
    state.last_monitor_ok = datetime.utcnow()


async def _outcome_job():
    if _client:
        await evaluate_signal_outcomes(_client)


async def _cleanup_job():
    try:
        # 8 суток: оценщику нужно 48ч на вердикт, а статистика винрейта
        # считается за 7 дней — при прежних 50ч решённые сигналы удалялись
        # раньше, чем попадали в семидневную выборку
        removed = await db.cleanup_old_signals(keep_hours=max(cfg.SIGNAL_TTL_HOURS, 192))
        if removed:
            log.info(f"Cleanup: removed {removed} old signals")
    except Exception as e:
        log.warning(f"Cleanup error: {e}")


async def _delayed_initial_scan():
    await asyncio.sleep(3)
    # Сначала СИНХРОНИЗАЦИЯ с биржей, потом скан. Иначе первый скан идёт
    # раньше первого тика монитора (+30с), state.positions пуст, и лимиты
    # MAX_POSITIONS / MAX_SAME_DIRECTION считаются от нуля — бот открывает
    # позиции поверх уже существующих после рестарта.
    try:
        await monitor_positions(_client)
    except Exception as e:
        log.error(f"Стартовая сверка позиций не удалась: {e}")
    try:
        await _scan_job()
    except Exception as e:
        log.error(f"Initial scan failed (non-fatal): {e}")


app = FastAPI(title="Gerchik Bot", lifespan=lifespan)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
