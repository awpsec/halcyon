from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.entities import SyncJob, SyncSettings, Video
from app.services.media import find_caption_tracks

logger = get_logger()
settings = get_settings()

GENERATED_SUBTITLE_SUFFIX = ".halcyon.vtt"
SUBTITLE_JOB_STALE_AFTER = timedelta(hours=6)


@dataclass(slots=True)
class SubtitleCandidate:
    video: Video
    source_path: Path
    output_path: Path


def generated_subtitle_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}{GENERATED_SUBTITLE_SUFFIX}")


def subtitle_service_configured(app_settings: Settings | None = None) -> bool:
    resolved = app_settings or settings
    return bool((resolved.subtitle_service_url or "").strip())


def reconcile_subtitle_job(db: Session, job: SyncJob) -> SyncJob:
    if job.status != "running":
        return job
    latest_update = job.updated_at or job.started_at or job.created_at
    if latest_update and datetime.utcnow() - latest_update <= SUBTITLE_JOB_STALE_AFTER:
        return job
    details = dict(job.details or {})
    details.setdefault("warning", "Stale subtitle job cleared")
    details["stale"] = True
    details.setdefault("percent", 0)
    job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.details = details
    db.commit()
    db.refresh(job)
    return job


def active_subtitle_job(db: Session) -> SyncJob | None:
    jobs = db.scalars(
        select(SyncJob)
        .where(
            SyncJob.scope == "subtitles",
            SyncJob.status.in_(("pending", "running")),
        )
        .order_by(SyncJob.created_at.asc(), SyncJob.id.asc())
    ).all()
    if not jobs:
        return None
    job = jobs[0]
    if job.status == "running":
        job = reconcile_subtitle_job(db, job)
        if job.status != "running":
            return None
    return job


