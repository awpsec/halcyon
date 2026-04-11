from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.entities import (
    Channel,
    LibraryRoot,
    MetadataOverride,
    PlaylistItem,
    QueueItem,
    RetentionItem,
    RetentionSettings,
    SavedVideo,
    ScanJob,
    SelectedFolder,
    Series,
    TranscodeJob,
    Video,
    VideoFile,
    VideoReaction,
    WatchHistory,
    WatchProgress,
    YouTubeCommentSnapshot,
    YouTubeMatch,
    YouTubeVideoSnapshot,
)
from app.services.media import generate_preview_clip, generate_thumbnail, fingerprint_file, is_video_file, probe_media
from app.services.utils import infer_folder_hints, infer_published_at, is_generic_channel_name, parse_episode_number, slugify

settings = get_settings()
logger = get_logger()
SCAN_STALE_AFTER = timedelta(minutes=10)
SCAN_MIN_STABLE_AGE = timedelta(seconds=45)
DEFAULT_LIBRARY_SENTINEL = ".halcyon-library-root"
TEMP_DOWNLOAD_MARKERS = (
    ".part",
    ".ytdl",
    ".tmp",
    ".temp",
    ".partial",
    ".crdownload",
    ".download",
    ".opdownload",
    ".unconfirmed",
)
YTDLP_FRAGMENT_PATTERN = re.compile(r"\.f\d{2,5}$", re.IGNORECASE)
RETENTION_SCAN_DIRNAME = ".halcyon-retention"
RETENTION_DELETE_BUFFER_DIRNAME = ".pending-delete"


def ensure_slug_uniqueness(db: Session, model, base_slug: str) -> str:
    slug = base_slug
    counter = 2
    while db.scalar(select(model).where(model.slug == slug)) is not None:
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


def get_or_create_channel(db: Session, name: str) -> Channel:
    slug = slugify(name)
    channel = db.scalar(select(Channel).where(Channel.slug == slug))
    if channel:
        return channel
    channel = Channel(name=name, slug=ensure_slug_uniqueness(db, Channel, slug))
    db.add(channel)
    db.flush()
    return channel


def get_or_create_series(db: Session, name: str) -> Series:
    slug = slugify(name)
    series = db.scalar(select(Series).where(Series.slug == slug))
    if series:
        return series
    series = Series(name=name, slug=ensure_slug_uniqueness(db, Series, slug))
    db.add(series)
    db.flush()
    return series


def _normalize_fs_path(value: str | Path) -> str:
    return str(Path(value).resolve(strict=False)).replace("/", "\\").rstrip("\\").casefold()


def _path_is_within(path_value: str | Path, roots: list[Path]) -> bool:
    normalized_path = _normalize_fs_path(path_value)
    for root in roots:
        normalized_root = _normalize_fs_path(root)
        if normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}\\"):
            return True
    return False


def _default_retention_staging_roots(mounted_roots: list[Path]) -> list[Path]:
    return [root / RETENTION_SCAN_DIRNAME for root in mounted_roots]


def _retention_scan_excluded_roots(db: Session, mounted_roots: list[Path]) -> list[Path]:
    settings_row = db.scalar(select(RetentionSettings))
    candidate_roots = _default_retention_staging_roots(mounted_roots)
    if settings_row and settings_row.staging_folder_path:
        candidate_roots.append(Path(settings_row.staging_folder_path).expanduser())

    excluded_roots: list[Path] = []
    for candidate in candidate_roots:
        if _path_is_within(candidate, mounted_roots) and all(_normalize_fs_path(candidate) != _normalize_fs_path(item) for item in excluded_roots):
            excluded_roots.append(candidate)
            excluded_roots.append(candidate / RETENTION_DELETE_BUFFER_DIRNAME)
    return excluded_roots


