from __future__ import annotations

import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeAlias

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timezone import (
    coerce_datetime_to_timezone,
    localize_utc_to_timezone,
    normalize_timezone_name,
    server_now,
    server_timezone_name,
)
from app.models.entities import (
    Channel,
    RetentionExclusion,
    RetentionItem,
    RetentionRun,
    RetentionSettings,
    SavedVideo,
    Series,
    Video,
    VideoFile,
)
from app.schemas.common import RetentionPendingItemOut
from app.services.scanner import _delete_video_dependencies
from app.services.subtitles import generated_subtitle_path

settings = get_settings()
logger = get_logger()

RETENTION_DELETE_GRACE = timedelta(hours=1)
RETENTION_RUN_TOKEN_LENGTH = 8
RETENTION_MIN_AUTO_INTERVAL_MINUTES = 5
RETENTION_MAX_AUTO_INTERVAL_MINUTES = 7 * 24 * 60
RETENTION_DELETE_BUFFER_DIRNAME = ".pending-delete"
VALID_RETENTION_AUTO_SCHEDULE_KINDS = {"interval", "daily", "weekly"}
NOOP_AUTO_RETENTION_MESSAGES = {"Retention disabled", "Retention not due yet"}
RetentionUndoAction: TypeAlias = tuple[str, Path, Path]


def _retention_timezone_name(settings_row: RetentionSettings) -> str:
    return normalize_timezone_name(settings_row.auto_timezone) or server_timezone_name()


def _retention_file_label(*, relative_path: str | None = None, absolute_path: str | None = None) -> str:
    normalized_relative = (relative_path or "").strip()
    if normalized_relative:
        return normalized_relative
    normalized_absolute = (absolute_path or "").strip()
    if normalized_absolute:
        return Path(normalized_absolute).name or normalized_absolute
    return "Unknown file"


def _retention_run_details_payload(
    *,
    marked_files: list[str] | None = None,
    deleted_files: list[str] | None = None,
    reverted_files: list[str] | None = None,
) -> dict:
    details: dict[str, list[str]] = {}
    if marked_files:
        details["marked_files"] = marked_files
    if deleted_files:
        details["deleted_files"] = deleted_files
    if reverted_files:
        details["reverted_files"] = reverted_files
    return details


def _cleanup_empty_retention_dirs(path: Path, stop_at: Path) -> None:
    current = path
    while current.exists() and current != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _clamp(value: int | None, minimum: int, maximum: int, fallback: int) -> int:
    if value is None:
        return fallback
    return max(minimum, min(int(value), maximum))


def _normalize_retention_settings(settings_row: RetentionSettings) -> bool:
    changed = False

    if not settings_row.retention_days:
        settings_row.retention_days = 30
        changed = True

    if settings_row.auto_schedule_kind not in VALID_RETENTION_AUTO_SCHEDULE_KINDS:
        settings_row.auto_schedule_kind = "interval"
        changed = True

    auto_interval_minutes = _clamp(
        settings_row.auto_interval_minutes,
        RETENTION_MIN_AUTO_INTERVAL_MINUTES,
        RETENTION_MAX_AUTO_INTERVAL_MINUTES,
        15,
    )
    if settings_row.auto_interval_minutes != auto_interval_minutes:
        settings_row.auto_interval_minutes = auto_interval_minutes
        changed = True

    auto_time_hour = _clamp(settings_row.auto_time_hour, 0, 23, 4)
    if settings_row.auto_time_hour != auto_time_hour:
        settings_row.auto_time_hour = auto_time_hour
        changed = True

    auto_time_minute = _clamp(settings_row.auto_time_minute, 0, 59, 0)
    if settings_row.auto_time_minute != auto_time_minute:
        settings_row.auto_time_minute = auto_time_minute
        changed = True

    auto_weekday = _clamp(settings_row.auto_weekday, 0, 6, 0)
    if settings_row.auto_weekday != auto_weekday:
        settings_row.auto_weekday = auto_weekday
        changed = True

    auto_timezone = normalize_timezone_name(settings_row.auto_timezone) or server_timezone_name()
    if settings_row.auto_timezone != auto_timezone:
        settings_row.auto_timezone = auto_timezone
        changed = True

    return changed


def default_retention_staging_folder() -> Path:
    if settings.mounted_roots:
        return settings.mounted_roots[0] / ".halcyon-retention"
    return settings.cache_dir / "retention-staging"


