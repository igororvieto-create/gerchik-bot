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
                data = await r.json()
        proxies = []
        for p in data.get("results", []):
            if p.get("valid"):
                url = (f"http://{p['username']}:{p['password']}"
                       f"@{p['proxy_address']}:{p['port']}")
                proxies.append(url)
        log.info(f"Webshare: loaded {len(proxies)} proxies")
        return proxies
    except Exception as e:
        log.warning(f"Webshare API fetch failed: {e}")
        return []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _scheduler

    try:
        await db.init_db()
        log.info("DB ready")
    except Exception as e:
        log.error(f"DB init failed (continuing anyway): {e}")

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

    initial_scan_task.cancel()

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
    deadline = 25.0
    while deadline > 0 and (_sc._SCANNING or _tr._MONITORING):
        await asyncio.sleep(0.5)
        deadline -= 0.5
    if _sc._SCANNING or _tr._MONITORING:
        log.warning("shutdown: задачи не завершились за 25с — закрываю принудительно")

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
    if _client:
        await monitor_positions(_client)


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