def _delete_video_dependencies(db: Session, video: Video) -> None:
    match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
    youtube_video_id = match.youtube_video_id if match else None

    db.query(WatchProgress).filter(WatchProgress.video_id == video.id).delete(synchronize_session=False)
    db.query(WatchHistory).filter(WatchHistory.video_id == video.id).delete(synchronize_session=False)
    db.query(VideoReaction).filter(VideoReaction.video_id == video.id).delete(synchronize_session=False)
    db.query(SavedVideo).filter(SavedVideo.video_id == video.id).delete(synchronize_session=False)
    db.query(QueueItem).filter(QueueItem.video_id == video.id).delete(synchronize_session=False)
    db.query(PlaylistItem).filter(PlaylistItem.video_id == video.id).delete(synchronize_session=False)
    db.query(MetadataOverride).filter(
        MetadataOverride.target_type == "video",
        MetadataOverride.target_id == video.id,
    ).delete(synchronize_session=False)
    db.query(TranscodeJob).filter(TranscodeJob.video_id == video.id).delete(synchronize_session=False)
    db.query(VideoFile).filter(VideoFile.video_id == video.id).delete(synchronize_session=False)
    db.query(YouTubeMatch).filter(YouTubeMatch.video_id == video.id).delete(synchronize_session=False)

    if youtube_video_id:
        db.query(YouTubeCommentSnapshot).filter(
            YouTubeCommentSnapshot.youtube_video_id == youtube_video_id
        ).delete(synchronize_session=False)
        db.query(YouTubeVideoSnapshot).filter(
            YouTubeVideoSnapshot.youtube_video_id == youtube_video_id
        ).delete(synchronize_session=False)

    db.delete(video)


def cleanup_missing_files(db: Session, managed_roots: list[Path], discovered_paths: set[str]) -> int:
    if not managed_roots:
        return 0

    removed = 0
    protected_file_ids = set(
        db.scalars(
            select(RetentionItem.video_file_id).where(
                RetentionItem.status.in_(("staged", "error"))
            )
        ).all()
    )
    managed_files = db.scalars(select(VideoFile).options(joinedload(VideoFile.video))).all()
    for video_file in managed_files:
        if video_file.id in protected_file_ids:
            continue
        if not _path_is_within(video_file.absolute_path, managed_roots):
            continue
        normalized_path = _normalize_fs_path(video_file.absolute_path)
        if normalized_path in discovered_paths:
            continue

        video = video_file.video
        logger.info("Scan removing missing file path=%s video_id=%s", video_file.absolute_path, video.id if video else None)
        db.delete(video_file)
        db.flush()

        if video:
            remaining_files = db.scalar(select(func.count(VideoFile.id)).where(VideoFile.video_id == video.id)) or 0
            if remaining_files == 0:
                _delete_video_dependencies(db, video)
        removed += 1

    return removed


def cleanup_orphan_videos(db: Session) -> int:
    removed = 0
    protected_video_ids = set(
        db.scalars(
            select(RetentionItem.video_id).where(
                RetentionItem.status.in_(("staged", "error"))
            )
        ).all()
    )
    orphan_videos = db.scalars(
        select(Video)
        .options(joinedload(Video.channel), joinedload(Video.files), joinedload(Video.youtube_match))
        .where(~Video.files.any())
    ).unique().all()

    for video in orphan_videos:
        if video.id in protected_video_ids:
            continue
        if video.youtube_match and video.youtube_match.youtube_video_id:
            continue
        logger.info("Scan removing orphan video id=%s title=%s", video.id, video.title)
        _delete_video_dependencies(db, video)
        removed += 1

    return removed


def reconcile_scan_job(db: Session, job: ScanJob) -> ScanJob:
    if job.status != "running":
        return job
    latest_update = job.updated_at or job.started_at or job.created_at
    if latest_update and datetime.utcnow() - latest_update <= SCAN_STALE_AFTER:
        return job
    details = dict(job.details or {})
    details.setdefault("warning", "Stale scan job cleared")
    details["stale"] = True
    details.setdefault("percent", 0)
    job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.details = details
    db.commit()
    db.refresh(job)
    logger.warning("Cleared stale scan job id=%s scope=%s", job.id, job.scope)
    return job