def get_or_create_retention_settings(db: Session) -> RetentionSettings:
    settings_row = db.scalar(select(RetentionSettings))
    if not settings_row:
        settings_row = RetentionSettings(
            enabled=False,
            retention_days=30,
            auto_schedule_kind="interval",
            auto_interval_minutes=15,
            auto_time_hour=4,
            auto_time_minute=0,
            auto_weekday=0,
            auto_timezone=server_timezone_name(),
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    if _normalize_retention_settings(settings_row):
        db.commit()
        db.refresh(settings_row)
    return settings_row


def effective_retention_staging_folder(settings_row: RetentionSettings) -> Path:
    raw = (settings_row.staging_folder_path or "").strip()
    return Path(raw).expanduser() if raw else default_retention_staging_folder()


def retention_mount_roots() -> list[Path]:
    roots = [Path(root).expanduser() for root in settings.mounted_roots]
    return roots or [default_retention_staging_folder().parent]


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _path_is_within(base_path: Path, candidate_path: Path) -> bool:
    try:
        _normalized_path(candidate_path).relative_to(_normalized_path(base_path))
        return True
    except ValueError:
        return False


def _matching_retention_root(path: Path) -> Path | None:
    matches = [
        root
        for root in retention_mount_roots()
        if path == root or _path_is_within(root, path)
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(str(_normalized_path(item))))


def validate_retention_staging_folder_path(raw_path: str | None) -> Path | None:
    normalized = (raw_path or "").strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Retention folder must be an absolute path")
    if not candidate.exists():
        raise ValueError("Retention folder does not exist")
    if not candidate.is_dir():
        raise ValueError("Retention folder must be a directory")
    if _matching_retention_root(candidate) is None:
        raise ValueError("Retention folder must stay within a mounted library root")
    return candidate


def browse_retention_folders(raw_path: str | None = None) -> dict:
    roots = retention_mount_roots()
    requested_text = (raw_path or "").strip()
    if not requested_text:
        root_path = roots[0]
        browse_path = root_path
        prefix = ""
    else:
        requested_path = Path(requested_text).expanduser()
        if requested_path.is_absolute():
            root_path = _matching_retention_root(requested_path)
            if root_path is None:
                raise ValueError("Path is outside the mounted library roots")
            browse_path = requested_path
        else:
            root_path = roots[0]
            browse_path = root_path / requested_path

        prefix = ""
        if not browse_path.exists() or not browse_path.is_dir():
            prefix = browse_path.name
            browse_path = browse_path.parent
            while browse_path != browse_path.parent and (not browse_path.exists() or not browse_path.is_dir()):
                if browse_path == root_path:
                    break
                browse_path = browse_path.parent
            if not browse_path.exists() or not browse_path.is_dir() or not (browse_path == root_path or _path_is_within(root_path, browse_path)):
                browse_path = root_path

    directories = []
    for child in sorted(browse_path.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if prefix and not child.name.lower().startswith(prefix.lower()):
            continue
        directories.append({"name": child.name, "path": str(child)})

    parent_path: str | None = None
    if browse_path != root_path:
        candidate_parent = browse_path.parent
        if candidate_parent == root_path or _path_is_within(root_path, candidate_parent):
            parent_path = str(candidate_parent)

    return {
        "roots": [str(root) for root in roots],
        "root_path": str(root_path),
        "browse_path": str(browse_path),
        "input_path": requested_text,
        "prefix": prefix,
        "parent_path": parent_path,
        "create_parent_path": str(browse_path),
        "directories": directories,
    }


def create_retention_folder(parent_raw_path: str, name: str) -> Path:
    parent_text = (parent_raw_path or "").strip()
    directory_name = (name or "").strip()
    if not parent_text:
        raise ValueError("Parent path is required")
    if not directory_name:
        raise ValueError("Directory name is required")
    if directory_name in {".", ".."} or any(separator in directory_name for separator in ("/", "\\")):
        raise ValueError("Directory name must not contain path separators")

    parent_path = Path(parent_text).expanduser()
    if not parent_path.exists() or not parent_path.is_dir():
        raise ValueError("Parent path does not exist")
    if _matching_retention_root(parent_path) is None:
        raise ValueError("Parent path is outside the mounted library roots")

    target_path = parent_path / directory_name
    target_path.mkdir(parents=False, exist_ok=True)
    return target_path


def _retention_exclusion_sets(db: Session) -> dict[str, set[int]]:
    rows = db.scalars(select(RetentionExclusion)).all()
    grouped = {"video": set(), "series": set(), "channel": set()}
    for row in rows:
        if row.target_type in grouped:
            grouped[row.target_type].add(row.target_id)
    return grouped


def _saved_video_ids(db: Session) -> set[int]:
    return set(db.scalars(select(SavedVideo.video_id)).all())


def video_is_retention_exempt(
    video: Video,
    *,
    saved_video_ids: set[int],
    exclusion_sets: dict[str, set[int]],
) -> bool:
    if video.id in saved_video_ids:
        return True
    if video.series_id and video.series_id in exclusion_sets["series"]:
        return True
    if video.channel_id and video.channel_id in exclusion_sets["channel"]:
        return True
    if video.id in exclusion_sets["video"]:
        return True
    return False


def list_retention_pending_items(db: Session) -> list[RetentionPendingItemOut]:
    items = db.scalars(
        select(RetentionItem)
        .where(RetentionItem.status == "staged")
        .order_by(RetentionItem.delete_after_at.asc(), RetentionItem.marked_at.desc())
    ).all()
    video_ids = [item.video_id for item in items]
    videos = (
        {
            video.id: video
            for video in db.scalars(
                select(Video)
                .options(joinedload(Video.channel))
                .where(Video.id.in_(video_ids))
            ).unique().all()
        }
        if video_ids
        else {}
    )
    return [
        RetentionPendingItemOut(
            id=item.id,
            video_id=item.video_id,
            video_title=videos[item.video_id].title if item.video_id in videos else f"Video {item.video_id}",
            channel_name=videos[item.video_id].channel.name if item.video_id in videos and videos[item.video_id].channel else None,
            thumbnail_url=f"/api/videos/{item.video_id}/thumbnail",
            marked_at=item.marked_at,
            delete_after_at=item.delete_after_at,
            run_token=item.run_token,
        )
        for item in items
    ]


def _ensure_staging_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _stage_path(base_folder: Path, run_token: str, video_file: VideoFile) -> Path:
    source_path = Path(video_file.absolute_path)
    extension = source_path.suffix
    filename = f"{video_file.id}_{video_file.fingerprint[:12]}{extension}"
    return base_folder / run_token / filename


def _delete_buffer_path(base_folder: Path, delete_token: str, item: RetentionItem) -> Path:
    staged_path = Path(item.staged_absolute_path)
    filename = f"{item.video_file_id}_{staged_path.name}"
    return base_folder / RETENTION_DELETE_BUFFER_DIRNAME / delete_token / filename


def _mark_last_run(
    settings_row: RetentionSettings,
    *,
    trigger: str,
    status: str,
    message: str,
    marked_count: int,
    deleted_count: int,
    reverted_count: int,
    run_token: str | None,
) -> None:
    settings_row.last_run_at = datetime.utcnow()
    settings_row.last_run_trigger = trigger
    settings_row.last_run_status = status
    settings_row.last_run_message = message
    settings_row.last_run_marked_count = marked_count
    settings_row.last_run_deleted_count = deleted_count
    settings_row.last_run_reverted_count = reverted_count
    settings_row.last_run_token = run_token


def list_retention_runs(db: Session) -> list[RetentionRun]:
    runs = db.scalars(
        select(RetentionRun)
        .where(
            (RetentionRun.trigger != "auto")
            | (RetentionRun.status != "skipped")
            | (~RetentionRun.message.in_(NOOP_AUTO_RETENTION_MESSAGES))
        )
        .order_by(RetentionRun.created_at.desc(), RetentionRun.id.desc())
    ).all()
    if _backfill_retention_run_details(db, runs):
        db.commit()
    return runs


def retention_reclaimed_bytes(db: Session) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(RetentionItem.file_size_bytes), 0)).where(
                RetentionItem.status == "deleted"
            )
        )
        or 0
    )


