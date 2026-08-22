"""
Main application entry point.
Runs FastAPI (for GitHub webhooks) and Telegram bot together.
"""
import logging
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from dotenv import load_dotenv

load_dotenv()

from app.models.database import init_db
from app.config import validate_settings
from app.handlers.webhook import router as webhook_router
from app.handlers.telegram_bot import build_telegram_app
from app.services.review_queue import review_worker, sync_registered_webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# HTTP client request URLs can contain credentials (notably Telegram bot tokens).
# Keep application lifecycle/job logs while suppressing dependency request lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Global Telegram app instance
telegram_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global telegram_app

    validate_settings()
    logger.info("Initializing database...")
    await init_db()

    # Start Telegram bot
    logger.info("Starting Telegram bot...")
    telegram_app = build_telegram_app()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot is running.")

    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(review_worker(stop_event), name="review-worker")
    hook_sync_task = asyncio.create_task(sync_registered_webhooks(), name="webhook-sync")

    yield

    stop_event.set()
    await worker_task
    if not hook_sync_task.done():
        hook_sync_task.cancel()
    await asyncio.gather(hook_sync_task, return_exceptions=True)
    logger.info("Shutting down Telegram bot...")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(
    title="GitHub PR Review Bot",
    description="Automated PR review and merge bot with Telegram interface",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)


@app.get("/")
async def health():
    return {
        "status": "running",
        "service": "GitHub PR Review Bot",
        "telegram": "active",
    }


@app.get("/health")
async def health_check():
    return {"status": "ok"}