def _should_skip_scan_path(file_path: Path) -> bool:
    lowered_name = file_path.name.casefold()
    if any(marker in lowered_name for marker in TEMP_DOWNLOAD_MARKERS):
        return True
    if YTDLP_FRAGMENT_PATTERN.search(file_path.stem):
        return True
    try:
        stat = file_path.stat()
    except OSError:
        return True
    if stat.st_size <= 0:
        return True
    modified_at = datetime.fromtimestamp(stat.st_mtime)
    if datetime.utcnow() - modified_at < SCAN_MIN_STABLE_AGE:
        return True
    return False


def _hydrate_local_media_artifacts(video: Video, file_path: Path, fingerprint: str, duration_seconds: int) -> None:
    expected_thumbnail_path = settings.cache_dir / "thumbnails" / f"{fingerprint}.jpg"
    if video.thumbnail_path != str(expected_thumbnail_path) or not expected_thumbnail_path.exists():
        generated_thumbnail = generate_thumbnail(file_path, settings.cache_dir, fingerprint)
        if generated_thumbnail:
            video.thumbnail_path = generated_thumbnail

    if duration_seconds > 0:
        expected_preview_path = settings.cache_dir / "previews" / f"{fingerprint}.mp4"
        if not expected_preview_path.exists() or expected_preview_path.stat().st_size <= 0:
            generate_preview_clip(file_path, settings.cache_dir, fingerprint)


def _content_signature(path: Path) -> tuple[int, str]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 128))
    return stat.st_size, digest.hexdigest()