def _record_retention_run(
    db: Session,
    *,
    trigger: str,
    status: str,
    message: str,
    details: dict | None = None,
    marked_count: int,
    deleted_count: int,
    reverted_count: int,
    run_token: str | None,
) -> None:
    db.add(
        RetentionRun(
            trigger=trigger,
            status=status,
            message=message,
            details=details or {},
            marked_count=marked_count,
            deleted_count=deleted_count,
            reverted_count=reverted_count,
            run_token=run_token,
        )
    )


def _retention_details_missing(run: RetentionRun) -> bool:
    return not isinstance(run.details, dict) or not run.details


def _retention_window_labels(
    db: Session,
    *,
    timestamp_field,
    timestamp_value: datetime,
    count: int,
    status: str | None = None,
) -> list[str]:
    if count <= 0:
        return []
    for window_seconds in (2, 20, 120):
        start = timestamp_value - timedelta(seconds=window_seconds)
        end = timestamp_value + timedelta(seconds=window_seconds)
        statement = select(RetentionItem).where(timestamp_field >= start, timestamp_field <= end)
        if status is not None:
            statement = statement.where(RetentionItem.status == status)
        items = db.scalars(statement.order_by(timestamp_field.asc(), RetentionItem.id.asc())).all()
        if len(items) == count:
            return [
                _retention_file_label(
                    relative_path=item.original_relative_path,
                    absolute_path=item.original_absolute_path,
                )
                for item in items
            ]
    return []


