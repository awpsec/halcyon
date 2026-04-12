from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
import shutil
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.entities import (
    Channel,
    MetadataOverride,
    PlaylistItem,
    QueueItem,
    RetentionItem,
    SavedVideo,
    SelectedFolder,
    SyncJob,
    SyncSettings,
    TranscodeJob,
    Series,
    Video,
    VideoFile,
    VideoReaction,
    WatchHistory,
    WatchProgress,
    YouTubeChannelSnapshot,
    YouTubeCommentSnapshot,
    YouTubeMatch,
    YouTubeVideoSnapshot,
)
from app.core.config import get_settings
from app.services.media import download_thumbnail, fingerprint_file, generate_thumbnail
from app.services.utils import canonicalize_search_text, clean_display_title, is_generic_channel_name, normalize_text, parse_episode_number, resolve_display_name, slugify, tokenize_text

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
GOOGLE_SEARCH_BASE = "https://www.google.com/search"
YOUTUBE_WEB_SEARCH_BASE = "https://www.youtube.com/results"
RETURN_YOUTUBE_DISLIKE_BASE = "https://returnyoutubedislikeapi.com/votes"
logger = get_logger()
_REQUEST_LOCK = asyncio.Lock()
_SYNC_LOCK = asyncio.Lock()
_RYD_LOCK = asyncio.Lock()
_LAST_REQUEST_AT = 0.0
_RYD_REQUEST_TIMES: deque[float] = deque()
_RYD_DAY = datetime.utcnow().date()
_RYD_DAY_COUNT = 0
_RYD_BACKOFF_UNTIL: datetime | None = None
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
SYNC_STALE_AFTER = timedelta(minutes=5)
MATCH_REFRESH_AFTER = timedelta(hours=6)
CHANNEL_ART_RETRY_AFTER = timedelta(minutes=15)
RYD_RATE_LIMIT_PER_MINUTE = 100
RYD_RATE_LIMIT_PER_DAY = 10_000
YOUTUBE_API_DAILY_QUOTA_LIMIT = 10_000
YOUTUBE_API_QUOTA_COSTS = {
    "search": 100,
    "videos": 1,
    "channels": 1,
    "playlists": 1,
    "playlistItems": 1,
    "commentThreads": 1,
}
LIVE_RETENTION_STATUSES = ("staged", "error")
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

try:
    YOUTUBE_API_QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:
    YOUTUBE_API_QUOTA_TZ = timezone.utc
GENERIC_PLAYLIST_TITLES = {
    "uploads",
    "popular uploads",
    "videos",
    "all videos",
    "featured",
    "live",
    "live streams",
    "shorts",
    "saved",
}
MAX_CHANNEL_PLAYLISTS = 40
MAX_PLAYLIST_ITEMS = 300


def _ryd_next_reset_time(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time())


def _reset_ryd_day_window(now: datetime) -> None:
    global _RYD_DAY, _RYD_DAY_COUNT, _RYD_BACKOFF_UNTIL

    if now.date() != _RYD_DAY:
        _RYD_DAY = now.date()
        _RYD_DAY_COUNT = 0
        if _RYD_BACKOFF_UNTIL and now >= _RYD_BACKOFF_UNTIL:
            _RYD_BACKOFF_UNTIL = None


async def _wait_for_ryd_slot() -> bool:
    global _RYD_DAY_COUNT, _RYD_BACKOFF_UNTIL

    async with _RYD_LOCK:
        now = datetime.utcnow()
        _reset_ryd_day_window(now)
        if _RYD_BACKOFF_UNTIL and now < _RYD_BACKOFF_UNTIL:
            return False
        if _RYD_DAY_COUNT >= RYD_RATE_LIMIT_PER_DAY:
            _RYD_BACKOFF_UNTIL = _ryd_next_reset_time(now)
            logger.warning("RYD quota reached for the day; backing off until %s", _RYD_BACKOFF_UNTIL.isoformat())
            return False

        while True:
            current = time.monotonic()
            while _RYD_REQUEST_TIMES and current - _RYD_REQUEST_TIMES[0] >= 60:
                _RYD_REQUEST_TIMES.popleft()
            if len(_RYD_REQUEST_TIMES) < RYD_RATE_LIMIT_PER_MINUTE:
                break
            wait_seconds = max(0.05, 60 - (current - _RYD_REQUEST_TIMES[0]))
            await asyncio.sleep(wait_seconds)

        _RYD_REQUEST_TIMES.append(time.monotonic())
        _RYD_DAY_COUNT += 1
        return True


async def _mark_ryd_backoff_for_day() -> None:
    global _RYD_BACKOFF_UNTIL

    async with _RYD_LOCK:
        now = datetime.utcnow()
        _RYD_BACKOFF_UNTIL = _ryd_next_reset_time(now)
        logger.warning("RYD returned 429; backing off until %s", _RYD_BACKOFF_UNTIL.isoformat())


def ensure_slug_uniqueness(db: Session, model, base_slug: str) -> str:
    candidate = base_slug or "item"
    suffix = 2
    while db.scalar(select(model).where(model.slug == candidate)):
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def get_or_create_series(db: Session, name: str) -> Series:
    desired_name = clean_display_title(name).strip()
    desired_slug = slugify(desired_name)
    series = db.scalar(select(Series).where(Series.slug == desired_slug))
    if series:
        return series
    series = Series(name=desired_name, slug=ensure_slug_uniqueness(db, Series, desired_slug))
    db.add(series)
    db.flush()
    return series


def is_generic_playlist_title(title: str | None) -> bool:
    normalized = normalize_text(title or "")
    return normalized in GENERIC_PLAYLIST_TITLES


async def fetch_channel_public_playlists(
    client: httpx.AsyncClient,
    api_key: str,
    youtube_channel_id: str,
    requests_per_second: int,
) -> list[dict]:
    items: list[dict] = []
    next_page_token: str | None = None
    while len(items) < MAX_CHANNEL_PLAYLISTS:
        response = await throttled_get(
            client,
            f"{YOUTUBE_API_BASE}/playlists",
            params={
                "part": "snippet,contentDetails",
                "channelId": youtube_channel_id,
                "maxResults": min(50, MAX_CHANNEL_PLAYLISTS - len(items)),
                "pageToken": next_page_token,
                "key": api_key,
            },
            requests_per_second=requests_per_second,
        )
        if response.is_error:
            message = _extract_api_error(response)
            raise YouTubeSyncError(
                f"YouTube playlist lookup failed: {message}",
                fatal="quotaExceeded" in message or "rateLimitExceeded" in message,
            )
        payload = response.json()
        batch = payload.get("items", [])
        items.extend(batch)
        next_page_token = payload.get("nextPageToken")
        if not next_page_token or not batch:
            break
    return items[:MAX_CHANNEL_PLAYLISTS]


async def fetch_playlist_video_positions(
    client: httpx.AsyncClient,
    api_key: str,
    playlist_id: str,
    requests_per_second: int,
) -> dict[str, int]:
    positions: dict[str, int] = {}
    next_page_token: str | None = None
    fetched = 0
    while fetched < MAX_PLAYLIST_ITEMS:
        response = await throttled_get(
            client,
            f"{YOUTUBE_API_BASE}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": min(50, MAX_PLAYLIST_ITEMS - fetched),
                "pageToken": next_page_token,
                "key": api_key,
            },
            requests_per_second=requests_per_second,
        )
        if response.is_error:
            message = _extract_api_error(response)
            raise YouTubeSyncError(
                f"YouTube playlist membership lookup failed: {message}",
                fatal="quotaExceeded" in message or "rateLimitExceeded" in message,
            )
        payload = response.json()
        batch = payload.get("items", [])
        for item in batch:
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})
            video_id = (
                content_details.get("videoId")
                or snippet.get("resourceId", {}).get("videoId")
            )
            if not video_id:
                continue
            positions[video_id] = int(snippet.get("position") or 0)
        fetched += len(batch)
        next_page_token = payload.get("nextPageToken")
        if not next_page_token or not batch:
            break
    return positions


async def fetch_channel_playlist_memberships(
    client: httpx.AsyncClient,
    api_key: str,
    youtube_channel_id: str,
    requests_per_second: int,
    playlist_cache: dict[str, list[dict]],
) -> list[dict]:
    cached = playlist_cache.get(youtube_channel_id)
    if cached is not None:
        return cached

    memberships: list[dict] = []
    for playlist in await fetch_channel_public_playlists(client, api_key, youtube_channel_id, requests_per_second):
        playlist_id = playlist.get("id")
        title = clean_display_title(playlist.get("snippet", {}).get("title") or "")
        if not playlist_id or not title or is_generic_playlist_title(title):
            continue
        positions = await fetch_playlist_video_positions(client, api_key, playlist_id, requests_per_second)
        memberships.append(
            {
                "id": playlist_id,
                "title": title,
                "description": playlist.get("snippet", {}).get("description") or "",
                "item_count": int(playlist.get("contentDetails", {}).get("itemCount") or 0),
                "positions": positions,
            }
        )

    playlist_cache[youtube_channel_id] = memberships
    return memberships


def choose_playlist_series_title(
    video: Video,
    youtube_video_id: str,
    playlist_memberships: list[dict],
) -> tuple[str | None, int | None]:
    title_tokens = set(tokenize_text(video.title))
    current_series_tokens = set(tokenize_text(video.series.name)) if video.series and video.series.name else set()
    current_series_name = normalize_text(video.series.name) if video.series and video.series.name else ""
    episode_number = parse_episode_number(video.title)

    candidates: list[tuple[float, str, int | None]] = []
    for playlist in playlist_memberships:
        positions = playlist.get("positions") or {}
        if youtube_video_id not in positions:
            continue
        playlist_title = clean_display_title(playlist.get("title") or "")
        if not playlist_title or is_generic_playlist_title(playlist_title):
            continue

        playlist_tokens = set(tokenize_text(playlist_title))
        score = 1.0  # exact playlist membership baseline
        if playlist_tokens and title_tokens:
            score += (len(playlist_tokens & title_tokens) / max(1, len(playlist_tokens))) * 2.6
        if current_series_tokens and playlist_tokens:
            score += (len(playlist_tokens & current_series_tokens) / max(1, len(playlist_tokens))) * 3.0
        if current_series_name and normalize_text(playlist_title) == current_series_name:
            score += 2.0
        if episode_number is not None and len(playlist_tokens) >= 2:
            score += 0.5
        if re.search(r"\b(series|season|episode|part|arc|campaign|mod|standalone|overpoch|exile|tanoa)\b", normalize_text(playlist_title)):
            score += 0.35

        candidates.append((score, playlist_title, positions.get(youtube_video_id)))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], item[2] if item[2] is not None else 10_000, item[1].lower()))
    _, best_title, best_position = candidates[0]
    return best_title, best_position


def distinct_youtube_channel_ids_for_channel(db: Session, channel_id: int | None) -> list[str]:
    if not channel_id:
        return []
    return [
        item
        for item in db.scalars(
            select(YouTubeMatch.youtube_channel_id)
            .join(Video, Video.id == YouTubeMatch.video_id)
            .where(Video.channel_id == channel_id, YouTubeMatch.youtube_channel_id.is_not(None))
            .distinct()
        ).all()
        if item
    ]


def channel_maps_cleanly_to_youtube(db: Session, channel_id: int | None) -> str | None:
    youtube_channel_ids = distinct_youtube_channel_ids_for_channel(db, channel_id)
    if len(youtube_channel_ids) == 1:
        return youtube_channel_ids[0]
    return None


