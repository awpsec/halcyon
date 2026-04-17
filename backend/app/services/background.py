from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.entities import SyncJob, SyncSettings
from app.services.retention import run_retention_cycle
from app.services.scanner import scan_selected_folders_if_idle
from app.services.subtitles import (
    active_subtitle_job,
    create_subtitle_backfill_job,
    missing_subtitle_candidates,
    process_subtitle_backfill_job,
    subtitle_service_configured,
)
from app.services.sync import normalize_channel_assignments, reconcile_sync_job, refresh_live_streams, sync_scope

LIVE_SYNC_MIN_INTERVAL_SECONDS = 1200
BACKGROUND_ORPHAN_SYNC_BATCH_SIZE = 8
BACKGROUND_LIBRARY_SYNC_BATCH_SIZE = 6
logger = get_logger()


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
        if sync_settings.live_tab_enabled:
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
        if sync_settings.automatic_detection_enabled:
            if not active_running_sync_jobs(db):
                await sync_scope(
                    db,
                    scope="orphans",
                    target_id=None,
                    api_key=api_key,
                    allow_api_discovery=True,
                    background_api_discovery=True,
                    max_videos=BACKGROUND_ORPHAN_SYNC_BATCH_SIZE,
                    quiet_if_idle=True,
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
        await sync_scope(
            db,
            scope="library",
            target_id=None,
            api_key=api_key,
            allow_api_discovery=True,
            background_api_discovery=True,
            max_videos=BACKGROUND_LIBRARY_SYNC_BATCH_SIZE,
            quiet_if_idle=True,
        )


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
            job_details = dict(subtitle_job.details or {})
            logger.info(
                "Subtitle background continuing job id=%s status=%s processed=%s total=%s remaining=%s",
                subtitle_job.id,
                subtitle_job.status,
                job_details.get("processed"),
                job_details.get("total"),
                job_details.get("remaining"),
            )
            await process_subtitle_backfill_job(db, subtitle_job, app_settings=settings)
            sync_settings.last_subtitle_sync_at = datetime.utcnow()
            db.commit()
            return
        if not sync_settings.subtitle_generation_enabled:
            return
        newest_candidates = missing_subtitle_candidates(
            db,
            limit=max(1, int(settings.subtitle_auto_batch_size or 1)),
        )
        if not newest_candidates:
            sync_settings.last_subtitle_sync_at = datetime.utcnow()
            db.commit()
            return
        configured_interval = max(
            max(60, int(settings.subtitle_auto_min_interval_seconds or 300)),
            min(sync_settings.scan_interval_seconds or settings.scan_interval_seconds, 3600),
        )
        newest_candidate_created_at = max(
            (
                candidate.video.created_at
                for candidate in newest_candidates
                if candidate.video.created_at is not None
            ),
            default=None,
        )
        should_run_now = bool(
            sync_settings.last_subtitle_sync_at is None
            or (
                newest_candidate_created_at is not None
                and newest_candidate_created_at > sync_settings.last_subtitle_sync_at
            )
            or datetime.utcnow() - sync_settings.last_subtitle_sync_at >= timedelta(seconds=configured_interval)
        )
        if not should_run_now:
            seconds_since_last_pass = (
                int((datetime.utcnow() - sync_settings.last_subtitle_sync_at).total_seconds())
                if sync_settings.last_subtitle_sync_at
                else None
            )
            logger.info(
                "Subtitle background deferred candidates=%s last_pass_age_seconds=%s interval_seconds=%s newest_candidate_created_at=%s",
                len(newest_candidates),
                seconds_since_last_pass,
                configured_interval,
                newest_candidate_created_at.isoformat() if newest_candidate_created_at else None,
            )
            return
        job = create_subtitle_backfill_job(db)
        logger.info(
            "Subtitle background starting job id=%s candidates=%s trigger=%s",
            job.id,
            len(newest_candidates),
            "new-onboarded-video" if sync_settings.last_subtitle_sync_at and newest_candidate_created_at and newest_candidate_created_at > sync_settings.last_subtitle_sync_at else "interval",
        )
        await process_subtitle_backfill_job(db, job, app_settings=settings)
        sync_settings.last_subtitle_sync_at = datetime.utcnow()
        db.commit()


async def run_background_cycle_once(settings: Settings) -> None:
    background_scan_once(settings)
    await background_auto_sync_once(settings)
    await background_subtitles_once(settings)
    background_retention_once()