def _backfill_retention_run_details(db: Session, runs: list[RetentionRun]) -> bool:
    changed = False
    for run in runs:
        if not _retention_details_missing(run):
            continue
        details = _retention_run_details_payload(
            marked_files=_retention_window_labels(
                db,
                timestamp_field=RetentionItem.marked_at,
                timestamp_value=run.created_at,
                count=run.marked_count,
            ),
            deleted_files=_retention_window_labels(
                db,
                timestamp_field=RetentionItem.updated_at,
                timestamp_value=run.created_at,
                count=run.deleted_count,
                status="deleted",
            ),
            reverted_files=_retention_window_labels(
                db,
                timestamp_field=RetentionItem.updated_at,
                timestamp_value=run.created_at,
                count=run.reverted_count,
                status="reverted",
            ),
        )
        if details:
            run.details = details
            changed = True
    return changed


def _prune_noop_auto_retention_runs(db: Session) -> int:
    rows = db.scalars(
        select(RetentionRun).where(
            RetentionRun.trigger == "auto",
            RetentionRun.status == "skipped",
            RetentionRun.message.in_(NOOP_AUTO_RETENTION_MESSAGES),
        )
    ).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def _undo_retention_filesystem_actions(actions: list[RetentionUndoAction]) -> None:
    for action, source_path, target_path in reversed(actions):
        if action != "move":
            continue
        if not source_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(target_path))


def _finalize_deleted_retention_paths(paths: list[Path], staging_root: Path) -> None:
    delete_root = staging_root / RETENTION_DELETE_BUFFER_DIRNAME
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                _cleanup_empty_retention_dirs(path.parent, delete_root)
        except OSError:
            logger.exception("Unable to finalize retention delete for %s", path)
    if delete_root.exists():
        _cleanup_empty_retention_dirs(delete_root, staging_root)


def _refresh_video_availability(db: Session, video_id: int) -> None:
    db.flush()
    video = db.get(Video, video_id)
    if video is None:
        return
    has_staged_file = db.scalar(
        select(RetentionItem.id).where(
            RetentionItem.video_id == video_id,
            RetentionItem.status == "staged",
        ).limit(1)
    )
    if has_staged_file is not None:
        video.is_available = False
        return
    has_any_file = db.scalar(select(VideoFile.id).where(VideoFile.video_id == video_id).limit(1))
    video.is_available = has_any_file is not None


def record_retention_failure(db: Session, *, trigger: str, message: str) -> dict:
    settings_row = get_or_create_retention_settings(db)
    normalized_message = (message or "Unknown retention error").strip() or "Unknown retention error"
    _mark_last_run(
        settings_row,
        trigger=trigger,
        status="failed",
        message=normalized_message,
        marked_count=0,
        deleted_count=0,
        reverted_count=0,
        run_token=None,
    )
    _record_retention_run(
        db,
        trigger=trigger,
        status="failed",
        message=normalized_message,
        details={},
        marked_count=0,
        deleted_count=0,
        reverted_count=0,
        run_token=None,
    )
    db.commit()
    return {
        "status": "failed",
        "message": normalized_message,
        "marked": 0,
        "deleted": 0,
        "reverted": 0,
        "run_token": None,
    }


def _stage_video_file(
    db: Session,
    *,
    video: Video,
    video_file: VideoFile,
    run_token: str,
    staging_root: Path,
    undo_actions: list[RetentionUndoAction] | None = None,
) -> bool:
    source_path = Path(video_file.absolute_path)
    if not source_path.exists() or not source_path.is_file():
        logger.warning("Retention skip missing file video_id=%s path=%s", video.id, source_path)
        return False

    staged_path = _stage_path(staging_root, run_token, video_file)
    if staged_path.exists():
        raise RuntimeError(f"Retention staging path already exists: {staged_path}")

    file_size_bytes = video_file.file_size or source_path.stat().st_size
    marked_at = datetime.utcnow()

    _ensure_staging_directory(staged_path.parent)
    shutil.move(str(source_path), str(staged_path))
    if undo_actions is not None:
        undo_actions.append(("move", staged_path, source_path))

    retention_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
    if retention_item is None:
        retention_item = RetentionItem(
            video_id=video.id,
            video_file_id=video_file.id,
            original_absolute_path=str(source_path),
            staged_absolute_path=str(staged_path),
            original_relative_path=video_file.relative_path,
            original_video_created_at=video.created_at,
            file_size_bytes=file_size_bytes,
            file_fingerprint=video_file.fingerprint,
            marked_at=marked_at,
            delete_after_at=marked_at + RETENTION_DELETE_GRACE,
            status="staged",
            run_token=run_token,
        )
        db.add(retention_item)
    else:
        retention_item.video_id = video.id
        retention_item.original_absolute_path = str(source_path)
        retention_item.staged_absolute_path = str(staged_path)
        retention_item.original_relative_path = video_file.relative_path
        retention_item.original_video_created_at = video.created_at
        retention_item.file_size_bytes = file_size_bytes
        retention_item.file_fingerprint = video_file.fingerprint
        retention_item.marked_at = marked_at
        retention_item.delete_after_at = marked_at + RETENTION_DELETE_GRACE
        retention_item.status = "staged"
        retention_item.run_token = run_token
        retention_item.last_error = None

    video_file.absolute_path = str(staged_path)
    video.is_available = False
    return True