def resolve_synced_channel_target(
    db: Session,
    video: Video,
    youtube_channel_id: str | None,
    youtube_channel_title: str | None,
) -> Channel | None:
    if not youtube_channel_id:
        return video.channel

    current_channel = video.channel
    current_channel_ids = distinct_youtube_channel_ids_for_channel(db, current_channel.id if current_channel else None)
    current_channel_video_count = (
        (db.scalar(select(func.count(Video.id)).where(Video.channel_id == current_channel.id)) or 0)
        if current_channel
        else 0
    )

    existing_channel_ids = db.scalars(
        select(Video.channel_id)
        .join(YouTubeMatch, YouTubeMatch.video_id == Video.id)
        .where(
            YouTubeMatch.youtube_channel_id == youtube_channel_id,
            Video.channel_id.is_not(None),
            Video.id != video.id,
        )
        .order_by(YouTubeMatch.last_synced_at.desc().nullslast(), Video.id.desc())
    ).all()
    for existing_channel_id in existing_channel_ids:
        if not existing_channel_id:
            continue
        existing_channel = db.get(Channel, existing_channel_id)
        if not existing_channel:
            continue
        if existing_channel.slug == "unknown-channel" or is_generic_channel_name(existing_channel.name):
            continue
        video.channel = existing_channel
        return existing_channel

    if current_channel:
        current_is_generic = current_channel.slug == "unknown-channel" or is_generic_channel_name(current_channel.name)
        needs_split = (
            current_is_generic
            or len(current_channel_ids) > 1
            or (current_channel_ids and youtube_channel_id not in current_channel_ids)
        )
        if not needs_split:
            return current_channel
        if current_channel_video_count <= 1 and not current_is_generic:
            return current_channel

    desired_name = youtube_channel_title or (current_channel.name if current_channel else None) or "Unknown Channel"
    desired_slug = slugify(desired_name)
    existing_slug_channel = db.scalar(select(Channel).where(Channel.slug == desired_slug))
    if existing_slug_channel:
        existing_slug_channel_ids = distinct_youtube_channel_ids_for_channel(db, existing_slug_channel.id)
        if not existing_slug_channel_ids or youtube_channel_id in existing_slug_channel_ids:
            video.channel = existing_slug_channel
            return existing_slug_channel

    new_channel = Channel(
        name=desired_name,
        slug=ensure_slug_uniqueness(db, Channel, desired_slug),
        inferred_from_path=False,
    )
    db.add(new_channel)
    db.flush()
    video.channel = new_channel
    return new_channel


def refresh_channel_from_snapshot(channel: Channel, channel_snapshot: YouTubeChannelSnapshot | None) -> None:
    if not channel_snapshot or channel.slug == "unknown-channel":
        return
    if channel_snapshot.title:
        channel.name = resolve_display_name(channel.name, channel_snapshot.title) or channel_snapshot.title or channel.name
    if channel_snapshot.description:
        channel.description = channel_snapshot.description
    if channel_snapshot.avatar_url:
        channel.avatar_url = channel_snapshot.avatar_url
    if channel_snapshot.banner_url:
        channel.banner_url = channel_snapshot.banner_url
    channel.inferred_from_path = False


def normalize_channel_assignments(db: Session) -> None:
    generic_channels = db.scalars(select(Channel).where(Channel.slug == "unknown-channel")).all()
    changed = False
    for channel in generic_channels:
        desired_name = "Unknown Channel"
        if (
            channel.name != desired_name
            or channel.description
            or channel.avatar_url
            or channel.banner_url
            or not channel.inferred_from_path
        ):
            channel.name = desired_name
            channel.description = None
            channel.avatar_url = None
            channel.banner_url = None
            channel.inferred_from_path = True
            changed = True

    matched_videos = db.scalars(
        select(Video)
        .options(joinedload(Video.channel), joinedload(Video.youtube_match))
        .join(YouTubeMatch, YouTubeMatch.video_id == Video.id)
        .where(YouTubeMatch.youtube_channel_id.is_not(None))
    ).unique().all()
    for video in matched_videos:
        youtube_channel_id = video.youtube_match.youtube_channel_id if video.youtube_match else None
        if not youtube_channel_id:
            continue
        channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == youtube_channel_id)
        )
        target_channel = resolve_synced_channel_target(
            db,
            video,
            youtube_channel_id,
            channel_snapshot.title if channel_snapshot else (video.channel.name if video.channel else None),
        )
        if target_channel:
            refresh_channel_from_snapshot(target_channel, channel_snapshot)
            changed = True

    if changed:
        db.commit()


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _path_is_within(base_path: Path, candidate_path: Path) -> bool:
    try:
        _normalized_path(candidate_path).relative_to(_normalized_path(base_path))
        return True
    except ValueError:
        return False


def _matching_mounted_root(path: Path) -> Path | None:
    settings = get_settings()
    matches = [
        root
        for root in settings.mounted_roots
        if path == root or _path_is_within(root, path)
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(str(_normalized_path(item))))


def _is_transient_download_artifact(path: Path) -> bool:
    lowered_name = path.name.casefold()
    if any(marker in lowered_name for marker in TEMP_DOWNLOAD_MARKERS):
        return True
    return YTDLP_FRAGMENT_PATTERN.search(path.stem) is not None


def _selected_library_roots_for_mount(db: Session, mounted_root: Path) -> list[Path]:
    rows = db.scalars(
        select(SelectedFolder)
        .options(joinedload(SelectedFolder.root))
        .where(SelectedFolder.is_enabled.is_(True))
    ).all()
    selected_roots: list[Path] = []
    normalized_mounted_root = _normalized_path(mounted_root)
    for row in rows:
        if not row.root:
            continue
        root_path = Path(row.root.path)
        if _normalized_path(root_path) != normalized_mounted_root:
            continue
        candidate = root_path / row.relative_path if row.relative_path else root_path
        if candidate not in selected_roots:
            selected_roots.append(candidate)
    return selected_roots


def _organization_roots(db: Session, source_path: Path) -> tuple[Path | None, Path | None]:
    mounted_root = _matching_mounted_root(source_path)
    if mounted_root is None:
        return None, None

    selected_roots = _selected_library_roots_for_mount(db, mounted_root)
    containing = [
        candidate
        for candidate in selected_roots
        if source_path == candidate or _path_is_within(candidate, source_path)
    ]
    if containing:
        return mounted_root, max(containing, key=lambda item: len(str(_normalized_path(item))))
    if len(selected_roots) == 1:
        return mounted_root, selected_roots[0]
    return mounted_root, mounted_root


def _organization_target_path(db: Session, *, video_file: VideoFile, channel: Channel | None) -> Path | None:
    if channel is None or not channel.slug or channel.slug == "unknown-channel":
        return None

    source_path = Path(video_file.absolute_path)
    if _is_transient_download_artifact(source_path):
        return None
    mounted_root, content_root = _organization_roots(db, source_path)
    if mounted_root is None or content_root is None:
        return None

    source_within_content_root = source_path == content_root or _path_is_within(content_root, source_path)
    relative_path = source_path.relative_to(content_root if source_within_content_root else mounted_root)
    if not relative_path.parts:
        return None

    head = relative_path.parts[0]
    if head.startswith("."):
        return None
    if source_within_content_root and len(relative_path.parts) > 1 and head == channel.slug:
        return None

    tail_parts = relative_path.parts[1:] if len(relative_path.parts) > 1 else (relative_path.name,)
    return content_root / channel.slug / Path(*tail_parts)


def _next_available_path(target_path: Path, source_path: Path) -> Path:
    if target_path == source_path or not target_path.exists():
        return target_path
    counter = 2
    while True:
        candidate = target_path.with_name(f"{target_path.stem} ({counter}){target_path.suffix}")
        if candidate == source_path or not candidate.exists():
            return candidate
        counter += 1


def _file_signature(path: Path) -> tuple[int, str]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 128))
    return stat.st_size, digest.hexdigest()


def _files_look_equivalent(source_path: Path, target_path: Path, *, expected_size: int | None = None) -> bool:
    if not target_path.exists() or not target_path.is_file():
        return False
    try:
        target_size, target_digest = _file_signature(target_path)
        if source_path.exists() and source_path.is_file():
            source_size, source_digest = _file_signature(source_path)
            return source_size == target_size and source_digest == target_digest
        return expected_size is not None and target_size == expected_size
    except OSError:
        return False


def _canonical_existing_target_path(db: Session, *, video_file: VideoFile, source_path: Path, target_path: Path) -> Path | None:
    if not target_path.exists() or not target_path.is_file():
        return None
    existing_target_row = db.scalar(select(VideoFile).where(VideoFile.absolute_path == str(target_path)).limit(1))
    if existing_target_row and existing_target_row.id != video_file.id:
        return None
    try:
        if _files_look_equivalent(source_path, target_path, expected_size=video_file.file_size):
            return target_path
    except OSError:
        return None
    return None


def _video_has_live_retention_item(db: Session, video_file_id: int | None) -> bool:
    if video_file_id is None:
        return False
    return db.scalar(
        select(RetentionItem.id).where(
            RetentionItem.video_file_id == video_file_id,
            RetentionItem.status.in_(LIVE_RETENTION_STATUSES),
        ).limit(1)
    ) is not None


def video_requires_organization(db: Session, video: Video) -> bool:
    if not video.channel or not video.files or video.channel.slug == "unknown-channel":
        return False
    for video_file in video.files:
        if _video_has_live_retention_item(db, video_file.id):
            continue
        target_path = _organization_target_path(db, video_file=video_file, channel=video.channel)
        if target_path is not None and _normalized_path(target_path) != _normalized_path(Path(video_file.absolute_path)):
            return True
    return False


def _merge_unique_video_relation(
    db: Session,
    model,
    *,
    source_video_id: int,
    target_video_id: int,
    unique_field: str,
) -> None:
    rows = db.scalars(select(model).where(model.video_id == source_video_id)).all()
    for row in rows:
        unique_value = getattr(row, unique_field)
        existing = db.scalar(
            select(model).where(
                getattr(model, unique_field) == unique_value,
                model.video_id == target_video_id,
            )
        )
        if existing:
            if isinstance(row, WatchProgress):
                existing.position_seconds = max(existing.position_seconds or 0, row.position_seconds or 0)
                existing.completed = bool(existing.completed or row.completed)
            db.query(model).filter(model.id == row.id).delete(synchronize_session=False)
            continue
        db.query(model).filter(model.id == row.id).update(
            {model.video_id: target_video_id},
            synchronize_session=False,
        )


def _merge_duplicate_video_files(db: Session, *, source_video: Video, target_video: Video) -> None:
    source_files = db.scalars(select(VideoFile).where(VideoFile.video_id == source_video.id)).all()
    target_files = db.scalars(select(VideoFile).where(VideoFile.video_id == target_video.id)).all()
    target_fingerprints = {item.fingerprint for item in target_files if item.fingerprint}

    for source_file in source_files:
        source_path = Path(source_file.absolute_path)
        if not target_files:
            source_file.video_id = target_video.id
            target_files.append(source_file)
            if source_file.fingerprint:
                target_fingerprints.add(source_file.fingerprint)
            continue

        if source_file.fingerprint and source_file.fingerprint in target_fingerprints:
            if source_path.exists():
                try:
                    source_path.unlink()
                except OSError as exc:
                    logger.warning(
                        "Sync duplicate cleanup failed path=%s target_video_id=%s error=%s",
                        source_path,
                        target_video.id,
                        exc,
                    )
            retention_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == source_file.id))
            if retention_item:
                retention_item.video_id = target_video.id
                retention_item.video_file_id = None
            db.delete(source_file)
            continue

        retention_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == source_file.id))
        if retention_item:
            retention_item.video_id = target_video.id
        if source_path.exists():
            try:
                source_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Sync duplicate cleanup failed path=%s target_video_id=%s error=%s",
                    source_path,
                    target_video.id,
                    exc,
                )
        db.delete(source_file)


