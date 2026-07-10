import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core.config import cfg
from core.state import state
from core import db
from exchange.bybit import BybitClient
from strategy.scanner import run_scan_and_broadcast
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

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _scan_job,
        "interval",
        minutes=cfg.SCAN_INTERVAL_MIN,
        id="scan",
        max_instances=1,
    )
    _scheduler.add_job(
        _cleanup_job,
        "cron",
        hour="*/6",
        id="cleanup",
    )
    _scheduler.start()
    log.info(f"Scheduler started — scan every {cfg.SCAN_INTERVAL_MIN} min")

    # Run an initial scan shortly after startup
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


async def _cleanup_job():
    removed = await db.cleanup_old_signals(keep_hours=48)
    if removed:
        log.info(f"Cleanup: removed {removed} old signals")


async def _delayed_initial_scan():
    await asyncio.sleep(3)
    await _scan_job()


app = FastAPI(title="Bybit OI Scanner", lifespan=lifespan)
app.include_router(router)

# Serve PWA static files
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