def upsert_video_for_path(
    db: Session,
    file_path: Path,
    mounted_root: Path,
    classification_root: Path | None = None,
) -> tuple[Video, str]:
    relative_path = file_path.relative_to(mounted_root).as_posix()
    classification_relative = file_path.relative_to(classification_root or mounted_root)
    container_hint = classification_root.name if classification_root and classification_root != mounted_root else None
    title, channel_hint, series_hint = infer_folder_hints(classification_relative, container_hint=container_hint)
    episode_number = parse_episode_number(title)
    published_at = infer_published_at(file_path)
    fingerprint = fingerprint_file(file_path)
    file_size, content_digest = _content_signature(file_path)

    existing_file = db.scalar(select(VideoFile).where(VideoFile.absolute_path == str(file_path)))
    if not existing_file:
        duplicate_candidates = db.scalars(
            select(VideoFile)
            .options(joinedload(VideoFile.video))
            .where(
                VideoFile.file_size == file_size,
                VideoFile.absolute_path != str(file_path),
            )
        ).all()
        duplicate_file = None
        for candidate in duplicate_candidates:
            candidate_path = Path(candidate.absolute_path)
            if not candidate_path.exists():
                continue
            try:
                _candidate_size, candidate_digest = _content_signature(candidate_path)
            except OSError:
                continue
            if candidate_digest == content_digest:
                duplicate_file = candidate
                break

        if duplicate_file:
            duplicate_path = Path(duplicate_file.absolute_path)
            if duplicate_path.exists():
                try:
                    file_path.unlink()
                    if duplicate_file.video:
                        duplicate_file.video.is_available = True
                    db.flush()
                    logger.info(
                        "Scan deleted duplicate file path=%s duplicate_of=%s video_id=%s",
                        file_path,
                        duplicate_path,
                        duplicate_file.video_id,
                    )
                    return duplicate_file.video, duplicate_file.absolute_path
                except OSError as exc:
                    logger.warning(
                        "Scan duplicate cleanup failed path=%s duplicate_of=%s error=%s",
                        file_path,
                        duplicate_path,
                        exc,
                    )
                    if duplicate_file.video:
                        duplicate_file.video.is_available = True
                    return duplicate_file.video, duplicate_file.absolute_path

            duplicate_file.absolute_path = str(file_path)
            duplicate_file.relative_path = relative_path
            existing_file = duplicate_file

    channel = get_or_create_channel(db, channel_hint)
    series = get_or_create_series(db, series_hint) if series_hint else None

    fallback_retention_item = None
    fallback_created_at = None
    if existing_file:
        video = existing_file.video
    else:
        base_slug = slugify(f"{channel_hint}-{title}")
        video = db.scalar(select(Video).where(Video.slug == base_slug))
        if not video:
            fallback_retention_item = db.scalar(
                select(RetentionItem)
                .where(
                    or_(
                        RetentionItem.original_absolute_path == str(file_path),
                        RetentionItem.file_fingerprint == fingerprint,
                    )
                )
                .order_by(RetentionItem.updated_at.desc(), RetentionItem.id.desc())
                .limit(1)
            )
            fallback_created_at = fallback_retention_item.original_video_created_at if fallback_retention_item else None
        if not video:
            video = Video(
                title=title,
                slug=ensure_slug_uniqueness(db, Video, base_slug),
                channel_id=channel.id,
                series_id=series.id if series else None,
                episode_number=episode_number,
                published_at=published_at,
                metadata_confidence=0.72 if series else 0.6,
                created_at=fallback_created_at or datetime.utcnow(),
            )
            db.add(video)
            db.flush()

    preserve_synced_channel = bool(
        video.youtube_match
        and video.youtube_match.youtube_channel_id
        and video.channel
        and video.channel.slug != "unknown-channel"
        and not is_generic_channel_name(video.channel.name)
        and is_generic_channel_name(channel_hint)
    )

    video.title = title
    video.channel_id = video.channel.id if preserve_synced_channel and video.channel else channel.id
    video.series_id = series.id if series else None
    video.episode_number = episode_number
    video.published_at = published_at
    video.is_available = True
    if fallback_created_at and video.created_at and video.created_at > fallback_created_at:
        video.created_at = fallback_created_at

    stat = file_path.stat()

    if existing_file:
        video_file = existing_file
    else:
        fingerprint = fingerprint_file(file_path)
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(file_path),
            relative_path=relative_path,
            fingerprint=fingerprint,
        )
        db.add(video_file)

    modified_at = datetime.fromtimestamp(stat.st_mtime)
    expected_preview_path = (
        settings.cache_dir / "previews" / f"{video_file.fingerprint}.mp4"
        if existing_file and video_file.fingerprint
        else None
    )
    has_preview_clip = (
        video.duration_seconds <= 0
        or (
            expected_preview_path is not None
            and expected_preview_path.exists()
            and expected_preview_path.stat().st_size > 0
        )
    )
    unchanged = (
        existing_file is not None
        and video_file.file_size == stat.st_size
        and video_file.modified_at is not None
        and int(video_file.modified_at.timestamp()) == int(stat.st_mtime)
        and video.duration_seconds > 0
        and bool(video.thumbnail_path)
        and has_preview_clip
    )

    if unchanged:
        video.is_available = True
        db.flush()
        return video, video_file.absolute_path

    metadata = probe_media(file_path)
    video.duration_seconds = metadata["duration_seconds"]
    fingerprint = video_file.fingerprint if existing_file and video_file.fingerprint else fingerprint
    video_file.relative_path = relative_path
    video_file.file_size = metadata["file_size"]
    video_file.modified_at = modified_at
    video_file.codec_summary = metadata["codec_summary"]
    video_file.resolution = metadata["resolution"]
    video_file.fingerprint = fingerprint
    _hydrate_local_media_artifacts(video, file_path, fingerprint, metadata["duration_seconds"])
    db.flush()
    return video, video_file.absolute_path