def merge_duplicate_video_into_target(db: Session, *, target_video: Video, duplicate_video: Video) -> None:
    if target_video.id == duplicate_video.id:
        return

    _merge_unique_video_relation(
        db,
        WatchProgress,
        source_video_id=duplicate_video.id,
        target_video_id=target_video.id,
        unique_field="user_id",
    )
    _merge_unique_video_relation(
        db,
        VideoReaction,
        source_video_id=duplicate_video.id,
        target_video_id=target_video.id,
        unique_field="user_id",
    )
    _merge_unique_video_relation(
        db,
        SavedVideo,
        source_video_id=duplicate_video.id,
        target_video_id=target_video.id,
        unique_field="user_id",
    )

    db.query(WatchHistory).filter(WatchHistory.video_id == duplicate_video.id).update(
        {WatchHistory.video_id: target_video.id},
        synchronize_session=False,
    )
    db.query(QueueItem).filter(QueueItem.video_id == duplicate_video.id).update(
        {QueueItem.video_id: target_video.id},
        synchronize_session=False,
    )
    db.query(PlaylistItem).filter(PlaylistItem.video_id == duplicate_video.id).update(
        {PlaylistItem.video_id: target_video.id},
        synchronize_session=False,
    )
    db.query(MetadataOverride).filter(
        MetadataOverride.target_type == "video",
        MetadataOverride.target_id == duplicate_video.id,
    ).update({MetadataOverride.target_id: target_video.id}, synchronize_session=False)
    db.query(TranscodeJob).filter(TranscodeJob.video_id == duplicate_video.id).update(
        {TranscodeJob.video_id: target_video.id},
        synchronize_session=False,
    )
    db.query(RetentionItem).filter(RetentionItem.video_id == duplicate_video.id).update(
        {RetentionItem.video_id: target_video.id},
        synchronize_session=False,
    )

    _merge_duplicate_video_files(db, source_video=duplicate_video, target_video=target_video)

    duplicate_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == duplicate_video.id))
    if duplicate_match:
        db.delete(duplicate_match)

    db.delete(duplicate_video)
    db.flush()


def auto_organize_channel_files(
    db: Session,
    *,
    video: Video,
    channel: Channel | None,
) -> list[tuple[Path, Path]]:
    if channel is None or not channel.slug or channel.slug == "unknown-channel":
        return []

    moved_paths: list[tuple[Path, Path]] = []
    path_updates: list[tuple[VideoFile, str, str]] = []
    relinked_duplicate_sources: list[tuple[Path, Path]] = []

    try:
        for video_file in list(video.files or []):
            if _video_has_live_retention_item(db, video_file.id):
                continue

            source_path = Path(video_file.absolute_path)
            target_base_path = _organization_target_path(db, video_file=video_file, channel=channel)
            if target_base_path is None:
                continue

            mounted_root = _matching_mounted_root(source_path)
            if mounted_root is None:
                continue

            canonical_target_path = _canonical_existing_target_path(
                db,
                video_file=video_file,
                source_path=source_path,
                target_path=target_base_path,
            )
            if canonical_target_path is not None:
                path_updates.append((video_file, video_file.absolute_path, video_file.relative_path))
                if source_path.exists() and source_path.is_file() and source_path != canonical_target_path:
                    source_path.unlink()
                    relinked_duplicate_sources.append((canonical_target_path, source_path))
                video_file.absolute_path = str(canonical_target_path)
                video_file.relative_path = canonical_target_path.relative_to(mounted_root).as_posix()
                continue

            if not source_path.exists() or not source_path.is_file():
                continue

            target_path = _next_available_path(target_base_path, source_path)
            if target_path == source_path:
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(target_path))
            moved_paths.append((target_path, source_path))
            path_updates.append((video_file, video_file.absolute_path, video_file.relative_path))
            video_file.absolute_path = str(target_path)
            video_file.relative_path = target_path.relative_to(mounted_root).as_posix()
        return moved_paths
    except Exception:
        for moved_path, original_path in reversed(moved_paths):
            if moved_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved_path), str(original_path))
        for canonical_path, original_path in reversed(relinked_duplicate_sources):
            if canonical_path.exists() and not original_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(canonical_path), str(original_path))
        for video_file, original_absolute_path, original_relative_path in path_updates:
            video_file.absolute_path = original_absolute_path
            video_file.relative_path = original_relative_path
        raise


