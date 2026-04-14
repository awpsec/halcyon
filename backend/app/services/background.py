from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.db.session import SessionLocal
from app.models.entities import SyncJob, SyncSettings
from app.services.retention import run_retention_cycle
from app.services.scanner import scan_selected_folders_if_idle
from app.services.subtitles import active_subtitle_job, process_subtitle_backfill_job, run_automatic_subtitle_pass, subtitle_service_configured
from app.services.sync import normalize_channel_assignments, reconcile_sync_job, refresh_live_streams, sync_scope

LIVE_SYNC_MIN_INTERVAL_SECONDS = 900


def current_scan_interval_seconds(settings: Settings) -> int:
    with SessionLocal() as db:
        sync_settings = db.scalar(select(SyncSettings))
        if sync_settings and sync_settings.scan_interval_seconds:
            return max(5, min(sync_settings.scan_interval_seconds, 3600))
    return settings.scan_interval_seconds


def background_scan_once(settings: Settings) -> None:
    with SessionLocal() as db:
        sync_settings = db.scalar(select(SyncSettings))
        if sync_settings and not sync_settings.automatic_detection_enabled:
            return
        normalize_channel_assignments(db)
        scan_selected_folders_if_idle(db, settings.mounted_roots, trigger="auto")


def active_running_sync_jobs(db) -> list[SyncJob]:
    running_jobs = db.scalars(
        select(SyncJob)
        .where(
            SyncJob.status == "running",
            SyncJob.scope != "subtitles",
        )
        .order_by(SyncJob.created_at.desc())
    ).all()
    return [job for job in (reconcile_sync_job(db, job) for job in running_jobs) if job.status == "running"]


async def background_auto_sync_once(settings: Settings) -> None:
    with SessionLocal() as db:
        sync_settings = db.scalar(select(SyncSettings))
        if not sync_settings:
            return
        api_key = (sync_settings.youtube_api_key or "").strip() or settings.youtube_api_key
        if sync_settings.automatic_detection_enabled:
            if not active_running_sync_jobs(db):
                await sync_scope(db, scope="orphans", target_id=None, api_key=api_key, quiet_if_idle=True)
        if sync_settings.live_tab_enabled and api_key:
            configured_interval = max(
                LIVE_SYNC_MIN_INTERVAL_SECONDS,
                min(sync_settings.scan_interval_seconds or settings.scan_interval_seconds, 3600),
            )
            if (
                not sync_settings.last_live_sync_at
                or datetime.utcnow() - sync_settings.last_live_sync_at >= timedelta(seconds=configured_interval)
            ):
                await refresh_live_streams(
                    db,
                    api_key=api_key,
                    requests_per_second=sync_settings.requests_per_second or 3,
                )
        if not sync_settings.automatic_sync_enabled:
            return
        if active_running_sync_jobs(db):
            return
        configured_interval = max(
            5,
            min(sync_settings.scan_interval_seconds or settings.scan_interval_seconds, 3600),
        )
        sync_interval = max(configured_interval, 900)
        if sync_settings.last_library_sync_at and datetime.utcnow() - sync_settings.last_library_sync_at < timedelta(seconds=sync_interval):
            return
        await sync_scope(db, scope="library", target_id=None, api_key=api_key, quiet_if_idle=True)


def background_retention_once() -> None:
    with SessionLocal() as db:
        run_retention_cycle(db, trigger="auto")


async def background_subtitles_once(settings: Settings) -> None:
    if not subtitle_service_configured(settings):
        return
    with SessionLocal() as db:
        sync_settings = db.scalar(select(SyncSettings))
        if not sync_settings:
            return
        subtitle_job = active_subtitle_job(db)
        if subtitle_job:
            await process_subtitle_backfill_job(db, subtitle_job, app_settings=settings)
            return
        if not sync_settings.subtitle_generation_enabled:
            return
        configured_interval = max(
            max(60, int(settings.subtitle_auto_min_interval_seconds or 300)),
            min(sync_settings.scan_interval_seconds or settings.scan_interval_seconds, 3600),
        )
        if (
            sync_settings.last_subtitle_sync_at
            and datetime.utcnow() - sync_settings.last_subtitle_sync_at < timedelta(seconds=configured_interval)
        ):
            return
        await run_automatic_subtitle_pass(db, sync_settings, app_settings=settings)


async def run_background_cycle_once(settings: Settings) -> None:
    background_scan_once(settings)
    await background_auto_sync_once(settings)
    await background_subtitles_once(settings)
    background_retention_once()
