import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from core.config import cfg
from core.state import state
from core import db
from exchange.bybit import BybitClient
from strategy.scanner import run_scan_and_broadcast
from strategy.trader import monitor_positions
from api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

_client: BybitClient | None = None
_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _scheduler

    await db.init_db()
    log.info("DB ready")

    _client = BybitClient(cfg.BYBIT_API_KEY, cfg.BYBIT_SECRET)
    log.info(f"AUTO_TRADE={'ON' if cfg.AUTO_TRADE else 'OFF'} "
             f"api_key={'set' if cfg.BYBIT_API_KEY else 'not set'}")

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _scan_job,
        "interval",
        minutes=cfg.SCAN_INTERVAL_MIN,
        id="scan",
        max_instances=1,
    )
    _scheduler.add_job(
        _monitor_job,
        "interval",
        seconds=30,
        id="monitor",
        max_instances=1,
    )
    _scheduler.add_job(
        _cleanup_job,
        "cron",
        hour="*/6",
        id="cleanup",
    )
    _scheduler.start()
    log.info(f"Scheduler started — scan every {cfg.SCAN_INTERVAL_MIN} min, monitor every 30s")

    asyncio.create_task(_delayed_initial_scan())

    yield

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _client:
        await _client.close()
    log.info("Shutdown complete")


async def _scan_job():
    if _client:
        await run_scan_and_broadcast(_client, cfg.NTFY_URL)


async def _monitor_job():
    if _client:
        await monitor_positions(_client)


async def _cleanup_job():
    removed = await db.cleanup_old_signals(keep_hours=48)
    if removed:
        log.info(f"Cleanup: removed {removed} old signals")


async def _delayed_initial_scan():
    await asyncio.sleep(3)
    await _scan_job()


app = FastAPI(title="Bybit OI Scanner", lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting uvicorn on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
