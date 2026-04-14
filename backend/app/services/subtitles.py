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
        if output_path.exists():
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
    timeout_seconds = max(30, int(resolved_settings.subtitle_request_timeout_seconds or 1800))
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


async def _process_candidate(candidate: SubtitleCandidate, *, force: bool = False, app_settings: Settings | None = None) -> str:
    if not candidate.source_path.exists():
        return "skipped"
    if find_caption_tracks(candidate.source_path) and not force:
        return "skipped"
    candidate.output_path.parent.mkdir(parents=True, exist_ok=True)
    await request_subtitle_generation(candidate.source_path, candidate.output_path, force=force, app_settings=app_settings)
    return "generated" if candidate.output_path.exists() else "failed"


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

    details = dict(job.details or {})
    candidates = missing_subtitle_candidates(db, limit=max(1, int(resolved_settings.subtitle_manual_batch_size or 5)))
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
        try:
            result = await _process_candidate(candidate, app_settings=resolved_settings)
        except Exception as exc:
            failed += 1
            batch_failed += 1
            logger.warning(
                "Subtitle generation failed video_id=%s path=%s error=%s",
                candidate.video.id,
                candidate.source_path,
                exc,
            )
            details["last_error"] = str(exc)
            result = "failed"
        if result == "generated":
            generated += 1
            batch_generated += 1
        elif result == "skipped":
            skipped += 1
            batch_skipped += 1
        elif result == "failed":
            batch_failed += 1
        processed += 1

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
    if not candidates:
        sync_settings.last_subtitle_sync_at = datetime.utcnow()
        db.commit()
        return False
    generated_any = False
    for candidate in candidates:
        try:
            result = await _process_candidate(candidate, app_settings=resolved_settings)
        except Exception as exc:
            logger.warning(
                "Automatic subtitle generation failed video_id=%s path=%s error=%s",
                candidate.video.id,
                candidate.source_path,
                exc,
            )
            break
        generated_any = generated_any or result == "generated"
    sync_settings.last_subtitle_sync_at = datetime.utcnow()
    db.commit()
    return generated_any