class YouTubeSyncError(RuntimeError):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def current_youtube_quota_day(now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(YOUTUBE_API_QUOTA_TZ).date().isoformat()


def normalize_youtube_api_quota(settings_row: SyncSettings, *, now: datetime | None = None) -> bool:
    current_day = current_youtube_quota_day(now)
    changed = False
    if settings_row.youtube_api_quota_day != current_day:
        settings_row.youtube_api_quota_day = current_day
        settings_row.youtube_api_quota_used_units = 0
        changed = True
    elif settings_row.youtube_api_quota_used_units is None:
        settings_row.youtube_api_quota_used_units = 0
        changed = True
    return changed


def build_youtube_api_quota_summary(settings_row: SyncSettings) -> dict[str, int | float | bool]:
    used_units = max(0, min(int(settings_row.youtube_api_quota_used_units or 0), YOUTUBE_API_DAILY_QUOTA_LIMIT))
    remaining_units = max(0, YOUTUBE_API_DAILY_QUOTA_LIMIT - used_units)
    remaining_percent = (remaining_units / YOUTUBE_API_DAILY_QUOTA_LIMIT) * 100 if YOUTUBE_API_DAILY_QUOTA_LIMIT else 0.0
    return {
        "youtube_api_quota_daily_limit": YOUTUBE_API_DAILY_QUOTA_LIMIT,
        "youtube_api_quota_used_units": used_units,
        "youtube_api_quota_remaining_units": remaining_units,
        "youtube_api_quota_remaining_percent": round(remaining_percent, 2),
        "youtube_api_quota_estimated": True,
    }


def track_youtube_api_quota_request(url: str) -> None:
    if not url.startswith(YOUTUBE_API_BASE):
        return
    endpoint = url.rstrip("/").rsplit("/", 1)[-1]
    cost = YOUTUBE_API_QUOTA_COSTS.get(endpoint)
    if not cost:
        return
    try:
        with SessionLocal() as db:
            settings_row = db.scalar(select(SyncSettings))
            if not settings_row:
                settings_row = SyncSettings()
                db.add(settings_row)
                db.flush()
            normalize_youtube_api_quota(settings_row)
            settings_row.youtube_api_quota_used_units = min(
                YOUTUBE_API_DAILY_QUOTA_LIMIT,
                int(settings_row.youtube_api_quota_used_units or 0) + cost,
            )
            db.commit()
    except Exception as exc:
        logger.warning("Skipping YouTube quota tracking endpoint=%s error=%s", endpoint, exc)


async def throttled_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    requests_per_second: int = 3,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    global _LAST_REQUEST_AT

    effective_rps = max(1, requests_per_second)
    min_interval = 1.0 / effective_rps
    async with _REQUEST_LOCK:
        wait_seconds = max(0.0, min_interval - (time.monotonic() - _LAST_REQUEST_AT))
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        track_youtube_api_quota_request(url)
        response = await client.get(url, params=params, headers=headers)
        _LAST_REQUEST_AT = time.monotonic()
    return response


def parse_abbreviated_number(value: str | None) -> int | None:
    if not value:
        return None
    working = value.replace(",", "").strip().upper()
    multiplier = 1
    if working.endswith("K"):
        multiplier = 1_000
        working = working[:-1]
    elif working.endswith("M"):
        multiplier = 1_000_000
        working = working[:-1]
    elif working.endswith("B"):
        multiplier = 1_000_000_000
        working = working[:-1]
    try:
        return int(float(working) * multiplier)
    except ValueError:
        return None


def parse_channel_stat_text(value: Any) -> int | None:
    text = extract_text_content(value)
    if not text:
        return None
    normalized = re.sub(r"\b(subscribers?|views?|videos?)\b", "", text, flags=re.IGNORECASE).strip()
    normalized = normalized.replace(" ", "")
    return parse_abbreviated_number(normalized)


def parse_maybe_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_published_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    working = value.strip()
    try:
        if len(working) == 10 and working.count("-") == 2:
            return datetime.fromisoformat(f"{working}T00:00:00+00:00")
        return datetime.fromisoformat(working.replace("Z", "+00:00"))
    except ValueError:
        return None


TRUSTED_PUBLISHED_AT_SOURCES = {"youtube-api", "watch-page"}


def resolve_snapshot_published_at(
    *,
    youtube_published_at: str | None,
    source: str | None,
    existing_published_at: datetime | None = None,
    existing_source: str | None = None,
) -> tuple[datetime | None, str | None]:
    if source in TRUSTED_PUBLISHED_AT_SOURCES:
        parsed = parse_published_datetime(youtube_published_at)
        if parsed:
            return parsed, source
    if existing_published_at and existing_source in TRUSTED_PUBLISHED_AT_SOURCES:
        return existing_published_at, existing_source
    return None, None


def parse_human_date(value: str | None) -> datetime | None:
    if not value:
        return None
    working = value.replace("Joined", "").strip()
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime(working, pattern)
        except ValueError:
            continue
    return None


def extract_text_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        parts = [extract_text_content(item) for item in value]
        cleaned = [part for part in parts if part]
        return " ".join(cleaned).strip() or None
    if isinstance(value, dict):
        for key in ("content", "simpleText", "text"):
            if key in value:
                return extract_text_content(value.get(key))
        if "runs" in value:
            return extract_text_content(value.get("runs"))
    return None


def find_nested_mapping(value: Any, key: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if key in value and isinstance(value[key], dict):
            return value[key]
        for item in value.values():
            found = find_nested_mapping(item, key)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_nested_mapping(item, key)
            if found:
                return found
    return None


def collect_image_sources(value: Any, results: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("sourceUrl")
        if isinstance(url, str) and url.startswith("http"):
            results.append(
                {
                    "url": url,
                    "width": parse_maybe_int(value.get("width")),
                    "height": parse_maybe_int(value.get("height")),
                }
            )
        for item in value.values():
            collect_image_sources(item, results)
    elif isinstance(value, list):
        for item in value:
            collect_image_sources(item, results)


def is_trusted_channel_art_url(url: str, *, banner: bool) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.netloc or "").casefold()
    path = (parsed.path or "").casefold()
    query = (parsed.query or "").casefold()
    if "favicon" in path or "favicon" in query:
        return False
    if host.startswith("encrypted-tbn") and host.endswith("gstatic.com"):
        return False
    if banner:
        return host in {"yt3.googleusercontent.com", "yt3.ggpht.com", "i.ytimg.com"}
    return host in {"yt3.googleusercontent.com", "yt3.ggpht.com"}


def pick_best_image_url(value: Any, *, banner: bool) -> str | None:
    images: list[dict[str, Any]] = []
    collect_image_sources(value, images)
    images = [item for item in images if is_trusted_channel_art_url(item.get("url") or "", banner=banner)]
    if not images:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        width = item.get("width") or 0
        height = item.get("height") or 0
        ratio = (width / height) if width and height else 0
        if banner:
            is_bannerish = 1 if ratio >= 2.2 else 0
            return (is_bannerish, width, height)
        is_avatarish = 1 if 0.75 <= ratio <= 1.5 or not ratio else 0
        return (is_avatarish, width, height)

    return max(images, key=score).get("url")


def normalize_links(value: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(value, list):
        return results
    for item in value:
        candidate = item.get("channelExternalLinkViewModel") if isinstance(item, dict) else None
        payload = candidate if isinstance(candidate, dict) else item
        if not isinstance(payload, dict):
            continue
        title = (
            extract_text_content(payload.get("title"))
            or extract_text_content(payload.get("linkTitle"))
            or extract_text_content(payload.get("content"))
            or extract_text_content(payload.get("displayUrl"))
        )
        url = payload.get("link", {}).get("content") if isinstance(payload.get("link"), dict) else None
        if not url and isinstance(payload.get("navigationEndpoint"), dict):
            url = payload.get("navigationEndpoint", {}).get("urlEndpoint", {}).get("url")
        if not url:
            url = extract_text_content(payload.get("link"))
        if isinstance(url, str) and url.startswith("/redirect?q="):
            parsed = parse_qs(urlparse(url).query)
            url = parsed.get("q", [url])[0]
        if isinstance(url, str):
            url = url.strip()
            if url.startswith("//"):
                url = f"https:{url}"
            elif url.startswith("/"):
                url = f"https://www.youtube.com{url}"
            elif url.startswith("www."):
                url = f"https://{url}"
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        parsed_url = urlparse(url)
        url = parsed_url._replace(fragment="").geturl().rstrip("/")
        if url in seen:
            continue
        seen.add(url)
        cleaned = {"title": (title or parsed_url.netloc.replace("www.", "") or url).strip(), "url": url}
        if cleaned not in results:
            results.append(cleaned)
    return results


def upgrade_banner_url(url: str | None) -> str | None:
    if not url:
        return None
    upgraded = re.sub(r"=w\d+(?:-.*)?$", "=w2560-fcrop64=1,00005a57ffffa5a8-k-c0xffffffff-no-nd-rj", url)
    if upgraded == url and "yt3.googleusercontent.com" in url and "=" not in url.rsplit("/", 1)[-1]:
        upgraded = f"{url}=w2560-fcrop64=1,00005a57ffffa5a8-k-c0xffffffff-no-nd-rj"
    return upgraded


def prefer_high_res_banners_enabled(db: Session) -> bool:
    settings_row = db.scalar(select(SyncSettings))
    return bool(settings_row and settings_row.prefer_high_res_banners)


def allow_fallback_art_enabled(db: Session) -> bool:
    settings_row = db.scalar(select(SyncSettings))
    return bool(settings_row and settings_row.allow_fallback_art)


def extract_json_blob(text: str, markers: list[str]) -> dict | None:
    for marker in markers:
        marker_index = text.find(marker)
        if marker_index == -1:
            continue
        start = text.find("{", marker_index)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "\"":
                    in_string = False
                continue
            if char == "\"":
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
    return None


def extract_meta_content(text: str, property_name: str) -> str | None:
    pattern = rf'<meta\s+(?:property|name)="{re.escape(property_name)}"\s+content="([^"]+)"'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).replace("&amp;", "&").strip() or None


def is_snapshot_fresh(value: datetime | None, *, max_age: timedelta = MATCH_REFRESH_AFTER) -> bool:
    if not value:
        return False
    compare = value.replace(tzinfo=None) if value.tzinfo else value
    return datetime.utcnow() - compare <= max_age


def extract_video_ids_from_urls(urls: list[str]) -> list[str]:
    video_ids: list[str] = []
    for raw_url in urls:
        parsed = urlparse(raw_url)
        candidate = parse_qs(parsed.query).get("v", [None])[0]
        if not candidate:
            continue
        if candidate not in video_ids:
            video_ids.append(candidate)
    return video_ids


def _extract_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = error.get("message") or response.text or response.reason_phrase
    reasons = error.get("errors") or []
    reason = reasons[0].get("reason") if reasons and isinstance(reasons[0], dict) else None
    if reason:
        return f"{reason}: {message}"
    return message


def score_match(video: Video, item: dict, *, channel_hints: list[str] | None = None) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    title = normalize_text(item.get("snippet", {}).get("title") or "")
    channel = normalize_text(item.get("snippet", {}).get("channelTitle") or "")
    video_title = normalize_text(video.title)
    video_tokens = set(tokenize_text(video_title))
    title_tokens = set(tokenize_text(title))
    overlap = len(video_tokens & title_tokens) / max(1, len(video_tokens))

    if video_title == title:
        score += 0.48
        reasons.append("exact-title")
    elif overlap >= 0.8:
        score += 0.42
        reasons.append("title-overlap-high")
    elif overlap >= 0.55:
        score += 0.28
        reasons.append("title-overlap")
    elif overlap >= 0.35:
        score += 0.16
        reasons.append("title-partial")
    elif overlap >= 0.2 and video.duration_seconds >= 1800:
        score += 0.08
        reasons.append("title-partial-longform")

    if video.channel and not is_generic_channel_name(video.channel.name):
        resolved_name = resolve_display_name(video.channel.name, item.get("snippet", {}).get("channelTitle"))
        if channel and resolved_name == item.get("snippet", {}).get("channelTitle"):
            score += 0.24
            reasons.append("channel")
        elif channel:
            score -= 0.18
            reasons.append("channel-mismatch")
    elif channel_hints:
        if any(resolve_display_name(hint, item.get("snippet", {}).get("channelTitle")) == item.get("snippet", {}).get("channelTitle") for hint in channel_hints):
            score += 0.24
            reasons.append("channel-hint")
    duration = item.get("_waytube_duration_seconds")
    if duration and video.duration_seconds:
        delta = abs(duration - video.duration_seconds)
        if delta <= 3:
            score += 0.28
            reasons.append("duration-tight")
        elif delta <= 10:
            score += 0.2
            reasons.append("duration")
        elif delta <= 25:
            score += 0.1
            reasons.append("duration-loose")
        elif video.duration_seconds >= 1800 and delta <= 90:
            score += 0.18
            reasons.append("duration-longform")
        elif delta >= max(90, int(video.duration_seconds * 0.2)):
            score -= 0.42
            reasons.append("duration-mismatch")
        elif delta >= max(45, int(video.duration_seconds * 0.1)):
            score -= 0.22
            reasons.append("duration-far")
    elif video.duration_seconds:
        score -= 0.06
        reasons.append("duration-missing")
    if video.published_at and item.get("snippet", {}).get("publishedAt"):
        if video.published_at.strftime("%Y-%m-%d") == item["snippet"]["publishedAt"][:10]:
            score += 0.1
            reasons.append("date")
    candidate_episode = parse_episode_number(item.get("snippet", {}).get("title") or "")
    if video.episode_number and candidate_episode:
        if video.episode_number == candidate_episode:
            score += 0.16
            reasons.append("episode")
        else:
            score -= 0.14
            reasons.append("episode-mismatch")
    if video.series and video.series.name:
        series_tokens = set(tokenize_text(video.series.name))
        if series_tokens and title_tokens:
            overlap = len(series_tokens & title_tokens) / max(1, len(series_tokens))
            if overlap >= 0.75:
                score += 0.12
                reasons.append("series")
    return score, reasons


def infer_channel_ids_from_neighbor_titles(db: Session, video: Video, *, limit: int = 3) -> list[str]:
    video_tokens = set(tokenize_text(clean_display_title(video.title) or video.title))
    if len(video_tokens) < 2:
        return []

    rows = db.execute(
        select(Video.title, YouTubeMatch.youtube_channel_id)
        .join(YouTubeMatch, YouTubeMatch.video_id == Video.id)
        .where(
            Video.id != video.id,
            YouTubeMatch.status == "matched",
            YouTubeMatch.youtube_channel_id.is_not(None),
        )
    ).all()

    scored: dict[str, float] = {}
    for candidate_title, youtube_channel_id in rows:
        if not youtube_channel_id or not candidate_title:
            continue
        candidate_tokens = set(tokenize_text(clean_display_title(candidate_title) or candidate_title))
        if not candidate_tokens:
            continue
        shared_tokens = video_tokens & candidate_tokens
        shared_count = len(shared_tokens)
        overlap = shared_count / max(1, len(video_tokens))
        if shared_count < 2 and overlap < 0.34:
            continue
        score = max(overlap, shared_count / max(1, min(len(video_tokens), len(candidate_tokens))))
        scored[youtube_channel_id] = max(scored.get(youtube_channel_id, 0.0), score)

    return [
        channel_id
        for channel_id, _score in sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def infer_channel_ids_from_series_neighbors(db: Session, video: Video, *, limit: int = 3) -> list[str]:
    if not video.series_id:
        return []

    video_tokens = set(tokenize_text(clean_display_title(video.title) or video.title))
    episode_number = parse_episode_number(video.title)
    rows = db.execute(
        select(Video.title, YouTubeMatch.youtube_channel_id)
        .join(YouTubeMatch, YouTubeMatch.video_id == Video.id)
        .where(
            Video.id != video.id,
            Video.series_id == video.series_id,
            YouTubeMatch.status == "matched",
            YouTubeMatch.youtube_channel_id.is_not(None),
        )
        .order_by(YouTubeMatch.last_synced_at.desc().nullslast(), Video.id.desc())
    ).all()

    scored: dict[str, float] = {}
    for candidate_title, youtube_channel_id in rows:
        if not youtube_channel_id:
            continue

        score = 1.0
        candidate_title = clean_display_title(candidate_title or "")
        if candidate_title:
            candidate_tokens = set(tokenize_text(candidate_title))
            if video_tokens and candidate_tokens:
                shared_tokens = video_tokens & candidate_tokens
                overlap = len(shared_tokens) / max(1, len(video_tokens))
                if overlap:
                    score += overlap
            candidate_episode = parse_episode_number(candidate_title)
            if episode_number is not None and candidate_episode is not None:
                if episode_number == candidate_episode:
                    score += 0.35
                elif abs(episode_number - candidate_episode) <= 2:
                    score += 0.18

        scored[youtube_channel_id] = max(scored.get(youtube_channel_id, 0.0), score)

    return [
        channel_id
        for channel_id, _score in sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def channel_name_hints_for_ids(db: Session, channel_ids: list[str]) -> list[str]:
    hints: list[str] = []
    for channel_id in channel_ids:
        snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == channel_id)
        )
        if snapshot and snapshot.title:
            resolved = clean_display_title(snapshot.title)
            if resolved and resolved not in hints:
                hints.append(resolved)
            continue

        channel_name = db.scalar(
            select(Channel.name)
            .join(Video, Video.channel_id == Channel.id)
            .join(YouTubeMatch, YouTubeMatch.video_id == Video.id)
            .where(YouTubeMatch.youtube_channel_id == channel_id)
            .order_by(Video.id.desc())
            .limit(1)
        )
        if channel_name:
            resolved = clean_display_title(channel_name)
            if resolved and resolved not in hints:
                hints.append(resolved)
    return hints


def video_requires_discovery(video: Video) -> bool:
    match = video.youtube_match
    if not match:
        return True
    if match.status in {"unmatched", "review"}:
        return True
    if not match.youtube_video_id or not match.youtube_channel_id:
        return True
    return False


def channel_art_requires_refresh(
    db: Session,
    video: Video,
    *,
    api_key_available: bool,
    allow_fallback_art: bool,
    prefer_high_res_banners: bool,
) -> bool:
    match = video.youtube_match
    if not match or match.status != "matched" or not match.youtube_channel_id:
        return False
    channel_snapshot = db.scalar(select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == match.youtube_channel_id))
    if not channel_snapshot:
        return api_key_available or allow_fallback_art
    if prefer_high_res_banners and channel_snapshot.banner_url:
        upgraded_banner = upgrade_banner_url(channel_snapshot.banner_url)
        if upgraded_banner and upgraded_banner != channel_snapshot.banner_url:
            return True
    if not api_key_available and not allow_fallback_art:
        return False
    if channel_snapshot.avatar_url and channel_snapshot.banner_url:
        return False
    return not is_snapshot_fresh(channel_snapshot.fetched_at, max_age=CHANNEL_ART_RETRY_AFTER)


def video_requires_refresh(
    db: Session,
    video: Video,
    *,
    api_key_available: bool = False,
    allow_fallback_art: bool = False,
    prefer_high_res_banners: bool = False,
) -> bool:
    match = video.youtube_match
    if not match or match.status != "matched" or not match.youtube_video_id:
        return False
    snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == match.youtube_video_id))
    if not snapshot:
        return True
    if not is_snapshot_fresh(snapshot.fetched_at):
        return True
    if snapshot.view_count is None or snapshot.like_count is None or snapshot.dislike_count is None:
        return True
    if (
        snapshot.published_at is None
        or snapshot.published_at_source not in TRUSTED_PUBLISHED_AT_SOURCES
        or snapshot.duration_seconds is None
        or not snapshot.thumbnail_url
    ):
        return True
    if channel_art_requires_refresh(
        db,
        video,
        api_key_available=api_key_available,
        allow_fallback_art=allow_fallback_art,
        prefer_high_res_banners=prefer_high_res_banners,
    ):
        return True
    return False