def _handle_missing_source_file(db: Session, *, video: Video, video_file: VideoFile) -> None:
    retention_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
    if retention_item is not None:
        retention_item.status = "error"
        retention_item.last_error = "Source file missing on disk"
        retention_item.run_token = None
    video.is_available = False


def _revert_retention_item(
    db: Session,
    item: RetentionItem,
    *,
    undo_actions: list[RetentionUndoAction] | None = None,
) -> bool:
    video_file = db.get(VideoFile, item.video_file_id)
    staged_path = Path(item.staged_absolute_path)
    original_path = Path(item.original_absolute_path)

    if not staged_path.exists() or not staged_path.is_file():
        item.status = "error"
        item.last_error = "Staged file missing"
        item.run_token = None
        return False
    if original_path.exists():
        item.status = "error"
        item.last_error = "Original path already exists"
        item.run_token = None
        return False

    original_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_path), str(original_path))
    if undo_actions is not None:
        undo_actions.append(("move", original_path, staged_path))

    if video_file:
        video_file.absolute_path = str(original_path)
        if item.original_relative_path:
            video_file.relative_path = item.original_relative_path

    item.status = "reverted"
    item.last_error = None
    item.run_token = None
    _cleanup_empty_retention_dirs(staged_path.parent, staged_path.parents[1])
    _refresh_video_availability(db, item.video_id)
    return True


def _delete_retention_item(
    db: Session,
    item: RetentionItem,
    *,
    staging_root: Path,
    delete_token: str,
    undo_actions: list[RetentionUndoAction] | None = None,
    delete_finalize_paths: list[Path] | None = None,
) -> bool:
    video_file = db.get(VideoFile, item.video_file_id) if item.video_file_id is not None else None
    video = db.get(Video, item.video_id) if item.video_id is not None else None
    staged_path = Path(item.staged_absolute_path)
    subtitle_source_path = generated_subtitle_path(Path(item.original_absolute_path))

    if not staged_path.exists() or not staged_path.is_file():
        item.status = "error"
        item.last_error = "Staged file missing"
        item.run_token = None
        return False

    delete_buffer_path = _delete_buffer_path(staging_root, delete_token, item)
    if delete_buffer_path.exists():
        raise RuntimeError(f"Retention delete buffer already exists: {delete_buffer_path}")

    _ensure_staging_directory(delete_buffer_path.parent)
    shutil.move(str(staged_path), str(delete_buffer_path))
    if undo_actions is not None:
        undo_actions.append(("move", delete_buffer_path, staged_path))
    if delete_finalize_paths is not None:
        delete_finalize_paths.append(delete_buffer_path)
    _cleanup_empty_retention_dirs(staged_path.parent, staging_root)

    if subtitle_source_path.exists() and subtitle_source_path.is_file():
        delete_buffer_subtitle_path = delete_buffer_path.with_name(subtitle_source_path.name)
        if delete_buffer_subtitle_path.exists():
            raise RuntimeError(f"Retention subtitle delete buffer already exists: {delete_buffer_subtitle_path}")
        shutil.move(str(subtitle_source_path), str(delete_buffer_subtitle_path))
        if undo_actions is not None:
            undo_actions.append(("move", delete_buffer_subtitle_path, subtitle_source_path))
        if delete_finalize_paths is not None:
            delete_finalize_paths.append(delete_buffer_subtitle_path)

    remaining_file = None
    if video is not None:
        remaining_file_query = select(VideoFile.id).where(VideoFile.video_id == video.id)
        if video_file is not None:
            remaining_file_query = remaining_file_query.where(VideoFile.id != video_file.id)
        remaining_file = db.scalar(remaining_file_query.limit(1))

    item.video_file_id = None
    if video is None or remaining_file is None:
        item.video_id = None
    db.flush()

    if video_file:
        db.delete(video_file)
        db.flush()

    if video is not None:
        if remaining_file is None:
            _delete_video_dependencies(db, video)
        else:
            _refresh_video_availability(db, video.id)

    item.video_id = None
    item.status = "deleted"
    item.last_error = None
    item.run_token = None
    return True