def create_subtitle_backfill_job(db: Session) -> SyncJob:
    existing = active_subtitle_job(db)
    if existing:
        details = dict(existing.details or {})
        logger.info(
            "Subtitle job reuse id=%s status=%s processed=%s total=%s remaining=%s",
            existing.id,
            existing.status,
            details.get("processed"),
            details.get("total"),
            details.get("remaining"),
        )
        return existing
    total = count_missing_subtitle_candidates(db)
    job = SyncJob(
        scope="subtitles",
        status="pending",
        details={
            "mode": "backfill",
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "processed": 0,
            "remaining": total,
            "total": total,
            "percent": 0,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Subtitle job created id=%s total_candidates=%s", job.id, total)
    return job


def _candidate_source_path(video: Video) -> Path | None:
    ordered_files = sorted(
        video.files,
        key=lambda item: (
            item.relative_path.lower(),
            item.id,
        ),
    )
    for video_file in ordered_files:
        candidate = Path(video_file.absolute_path)
        if candidate.exists():
            return candidate
    return None


def missing_subtitle_candidates(db: Session, *, limit: int | None = None) -> list[SubtitleCandidate]:
    rows = db.scalars(
        select(Video)
        .options(joinedload(Video.files))
        .where(Video.is_available.is_(True), Video.files.any())
        .order_by(Video.created_at.desc(), Video.id.desc())
    ).unique().all()
    candidates: list[SubtitleCandidate] = []
    for video in rows:
        source_path = _candidate_source_path(video)
        if not source_path:
            continue
        if find_caption_tracks(source_path):
            continue
        output_path = generated_subtitle_path(source_path)
        if output_path.exists() and output_path.stat().st_size > 0:
            continue
        candidates.append(
            SubtitleCandidate(
                video=video,
                source_path=source_path,
                output_path=output_path,
            )
        )
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def count_missing_subtitle_candidates(db: Session) -> int:
    return len(missing_subtitle_candidates(db))


async def request_subtitle_generation(
    source_path: Path,
    output_path: Path,
    *,
    force: bool = False,
    app_settings: Settings | None = None,
) -> dict:
    resolved_settings = app_settings or settings
    base_url = (resolved_settings.subtitle_service_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Subtitle service URL is not configured")
    timeout_seconds = max(30, int(resolved_settings.subtitle_request_timeout_seconds or 14400))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=5.0),
        follow_redirects=True,
    ) as client:
        response = await client.post(
            f"{base_url}/transcriptions",
            json={
                "source_path": str(source_path),
                "output_path": str(output_path),
                "force": force,
            },
        )
    if response.is_error:
        detail = response.text.strip() or f"Subtitle service failed with {response.status_code}"
        raise RuntimeError(detail)
    return response.json()


async def _process_candidate(candidate: SubtitleCandidate, *, force: bool = False, app_settings: Settings | None = None) -> tuple[str, str | None]:
    if not candidate.source_path.exists():
        return "skipped", "Source media file no longer exists"
    if find_caption_tracks(candidate.source_path) and not force:
        return "skipped", "Caption track already exists"
    if candidate.output_path.exists():
        try:
            if candidate.output_path.stat().st_size <= 0:
                candidate.output_path.unlink(missing_ok=True)
        except OSError:
            return "failed", "Existing subtitle sidecar could not be replaced"
    candidate.output_path.parent.mkdir(parents=True, exist_ok=True)
    response = await request_subtitle_generation(candidate.source_path, candidate.output_path, force=force, app_settings=app_settings)
    if candidate.output_path.exists():
        if response.get("cached"):
            return "generated", "Subtitle sidecar already existed"
        return "generated", f"Generated {response.get('segments', 0)} segments"
    return "failed", "Subtitle sidecar was not written"


async def process_subtitle_backfill_job(
    db: Session,
    job: SyncJob,
    *,
    app_settings: Settings | None = None,
) -> SyncJob:
    resolved_settings = app_settings or settings
    if job.status == "pending":
        job.status = "running"
        job.started_at = datetime.utcnow()
        details = dict(job.details or {})
        details.setdefault("mode", "backfill")
        details.setdefault("generated", 0)
        details.setdefault("skipped", 0)
        details.setdefault("failed", 0)
        details.setdefault("processed", 0)
        details.setdefault("total", count_missing_subtitle_candidates(db))
        details.setdefault("remaining", details["total"])
        details.setdefault("percent", 0)
        job.details = details
        db.commit()
        db.refresh(job)
        logger.info(
            "Subtitle job started id=%s total=%s batch_size=%s",
            job.id,
            details.get("total"),
            max(1, int(resolved_settings.subtitle_manual_batch_size or 5)),
        )

    details = dict(job.details or {})
    candidates = missing_subtitle_candidates(db, limit=max(1, int(resolved_settings.subtitle_manual_batch_size or 5)))
    logger.info(
        "Subtitle job batch id=%s candidates=%s processed=%s total=%s remaining=%s",
        job.id,
        len(candidates),
        details.get("processed"),
        details.get("total"),
        details.get("remaining"),
    )
    if not candidates:
        details["remaining"] = 0
        details["percent"] = 100
        details["message"] = "Subtitle backfill complete"
        job.status = "completed"
        job.finished_at = datetime.utcnow()
        job.details = details
        db.commit()
        db.refresh(job)
        return job

    generated = int(details.get("generated", 0))
    skipped = int(details.get("skipped", 0))
    failed = int(details.get("failed", 0))
    processed = int(details.get("processed", 0))
    batch_generated = 0
    batch_skipped = 0
    batch_failed = 0

    for candidate in candidates:
        total = max(int(details.get("total", 0)), processed + len(candidates))
        details.update(
            {
                "mode": "backfill",
                "generated": generated,
                "skipped": skipped,
                "failed": failed,
                "processed": processed,
                "remaining": max(0, total - processed),
                "total": total,
                "percent": 100 if total <= 0 else min(100, round((processed / total) * 100)),
                "active_video_id": candidate.video.id,
                "active_title": candidate.video.title,
                "current_video_id": candidate.video.id,
                "current_title": candidate.video.title,
                "current_index": min(total, processed + 1),
                "last_result": "running",
                "last_detail": "Subtitle generation in progress",
                "message": f"Generating subtitles for {candidate.video.title}",
            }
        )
        job.details = details
        db.commit()
        db.refresh(job)
        logger.info(
            "Subtitle generation started video_id=%s title=%s path=%s",
            candidate.video.id,
            candidate.video.title,
            candidate.source_path,
        )
        failure_already_counted = False
        try:
            result, detail = await _process_candidate(candidate, app_settings=resolved_settings)
        except Exception as exc:
            failed += 1
            batch_failed += 1
            failure_already_counted = True
            logger.warning(
                "Subtitle generation failed video_id=%s path=%s error=%s",
                candidate.video.id,
                candidate.source_path,
                exc,
            )
            details["last_error"] = str(exc)
            result = "failed"
            detail = str(exc)
        if result == "generated":
            generated += 1
            batch_generated += 1
            logger.info(
                "Subtitle generation successful video_id=%s title=%s path=%s detail=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "ok",
            )
        elif result == "skipped":
            skipped += 1
            batch_skipped += 1
            logger.info(
                "Subtitle generation skipped video_id=%s title=%s path=%s reason=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "Skipped",
            )
        elif result == "failed":
            if not failure_already_counted:
                failed += 1
                batch_failed += 1
            logger.warning(
                "Subtitle generation failed video_id=%s title=%s path=%s reason=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "Unknown failure",
            )
        processed += 1

        total = max(int(details.get("total", 0)), processed)
        details.update(
            {
                "mode": "backfill",
                "generated": generated,
                "skipped": skipped,
                "failed": failed,
                "processed": processed,
                "remaining": max(0, total - processed),
                "total": total,
                "percent": 100 if total <= 0 else min(100, round((processed / total) * 100)),
                "active_video_id": None,
                "active_title": None,
                "current_video_id": candidate.video.id,
                "current_title": candidate.video.title,
                "current_index": min(total, processed),
                "last_result": result,
                "last_detail": detail,
            }
        )
        job.details = details
        db.commit()
        db.refresh(job)

    remaining = count_missing_subtitle_candidates(db)
    total = max(int(details.get("total", 0)), processed + remaining)
    details.update(
        {
            "mode": "backfill",
            "generated": generated,
            "skipped": skipped,
            "failed": failed,
            "processed": processed,
            "remaining": remaining,
            "total": total,
            "percent": 100 if total <= 0 else min(100, round(((total - remaining) / total) * 100)),
            "active_video_id": None,
            "active_title": None,
            "current_index": min(total, processed),
        }
    )
    if remaining == 0:
        details["message"] = "Subtitle backfill complete"
        job.status = "completed"
        job.finished_at = datetime.utcnow()
    elif batch_generated == 0 and batch_skipped == 0 and batch_failed > 0:
        details["message"] = "Subtitle backfill failed"
        job.status = "failed"
        job.finished_at = datetime.utcnow()
    else:
        job.status = "running"
        job.finished_at = None
        details["message"] = "Subtitle backfill in progress"
    job.details = details
    db.commit()
    db.refresh(job)
    return job


async def run_automatic_subtitle_pass(
    db: Session,
    sync_settings: SyncSettings,
    *,
    app_settings: Settings | None = None,
) -> bool:
    resolved_settings = app_settings or settings
    candidates = missing_subtitle_candidates(db, limit=max(1, int(resolved_settings.subtitle_auto_batch_size or 1)))
    logger.info(
        "Subtitle automatic pass candidates=%s batch_size=%s",
        len(candidates),
        max(1, int(resolved_settings.subtitle_auto_batch_size or 1)),
    )
    if not candidates:
        sync_settings.last_subtitle_sync_at = datetime.utcnow()
        db.commit()
        return False
    generated_any = False
    for candidate in candidates:
        try:
            result, detail = await _process_candidate(candidate, app_settings=resolved_settings)
        except Exception as exc:
            logger.warning(
                "Automatic subtitle generation failed video_id=%s path=%s error=%s",
                candidate.video.id,
                candidate.source_path,
                exc,
            )
            break
        if result == "generated":
            logger.info(
                "Automatic subtitle generation successful video_id=%s title=%s path=%s detail=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "ok",
            )
        elif result == "skipped":
            logger.info(
                "Automatic subtitle generation skipped video_id=%s title=%s path=%s reason=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "Skipped",
            )
        else:
            logger.warning(
                "Automatic subtitle generation failed video_id=%s title=%s path=%s reason=%s",
                candidate.video.id,
                candidate.video.title,
                candidate.source_path,
                detail or "Unknown failure",
            )
        generated_any = generated_any or result == "generated"
    sync_settings.last_subtitle_sync_at = datetime.utcnow()
    db.commit()
    return generated_any