def parse_iso8601_duration(value: str | None) -> int | None:
    if not value:
        return None
    hours = minutes = seconds = 0
    working = value.replace("PT", "")
    current = ""
    for char in working:
        if char.isdigit():
            current += char
        elif char == "H":
            hours = int(current or "0")
            current = ""
        elif char == "M":
            minutes = int(current or "0")
            current = ""
        elif char == "S":
            seconds = int(current or "0")
            current = ""
    return hours * 3600 + minutes * 60 + seconds


def build_search_queries(
    video: Video,
    *,
    include_channel: bool = True,
    channel_hints: list[str] | None = None,
) -> list[str]:
    raw_title = clean_display_title(video.title)
    normalized_title = " ".join(tokenize_text(video.title))
    title_tokens = tokenize_text(video.title)
    compact_title = " ".join(title_tokens[:12]) if title_tokens else normalized_title
    queries = []
    hinted_channels = [hint.strip() for hint in (channel_hints or []) if hint and hint.strip()]
    if include_channel and video.channel and video.channel.name:
        queries.append(f"{video.channel.name} {raw_title}".strip())
        queries.append(f"{video.channel.name} {compact_title}".strip())
        queries.append(f"\"{compact_title}\" {video.channel.name}".strip())
    for hint in hinted_channels:
        queries.append(f"{hint} {raw_title}".strip())
        queries.append(f"{hint} {compact_title}".strip())
        queries.append(f"\"{compact_title}\" {hint}".strip())
    queries.append(raw_title)
    queries.append(f"\"{raw_title}\"")
    queries.append(normalized_title)
    if compact_title and compact_title.lower() != video.title.lower():
        queries.append(compact_title)
    if video.series and video.episode_number:
        series_title = canonicalize_search_text(video.series.name)
        queries.append(f"{series_title} part {video.episode_number}".strip())
        series_match = re.search(r"\b(\d+)\b", series_title)
        if series_match:
            queries.append(f"series {series_match.group(1)} part {video.episode_number}".strip())
    deduped: list[str] = []
    for query in queries:
        cleaned = " ".join(query.split()).strip()
        cleaned = re.sub(r"\bunknown channel\b", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped[:4]


async def fetch_channel_candidates(client: httpx.AsyncClient, api_key: str, channel_name: str, requests_per_second: int) -> list[dict]:
    response = await throttled_get(
        client,
        f"{YOUTUBE_API_BASE}/search",
        params={
            "part": "snippet",
            "maxResults": 5,
            "q": channel_name,
            "type": "channel",
            "key": api_key,
        },
        requests_per_second=requests_per_second,
    )
    if response.is_error:
        message = _extract_api_error(response)
        raise YouTubeSyncError(f"YouTube channel search failed: {message}", fatal="quotaExceeded" in message or "rateLimitExceeded" in message)
    return response.json().get("items", [])


async def fetch_search_candidates(
    client: httpx.AsyncClient,
    api_key: str,
    queries: list[str],
    requests_per_second: int,
    channel_ids: list[str] | None = None,
    status_callback=None,
) -> list[dict]:
    merged = []
    seen_ids: set[str] = set()
    scoped_channel_ids = channel_ids or [None]
    for channel_id in scoped_channel_ids:
        for index, query in enumerate(queries):
            if status_callback:
                status_callback(phase="search", source="youtube-api", query=query, channel_id=channel_id)
            logger.info("Sync youtube api query=%s channel_id=%s", query, channel_id or "")
            params = {
                "part": "snippet",
                "maxResults": 8 if channel_id else 10,
                "q": query,
                "type": "video",
                "key": api_key,
            }
            if channel_id:
                params["channelId"] = channel_id
            response = await throttled_get(client, f"{YOUTUBE_API_BASE}/search", params=params, requests_per_second=requests_per_second)
            if response.is_error:
                message = _extract_api_error(response)
                raise YouTubeSyncError(f"YouTube search failed: {message}", fatal="quotaExceeded" in message or "rateLimitExceeded" in message)
            items = response.json().get("items", [])
            if not items:
                continue

            merged.extend(
                await hydrate_video_candidates(
                    client,
                    api_key,
                    items,
                    requests_per_second=requests_per_second,
                    source="youtube-api",
                    seen_ids=seen_ids,
                )
            )
            if channel_id and len(merged) >= 8:
                break
            if merged and index == 0 and not channel_id:
                break
    return merged


async def hydrate_video_candidates(
    client: httpx.AsyncClient,
    api_key: str,
    items: list[dict],
    *,
    requests_per_second: int,
    source: str,
    seen_ids: set[str],
) -> list[dict]:
    hydrated: list[dict] = []
    ids = ",".join(item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId"))
    if not ids:
        return hydrated
    details = await throttled_get(
        client,
        f"{YOUTUBE_API_BASE}/videos",
        params={"part": "snippet,contentDetails,statistics", "id": ids, "key": api_key},
        requests_per_second=requests_per_second,
    )
    if details.is_error:
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            if not video_id or video_id in seen_ids:
                continue
            hydrated.append(
                {
                    "id": video_id,
                    "snippet": item.get("snippet", {}),
                    "statistics": {},
                    "_waytube_duration_seconds": None,
                    "_waytube_source": source,
                }
            )
            seen_ids.add(video_id)
        return hydrated

    by_id = {item["id"]: item for item in details.json().get("items", [])}
    for item in items:
        video_id = item.get("id", {}).get("videoId")
        if not video_id or video_id in seen_ids:
            continue
        detail = by_id.get(video_id)
        if not detail:
            continue
        detail["_waytube_duration_seconds"] = parse_iso8601_duration(detail.get("contentDetails", {}).get("duration"))
        detail["_waytube_source"] = source
        hydrated.append(detail)
        seen_ids.add(video_id)
    return hydrated


async def fetch_recent_channel_upload_candidates(
    client: httpx.AsyncClient,
    api_key: str,
    channel_ids: list[str],
    requests_per_second: int,
    status_callback=None,
) -> list[dict]:
    merged: list[dict] = []
    seen_ids: set[str] = set()
    for channel_id in channel_ids[:2]:
        if status_callback:
            status_callback(phase="search", source="youtube-api-channel-recent", channel_id=channel_id)
        logger.info("Sync youtube api recent channel uploads channel_id=%s", channel_id)
        response = await throttled_get(
            client,
            f"{YOUTUBE_API_BASE}/search",
            params={
                "part": "snippet",
                "maxResults": 12,
                "channelId": channel_id,
                "order": "date",
                "type": "video",
                "key": api_key,
            },
            requests_per_second=requests_per_second,
        )
        if response.is_error:
            message = _extract_api_error(response)
            raise YouTubeSyncError(
                f"YouTube recent uploads lookup failed: {message}",
                fatal="quotaExceeded" in message or "rateLimitExceeded" in message,
            )
        items = response.json().get("items", [])
        if not items:
            continue
        merged.extend(
            await hydrate_video_candidates(
                client,
                api_key,
                items,
                requests_per_second=requests_per_second,
                source="youtube-api-channel-recent",
                seen_ids=seen_ids,
            )
        )
    return merged


async def fetch_top_comments(client: httpx.AsyncClient, api_key: str, youtube_video_id: str, limit: int, requests_per_second: int) -> list[dict]:
    items: list[dict] = []
    next_page_token: str | None = None
    remaining = max(1, min(limit, 100))
    while remaining > 0:
        response = await throttled_get(
            client,
            f"{YOUTUBE_API_BASE}/commentThreads",
            params={
                "part": "snippet",
                "videoId": youtube_video_id,
                "maxResults": min(remaining, 100),
                "pageToken": next_page_token,
                "order": "relevance",
                "textFormat": "plainText",
                "key": api_key,
            },
            requests_per_second=requests_per_second,
        )
        if response.is_error:
            return items
        payload = response.json()
        batch = payload.get("items", [])
        items.extend(batch)
        remaining -= len(batch)
        next_page_token = payload.get("nextPageToken")
        if not next_page_token or not batch:
            break
    return items[: max(1, min(limit, 100))]


async def fetch_channel_details(client: httpx.AsyncClient, api_key: str, youtube_channel_id: str, requests_per_second: int) -> dict | None:
    response = await throttled_get(
        client,
        f"{YOUTUBE_API_BASE}/channels",
        params={
            "part": "snippet,statistics,brandingSettings",
            "id": youtube_channel_id,
            "maxResults": 1,
            "key": api_key,
        },
        requests_per_second=requests_per_second,
    )
    if response.is_error:
        return None
    items = response.json().get("items", [])
    return items[0] if items else None


async def fetch_channel_about_details(
    client: httpx.AsyncClient,
    youtube_channel_id: str,
    requests_per_second: int,
    *,
    include_art: bool = False,
) -> dict | None:
    response = await throttled_get(
        client,
        f"https://www.youtube.com/channel/{youtube_channel_id}/about",
        params={"hl": "en"},
        requests_per_second=requests_per_second,
        headers=REQUEST_HEADERS,
    )
    if response.is_error:
        return None

    html = response.text
    initial = extract_json_blob(html, ["var ytInitialData = ", "ytInitialData = "]) or {}
    about = find_nested_mapping(initial, "aboutChannelViewModel") or {}
    canonical_url = None
    canonical_match = re.search(r'"canonicalChannelUrl":"([^"]+)"', html)
    if canonical_match:
        canonical_url = canonical_match.group(1).replace("\\u0026", "&").replace("\\/", "/").rstrip("/")

    joined_text = extract_text_content(about.get("joinedDateText"))
    if not joined_text:
        joined_match = re.search(r'"joinedDateText":\{"content":"([^"]+)"', html)
        joined_text = joined_match.group(1).replace("\\u0026", "&") if joined_match else None

    title = extract_text_content(about.get("title"))
    description = extract_text_content(about.get("description"))
    subscriber_count = parse_channel_stat_text(about.get("subscriberCountText"))
    view_count = parse_channel_stat_text(about.get("viewCountText"))
    video_count = parse_channel_stat_text(about.get("videoCountText"))
    links = normalize_links(about.get("links"))
    if canonical_url:
        links = [link for link in links if link["url"].rstrip("/") != canonical_url]
    links = [
        link
        for link in links
        if not (
            urlparse(link["url"]).netloc.endswith("youtube.com")
            and urlparse(link["url"]).path.startswith(("/@", "/channel/", "/c/", "/user/"))
        )
    ]
    avatar_url = extract_meta_content(html, "og:image") if include_art else None
    if avatar_url and not is_trusted_channel_art_url(avatar_url, banner=False):
        avatar_url = None
    if not avatar_url and include_art:
        avatar_url = pick_best_image_url(about, banner=False)
    banner_url = pick_best_image_url(initial, banner=True) if include_art else None
    return {
        "title": title,
        "description": description,
        "joined_at": parse_human_date(joined_text),
        "canonical_url": canonical_url,
        "links": links,
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "subscriber_count": subscriber_count,
        "video_count": video_count,
        "view_count": view_count,
    }


async def fetch_video_details_by_id(
    client: httpx.AsyncClient,
    api_key: str,
    youtube_video_id: str,
    requests_per_second: int,
) -> dict | None:
    response = await throttled_get(
        client,
        f"{YOUTUBE_API_BASE}/videos",
        params={
            "part": "snippet,contentDetails,statistics",
            "id": youtube_video_id,
            "maxResults": 1,
            "key": api_key,
        },
        requests_per_second=requests_per_second,
    )
    if response.is_error:
        message = _extract_api_error(response)
        raise YouTubeSyncError(f"YouTube video refresh failed: {message}", fatal="quotaExceeded" in message or "rateLimitExceeded" in message)
    items = response.json().get("items", [])
    if not items:
        return None
    detail = items[0]
    detail["_waytube_duration_seconds"] = parse_iso8601_duration(detail.get("contentDetails", {}).get("duration"))
    detail["_waytube_source"] = "youtube-api"
    return detail


async def fetch_return_youtube_dislike_details(
    client: httpx.AsyncClient,
    youtube_video_id: str,
    requests_per_second: int,
) -> dict[str, Any] | None:
    if not await _wait_for_ryd_slot():
        return None
    response = await throttled_get(
        client,
        RETURN_YOUTUBE_DISLIKE_BASE,
        params={"videoId": youtube_video_id},
        requests_per_second=requests_per_second,
        headers=REQUEST_HEADERS,
    )
    if response.status_code == 429:
        await _mark_ryd_backoff_for_day()
        return None
    if response.is_error:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


async def fetch_google_dork_video_ids(
    client: httpx.AsyncClient,
    queries: list[str],
    requests_per_second: int,
    status_callback=None,
) -> list[str]:
    results: list[str] = []
    for query in queries:
        if status_callback:
            status_callback(phase="search", source="google-dork", query=query)
        logger.info("Sync google dork query=%s", query)
        response = await throttled_get(
            client,
            GOOGLE_SEARCH_BASE,
            params={"hl": "en", "num": 8, "q": f'site:youtube.com/watch "{query}"'},
            requests_per_second=requests_per_second,
            headers=REQUEST_HEADERS,
        )
        if response.is_error:
            continue
        urls = [
            unquote(match)
            for match in re.findall(r"/url\?q=(https?://(?:www\.)?youtube\.com/watch\?v=[^&\"'>]+)", response.text)
        ]
        for video_id in extract_video_ids_from_urls(urls):
            if video_id not in results:
                results.append(video_id)
        if results:
            break
    return results[:8]


async def fetch_youtube_web_video_ids(
    client: httpx.AsyncClient,
    queries: list[str],
    requests_per_second: int,
    status_callback=None,
) -> list[str]:
    results: list[str] = []
    for query in queries:
        if status_callback:
            status_callback(phase="search", source="youtube-web", query=query)
        logger.info("Sync youtube web query=%s", query)
        response = await throttled_get(
            client,
            YOUTUBE_WEB_SEARCH_BASE,
            params={"search_query": query, "hl": "en"},
            requests_per_second=requests_per_second,
            headers=REQUEST_HEADERS,
        )
        if response.is_error:
            continue
        for video_id in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', response.text):
            if video_id not in results:
                results.append(video_id)
            if len(results) >= 8:
                break
        if results:
            break
    return results[:8]


async def fetch_watch_page_candidate(
    client: httpx.AsyncClient,
    youtube_video_id: str,
    requests_per_second: int,
    status_callback=None,
) -> dict | None:
    if status_callback:
        status_callback(phase="fetch", source="watch-page", youtube_video_id=youtube_video_id)
    logger.info("Sync watch page fetch video_id=%s", youtube_video_id)
    response = await throttled_get(
        client,
        "https://www.youtube.com/watch",
        params={"v": youtube_video_id, "hl": "en"},
        requests_per_second=requests_per_second,
        headers=REQUEST_HEADERS,
    )
    if response.is_error:
        return None
    player = extract_json_blob(response.text, ["var ytInitialPlayerResponse = ", "ytInitialPlayerResponse = "]) or {}
    initial = extract_json_blob(response.text, ["var ytInitialData = ", "ytInitialData = "]) or {}
    video_details = player.get("videoDetails", {})
    microformat = player.get("microformat", {}).get("playerMicroformatRenderer", {})
    title = video_details.get("title")
    author = video_details.get("author")
    channel_id = video_details.get("channelId")
    description = video_details.get("shortDescription")
    length_seconds = parse_maybe_int(video_details.get("lengthSeconds"))
    view_count = parse_maybe_int(video_details.get("viewCount"))
    published_at = microformat.get("publishDate")
    thumbnails = video_details.get("thumbnail", {}).get("thumbnails", [])
    thumbnail_url = thumbnails[-1]["url"] if thumbnails else None
    html_text = response.text
    like_match = re.search(r'"label":"([\d.,KMB]+)\s+likes"', html_text)
    comment_match = re.search(r'"countText":\{"simpleText":"([\d.,KMB]+)\s+Comments?"', html_text)
    return {
        "id": youtube_video_id,
        "snippet": {
            "title": clean_display_title(title or ""),
            "channelTitle": author,
            "channelId": channel_id,
            "description": description,
            "publishedAt": f"{published_at}T00:00:00+00:00" if published_at else None,
            "thumbnails": {"high": {"url": thumbnail_url}} if thumbnail_url else {},
        },
        "statistics": {
            "viewCount": view_count,
            "likeCount": parse_abbreviated_number(like_match.group(1)) if like_match else None,
            "commentCount": parse_abbreviated_number(comment_match.group(1)) if comment_match else None,
        },
        "_waytube_duration_seconds": length_seconds,
        "_waytube_source": "watch-page",
    }


async def fetch_fallback_candidates(client: httpx.AsyncClient, queries: list[str], requests_per_second: int, status_callback=None) -> list[dict]:
    candidate_ids = await fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=status_callback)
    if not candidate_ids:
        candidate_ids = await fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=status_callback)
    candidates: list[dict] = []
    for youtube_video_id in candidate_ids[:6]:
        candidate = await fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=status_callback)
        if candidate:
            candidates.append(candidate)
    return candidates