def _most_recent_weekly_schedule(settings_row: RetentionSettings, now: datetime) -> datetime:
    scheduled_today = now.replace(
        hour=settings_row.auto_time_hour,
        minute=settings_row.auto_time_minute,
        second=0,
        microsecond=0,
    )
    days_ago = (now.weekday() - settings_row.auto_weekday) % 7
    scheduled_at = scheduled_today - timedelta(days=days_ago)
    if scheduled_at > now:
        scheduled_at -= timedelta(days=7)
    return scheduled_at


def _retention_schedule_now(settings_row: RetentionSettings, now: datetime | None = None) -> datetime:
    if now is None:
        return coerce_datetime_to_timezone(server_now(), _retention_timezone_name(settings_row))
    return coerce_datetime_to_timezone(now, _retention_timezone_name(settings_row))


def _retention_localize_utc(stored_at: datetime | None, settings_row: RetentionSettings) -> datetime | None:
    return localize_utc_to_timezone(stored_at, _retention_timezone_name(settings_row))


def retention_auto_run_due(settings_row: RetentionSettings, *, now: datetime | None = None) -> bool:
    schedule_now = _retention_schedule_now(settings_row, now)
    last_auto_run_at = _retention_localize_utc(settings_row.last_auto_run_at, settings_row)

    if settings_row.auto_schedule_kind == "daily":
        scheduled_at = schedule_now.replace(
            hour=settings_row.auto_time_hour,
            minute=settings_row.auto_time_minute,
            second=0,
            microsecond=0,
        )
        if scheduled_at > schedule_now:
            scheduled_at -= timedelta(days=1)
        return last_auto_run_at is None or last_auto_run_at < scheduled_at

    if settings_row.auto_schedule_kind == "weekly":
        scheduled_at = _most_recent_weekly_schedule(settings_row, schedule_now)
        return last_auto_run_at is None or last_auto_run_at < scheduled_at

    interval = timedelta(minutes=settings_row.auto_interval_minutes)
    return last_auto_run_at is None or schedule_now - last_auto_run_at >= interval


def _retention_summary_message(
    *,
    marked_count: int,
    deleted_count: int,
    reverted_count: int,
    missing_source_count: int = 0,
    issue_count: int = 0,
) -> str:
    message = f"Marked {marked_count}, deleted {deleted_count}, reverted {reverted_count}"
    if missing_source_count:
        message = f"{message}, skipped {missing_source_count} missing source file{'s' if missing_source_count != 1 else ''}"
    if issue_count:
        message = f"{message}, encountered {issue_count} retention issue{'s' if issue_count != 1 else ''}"
    return message


