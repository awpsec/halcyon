from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.init_db import init_db, seed_defaults
from app.db.session import SessionLocal
from app.services.background import current_scan_interval_seconds, run_background_cycle_once
from app.services.sync import normalize_channel_assignments

settings = get_settings()
setup_logging()
logger = get_logger()


async def _scan_loop(stop_event: asyncio.Event) -> None:
    await asyncio.sleep(2)
    while not stop_event.is_set():
        try:
            await run_background_cycle_once(settings)
        except Exception:
            logger.exception("Background cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=current_scan_interval_seconds(settings))
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup")
    init_db()
    with SessionLocal() as db:
        seed_defaults(db, [str(path) for path in settings.mounted_roots])
        normalize_channel_assignments(db)
    stop_event = asyncio.Event()
    scan_task = asyncio.create_task(_scan_loop(stop_event)) if settings.background_tasks_enabled else None
    yield
    if scan_task:
        stop_event.set()
        scan_task.cancel()
        with suppress(asyncio.CancelledError):
            await scan_task
    logger.info("Application shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.allow_origin] if settings.allow_origin != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix=settings.api_prefix)

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        @app.get("/assets/{asset_path:path}")
        def frontend_asset(asset_path: str) -> FileResponse:
            asset_file = assets_dir / asset_path
            if not asset_file.exists() or not asset_file.is_file():
                return FileResponse(frontend_dist / "index.html")
            media_type, _ = mimetypes.guess_type(asset_file.name)
            return FileResponse(asset_file, media_type=media_type or "application/octet-stream")

    @app.get("/favicon.svg")
    def favicon() -> FileResponse:
        return FileResponse(frontend_dist / "favicon.svg", media_type="image/svg+xml")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(frontend_dist / "index.html")
else:
    @app.get("/")
    def root() -> dict:
        return {"name": settings.app_name, "status": "ok"}