async def apply_sync_item(
    db: Session,
    video: Video,
    item: dict,
    *,
    comment_limit: int,
    requests_per_second: int,
    client: httpx.AsyncClient,
    api_key: str | None,
    channel_cache: dict[str, dict | None] | None = None,
    playlist_cache: dict[str, list[dict]] | None = None,
    allow_fallback_art: bool = False,
    prefer_high_res_banners: bool = False,
    confidence: float = 1.0,
    reasons: list[str] | None = None,
    status: str = "matched",
) -> YouTubeMatch:
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    video_id = item.get("id")
    channel_id = snippet.get("channelId")

    if video_id:
        conflicting_matches = db.execute(
            select(YouTubeMatch)
            .options(joinedload(YouTubeMatch.video).joinedload(Video.files))
            .where(
                YouTubeMatch.youtube_video_id == video_id,
                YouTubeMatch.video_id != video.id,
            )
            .order_by(YouTubeMatch.id.asc())
        ).unique().scalars().all()
        for conflicting_match in conflicting_matches:
            if conflicting_match.video:
                logger.info(
                    "Sync merging duplicate video_id=%s duplicate_video_id=%s youtube_video_id=%s",
                    video.id,
                    conflicting_match.video_id,
                    video_id,
                )
                merge_duplicate_video_into_target(db, target_video=video, duplicate_video=conflicting_match.video)
            else:
                logger.warning(
                    "Sync clearing stale youtube match id=%s youtube_video_id=%s",
                    conflicting_match.id,
                    video_id,
                )
                db.delete(conflicting_match)
        db.flush()

    match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
    if not match:
        match = YouTubeMatch(video_id=video.id)
        db.add(match)
        db.flush()

    match.youtube_video_id = video_id
    match.youtube_channel_id = channel_id
    match.confidence = confidence
    match.reasons = reasons or []
    match.status = status
    match.last_synced_at = datetime.utcnow()
    match.stale = False

    snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == video_id))
    if not snapshot:
        snapshot = YouTubeVideoSnapshot(youtube_video_id=video_id, title=snippet.get("title", video.title))
        db.add(snapshot)
    snapshot.youtube_channel_id = channel_id
    snapshot.title = clean_display_title(snippet.get("title", video.title) or video.title)
    snapshot.description = snippet.get("description")
    snapshot.duration_seconds = item.get("_waytube_duration_seconds")
    thumbnail_url = (
        snippet.get("thumbnails", {}).get("maxres", {}).get("url")
        or snippet.get("thumbnails", {}).get("high", {}).get("url")
        or snippet.get("thumbnails", {}).get("medium", {}).get("url")
        or snippet.get("thumbnails", {}).get("default", {}).get("url")
    )
    snapshot.thumbnail_url = thumbnail_url
    snapshot.tags = snippet.get("tags", [])
    snapshot.view_count = parse_maybe_int(statistics.get("viewCount"))
    snapshot.like_count = parse_maybe_int(statistics.get("likeCount"))
    snapshot.dislike_count = None
    snapshot.rating = None
    ryd_snapshot = await fetch_return_youtube_dislike_details(client, video_id, requests_per_second)
    if ryd_snapshot:
        ryd_likes = parse_maybe_int(ryd_snapshot.get("likes"))
        ryd_raw_likes = parse_maybe_int(ryd_snapshot.get("rawLikes"))
        if ryd_likes is not None and ryd_likes > 0:
            snapshot.like_count = ryd_likes
        elif ryd_raw_likes is not None and ryd_raw_likes > 0:
            snapshot.like_count = ryd_raw_likes
        snapshot.dislike_count = parse_maybe_int(ryd_snapshot.get("dislikes"))
        try:
            snapshot.rating = float(ryd_snapshot.get("rating")) if ryd_snapshot.get("rating") is not None else None
        except (TypeError, ValueError):
            snapshot.rating = None
    snapshot.published_at, snapshot.published_at_source = resolve_snapshot_published_at(
        youtube_published_at=snippet.get("publishedAt"),
        source=item.get("_waytube_source"),
        existing_published_at=snapshot.published_at,
        existing_source=snapshot.published_at_source,
    )
    snapshot.fetched_at = datetime.utcnow()
    matched_fields: list[str] = ["title"]
    organization_moves: list[tuple[Path, Path]] = []
    if snapshot.description:
        matched_fields.append("description")
    if snapshot.published_at is not None:
        matched_fields.append("uploaded")
    if snapshot.like_count is not None:
        matched_fields.append("likes")
    if snapshot.view_count is not None:
        matched_fields.append("views")
    cache_dir = get_settings().cache_dir
    if thumbnail_url:
        fingerprint = video.files[0].fingerprint if video.files else f"yt-{video_id}"
        downloaded = download_thumbnail(thumbnail_url, cache_dir, fingerprint, force_replace=True)
        if downloaded:
            video.thumbnail_path = downloaded
    elif not video.thumbnail_path and video.files:
        generated = generate_thumbnail(Path(video.files[0].absolute_path), cache_dir, video.files[0].fingerprint)
        if generated:
            video.thumbnail_path = generated

    if channel_id:
        matched_fields.append("channel")
        channel_snapshot = db.scalar(select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == channel_id))
        if not channel_snapshot:
            channel_snapshot = YouTubeChannelSnapshot(youtube_channel_id=channel_id, title=snippet.get("channelTitle", "Unknown Channel"))
            db.add(channel_snapshot)
        channel_snapshot.title = snippet.get("channelTitle", channel_snapshot.title)
        target_channel = resolve_synced_channel_target(db, video, channel_id, channel_snapshot.title)

        if channel_cache is not None and channel_id in channel_cache:
            channel_details = channel_cache[channel_id]
        else:
            channel_details = await fetch_channel_details(client, api_key, channel_id, requests_per_second) if api_key else None
            if channel_cache is not None:
                channel_cache[channel_id] = channel_details
        needs_fallback_art = allow_fallback_art and (
            not channel_snapshot.avatar_url
            or not channel_snapshot.banner_url
        )
        channel_fallback = await fetch_channel_about_details(
            client,
            channel_id,
            requests_per_second,
            include_art=needs_fallback_art and not channel_details,
        )
        if channel_details:
            channel_snippet = channel_details.get("snippet", {})
            channel_stats = channel_details.get("statistics", {})
            branding = channel_details.get("brandingSettings", {}).get("image", {})
            thumbnails = channel_snippet.get("thumbnails", {})
            avatar = thumbnails.get("high", {}).get("url") or thumbnails.get("default", {}).get("url")

            channel_snapshot.title = channel_snippet.get("title", channel_snapshot.title)
            channel_snapshot.description = channel_snippet.get("description")
            channel_snapshot.avatar_url = avatar or channel_snapshot.avatar_url
            banner_url = branding.get("bannerExternalUrl")
            channel_snapshot.banner_url = upgrade_banner_url(banner_url) if prefer_high_res_banners else banner_url
            channel_snapshot.subscriber_count = int(channel_stats["subscriberCount"]) if channel_stats.get("subscriberCount") else None
            channel_snapshot.video_count = int(channel_stats["videoCount"]) if channel_stats.get("videoCount") else None
            channel_snapshot.view_count = int(channel_stats["viewCount"]) if channel_stats.get("viewCount") else None
        if channel_fallback:
            channel_snapshot.title = channel_fallback.get("title") or channel_snapshot.title
            channel_snapshot.description = channel_snapshot.description or channel_fallback.get("description")
            fallback_avatar = channel_fallback.get("avatar_url") if allow_fallback_art and not channel_snapshot.avatar_url else None
            if fallback_avatar:
                channel_snapshot.avatar_url = fallback_avatar
            fallback_banner = channel_fallback.get("banner_url") if allow_fallback_art and not channel_snapshot.banner_url else None
            if fallback_banner:
                channel_snapshot.banner_url = upgrade_banner_url(fallback_banner) if prefer_high_res_banners else fallback_banner
            channel_snapshot.joined_at = channel_fallback.get("joined_at")
            channel_snapshot.canonical_url = channel_fallback.get("canonical_url")
            channel_snapshot.links = channel_fallback.get("links") or []
            if channel_snapshot.subscriber_count is None:
                channel_snapshot.subscriber_count = channel_fallback.get("subscriber_count")
            if channel_snapshot.video_count is None:
                channel_snapshot.video_count = channel_fallback.get("video_count")
            if channel_snapshot.view_count is None:
                channel_snapshot.view_count = channel_fallback.get("view_count")
        elif prefer_high_res_banners and channel_snapshot.banner_url:
            channel_snapshot.banner_url = upgrade_banner_url(channel_snapshot.banner_url)
        channel_snapshot.fetched_at = datetime.utcnow()

        if target_channel:
            refresh_channel_from_snapshot(target_channel, channel_snapshot)

        channel_snapshot.fetched_at = datetime.utcnow()

        if (
            status == "matched"
            and api_key
            and video_id
            and target_channel is not None
            and playlist_cache is not None
        ):
            try:
                playlist_memberships = await fetch_channel_playlist_memberships(
                    client,
                    api_key,
                    channel_id,
                    requests_per_second,
                    playlist_cache,
                )
            except YouTubeSyncError as exc:
                logger.warning(
                    "Sync playlist fallback video_id=%s channel_id=%s error=%s",
                    video.id,
                    channel_id,
                    exc,
                )
            else:
                playlist_series_title, playlist_position = choose_playlist_series_title(
                    video,
                    video_id,
                    playlist_memberships,
                )
                if playlist_series_title:
                    target_series = get_or_create_series(db, playlist_series_title)
                    video.series_id = target_series.id
                    if video.episode_number is None and playlist_position is not None:
                        video.episode_number = playlist_position + 1
                    video.metadata_confidence = max(video.metadata_confidence or 0.0, 0.88)
                    if "playlist-membership" not in match.reasons:
                        match.reasons = [*(match.reasons or []), "playlist-membership"]
                    matched_fields.append("playlist")
                    logger.info(
                        "Sync playlist grouped video_id=%s youtube_video_id=%s series=%s position=%s",
                        video.id,
                        video_id,
                        playlist_series_title,
                        playlist_position,
                    )

        if status == "matched":
            try:
                organization_moves = auto_organize_channel_files(db, video=video, channel=target_channel)
            except Exception as exc:
                logger.warning(
                    "Sync organize skipped video_id=%s channel=%s error=%s",
                    video.id,
                    target_channel.slug if target_channel else None,
                    exc,
                )
            else:
                if organization_moves:
                    matched_fields.append("organized")
                    logger.info(
                        "Sync organized video_id=%s channel=%s moved=%s",
                        video.id,
                        target_channel.slug if target_channel else None,
                        len(organization_moves),
                    )

    if match.status == "matched" and api_key:
        db.query(YouTubeCommentSnapshot).filter(YouTubeCommentSnapshot.youtube_video_id == video_id).delete()
        for comment_item in await fetch_top_comments(client, api_key, video_id, max(1, min(comment_limit, 100)), requests_per_second):
            top = comment_item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            db.add(
                YouTubeCommentSnapshot(
                    youtube_video_id=video_id,
                    author_name=top.get("authorDisplayName", "Unknown"),
                    body=top.get("textDisplay", ""),
                    like_count=parse_maybe_int(top.get("likeCount")) or 0,
                    reply_count=comment_item.get("snippet", {}).get("totalReplyCount", 0),
                    published_at=parse_published_datetime(top.get("publishedAt")),
                )
            )
        matched_fields.append("comments")

    if match.status == "matched":
        logger.info(
            "Sync matched video_id=%s title=%s youtube_video_id=%s matched_title=%s matched_channel=%s confidence=%.4f fields=%s reasons=%s",
            video.id,
            video.title,
            video_id,
            snapshot.title or video.title,
            snippet.get("channelTitle", "Unknown Channel"),
            confidence,
            ",".join(dict.fromkeys(matched_fields)),
            ",".join(match.reasons or []),
        )

    try:
        db.commit()
    except Exception:
        db.rollback()
        for moved_path, original_path in reversed(organization_moves):
            if moved_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved_path), str(original_path))
        raise
    db.refresh(match)
    return match