def scan_selected_folders(db: Session, mounted_roots: list[Path], *, trigger: str = "manual") -> ScanJob:
    trigger_label = "Auto" if trigger == "auto" else "Manual"
    auto_trigger = trigger == "auto"
    job = ScanJob(scope="library", status="running", started_at=datetime.utcnow(), details={})
    db.add(job)
    db.commit()
    db.refresh(job)
    if not auto_trigger:
        logger.info("%s scan started scope=library selected_roots=%s", trigger_label, [str(path) for path in mounted_roots])
    try:
        discovered = 0
        selected = db.scalars(select(SelectedFolder).options(joinedload(SelectedFolder.root)).where(SelectedFolder.is_enabled.is_(True))).all()
        selected_map: dict[str, list[str]] = {}
        for item in selected:
            if item.root:
                selected_map.setdefault(item.root.path, []).append(item.relative_path)
        available_roots = {
            root.path
            for root in db.scalars(select(LibraryRoot).where(LibraryRoot.is_available.is_(True))).all()
        }

        candidates: list[tuple[Path, Path, Path]] = []
        managed_roots: list[Path] = []
        excluded_scan_roots = _retention_scan_excluded_roots(db, mounted_roots)
        for root in mounted_roots:
            roots_to_scan = selected_map.get(str(root))
            if roots_to_scan is None:
                roots_to_scan = [""] if _uses_implicit_root_selection(root, available_roots) else []
            for relative in roots_to_scan:
                target_root = root / relative if relative else root
                classification_root = target_root if relative and len(Path(relative).parts) == 1 else root
                managed_roots.append(target_root)
                if not target_root.exists():
                    continue
                for path in target_root.rglob("*"):
                    if _path_is_within(path, excluded_scan_roots):
                        continue
                    if not is_video_file(path):
                        continue
                    if _should_skip_scan_path(path):
                        continue
                    candidates.append((path, root, classification_root))

        total = len(candidates)
        job.details = {"processed": 0, "total": total, "percent": 0}
        db.commit()

        discovered_paths: set[str] = set()
        for index, (path, root, classification_root) in enumerate(candidates, start=1):
            _video, tracked_path = upsert_video_for_path(db, path, root, classification_root=classification_root)
            discovered_paths.add(_normalize_fs_path(tracked_path))
            discovered += 1
            if index == total or index % 5 == 0:
                job.details = {"processed": index, "total": total, "percent": round((index / total) * 100) if total else 100}
                db.commit()
                if not auto_trigger:
                    logger.info("%s scan progress processed=%s total=%s percent=%s", trigger_label, index, total, job.details["percent"])

        removed = cleanup_missing_files(db, managed_roots, discovered_paths)
        removed += cleanup_orphan_videos(db)
        job.status = "completed"
        job.finished_at = datetime.utcnow()
        job.details = {"processed": discovered, "total": total, "percent": 100, "discovered": discovered, "removed": removed}
        db.commit()
        db.refresh(job)
        if not auto_trigger or discovered or removed:
            logger.info("%s scan completed discovered=%s total=%s removed=%s", trigger_label, discovered, total, removed)
        return job
    except Exception as exc:
        db.rollback()
        failed_job = db.get(ScanJob, job.id)
        if failed_job is not None:
            failed_job.status = "failed"
            failed_job.finished_at = datetime.utcnow()
            failed_job.details = {**(failed_job.details or {}), "error": str(exc)}
            db.commit()
            db.refresh(failed_job)
        logger.exception("%s scan failed error=%s", trigger_label, exc)
        raise


def scan_selected_folders_if_idle(db: Session, mounted_roots: list[Path], *, trigger: str = "auto") -> ScanJob | None:
    running = db.scalar(select(ScanJob).where(ScanJob.status == "running"))
    if running:
        running = reconcile_scan_job(db, running)
    if running and running.status == "running":
        return None
    return scan_selected_folders(db, mounted_roots, trigger=trigger)


def _uses_implicit_root_selection(root: Path, available_root_paths: set[str]) -> bool:
    normalized = root.as_posix().rstrip("/")
    if len(available_root_paths) != 1 or str(root) not in available_root_paths:
        return False
    if normalized == "/library":
        return (root / DEFAULT_LIBRARY_SENTINEL).exists()
    return root.name == "library"