def run_retention_cycle(db: Session, *, trigger: str = "auto", force: bool = False) -> dict:
    settings_row = get_or_create_retention_settings(db)
    now = datetime.utcnow()
    auto_disabled = trigger == "auto" and not settings_row.enabled
    auto_due = retention_auto_run_due(settings_row) if trigger == "auto" else False
    should_stage_candidates = (
        trigger == "manual"
        or (trigger == "auto" and settings_row.enabled and (force or auto_due))
    )

    exclusion_sets = _retention_exclusion_sets(db)
    saved_video_ids = _saved_video_ids(db)
    marked_count = 0
    deleted_count = 0
    reverted_count = 0
    missing_source_count = 0
    issue_count = 0
    run_token = secrets.token_hex(RETENTION_RUN_TOKEN_LENGTH)
    staging_root = effective_retention_staging_folder(settings_row)
    undo_actions: list[RetentionUndoAction] = []
    delete_finalize_paths: list[Path] = []
    marked_files: list[str] = []
    deleted_files: list[str] = []
    reverted_files: list[str] = []

    try:
        staged_items = db.scalars(
            select(RetentionItem)
            .where(RetentionItem.status == "staged")
            .order_by(RetentionItem.delete_after_at.asc(), RetentionItem.marked_at.asc())
        ).all()

        for item in staged_items:
            video = db.get(Video, item.video_id)
            if video is None:
                item.status = "error"
                item.last_error = "Video missing"
                item.run_token = None
                issue_count += 1
                continue

            if video_is_retention_exempt(video, saved_video_ids=saved_video_ids, exclusion_sets=exclusion_sets):
                file_label = _retention_file_label(
                    relative_path=item.original_relative_path,
                    absolute_path=item.original_absolute_path,
                )
                if _revert_retention_item(db, item, undo_actions=undo_actions):
                    reverted_count += 1
                    reverted_files.append(file_label)
                else:
                    issue_count += 1
                continue

            if now >= item.delete_after_at:
                file_label = _retention_file_label(
                    relative_path=item.original_relative_path,
                    absolute_path=item.original_absolute_path,
                )
                if _delete_retention_item(
                    db,
                    item,
                    staging_root=staging_root,
                    delete_token=run_token,
                    undo_actions=undo_actions,
                    delete_finalize_paths=delete_finalize_paths,
                ):
                    deleted_count += 1
                    deleted_files.append(file_label)
                else:
                    issue_count += 1

        if should_stage_candidates:
            cutoff = now - timedelta(days=max(1, settings_row.retention_days))
            retention_age_timestamp = func.coalesce(Video.published_at, Video.created_at)
            pending_video_ids = set(
                db.scalars(select(RetentionItem.video_id).where(RetentionItem.status == "staged")).all()
            )
            candidate_videos = db.scalars(
                select(Video)
                .options(joinedload(Video.files), joinedload(Video.channel), joinedload(Video.series))
                .where(
                    Video.is_available.is_(True),
                    retention_age_timestamp < cutoff,
                )
                .order_by(retention_age_timestamp.asc(), Video.id.asc())
            ).unique().all()

            for video in candidate_videos:
                if video.id in pending_video_ids or not video.files:
                    continue
                if video_is_retention_exempt(video, saved_video_ids=saved_video_ids, exclusion_sets=exclusion_sets):
                    continue

                staged_any = False
                for video_file in video.files:
                    existing_item = db.scalar(
                        select(RetentionItem).where(
                            RetentionItem.video_file_id == video_file.id,
                            RetentionItem.status == "staged",
                        )
                    )
                    if existing_item is not None:
                        staged_any = True
                        continue
                    if _stage_video_file(
                        db,
                        video=video,
                        video_file=video_file,
                        run_token=run_token,
                        staging_root=staging_root,
                        undo_actions=undo_actions,
                    ):
                        staged_any = True
                        marked_files.append(
                            _retention_file_label(
                                relative_path=video_file.relative_path,
                                absolute_path=video_file.absolute_path,
                            )
                        )
                    else:
                        _handle_missing_source_file(db, video=video, video_file=video_file)
                        missing_source_count += 1
                if staged_any:
                    marked_count += 1

        status = "completed"
        message = _retention_summary_message(
            marked_count=marked_count,
            deleted_count=deleted_count,
            reverted_count=reverted_count,
            missing_source_count=missing_source_count,
            issue_count=issue_count,
        )
        if trigger == "auto" and should_stage_candidates:
            settings_row.last_auto_run_at = now
        if (
            trigger == "auto"
            and not should_stage_candidates
            and not deleted_count
            and not reverted_count
            and not missing_source_count
            and not issue_count
        ):
            status = "skipped"
            message = "Retention disabled" if auto_disabled else "Retention not due yet"
        if trigger == "auto" and status == "skipped" and message in NOOP_AUTO_RETENTION_MESSAGES:
            _prune_noop_auto_retention_runs(db)
        else:
            _mark_last_run(
                settings_row,
                trigger=trigger,
                status=status,
                message=message,
                marked_count=marked_count,
                deleted_count=deleted_count,
                reverted_count=reverted_count,
                run_token=run_token if marked_count else None,
            )
            _record_retention_run(
                db,
                trigger=trigger,
                status=status,
                message=message,
                details=_retention_run_details_payload(
                    marked_files=marked_files,
                    deleted_files=deleted_files,
                    reverted_files=reverted_files,
                ),
                marked_count=marked_count,
                deleted_count=deleted_count,
                reverted_count=reverted_count,
                run_token=run_token if marked_count else None,
            )
        db.commit()
    except Exception:
        db.rollback()
        _undo_retention_filesystem_actions(undo_actions)
        raise

    _finalize_deleted_retention_paths(delete_finalize_paths, staging_root)
    if trigger != "auto" or any((marked_count, deleted_count, reverted_count, missing_source_count, issue_count)):
        logger.info(
            "Retention run completed trigger=%s marked=%s deleted=%s reverted=%s missing=%s issues=%s",
            trigger,
            marked_count,
            deleted_count,
            reverted_count,
            missing_source_count,
            issue_count,
        )
    return {
        "status": status,
        "message": message,
        "marked": marked_count,
        "deleted": deleted_count,
        "reverted": reverted_count,
        "run_token": run_token if marked_count else None,
    }