async def sync_video(
    db: Session,
    video: Video,
    api_key: str | None,
    comment_limit: int,
    requests_per_second: int,
    client: httpx.AsyncClient,
    channel_cache: dict[str, dict | None] | None = None,
    playlist_cache: dict[str, list[dict]] | None = None,
    allow_fallback_art: bool = False,
    prefer_high_res_banners: bool = False,
    force: bool = False,
    status_callback=None,
) -> YouTubeMatch:
    logger.info("Sync video start video_id=%s title=%s", video.id, video.title)
    if status_callback:
        status_callback(phase="prepare", title=video.title, source="sync")
    existing_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
    if existing_match and existing_match.status == "matched" and existing_match.youtube_video_id:
        needs_refresh = force or video_requires_refresh(
            db,
            video,
            api_key_available=bool(api_key),
            allow_fallback_art=allow_fallback_art,
            prefer_high_res_banners=prefer_high_res_banners,
        )
        if not needs_refresh:
            organization_moves: list[tuple[Path, Path]] = []
            if video_requires_organization(db, video) and video.channel:
                organization_moves = auto_organize_channel_files(db, video=video, channel=video.channel)
            existing_match.last_synced_at = datetime.utcnow()
            try:
                db.commit()
            except Exception:
                db.rollback()
                for moved_path, original_path in reversed(organization_moves):
                    if moved_path.exists():
                        original_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(moved_path), str(original_path))
                raise
            db.refresh(existing_match)
            return existing_match
        if not api_key:
            if status_callback:
                status_callback(phase="refresh", source="watch-page", youtube_video_id=existing_match.youtube_video_id)
            refresh_item = await fetch_watch_page_candidate(client, existing_match.youtube_video_id, requests_per_second, status_callback=status_callback)
            if refresh_item:
                refresh_reasons = list(dict.fromkeys((existing_match.reasons or []) + ["refresh-by-id"]))
                if force:
                    refresh_reasons.append("force-refresh")
                return await apply_sync_item(
                    db,
                    video,
                    refresh_item,
                    comment_limit=comment_limit,
                    requests_per_second=requests_per_second,
                    client=client,
                    api_key=api_key,
                    channel_cache=channel_cache,
                    playlist_cache=playlist_cache,
                    allow_fallback_art=allow_fallback_art,
                    prefer_high_res_banners=prefer_high_res_banners,
                    confidence=existing_match.confidence or 1.0,
                    reasons=refresh_reasons,
                    status="matched",
                )
            if not force:
                existing_match.last_synced_at = datetime.utcnow()
                db.commit()
                db.refresh(existing_match)
                return existing_match
        else:
            if status_callback:
                status_callback(phase="refresh", source="youtube-api", youtube_video_id=existing_match.youtube_video_id)
            refresh_item = await fetch_video_details_by_id(client, api_key, existing_match.youtube_video_id, requests_per_second)
            if refresh_item:
                refresh_reasons = list(dict.fromkeys((existing_match.reasons or []) + ["refresh-by-id"]))
                if force:
                    refresh_reasons.append("force-refresh")
                return await apply_sync_item(
                    db,
                    video,
                    refresh_item,
                    comment_limit=comment_limit,
                    requests_per_second=requests_per_second,
                    client=client,
                    api_key=api_key,
                    channel_cache=channel_cache,
                    playlist_cache=playlist_cache,
                    allow_fallback_art=allow_fallback_art,
                    prefer_high_res_banners=prefer_high_res_banners,
                    confidence=existing_match.confidence or 1.0,
                    reasons=refresh_reasons,
                    status="matched",
                )
            if not force:
                existing_match.last_synced_at = datetime.utcnow()
                db.commit()
                db.refresh(existing_match)
                return existing_match

    api_error: YouTubeSyncError | None = None
    channel_ids: list[str] = []
    if video.channel_id and not force:
        channel_ids.extend(
            [
                item
                for item in db.scalars(
                    select(YouTubeMatch.youtube_channel_id)
                    .join(Video, Video.id == YouTubeMatch.video_id)
                    .where(Video.channel_id == video.channel_id, YouTubeMatch.youtube_channel_id.is_not(None))
                    .distinct()
                ).all()
                if item
            ]
        )
    if not force:
        channel_ids.extend(infer_channel_ids_from_series_neighbors(db, video))
    deduped_channel_ids: list[str] = []
    for channel_id in channel_ids:
        if channel_id not in deduped_channel_ids:
            deduped_channel_ids.append(channel_id)
    if not deduped_channel_ids and video.channel and not is_generic_channel_name(video.channel.name):
        if api_key:
            try:
                for candidate in await fetch_channel_candidates(client, api_key, video.channel.name, requests_per_second):
                    snippet = candidate.get("snippet", {})
                    resolved_name = resolve_display_name(video.channel.name, snippet.get("channelTitle"))
                    if resolved_name == snippet.get("channelTitle") and candidate.get("id", {}).get("channelId"):
                        channel_ids.append(candidate["id"]["channelId"])
            except YouTubeSyncError as exc:
                logger.warning("Sync channel fallback video_id=%s channel=%s error=%s", video.id, video.channel.name, exc)
                api_error = exc
                if status_callback:
                    status_callback(phase="fallback", title=video.title, source="google-dork", warning=str(exc))
            if channel_cache is not None:
                for channel_snapshot in channel_cache.values():
                    title = channel_snapshot.get("snippet", {}).get("title") if channel_snapshot else None
                    channel_id = channel_snapshot.get("id") if channel_snapshot else None
                    if title and channel_id and resolve_display_name(video.channel.name, title) == title:
                        deduped_channel_ids.append(channel_id)
            deduped_channel_ids = list(dict.fromkeys(deduped_channel_ids))
    if not deduped_channel_ids and (not video.channel or is_generic_channel_name(video.channel.name)):
        for channel_id in infer_channel_ids_from_neighbor_titles(db, video):
            if channel_id not in deduped_channel_ids:
                deduped_channel_ids.append(channel_id)

    channel_hints = channel_name_hints_for_ids(db, deduped_channel_ids)
    scoped_queries = build_search_queries(
        video,
        include_channel=not force and not deduped_channel_ids,
        channel_hints=channel_hints,
    )
    candidates: list[dict] = []
    if api_key:
        try:
            candidates = await fetch_search_candidates(
                client,
                api_key,
                scoped_queries,
                requests_per_second,
                channel_ids=deduped_channel_ids[:2] or None,
                status_callback=status_callback,
            )
            if not candidates and deduped_channel_ids:
                candidates = await fetch_search_candidates(
                    client,
                    api_key,
                    build_search_queries(video, include_channel=not force, channel_hints=channel_hints),
                    requests_per_second,
                    channel_ids=None,
                    status_callback=status_callback,
                )
            if deduped_channel_ids:
                recent_candidates = await fetch_recent_channel_upload_candidates(
                    client,
                    api_key,
                    deduped_channel_ids,
                    requests_per_second,
                    status_callback=status_callback,
                )
                if recent_candidates:
                    seen_candidate_ids = {
                        item.get("id")
                        for item in candidates
                        if item.get("id")
                    }
                    candidates.extend(
                        candidate
                        for candidate in recent_candidates
                        if candidate.get("id") and candidate.get("id") not in seen_candidate_ids
                    )
        except YouTubeSyncError as exc:
            api_error = exc
            logger.warning("Sync api fallback video_id=%s title=%s error=%s", video.id, video.title, exc)
            if status_callback:
                status_callback(phase="fallback", title=video.title, source="google-dork", warning=str(exc))
    if not candidates:
        candidates = await fetch_fallback_candidates(
            client,
            build_search_queries(video, include_channel=not force, channel_hints=channel_hints),
            requests_per_second,
            status_callback=status_callback,
        )
    best_score = 0.0
    best_item = None
    reasons: list[str] = []
    for candidate in candidates:
        score, candidate_reasons = score_match(video, candidate, channel_hints=channel_hints)
        if score > best_score:
            best_score = score
            best_item = candidate
            reasons = candidate_reasons

    if best_item:
        channel_id = best_item.get("snippet", {}).get("channelId")
        known_channel_match = bool(channel_id and channel_id in deduped_channel_ids)
        generic_threshold = 0.72 if "duration-tight" in reasons or "duration" in reasons or "exact-title" in reasons else 0.8
        match_status = "matched" if best_score >= generic_threshold or (known_channel_match and best_score >= 0.58) else "review"
        return await apply_sync_item(
            db,
            video,
            best_item,
            comment_limit=comment_limit,
            requests_per_second=requests_per_second,
            client=client,
            api_key=api_key,
            channel_cache=channel_cache,
            playlist_cache=playlist_cache,
            allow_fallback_art=allow_fallback_art,
            prefer_high_res_banners=prefer_high_res_banners,
            confidence=best_score,
            reasons=reasons,
            status=match_status,
        )
    else:
        match = existing_match or db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        if not match:
            match = YouTubeMatch(video_id=video.id)
            db.add(match)
            db.flush()
        match.status = "unmatched"
        match.confidence = 0.0
        match.reasons = []
        match.stale = True
        logger.info("Sync unmatched video_id=%s title=%s", video.id, video.title)

    db.commit()
    db.refresh(match)
    return match


async def sync_scope(
    db: Session,
    scope: str,
    target_id: int | None,
    api_key: str | None,
    *,
    force: bool = False,
    prefer_high_res_banners_override: bool | None = None,
    quiet_if_idle: bool = False,
) -> SyncJob:
    async with _SYNC_LOCK:
        normalize_channel_assignments(db)
        settings = db.scalar(select(SyncSettings))
        comment_limit = settings.comment_limit if settings else 100
        requests_per_second = settings.requests_per_second if settings and settings.requests_per_second else 3
        allow_fallback_art = allow_fallback_art_enabled(db)
        prefer_high_res_banners = prefer_high_res_banners_override if prefer_high_res_banners_override is not None else prefer_high_res_banners_enabled(db)
        job = SyncJob(scope=scope, target_id=target_id, status="running", started_at=datetime.utcnow(), details={})
        db.add(job)
        db.commit()
        db.refresh(job)

        all_videos = db.scalars(select(Video).options(joinedload(Video.channel), joinedload(Video.series), joinedload(Video.youtube_match))).unique().all()
        if scope == "library":
            videos = [
                video
                for video in all_videos
                if video_requires_discovery(video)
                or video_requires_organization(db, video)
                or video_requires_refresh(
                    db,
                    video,
                    api_key_available=bool(api_key),
                    allow_fallback_art=allow_fallback_art,
                    prefer_high_res_banners=prefer_high_res_banners,
                )
            ]
        elif scope == "orphans":
            videos = [
                video
                for video in all_videos
                if video_requires_discovery(video)
                or video_requires_organization(db, video)
                or (video.channel and (video.channel.slug == "unknown-channel" or is_generic_channel_name(video.channel.name)))
                or channel_art_requires_refresh(
                    db,
                    video,
                    api_key_available=bool(api_key),
                    allow_fallback_art=allow_fallback_art,
                    prefer_high_res_banners=prefer_high_res_banners,
                )
            ]
        elif scope == "video" and target_id:
            video = db.get(Video, target_id)
            videos = [video] if video else []
        elif scope == "channel" and target_id:
            videos = [
                video
                for video in all_videos
                if video.channel_id == target_id
                and (
                    force
                    or prefer_high_res_banners_override is not None
                    or video_requires_discovery(video)
                    or video_requires_organization(db, video)
                    or video_requires_refresh(
                        db,
                        video,
                        api_key_available=bool(api_key),
                        allow_fallback_art=allow_fallback_art,
                        prefer_high_res_banners=prefer_high_res_banners,
                    )
                )
            ]
        elif scope == "series" and target_id:
            videos = [
                video
                for video in all_videos
                if video.series_id == target_id
                and (
                    force
                    or video_requires_discovery(video)
                    or video_requires_organization(db, video)
                    or video_requires_refresh(
                        db,
                        video,
                        api_key_available=bool(api_key),
                        allow_fallback_art=allow_fallback_art,
                        prefer_high_res_banners=prefer_high_res_banners,
                    )
                )
            ]
        else:
            videos = []

        matched = 0
        review = 0
        processed = 0
        errors = 0
        try:
            total = len(videos)
            channel_cache: dict[str, dict | None] = {}
            playlist_cache: dict[str, list[dict]] = {}
            job.details = {
                "processed": 0,
                "total": total,
                "percent": 0,
                "requests_per_second": requests_per_second,
                "force": force,
                "prefer_high_res_banners": prefer_high_res_banners,
            }
            db.commit()
            if not (quiet_if_idle and total == 0):
                logger.info("Sync started scope=%s target_id=%s total=%s", scope, target_id, total)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=REQUEST_HEADERS) as client:
                for video in videos:
                    if db.get(Video, video.id) is None:
                        continue

                    def report_status(**extra: dict) -> None:
                        current_percent = round((processed / total) * 100) if total else 100
                        job.details = {
                            "processed": processed,
                            "total": total,
                            "percent": current_percent,
                            "matched": matched,
                            "review": review,
                            "errors": errors,
                            "title": video.title,
                            "requests_per_second": requests_per_second,
                            "prefer_high_res_banners": prefer_high_res_banners,
                            **extra,
                        }
                        db.commit()

                    report_status(phase="search", source="sync", query=video.title)
                    try:
                        result = await sync_video(
                            db,
                            video,
                            api_key,
                            comment_limit,
                            requests_per_second,
                            client,
                            channel_cache=channel_cache,
                            playlist_cache=playlist_cache,
                            allow_fallback_art=allow_fallback_art,
                            prefer_high_res_banners=prefer_high_res_banners,
                            force=force,
                            status_callback=report_status,
                        )
                    except YouTubeSyncError as exc:
                        processed += 1
                        errors += 1
                        logger.warning("Sync warning scope=%s video_id=%s error=%s", scope, video.id, exc)
                        job.details = {
                            "processed": processed,
                            "total": total,
                            "percent": round((processed / total) * 100) if total else 100,
                            "matched": matched,
                            "review": review,
                            "errors": errors,
                            "warning": str(exc),
                            "title": video.title,
                            "requests_per_second": requests_per_second,
                            "force": force,
                            "prefer_high_res_banners": prefer_high_res_banners,
                        }
                        db.commit()
                        if exc.fatal:
                            api_key = None
                            logger.warning("Sync disabling API for remainder of scope=%s after fatal upstream error", scope)
                        continue

                    if result.status == "matched":
                        matched += 1
                    elif result.status == "review":
                        review += 1
                    processed += 1
                    job.details = {
                        "processed": processed,
                        "total": total,
                        "percent": round((processed / total) * 100) if total else 100,
                        "matched": matched,
                        "review": review,
                        "errors": errors,
                        "title": video.title,
                        "requests_per_second": requests_per_second,
                        "force": force,
                        "prefer_high_res_banners": prefer_high_res_banners,
                    }
                    db.commit()

            if settings and scope == "library":
                settings.last_library_sync_at = datetime.utcnow()

            job.status = "partial" if errors else "completed"
            job.finished_at = datetime.utcnow()
            job.details = {
                "matched": matched,
                "review": review,
                "processed": processed,
                "total": total,
                "percent": 100,
                "errors": errors,
                "warning": None if not errors else "Some items could not be refreshed from YouTube",
                "force": force,
                "prefer_high_res_banners": prefer_high_res_banners,
            }
            db.commit()
            db.refresh(job)
            if not (quiet_if_idle and total == 0 and matched == 0 and review == 0 and errors == 0):
                logger.info("Sync finished scope=%s target_id=%s matched=%s review=%s errors=%s status=%s", scope, target_id, matched, review, errors, job.status)
            return job
        except YouTubeSyncError as exc:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.details = {"processed": processed, "total": len(videos), "matched": matched, "review": review, "error": str(exc)}
            db.commit()
            db.refresh(job)
            logger.exception("Sync failed scope=%s target_id=%s error=%s", scope, target_id, exc)
            return job
        except Exception as exc:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.details = {"processed": processed, "total": len(videos), "matched": matched, "review": review, "error": str(exc)}
            db.commit()
            logger.exception("Sync crashed scope=%s target_id=%s", scope, target_id)
            raise


def reconcile_sync_job(db: Session, job: SyncJob) -> SyncJob:
    if job.status != "running":
        return job
    latest_update = job.updated_at or job.started_at or job.created_at
    if latest_update and datetime.utcnow() - latest_update <= SYNC_STALE_AFTER:
        return job
    details = dict(job.details or {})
    details.setdefault("warning", "Stale sync job cleared")
    details["stale"] = True
    details.setdefault("percent", 0)
    job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.details = details
    db.commit()
    db.refresh(job)
    logger.warning("Cleared stale sync job id=%s scope=%s target_id=%s", job.id, job.scope, job.target_id)
    return job