def revert_last_retention_run(db: Session) -> dict:
    settings_row = get_or_create_retention_settings(db)
    items = db.scalars(
        select(RetentionItem)
        .where(RetentionItem.status == "staged")
        .order_by(RetentionItem.marked_at.desc(), RetentionItem.id.desc())
    ).all()
    if not items:
        return {"status": "idle", "message": "No pending retention run to revert", "reverted": 0}

    reverted_count = 0
    issue_count = 0
    undo_actions: list[RetentionUndoAction] = []
    reverted_files: list[str] = []
    try:
        for item in items:
            file_label = _retention_file_label(
                relative_path=item.original_relative_path,
                absolute_path=item.original_absolute_path,
            )
            if _revert_retention_item(db, item, undo_actions=undo_actions):
                reverted_count += 1
                reverted_files.append(file_label)
            else:
                issue_count += 1

        message = f"Reverted {reverted_count} pending deletion{'s' if reverted_count != 1 else ''}"
        if issue_count:
            message = f"{message}, encountered {issue_count} retention issue{'s' if issue_count != 1 else ''}"
        _mark_last_run(
            settings_row,
            trigger="manual-revert",
            status="completed",
            message=message,
            marked_count=0,
            deleted_count=0,
            reverted_count=reverted_count,
            run_token=None,
        )
        _record_retention_run(
            db,
            trigger="manual-revert",
            status="completed",
            message=message,
            details=_retention_run_details_payload(reverted_files=reverted_files),
            marked_count=0,
            deleted_count=0,
            reverted_count=reverted_count,
            run_token=None,
        )
        db.commit()
    except Exception:
        db.rollback()
        _undo_retention_filesystem_actions(undo_actions)
        raise

    return {"status": "completed", "message": message, "reverted": reverted_count}


def delete_pending_retention_items(db: Session) -> dict:
    settings_row = get_or_create_retention_settings(db)
    staging_root = effective_retention_staging_folder(settings_row)
    items = db.scalars(
        select(RetentionItem)
        .where(RetentionItem.status == "staged")
        .order_by(RetentionItem.delete_after_at.asc(), RetentionItem.marked_at.asc(), RetentionItem.id.asc())
    ).all()
    if not items:
        return {"status": "idle", "message": "No pending deletions to delete", "deleted": 0}

    delete_token = secrets.token_hex(RETENTION_RUN_TOKEN_LENGTH)
    deleted_count = 0
    issue_count = 0
    undo_actions: list[RetentionUndoAction] = []
    delete_finalize_paths: list[Path] = []
    deleted_files: list[str] = []

    try:
        for item in items:
            file_label = _retention_file_label(
                relative_path=item.original_relative_path,
                absolute_path=item.original_absolute_path,
            )
            if _delete_retention_item(
                db,
                item,
                staging_root=staging_root,
                delete_token=delete_token,
                undo_actions=undo_actions,
                delete_finalize_paths=delete_finalize_paths,
            ):
                deleted_count += 1
                deleted_files.append(file_label)
            else:
                issue_count += 1

        message = f"Deleted {deleted_count} pending file{'s' if deleted_count != 1 else ''}"
        if issue_count:
            message = f"{message}, encountered {issue_count} retention issue{'s' if issue_count != 1 else ''}"
        _mark_last_run(
            settings_row,
            trigger="manual-delete",
            status="completed",
            message=message,
            marked_count=0,
            deleted_count=deleted_count,
            reverted_count=0,
            run_token=None,
        )
        _record_retention_run(
            db,
            trigger="manual-delete",
            status="completed",
            message=message,
            details=_retention_run_details_payload(deleted_files=deleted_files),
            marked_count=0,
            deleted_count=deleted_count,
            reverted_count=0,
            run_token=None,
        )
        db.commit()
    except Exception:
        db.rollback()
        _undo_retention_filesystem_actions(undo_actions)
        raise

    _finalize_deleted_retention_paths(delete_finalize_paths, staging_root)
    return {"status": "completed", "message": message, "deleted": deleted_count}


def retention_lookup(db: Session, query: str) -> dict:
    normalized = query.strip().lower()
    if len(normalized) < 2:
        return {"channels": [], "series": [], "videos": []}
    channels = db.scalars(
        select(Channel)
        .where(Channel.name.ilike(f"%{normalized}%"))
        .order_by(Channel.name.asc())
        .limit(8)
    ).all()
    series = db.scalars(
        select(Series)
        .where(Series.name.ilike(f"%{normalized}%"))
        .order_by(Series.name.asc())
        .limit(8)
    ).all()
    videos = db.scalars(
        select(Video)
        .options(joinedload(Video.channel))
        .where(Video.title.ilike(f"%{normalized}%"))
        .order_by(Video.created_at.desc(), Video.id.desc())
        .limit(10)
    ).unique().all()
    return {
        "channels": [
            {"id": item.id, "label": item.name, "subtitle": item.slug, "target_type": "channel"}
            for item in channels
        ],
        "series": [
            {"id": item.id, "label": item.name, "subtitle": item.slug, "target_type": "series"}
            for item in series
        ],
        "videos": [
            {
                "id": item.id,
                "label": item.title,
                "subtitle": item.channel.name if item.channel else None,
                "target_type": "video",
            }
            for item in videos
        ],
    }
