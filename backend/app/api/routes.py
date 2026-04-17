from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import math
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_configured_admin_user, get_current_admin_user, get_current_user, resolve_session_token
from app.core.config import get_settings
from app.core.logging import get_logger, read_log_lines
from app.core.timezone import normalize_timezone_name, server_timezone_name
from app.db.init_db import DEFAULT_ADMIN_USERNAME, clear_bootstrap_admin_credentials
from app.db.session import get_db
from app.models.entities import (
    Channel,
    LiveMonitoredChannel,
    LibraryRoot,
    MetadataOverride,
    Playlist,
    PlaylistItem,
    QueueItem,
    RetentionExclusion,
    RetentionItem,
    RetentionSettings,
    SavedVideo,
    ScanJob,
    SelectedFolder,
    Series,
    SessionToken,
    Subscription,
    SyncJob,
    SyncSettings,
    TranscodeJob,
    UserProfile,
    Video,
    VideoFile,
    VideoReaction,
    WatchHistory,
    WatchProgress,
    YouTubeCommentSnapshot,
    YouTubeCommentReplySnapshot,
    YouTubeChannelSnapshot,
    YouTubeMatch,
    YouTubeLiveStreamSnapshot,
    YouTubeVideoSnapshot,
)
from app.schemas.common import (
    ChannelOut,
    CollectionSaveIn,
    FeedSection,
    JobOut,
    LibraryRootOut,
    LiveOverviewOut,
    LiveStreamOut,
    LoginIn,
    MetadataOverrideIn,
    AdminUserPermissionIn,
    AdminPasswordChangeIn,
    AdminRecoveryIn,
    AdminSetupIn,
    AuthBootstrapOut,
    PlaylistCreateIn,
    PlaylistOut,
    ProgressIn,
    ProfileUpdateIn,
    ReactionIn,
    RetentionFolderCreateIn,
    RetentionExclusionIn,
    RetentionExclusionOut,
    RetentionLookupItem,
    RetentionPendingItemOut,
    RetentionRunOut,
    RetentionStatsOut,
    RetentionSettingsIn,
    RetentionSettingsOut,
    QueueBulkIn,
    QueueItemIn,
    RegisterIn,
    SessionUserOut,
    SelectedFolderIn,
    SelectedFolderOut,
    SessionOut,
    SeriesOut,
    SwitchSessionIn,
    SyncSettingsIn,
    SyncSettingsOut,
    UpdateStatusOut,
    UserPasswordChangeIn,
    UserPasswordResetByPinIn,
    UserPinSetIn,
    UserProfileOut,
    VideoSummary,
    WatchStateIn,
)
from app.services.app_update import build_update_status
from app.services.auth import hash_password, hash_session_token, verify_password, verify_recovery_phrase
from app.services.auth_rate_limit import clear_failures, is_limited, register_failure
from app.services.feed import build_home_feed, build_suggested_feed, summarize_video
from app.services.media import download_thumbnail, find_caption_tracks, generate_preview_clip, generate_thumbnail, is_video_file, placeholder_thumbnail_svg, probe_media, srt_to_vtt
from app.services.playback import (
    ensure_compatible_stream,
    ensure_hls_transcode,
    normalize_playback_client_profile,
    playback_client_profile,
    reconcile_transcode_job,
    resolve_playback,
    stop_transcode_job,
    transcode_is_throttled,
    wait_for_transcode_target,
    wait_for_transcode_playlist,
)
from app.services.retention import (
    browse_retention_folders,
    create_retention_folder,
    delete_pending_retention_items,
    effective_retention_staging_folder,
    get_or_create_retention_settings,
    list_retention_pending_items,
    list_retention_runs,
    record_retention_failure,
    retention_reclaimed_bytes,
    retention_lookup,
    revert_last_retention_run,
    run_retention_cycle,
    validate_retention_staging_folder_path,
)
from app.services.scanner import scan_selected_folders
from app.services.subtitles import create_subtitle_backfill_job
from app.services.sync import (
    REQUEST_HEADERS,
    allow_fallback_art_enabled,
    auto_organize_channel_files,
    build_youtube_api_quota_summary,
    extract_json_blob,
    monitored_live_channel_ids,
    normalize_youtube_api_quota,
    prefer_high_res_banners_enabled,
    reconcile_sync_job,
    refresh_live_streams,
    refresh_channel_from_snapshot,
    resolve_synced_channel_target,
    sync_scope,
    youtube_channel_matches_local_channel,
)
from app.services.utils import is_generic_channel_name, normalize_text, tokenize_text, tokens_match_query

router = APIRouter()
settings = get_settings()
logger = get_logger()
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
AUTH_LOGIN_LIMIT = 8
AUTH_LOGIN_WINDOW_SECONDS = 10 * 60


def _series_matches_channel_name(series_name: str | None, channel_name: str | None) -> bool:
    series_normalized = normalize_text(series_name or "")
    channel_normalized = normalize_text(channel_name or "")
    return bool(series_normalized and channel_normalized and series_normalized == channel_normalized)
AUTH_RESET_LIMIT = 5
AUTH_RESET_WINDOW_SECONDS = 15 * 60
AUTH_ADMIN_RECOVERY_LIMIT = 5
AUTH_ADMIN_RECOVERY_WINDOW_SECONDS = 15 * 60
LIVE_REFRESH_INTERVAL_SECONDS = 300
LIVE_EMPTY_REFRESH_INTERVAL_SECONDS = 60
LIVE_STALE_AFTER_SECONDS = 1800
DEFAULT_LIBRARY_SENTINEL = ".halcyon-library-root"
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIVE_EMBED_BLOCKED_STATUS_VALUES = {
    "LOGIN_REQUIRED",
    "AGE_CHECK_REQUIRED",
    "CONTENT_CHECK_REQUIRED",
}
YOUTUBE_COOKIES_FILENAME = "youtube-cookies.txt"
YOUTUBE_COOKIES_MAX_BYTES = 4 * 1024 * 1024


def _normalize_fs_path(value: str | Path) -> str:
    return str(Path(value).resolve(strict=False)).replace("/", "\\").rstrip("\\").casefold()


def _extract_youtube_video_id(value: str) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(normalized):
        return normalized
    match = re.search(r"(?:v=|/embed/|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", normalized)
    if match:
        return match.group(1)
    return None


def _dedupe_storage_roots(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    normalized_roots: list[str] = []
    for path in sorted(paths, key=lambda item: len(_normalize_fs_path(item))):
        normalized = _normalize_fs_path(path)
        if any(
            normalized == existing or normalized.startswith(f"{existing}\\")
            for existing in normalized_roots
        ):
            continue
        unique.append(path)
        normalized_roots.append(normalized)
    return unique


def _selected_storage_roots(db: Session) -> list[Path]:
    selected_rows = db.scalars(
        select(SelectedFolder)
        .options(joinedload(SelectedFolder.root))
        .where(SelectedFolder.is_enabled.is_(True))
        .order_by(SelectedFolder.id.asc())
    ).all()
    candidates: list[Path] = []
    if selected_rows:
        for row in selected_rows:
            if not row.root:
                continue
            root_path = Path(row.root.path)
            candidates.append(root_path / row.relative_path if row.relative_path else root_path)
    else:
        roots = db.scalars(select(LibraryRoot).where(LibraryRoot.is_available.is_(True)).order_by(LibraryRoot.id.asc())).all()
        implicit_roots = [root for root in roots if _uses_implicit_root_selection(root, len(roots))]
        candidates.extend(Path(root.path) for root in implicit_roots)
    return _dedupe_storage_roots(candidates)


def _selected_folder_counts_by_root(db: Session) -> dict[int, int]:
    counts = {
        root_id: count
        for root_id, count in db.execute(
            select(SelectedFolder.root_id, func.count(SelectedFolder.id)).group_by(SelectedFolder.root_id)
        ).all()
    }
    roots = db.scalars(select(LibraryRoot).where(LibraryRoot.is_available.is_(True)).order_by(LibraryRoot.id.asc())).all()
    for root in roots:
        if counts.get(root.id, 0) == 0 and _uses_implicit_root_selection(root, len(roots)):
            counts[root.id] = 1
    return counts


def _effective_selected_folders(db: Session) -> list[SelectedFolderOut]:
    actual_rows = db.scalars(select(SelectedFolder).order_by(SelectedFolder.id.asc())).all()
    items = [SelectedFolderOut.model_validate(row) for row in actual_rows]
    actual_root_ids = {row.root_id for row in actual_rows}
    roots = db.scalars(select(LibraryRoot).where(LibraryRoot.is_available.is_(True)).order_by(LibraryRoot.id.asc())).all()
    for root in roots:
        if root.id in actual_root_ids or not _uses_implicit_root_selection(root, len(roots)):
            continue
        items.append(
            SelectedFolderOut(
                id=-root.id,
                root_id=root.id,
                relative_path="",
                is_enabled=True,
            )
        )
    return items


def _uses_implicit_root_selection(root: LibraryRoot, available_root_count: int) -> bool:
    if available_root_count != 1:
        return False
    root_path = Path(root.path)
    normalized = root_path.as_posix().rstrip("/")
    if normalized == "/library":
        return (root_path / DEFAULT_LIBRARY_SENTINEL).exists()
    return root_path.name == "library"


def _is_transient_video_artifact(path: Path) -> bool:
    suffixes = {suffix.lower() for suffix in path.suffixes}
    if suffixes.intersection(TEMP_DOWNLOAD_MARKERS):
        return True
    return YTDLP_FRAGMENT_PATTERN.search(path.stem) is not None


def _scan_library_storage_bytes(content_roots: list[Path]) -> int:
    total_bytes = 0
    for content_root in content_roots:
        if not content_root.exists() or not content_root.is_dir():
            continue
        for current_root, dirnames, filenames in os.walk(content_root):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in {RETENTION_SCAN_DIRNAME, RETENTION_DELETE_BUFFER_DIRNAME, "__pycache__"}
            ]
            current_path = Path(current_root)
            for filename in filenames:
                file_path = current_path / filename
                if _is_transient_video_artifact(file_path) or not is_video_file(file_path):
                    continue
                try:
                    total_bytes += int(file_path.stat().st_size)
                except OSError:
                    continue
    return total_bytes


def _indexed_library_storage_bytes(db: Session, content_roots: list[Path]) -> int:
    if not content_roots:
        return 0

    normalized_roots = [_normalize_fs_path(root) for root in content_roots]
    total_bytes = 0
    rows = db.execute(
        select(VideoFile.absolute_path, VideoFile.file_size)
        .join(Video, Video.id == VideoFile.video_id)
        .where(Video.is_available.is_(True))
    ).all()
    for absolute_path, file_size in rows:
        normalized_path = _normalize_fs_path(absolute_path)
        if any(
            normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}\\")
            for normalized_root in normalized_roots
        ):
            total_bytes += int(file_size or 0)
    return total_bytes


def _client_ip(request: Request | None) -> str:
    if request is None:
        return "local-test"
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _auth_limit_key(bucket: str, *, request: Request | None, username: str | None = None) -> str:
    identity = _client_ip(request).casefold()
    subject = (username or "").strip().casefold() or "anonymous"
    return f"{bucket}:{identity}:{subject}"


def _playback_profile_for_request(request: Request | None) -> str:
    if request is None:
        return "default"
    return playback_client_profile(request.headers)


def _resolved_playback_profile(request: Request | None, client_profile: str | None) -> str:
    explicit_profile = normalize_playback_client_profile(client_profile)
    if explicit_profile != "default":
        return explicit_profile
    return _playback_profile_for_request(request)


def _enforce_auth_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    limited, retry_after = is_limited(key, limit=limit, window_seconds=window_seconds)
    if limited:
        raise HTTPException(
            status_code=429,
            detail=f"Too many authentication attempts. Try again in {retry_after} seconds.",
        )
DEFAULT_USER_AVATAR = "/assets/branding/default_avi.png"
WATCH_COMPLETION_THRESHOLD = 0.95
EXPLORE_HISTORY_LIMIT = 48
EXPLORE_STOPWORDS = {
    "about",
    "after",
    "again",
    "ambient",
    "around",
    "best",
    "channel",
    "episode",
    "from",
    "hours",
    "into",
    "just",
    "live",
    "mod",
    "new",
    "part",
    "series",
    "some",
    "that",
    "this",
    "video",
    "with",
    "without",
    "overpoch",
    "dayz",
    "arma",
}
TRUSTED_PUBLISHED_AT_SOURCES = {"youtube-api", "watch-page"}


def _video_query():
    return select(Video).options(joinedload(Video.channel), joinedload(Video.series), joinedload(Video.files), joinedload(Video.youtube_match))


def _authoritative_youtube_match(video: Video) -> YouTubeMatch | None:
    match = video.youtube_match
    if match and match.status == "matched":
        return match
    return None


def _youtube_cookies_path() -> Path:
    return settings.config_dir / YOUTUBE_COOKIES_FILENAME


def _youtube_cookies_status_values() -> tuple[bool, datetime | None]:
    cookie_path = _youtube_cookies_path()
    if not cookie_path.is_file():
        return False, None
    try:
        updated_at = datetime.utcfromtimestamp(cookie_path.stat().st_mtime)
    except OSError:
        return False, None
    return True, updated_at


def _youtube_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        joined = " ".join(part for item in value if (part := _youtube_text(item)))
        return joined.strip() or None
    if isinstance(value, dict):
        for key in ("simpleText", "text", "content"):
            text = _youtube_text(value.get(key))
            if text:
                return text
        runs = value.get("runs")
        if isinstance(runs, list):
            text = _youtube_text(runs)
            if text:
                return text
        for key in (
            "reason",
            "message",
            "messages",
            "subreason",
            "headline",
            "description",
            "errorScreen",
            "playerErrorMessageRenderer",
            "subreasonRenderer",
            "info",
        ):
            text = _youtube_text(value.get(key))
            if text:
                return text
    return None


def _live_embed_blocked_reason_from_html(html: str | None) -> str | None:
    if not html:
        return None
    player_response = extract_json_blob(
        html,
        [
            "var ytInitialPlayerResponse = ",
            "ytInitialPlayerResponse = ",
            'ytInitialPlayerResponse":',
        ],
    )
    playability_status = (
        player_response.get("playabilityStatus")
        if isinstance(player_response, dict)
        else None
    )
    if not isinstance(playability_status, dict):
        html_lower = html.lower()
        if "sign in to confirm your age" in html_lower or "age-restricted" in html_lower:
            return "This live stream needs an age-verified YouTube session."
        if "only available on youtube" in html_lower:
            return "YouTube is blocking the embedded player for this stream."
        return None
    status_value = str(playability_status.get("status") or "").strip().upper()
    raw_reason = _youtube_text(playability_status.get("reason")) or _youtube_text(playability_status)
    raw_reason_lower = (raw_reason or "").lower()
    if (
        status_value in LIVE_EMBED_BLOCKED_STATUS_VALUES
        or "confirm your age" in raw_reason_lower
        or "age-restricted" in raw_reason_lower
        or "only available on youtube" in raw_reason_lower
    ):
        if "confirm your age" in raw_reason_lower or "age-restricted" in raw_reason_lower:
            return "This live stream needs an age-verified YouTube session."
        if raw_reason:
            return raw_reason
        return "YouTube is blocking the embedded player for this stream."
    return None


async def _fetch_live_watch_page_html(youtube_video_id: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=REQUEST_HEADERS,
            timeout=10.0,
        ) as client:
            response = await client.get(
                "https://www.youtube.com/watch",
                params={"v": youtube_video_id, "hl": "en"},
            )
    except httpx.HTTPError:
        return None
    if response.is_error:
        return None
    return response.text


async def _fetch_live_embed_page_html(youtube_video_id: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=REQUEST_HEADERS,
            timeout=10.0,
        ) as client:
            response = await client.get(
                f"https://www.youtube.com/embed/{youtube_video_id}",
                params={"hl": "en", "playsinline": "1", "rel": "0"},
            )
    except httpx.HTTPError:
        return None
    if response.is_error:
        return None
    return response.text


def _pick_live_playback_url(info: dict[str, Any]) -> str | None:
    best_url: str | None = None
    best_score = -1.0
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        url = fmt.get("manifest_url") or fmt.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        protocol = str(fmt.get("protocol") or "").lower()
        score = 0.0
        if "m3u8" in protocol or ".m3u8" in url.lower():
            score += 10_000
        if fmt.get("vcodec") not in {None, "", "none"}:
            score += 500
        if fmt.get("acodec") not in {None, "", "none"}:
            score += 250
        try:
            score += float(fmt.get("height") or 0)
        except (TypeError, ValueError):
            pass
        try:
            score += float(fmt.get("tbr") or 0) / 10.0
        except (TypeError, ValueError):
            pass
        if score > best_score:
            best_score = score
            best_url = url
    if best_url:
        return best_url
    for key in ("manifest_url", "url"):
        direct = info.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct
    return None


def _extract_live_playback_url_sync(youtube_video_id: str, cookie_path: Path) -> str | None:
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        logger.warning("Live playback fallback unavailable because yt-dlp is missing: %s", exc)
        return None
    watch_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cookiefile": str(cookie_path),
        "noplaylist": True,
        "format": "best",
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(watch_url, download=False)
    except Exception as exc:
        logger.warning("Live playback fallback extraction failed for %s: %s", youtube_video_id, exc)
        return None
    if not isinstance(info, dict):
        return None
    return _pick_live_playback_url(info)


async def _resolve_live_playback(youtube_video_id: str) -> dict[str, Any]:
    watch_html, embed_html = await asyncio.gather(
        _fetch_live_watch_page_html(youtube_video_id),
        _fetch_live_embed_page_html(youtube_video_id),
    )
    embed_blocked_reason = _live_embed_blocked_reason_from_html(embed_html)
    if not embed_blocked_reason:
        embed_blocked_reason = _live_embed_blocked_reason_from_html(watch_html)
    payload: dict[str, Any] = {
        "playback_mode": "youtube-embed",
        "playback_url": None,
        "embed_blocked_reason": embed_blocked_reason,
    }
    if not embed_blocked_reason:
        return payload
    logger.info(
        "Live playback embed blocked video_id=%s reason=%s",
        youtube_video_id,
        embed_blocked_reason,
    )
    cookie_path = _youtube_cookies_path()
    if not cookie_path.is_file():
        logger.info(
            "Live playback embed blocked without cookies video_id=%s",
            youtube_video_id,
        )
        return payload
    playback_url = await asyncio.to_thread(
        _extract_live_playback_url_sync,
        youtube_video_id,
        cookie_path,
    )
    if playback_url:
        payload["playback_mode"] = "direct"
        payload["playback_url"] = playback_url
        logger.info(
            "Live playback direct fallback enabled video_id=%s",
            youtube_video_id,
        )
    else:
        logger.warning(
            "Live playback direct fallback failed video_id=%s cookies_configured=true",
            youtube_video_id,
        )
    return payload


def _trusted_snapshot_published_at(snapshot: YouTubeVideoSnapshot | None) -> datetime | None:
    if not snapshot or not snapshot.published_at:
        return None
    if snapshot.published_at_source not in TRUSTED_PUBLISHED_AT_SOURCES:
        return None
    return snapshot.published_at


def _video_recency_order_clause():
    trusted_snapshot_published_at = case(
        (
            and_(
                YouTubeMatch.status == "matched",
                YouTubeVideoSnapshot.published_at.is_not(None),
                YouTubeVideoSnapshot.published_at_source.in_(TRUSTED_PUBLISHED_AT_SOURCES),
            ),
            YouTubeVideoSnapshot.published_at,
        ),
        else_=None,
    )
    return func.coalesce(trusted_snapshot_published_at, Video.published_at, Video.created_at)


def _stable_explore_jitter(user_id: int, video_id: int) -> float:
    digest = hashlib.sha1(f"explore:{user_id}:{video_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _explore_tokens(*parts: str | None) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        for token in tokenize_text(normalize_text(part)):
            if len(token) < 3 or token in EXPLORE_STOPWORDS or token.isdigit():
                continue
            tokens.append(token)
    return tokens


def _explore_video_terms(video: Video) -> set[str]:
    description_sample = (video.description or "")[:280]
    tag_blob = " ".join(str(tag) for tag in (video.tags or []) if tag)
    return set(
        _explore_tokens(
            video.title,
            description_sample,
            video.channel.name if video.channel else None,
            video.series.name if video.series else None,
            tag_blob,
        )
    )


def _recent_interest_videos(db: Session, user_id: int, progress_map: dict[int, WatchProgress]) -> list[Video]:
    recent_history_ids = db.scalars(
        select(WatchHistory.video_id)
        .where(WatchHistory.user_id == user_id)
        .order_by(WatchHistory.watched_at.desc())
        .limit(EXPLORE_HISTORY_LIMIT)
    ).all()
    ordered_ids: list[int] = []
    seen_ids: set[int] = set()
    for video_id in recent_history_ids:
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        ordered_ids.append(video_id)
    for entry in sorted(
        progress_map.values(),
        key=lambda item: item.updated_at or datetime.min,
        reverse=True,
    ):
        if entry.video_id in seen_ids:
            continue
        seen_ids.add(entry.video_id)
        ordered_ids.append(entry.video_id)
    if not ordered_ids:
        return []
    videos_by_id = {
        video.id: video
        for video in db.scalars(_video_query().where(Video.id.in_(ordered_ids))).unique().all()
    }
    return [videos_by_id[video_id] for video_id in ordered_ids if video_id in videos_by_id]


def _build_explore_ranked_videos(
    db: Session,
    current_user: UserProfile,
    progress_map: dict[int, WatchProgress],
) -> list[Video]:
    queue_video_ids = set(db.scalars(select(QueueItem.video_id).where(QueueItem.user_id == current_user.id)).all())
    history_video_ids = set(db.scalars(select(WatchHistory.video_id).where(WatchHistory.user_id == current_user.id)).all())
    touched_video_ids = set(progress_map.keys()) | history_video_ids | queue_video_ids

    interest_videos = _recent_interest_videos(db, current_user.id, progress_map)
    touched_series_ids = {video.series_id for video in interest_videos if video.series_id}
    interest_channels = Counter(video.channel_id for video in interest_videos if video.channel_id)
    interest_tokens: Counter[str] = Counter()
    for index, video in enumerate(interest_videos):
        recency_weight = max(1.0, 8.0 - (index * 0.35))
        for token in _explore_video_terms(video):
            interest_tokens[token] += recency_weight

    candidate_videos = db.scalars(
        _video_query()
        .where(Video.is_available.is_(True))
        .order_by(Video.published_at.desc().nullslast(), Video.created_at.desc(), Video.id.desc())
    ).unique().all()

    scored: list[tuple[float, float, Video]] = []
    for video in candidate_videos:
        if video.id in touched_video_ids:
            continue
        if video.series_id and video.series_id in touched_series_ids:
            continue

        tokens = _explore_video_terms(video)
        overlap_score = sum(interest_tokens[token] for token in tokens if token in interest_tokens)
        channel_score = 0.75 * interest_channels.get(video.channel_id, 0)
        recency = video.published_at or video.created_at or datetime.min
        age_days = max(0.0, (datetime.utcnow() - recency).total_seconds() / 86400) if recency != datetime.min else 3650.0
        recency_score = max(0.0, 1.5 - min(age_days, 180.0) / 180.0)
        jitter = _stable_explore_jitter(current_user.id, video.id)

        if interest_tokens:
            final_score = overlap_score + channel_score + (jitter * 1.35)
            if final_score <= 0:
                continue
        else:
            final_score = recency_score + (jitter * 2.25)

        scored.append((final_score, jitter, video))

    scored.sort(
        key=lambda item: (
            item[0],
            item[2].published_at or item[2].created_at or datetime.min,
            item[1],
            item[2].id,
        ),
        reverse=True,
    )

    ranked: list[Video] = []
    used_video_ids: set[int] = set()
    per_channel_counts: Counter[int] = Counter()
    for _, _, video in scored:
        channel_id = video.channel_id or -video.id
        if per_channel_counts[channel_id] >= 2:
            continue
        ranked.append(video)
        used_video_ids.add(video.id)
        per_channel_counts[channel_id] += 1
    for _, _, video in scored:
        if video.id in used_video_ids:
            continue
        ranked.append(video)
    return ranked


def _channel_snapshot_for_channel(db: Session, channel_id: int) -> YouTubeChannelSnapshot | None:
    channel = db.get(Channel, channel_id)
    youtube_channel_ids = [
        item
        for item in db.scalars(
            select(YouTubeMatch.youtube_channel_id)
            .join(Video, Video.id == YouTubeMatch.video_id)
            .where(
                Video.channel_id == channel_id,
                YouTubeMatch.status == "matched",
                YouTubeMatch.youtube_channel_id.is_not(None),
            )
            .distinct()
            .limit(2)
        ).all()
        if item
    ]
    if channel and not is_generic_channel_name(channel.name):
        youtube_channel_ids = [
            youtube_channel_id
            for youtube_channel_id in youtube_channel_ids
            if youtube_channel_matches_local_channel(
                db,
                local_channel=channel,
                youtube_channel_id=youtube_channel_id,
            )
        ]
    if len(youtube_channel_ids) != 1:
        return None
    return db.scalar(select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == youtube_channel_ids[0]))


def _normalize_external_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    value = raw_url.strip()
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return None
    if "." in value and " " not in value:
        return f"https://{value}"
    return None


def _normalized_channel_links(snapshot: YouTubeChannelSnapshot | None) -> list[dict]:
    if not snapshot or not snapshot.links:
        return []
    normalized: list[dict] = []
    for item in snapshot.links:
        if not isinstance(item, dict):
            continue
        url = _normalize_external_url(str(item.get("url") or ""))
        if not url:
            continue
        title = str(item.get("title") or url).strip() or url
        normalized.append({"title": title, "url": url})
    return normalized


def _resolve_channel_ref(db: Session, channel_ref: str) -> Channel | None:
    normalized = channel_ref.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return db.get(Channel, int(normalized))
    return db.scalar(select(Channel).where(Channel.slug == normalized))


def _channel_has_new_video(db: Session, user: UserProfile, channel_id: int) -> bool:
    videos = db.execute(
        select(Video.id, Video.published_at, Video.created_at)
        .where(Video.channel_id == channel_id, Video.is_available.is_(True))
        .order_by(Video.published_at.desc().nullslast(), Video.created_at.desc())
    ).all()
    if not videos:
        return False
    seen_cutoff = user.last_subscription_seen_at
    watched_video_ids = set(db.scalars(select(WatchHistory.video_id).where(WatchHistory.user_id == user.id)).all())
    for video_id, published_at, created_at in videos:
        marker_time = published_at or created_at
        if seen_cutoff and marker_time and marker_time <= seen_cutoff:
            continue
        if video_id not in watched_video_ids:
            return True
    return False


def _playlist_preview_thumbnails(db: Session, playlist_id: int) -> list[str]:
    item_video_ids = db.scalars(
        select(PlaylistItem.video_id).where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.position.asc()).limit(3)
    ).all()
    if not item_video_ids:
        return []
    videos = {
        video.id: video
        for video in db.scalars(_video_query().where(Video.id.in_(item_video_ids))).unique().all()
    }
    return [
        summarize_video(videos[video_id], db=db).thumbnail_url or f"/api/videos/{video_id}/thumbnail"
        for video_id in item_video_ids
        if video_id in videos
    ]


def _saved_video_id_set(db: Session, user_id: int, video_ids: list[int]) -> set[int]:
    if not video_ids:
        return set()
    return set(
        db.scalars(
            select(SavedVideo.video_id).where(
                SavedVideo.user_id == user_id,
                SavedVideo.video_id.in_(video_ids),
            )
        ).all()
    )


def _playlist_video_ids(db: Session, playlist_id: int) -> list[int]:
    return db.scalars(
        select(PlaylistItem.video_id)
        .where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position.asc())
    ).all()


def _playlist_saved_summary(db: Session, playlist_id: int, user_id: int | None) -> tuple[int, bool]:
    video_ids = _playlist_video_ids(db, playlist_id)
    if not user_id or not video_ids:
        return 0, False
    saved_count = len(_saved_video_id_set(db, user_id, video_ids))
    return saved_count, saved_count == len(video_ids)


def _series_preview_thumbnails(db: Session, series_id: int) -> list[str]:
    videos = db.scalars(
        _video_query()
        .where(Video.series_id == series_id)
        .order_by(Video.episode_number.asc().nullslast(), Video.id.asc())
        .limit(3)
    ).unique().all()
    return [
        summarize_video(video, db=db).thumbnail_url or f"/api/videos/{video.id}/thumbnail"
        for video in videos
    ]


def _series_video_ids(db: Session, series_id: int) -> list[int]:
    return db.scalars(
        select(Video.id)
        .where(Video.series_id == series_id)
        .order_by(Video.episode_number.asc().nullslast(), Video.id.asc())
    ).all()


def _series_saved_summary(db: Session, series_id: int, user_id: int | None) -> tuple[int, bool]:
    video_ids = _series_video_ids(db, series_id)
    if not user_id or not video_ids:
        return 0, False
    saved_count = len(_saved_video_id_set(db, user_id, video_ids))
    return saved_count, saved_count == len(video_ids)


def _retention_exclusion_out(db: Session, exclusion: RetentionExclusion) -> dict:
    label = f"{exclusion.target_type.title()} {exclusion.target_id}"
    subtitle = None
    image_url = None
    if exclusion.target_type == "channel":
        channel = db.get(Channel, exclusion.target_id)
        if channel:
            label = channel.name
            subtitle = channel.slug
            image_url = _channel_image_proxy(channel.id, "avatar", channel.avatar_url)
    elif exclusion.target_type == "series":
        series = db.get(Series, exclusion.target_id)
        if series:
            label = series.name
            subtitle = series.slug
            first_video = db.scalar(
                select(Video)
                .where(Video.series_id == series.id)
                .order_by(Video.created_at.desc(), Video.id.desc())
                .limit(1)
            )
            if first_video:
                image_url = summarize_video(first_video, db=db).thumbnail_url or f"/api/videos/{first_video.id}/thumbnail"
    elif exclusion.target_type == "video":
        video = db.get(Video, exclusion.target_id)
        if video:
            label = video.title
            subtitle = video.channel.name if video.channel else None
            image_url = summarize_video(video, db=db).thumbnail_url or f"/api/videos/{video.id}/thumbnail"
    return {
        "id": exclusion.id,
        "target_type": exclusion.target_type,
        "target_id": exclusion.target_id,
        "label": label,
        "subtitle": subtitle,
        "image_url": image_url,
        "created_at": exclusion.created_at,
        "updated_at": exclusion.updated_at,
    }


def _retention_overview(db: Session, settings_row: RetentionSettings | None = None) -> dict:
    settings_row = settings_row or get_or_create_retention_settings(db)
    exclusions = db.scalars(
        select(RetentionExclusion).order_by(RetentionExclusion.created_at.desc())
    ).all()
    pending_items = list_retention_pending_items(db)
    return {
        "settings": RetentionSettingsOut.model_validate(settings_row).model_dump(),
        "effective_staging_folder": str(effective_retention_staging_folder(settings_row)),
        "exclusions": [_retention_exclusion_out(db, item) for item in exclusions],
        "pending_items": [item.model_dump() for item in pending_items],
        "history": [RetentionRunOut.model_validate(item).model_dump() for item in list_retention_runs(db)],
        "stats": RetentionStatsOut(reclaimed_bytes=retention_reclaimed_bytes(db)).model_dump(),
    }


def _apply_user_saved_flags(db: Session, current_user: UserProfile | None, summaries: list[VideoSummary]) -> list[VideoSummary]:
    if not current_user or not summaries:
        return summaries
    saved_ids = _saved_video_id_set(db, current_user.id, [summary.id for summary in summaries])
    return [
        summary.model_copy(update={"user_saved": summary.id in saved_ids})
        for summary in summaries
    ]


def _playlist_out(db: Session, playlist: Playlist, user_id: int | None = None) -> PlaylistOut:
    saved_count, all_videos_saved = _playlist_saved_summary(db, playlist.id, user_id)
    return PlaylistOut(
        id=playlist.id,
        name=playlist.name,
        description=playlist.description,
        item_count=db.scalar(select(func.count(PlaylistItem.id)).where(PlaylistItem.playlist_id == playlist.id)) or 0,
        preview_thumbnails=_playlist_preview_thumbnails(db, playlist.id),
        all_videos_saved=all_videos_saved,
        saved_video_count=saved_count,
    )


def _channel_out(db: Session, channel: Channel, *, user: UserProfile | None = None, subscribed: bool = False, video_count: int | None = None) -> ChannelOut:
    snapshot = _channel_snapshot_for_channel(db, channel.id)
    return ChannelOut(
        id=channel.id,
        name=snapshot.title if snapshot and snapshot.title else channel.name,
        slug=channel.slug,
        description=channel.description or snapshot.description if snapshot else channel.description,
        avatar_url=_channel_image_proxy(channel.id, "avatar", channel.avatar_url or snapshot.avatar_url if snapshot else channel.avatar_url),
        banner_url=_channel_image_proxy(channel.id, "banner", channel.banner_url or snapshot.banner_url if snapshot else channel.banner_url),
        video_count=video_count if video_count is not None else (db.scalar(select(func.count(Video.id)).where(Video.channel_id == channel.id)) or 0),
        subscribed=subscribed,
        subscriber_count=snapshot.subscriber_count if snapshot else None,
        view_count=snapshot.view_count if snapshot else None,
        youtube_video_count=snapshot.video_count if snapshot else None,
        youtube_fetched_at=snapshot.fetched_at if snapshot else None,
        joined_at=snapshot.joined_at if snapshot else None,
        canonical_url=_normalize_external_url(snapshot.canonical_url if snapshot else None),
        links=_normalized_channel_links(snapshot),
        has_new_video=_channel_has_new_video(db, user, channel.id) if user else False,
    )


def _channel_image_proxy(channel_id: int, kind: str, raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    fingerprint = hashlib.sha1(raw_url.encode("utf-8")).hexdigest()[:12]
    return f"/api/channels/{channel_id}/{kind}-image?v={fingerprint}"


def _with_channel_display(summary: VideoSummary | None, channel_name: str | None) -> VideoSummary | None:
    if summary and channel_name:
        summary.channel_name = channel_name
    return summary


def _with_channel_avatar(summary: VideoSummary | None, avatar_url: str | None) -> VideoSummary | None:
    if summary and avatar_url:
        summary.channel_avatar_url = avatar_url
    return summary


def _video_by_id(db: Session, video_id: int) -> Video | None:
    return db.scalars(_video_query().where(Video.id == video_id)).unique().one_or_none()


def _normalized_watch_progress(
    position_seconds: int | None,
    *,
    duration_seconds: int | None,
    completed: bool,
) -> tuple[int, bool]:
    safe_position = max(0, int(position_seconds or 0))
    if completed:
        return 0, True
    if duration_seconds and duration_seconds > 0:
        safe_position = min(safe_position, duration_seconds)
        completion_cutoff = max(1, math.floor(duration_seconds * WATCH_COMPLETION_THRESHOLD))
        if safe_position >= completion_cutoff:
            return 0, True
    return safe_position, False


def _resume_point_for(progress: WatchProgress | None, duration_seconds: int | None) -> int:
    if not progress:
        return 0
    normalized_position, completed = _normalized_watch_progress(
        progress.position_seconds,
        duration_seconds=duration_seconds,
        completed=bool(progress.completed),
    )
    return 0 if completed else normalized_position


def _video_ref_for(db: Session, video: Video) -> str:
    match = video.youtube_match
    if (
        match
        and match.status == "matched"
        and match.youtube_video_id
        and (
            not video.channel
            or is_generic_channel_name(video.channel.name)
            or not match.youtube_channel_id
            or youtube_channel_matches_local_channel(
                db,
                local_channel=video.channel,
                youtube_channel_id=match.youtube_channel_id,
            )
        )
    ):
        return match.youtube_video_id
    return str(video.id)


def _next_up_for_video(db: Session, video: Video) -> Video | None:
    next_up = None
    if video.series_id:
        next_up = db.scalars(
            _video_query()
            .where(
                Video.series_id == video.series_id,
                Video.episode_number.is_not(None),
                Video.episode_number > (video.episode_number or 0),
            )
            .order_by(Video.episode_number.asc())
        ).unique().first()
    if next_up is None and video.channel_id:
        next_up = db.scalars(
            _video_query()
            .where(Video.channel_id == video.channel_id, Video.id != video.id)
            .order_by(Video.created_at.desc())
        ).unique().first()
    return next_up


def _watch_suggestion_filters(
    video: Video,
    *,
    user_id: int,
    next_up_id: int | None,
    mode: str,
) -> tuple[str, list]:
    normalized_mode = (mode or "suggested").strip().lower()
    if normalized_mode not in {"suggested", "related"}:
        normalized_mode = "suggested"

    filters = [
        Video.is_available.is_(True),
        Video.id != video.id,
    ]
    if next_up_id:
        filters.append(Video.id != next_up_id)

    if normalized_mode == "suggested":
        filters.append(
            Video.id.not_in(
                select(WatchProgress.video_id).where(
                    WatchProgress.user_id == user_id,
                    WatchProgress.completed.is_(True),
                )
            )
        )

    if normalized_mode == "related":
        related_filters = []
        if video.series_id:
            related_filters.append(Video.series_id == video.series_id)
        if video.channel_id:
            related_filters.append(Video.channel_id == video.channel_id)
        if related_filters:
            filters.append(or_(*related_filters))
        else:
            filters.append(literal(False))

    return normalized_mode, filters


def _watch_suggestion_order(video: Video):
    same_channel = (
        (Video.channel_id == video.channel_id).desc()
        if video.channel_id is not None
        else literal(False).desc()
    )
    same_series = (
        (Video.series_id == video.series_id).desc()
        if video.series_id is not None
        else literal(False).desc()
    )
    return (
        same_channel,
        same_series,
        func.coalesce(Video.published_at, Video.created_at).desc(),
        Video.id.desc(),
    )


def _watch_suggestions_page(
    db: Session,
    *,
    video: Video,
    current_user: UserProfile,
    mode: str,
    offset: int,
    limit: int,
    next_up: Video | None,
    channel_display_name: str | None,
) -> dict:
    normalized_mode, filters = _watch_suggestion_filters(
        video,
        user_id=current_user.id,
        next_up_id=next_up.id if next_up else None,
        mode=mode,
    )
    bounded_offset = max(0, int(offset))
    bounded_limit = max(1, min(int(limit), 25))
    total = db.scalar(select(func.count(Video.id)).where(*filters)) or 0
    videos = (
        db.scalars(
            _video_query()
            .where(*filters)
            .order_by(*_watch_suggestion_order(video))
            .offset(bounded_offset)
            .limit(bounded_limit)
        )
        .unique()
        .all()
    )
    progress_map = (
        {
            item.video_id: item
            for item in db.scalars(
                select(WatchProgress).where(
                    WatchProgress.user_id == current_user.id,
                    WatchProgress.video_id.in_([item.id for item in videos]),
                )
            ).all()
        }
        if videos
        else {}
    )
    items = [
        _with_channel_display(
            summarize_video(item, progress_map.get(item.id), db=db),
            channel_display_name if item.channel_id == video.channel_id else None,
        )
        for item in videos
    ]
    return {
        "mode": normalized_mode,
        "items": items,
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": total,
        "has_more": bounded_offset + len(items) < total,
    }


def _resolve_video_ref(db: Session, video_ref: str) -> Video | None:
    normalized = video_ref.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return _video_by_id(db, int(normalized))
    match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.youtube_video_id == normalized))
    if not match:
        return None
    return _video_by_id(db, match.video_id)


def _playlist_video_lookup(db: Session, playlist_id: int) -> tuple[Playlist | None, list[VideoSummary]]:
    playlist = db.get(Playlist, playlist_id)
    if not playlist:
        return None, []
    items = db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.position.asc())).all()
    video_ids = [item.video_id for item in items]
    videos_by_id = {
        video.id: video
        for video in db.scalars(_video_query().where(Video.id.in_(video_ids)) if video_ids else _video_query().where(Video.id == -1)).unique().all()
    }
    return playlist, [summarize_video(videos_by_id[video_id], db=db) for video_id in video_ids if video_id in videos_by_id]


def _current_user_out(user: UserProfile) -> SessionUserOut:
    recovery_phrase = None
    if user.is_admin and user.requires_admin_setup and user.recovery_phrase_pending:
        recovery_phrase = user.recovery_phrase_pending.split()
    return SessionUserOut(
        id=user.id,
        name=user.name,
        display_name=user.display_name,
        accent_color=user.accent_color,
        avatar_url=user.avatar_url,
        is_admin=user.is_admin,
        has_pin=bool(user.pin_hash),
        requires_admin_setup=user.requires_admin_setup,
        admin_setup_recovery_phrase=recovery_phrase,
    )


def _profile_out(user: UserProfile) -> UserProfileOut:
    return UserProfileOut(
        id=user.id,
        name=user.name,
        display_name=user.display_name,
        accent_color=user.accent_color,
        avatar_url=user.avatar_url,
        is_admin=user.is_admin,
        has_pin=bool(user.pin_hash),
    )


def _admin_user(db: Session) -> UserProfile | None:
    return db.scalar(
        select(UserProfile)
        .where(UserProfile.is_admin.is_(True))
        .order_by(UserProfile.id.asc())
        .limit(1)
    )


def _registration_allowed(db: Session) -> bool:
    admin_user = _admin_user(db)
    return not admin_user or not admin_user.requires_admin_setup


def _validate_password(password: str) -> str:
    if len(password) < 8 or not password.strip():
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return password


def _validate_pin(pin: str) -> str:
    normalized = pin.strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise HTTPException(status_code=400, detail="PIN must be exactly 6 numeric digits")
    return normalized


def _normalize_username(username: str, *, strict: bool) -> str:
    normalized = re.sub(r"\s+", "", username or "").strip().lower()
    if not normalized and strict:
        raise HTTPException(status_code=400, detail="Username is required")
    if strict and len(normalized) > 80:
        raise HTTPException(status_code=400, detail="Username must be 80 characters or fewer")
    if strict and not re.fullmatch(r"[a-z0-9._-]+", normalized):
        raise HTTPException(
            status_code=400,
            detail="Username may only use letters, numbers, periods, underscores, and hyphens",
        )
    return normalized


def _apply_admin_password_change(
    admin_user: UserProfile,
    *,
    password: str,
    clear_setup: bool = False,
) -> None:
    admin_user.password_hash = hash_password(_validate_password(password))
    if clear_setup:
        admin_user.requires_admin_setup = False
        admin_user.recovery_phrase_pending = None


def _revoke_other_sessions(db: Session, user_id: int, keep_token: str | None) -> None:
    keep_hashed = hash_session_token(keep_token) if keep_token else None
    session_query = select(SessionToken).where(SessionToken.user_id == user_id)
    if keep_hashed:
        session_query = session_query.where(SessionToken.token != keep_hashed)
    for token in db.scalars(session_query).all():
        db.delete(token)
    db.flush()


def _create_session(db: Session, user: UserProfile, response: Response) -> SessionOut:
    raw_token = secrets.token_urlsafe(24)
    db.add(SessionToken(token=hash_session_token(raw_token), user_id=user.id))
    db.commit()
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=60 * 60 * 24 * 365,
    )
    return SessionOut(user=_current_user_out(user), session_token=raw_token)


def _active_youtube_api_key(db: Session) -> str | None:
    settings_row = db.scalar(select(SyncSettings))
    return (settings_row.youtube_api_key if settings_row and settings_row.youtube_api_key else None) or settings.youtube_api_key


def _live_tab_enabled(db: Session) -> bool:
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row or settings_row.live_tab_enabled is None:
        return True
    return bool(settings_row.live_tab_enabled)


def _serialize_live_stream(db: Session, stream: YouTubeLiveStreamSnapshot) -> LiveStreamOut:
    channel = stream.channel or (db.get(Channel, stream.channel_id) if stream.channel_id else None)
    channel_snapshot = _channel_snapshot_for_channel(db, channel.id) if channel else None
    channel_name = (
        channel_snapshot.title
        if channel_snapshot and channel_snapshot.title
        else (channel.name if channel else None)
    )
    avatar_url = (
        _channel_image_proxy(
            channel.id,
            "avatar",
            channel.avatar_url or (channel_snapshot.avatar_url if channel_snapshot else None),
        )
        if channel
        else None
    )
    banner_url = (
        _channel_image_proxy(
            channel.id,
            "banner",
            channel.banner_url or (channel_snapshot.banner_url if channel_snapshot else None),
        )
        if channel
        else None
    )
    return LiveStreamOut(
        youtube_video_id=stream.youtube_video_id,
        youtube_channel_id=stream.youtube_channel_id,
        title=stream.title,
        description=stream.description,
        thumbnail_url=stream.thumbnail_url,
        channel_id=channel.id if channel else None,
        channel_name=channel_name,
        channel_slug=channel.slug if channel else None,
        channel_avatar_url=avatar_url,
        channel_banner_url=banner_url,
        scheduled_start_at=stream.scheduled_start_at,
        actual_start_at=stream.actual_start_at,
        concurrent_viewers=stream.concurrent_viewers,
        is_live=stream.is_live,
        last_seen_at=stream.last_seen_at,
        fetched_at=stream.fetched_at,
        watch_url=f"https://www.youtube.com/watch?v={stream.youtube_video_id}",
        embed_url=f"https://www.youtube.com/embed/{stream.youtube_video_id}?autoplay=1&playsinline=1&rel=0&modestbranding=1",
        playback_mode="youtube-embed",
        playback_url=None,
        embed_blocked_reason=None,
    )


async def _detect_live_chat_enabled(youtube_video_id: str) -> bool:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=REQUEST_HEADERS,
            timeout=10.0,
        ) as client:
            response = await client.get(
                "https://www.youtube.com/watch",
                params={"v": youtube_video_id, "hl": "en"},
            )
    except httpx.HTTPError:
        return True

    if response.is_error:
        return True

    html = response.text
    return any(
        marker in html
        for marker in (
            '"liveChatRenderer"',
            '"liveChatHeaderRenderer"',
            '"conversationBar"',
        )
    )


def _has_fresh_live_streams(
    db: Session,
    *,
    youtube_video_id: str | None = None,
) -> bool:
    fresh_cutoff = datetime.utcnow() - timedelta(seconds=LIVE_STALE_AFTER_SECONDS)
    query = select(YouTubeLiveStreamSnapshot.id).where(
        YouTubeLiveStreamSnapshot.is_live.is_(True),
        YouTubeLiveStreamSnapshot.last_seen_at >= fresh_cutoff,
    )
    if youtube_video_id:
        query = query.where(YouTubeLiveStreamSnapshot.youtube_video_id == youtube_video_id)
    return db.scalar(query.limit(1)) is not None


async def _refresh_live_if_due(
    db: Session,
    *,
    youtube_video_id: str | None = None,
) -> None:
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row or not settings_row.live_tab_enabled:
        return
    api_key = _active_youtube_api_key(db)
    now = datetime.utcnow()
    last_live_sync_at = settings_row.last_live_sync_at
    standard_refresh_due = (
        not last_live_sync_at
        or now - last_live_sync_at >= timedelta(seconds=LIVE_REFRESH_INTERVAL_SECONDS)
    )
    empty_refresh_due = (
        not _has_fresh_live_streams(db, youtube_video_id=youtube_video_id)
        and (
            not last_live_sync_at
            or now - last_live_sync_at >= timedelta(seconds=LIVE_EMPTY_REFRESH_INTERVAL_SECONDS)
        )
    )
    if not standard_refresh_due and not empty_refresh_due:
        return
    await refresh_live_streams(
        db,
        api_key=api_key,
        requests_per_second=settings_row.requests_per_second or 3,
    )


def _live_overview_payload(db: Session) -> LiveOverviewOut:
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row:
        return LiveOverviewOut(enabled=True, api_key_configured=bool(settings.youtube_api_key), items=[])
    fresh_cutoff = datetime.utcnow() - timedelta(seconds=LIVE_STALE_AFTER_SECONDS)
    rows = db.scalars(
        select(YouTubeLiveStreamSnapshot)
        .options(joinedload(YouTubeLiveStreamSnapshot.channel))
        .where(
            YouTubeLiveStreamSnapshot.is_live.is_(True),
            YouTubeLiveStreamSnapshot.last_seen_at >= fresh_cutoff,
        )
        .order_by(
            YouTubeLiveStreamSnapshot.concurrent_viewers.desc().nullslast(),
            YouTubeLiveStreamSnapshot.actual_start_at.desc().nullslast(),
            YouTubeLiveStreamSnapshot.last_seen_at.desc(),
        )
    ).unique().all()
    return LiveOverviewOut(
        enabled=bool(settings_row.live_tab_enabled),
        api_key_configured=bool(_active_youtube_api_key(db)),
        last_live_sync_at=settings_row.last_live_sync_at,
        items=[_serialize_live_stream(db, row) for row in rows],
    )


def _sync_settings_payload(db: Session, settings_row: SyncSettings) -> SyncSettingsOut:
    youtube_cookies_configured, youtube_cookies_updated_at = _youtube_cookies_status_values()
    return SyncSettingsOut.model_validate(
        {
            **settings_row.__dict__,
            "live_monitored_channel_ids": sorted(monitored_live_channel_ids(db)),
            "youtube_api_key_configured": bool(_active_youtube_api_key(db)),
            "youtube_cookies_configured": youtube_cookies_configured,
            "youtube_cookies_updated_at": youtube_cookies_updated_at,
            **build_youtube_api_quota_summary(settings_row),
        }
    )


@router.get("/session/bootstrap", response_model=AuthBootstrapOut)
def session_bootstrap_status(db: Session = Depends(get_db)) -> AuthBootstrapOut:
    admin_user = _admin_user(db)
    return AuthBootstrapOut(
        admin_username=admin_user.name if admin_user else "admin",
        admin_setup_required=bool(admin_user and admin_user.requires_admin_setup),
        allow_registration=_registration_allowed(db),
    )


@router.get("/app/update-status", response_model=UpdateStatusOut)
def app_update_status(current_user: UserProfile = Depends(get_configured_admin_user)) -> UpdateStatusOut:
    del current_user
    return UpdateStatusOut(**build_update_status())


@router.get("/session/profiles", response_model=list[UserProfileOut])
def list_profiles(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> list[UserProfileOut]:
    del current_user
    profiles = db.scalars(select(UserProfile).order_by(UserProfile.display_name, UserProfile.id)).all()
    return [_profile_out(profile) for profile in profiles]


@router.put("/session/profiles/{user_id}/permissions", response_model=UserProfileOut)
def update_profile_permissions(
    user_id: int,
    payload: AdminUserPermissionIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> UserProfileOut:
    user = db.get(UserProfile, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not payload.is_admin and user.is_admin:
        admin_count = db.scalar(select(func.count(UserProfile.id)).where(UserProfile.is_admin.is_(True))) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="halcyon must always have at least one admin")
        if user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Use another admin account before removing your own admin access")
    user.is_admin = payload.is_admin
    db.commit()
    db.refresh(user)
    return _profile_out(user)


def _delete_user_profile_records(db: Session, user: UserProfile) -> None:
    playlist_ids = db.scalars(select(Playlist.id).where(Playlist.user_id == user.id)).all()
    if playlist_ids:
        db.query(PlaylistItem).filter(PlaylistItem.playlist_id.in_(playlist_ids)).delete(synchronize_session=False)
        db.query(Playlist).filter(Playlist.id.in_(playlist_ids)).delete(synchronize_session=False)
    db.query(SessionToken).filter(SessionToken.user_id == user.id).delete(synchronize_session=False)
    db.query(WatchProgress).filter(WatchProgress.user_id == user.id).delete(synchronize_session=False)
    db.query(WatchHistory).filter(WatchHistory.user_id == user.id).delete(synchronize_session=False)
    db.query(VideoReaction).filter(VideoReaction.user_id == user.id).delete(synchronize_session=False)
    db.query(SavedVideo).filter(SavedVideo.user_id == user.id).delete(synchronize_session=False)
    db.query(Subscription).filter(Subscription.user_id == user.id).delete(synchronize_session=False)
    db.query(QueueItem).filter(QueueItem.user_id == user.id).delete(synchronize_session=False)
    db.delete(user)


@router.delete("/session/profiles/{user_id}")
def delete_profile(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    user = db.get(UserProfile, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")
    if user.name == "guest":
        raise HTTPException(status_code=400, detail="The built-in guest account cannot be deleted")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Use another account before deleting your own profile")
    if user.is_admin:
        admin_count = db.scalar(select(func.count(UserProfile.id)).where(UserProfile.is_admin.is_(True))) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="halcyon must always have at least one admin")
    _delete_user_profile_records(db, user)
    db.commit()
    return {"ok": True}


@router.get("/session/me", response_model=SessionUserOut)
def session_me(current_user: UserProfile = Depends(get_current_user)) -> SessionUserOut:
    return _current_user_out(current_user)


def _build_profile_summary(db: Session, viewed_user: UserProfile) -> dict:
    subscriptions = db.scalars(select(Subscription).where(Subscription.user_id == viewed_user.id)).all()
    subscribed_channel_ids = [item.channel_id for item in subscriptions]
    channels = []
    if subscribed_channel_ids:
        channels = db.scalars(select(Channel).where(Channel.id.in_(subscribed_channel_ids)).order_by(Channel.name)).all()

    playlists = db.scalars(select(Playlist).where(Playlist.user_id == viewed_user.id).order_by(Playlist.name)).all()
    recent_history = db.scalars(
        select(WatchHistory).where(WatchHistory.user_id == viewed_user.id).order_by(WatchHistory.watched_at.desc()).limit(40)
    ).all()

    recent_video_ids: list[int] = []
    for item in recent_history:
        if item.video_id not in recent_video_ids:
            recent_video_ids.append(item.video_id)
    recent_videos = []
    if recent_video_ids:
        recent_lookup = {
            video.id: video
            for video in db.scalars(_video_query().where(Video.id.in_(recent_video_ids))).unique().all()
        }
        recent_videos = [summarize_video(recent_lookup[video_id], db=db) for video_id in recent_video_ids if video_id in recent_lookup]

    liked_reactions = db.scalars(
        select(VideoReaction).where(VideoReaction.user_id == viewed_user.id, VideoReaction.reaction == "like").order_by(VideoReaction.updated_at.desc())
    ).all()
    saved_videos = db.scalars(
        select(SavedVideo).where(SavedVideo.user_id == viewed_user.id).order_by(SavedVideo.updated_at.desc())
    ).all()
    queue = db.scalars(select(QueueItem).where(QueueItem.user_id == viewed_user.id).order_by(QueueItem.position)).all()
    queue_lookup = {}
    if queue:
        queue_lookup = {
            video.id: video
            for video in db.scalars(_video_query().where(Video.id.in_([item.video_id for item in queue]))).unique().all()
        }
    liked_lookup = {}
    if liked_reactions:
        liked_lookup = {
            video.id: video
            for video in db.scalars(_video_query().where(Video.id.in_([item.video_id for item in liked_reactions]))).unique().all()
        }
    saved_lookup = {}
    if saved_videos:
        saved_lookup = {
            video.id: video
            for video in db.scalars(_video_query().where(Video.id.in_([item.video_id for item in saved_videos]))).unique().all()
        }

    return {
        "profile": _profile_out(viewed_user),
        "playlists": [_playlist_out(db, item, viewed_user.id).model_dump() for item in playlists],
        "subscriptions": [
            {
                **_channel_out(db, channel, user=viewed_user, subscribed=True).model_dump(),
            }
            for channel in channels
        ],
        "recently_watched": recent_videos,
        "liked_videos": [
            summarize_video(liked_lookup[item.video_id], db=db)
            for item in liked_reactions
            if item.video_id in liked_lookup
        ],
        "saved_videos": [
            summarize_video(saved_lookup[item.video_id], db=db).model_copy(update={"user_saved": True})
            for item in saved_videos
            if item.video_id in saved_lookup
        ],
        "queue": [
            {
                "id": item.id,
                "position": item.position,
                "video": summarize_video(queue_lookup[item.video_id], db=db),
            }
            for item in queue
            if item.video_id in queue_lookup
        ],
    }


@router.get("/profile/summary")
def profile_summary(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    return _build_profile_summary(db, current_user)


@router.get("/profile/{username}/summary")
def public_profile_summary(username: str, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    normalized_username = _normalize_username(username, strict=False)
    viewed_user = db.scalar(select(UserProfile).where(func.lower(UserProfile.name) == normalized_username))
    if not viewed_user:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _build_profile_summary(db, viewed_user)


@router.get("/profile/{username}/saved")
def public_profile_saved_videos(username: str, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    normalized_username = _normalize_username(username, strict=False)
    viewed_user = db.scalar(select(UserProfile).where(func.lower(UserProfile.name) == normalized_username))
    if not viewed_user:
        raise HTTPException(status_code=404, detail="Profile not found")
    saved_rows = db.scalars(
        select(SavedVideo)
        .where(SavedVideo.user_id == viewed_user.id)
        .order_by(SavedVideo.updated_at.desc())
    ).all()
    video_ids = [row.video_id for row in saved_rows]
    saved_lookup: dict[int, Video] = {}
    if video_ids:
        saved_lookup = {
            video.id: video
            for video in db.scalars(_video_query().where(Video.id.in_(video_ids))).unique().all()
        }
    return {
        "profile": _profile_out(viewed_user),
        "items": [
            summarize_video(saved_lookup[video_id], db=db).model_copy(update={"user_saved": True})
            for video_id in video_ids
            if video_id in saved_lookup
        ],
    }


@router.post("/subscriptions/clear-new")
def clear_new_subscription_markers(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    current_user.last_subscription_seen_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/session/select/{user_id}", response_model=SessionOut)
def select_profile(
    user_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SessionOut:
    del current_user
    user = db.get(UserProfile, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _create_session(db, user, response)


@router.post("/session/login", response_model=SessionOut)
def login(payload: LoginIn, response: Response, request: Request, db: Session = Depends(get_db)) -> SessionOut:
    normalized_username = _normalize_username(payload.username, strict=False)
    limit_key = _auth_limit_key("login", request=request, username=normalized_username)
    _enforce_auth_rate_limit(limit_key, limit=AUTH_LOGIN_LIMIT, window_seconds=AUTH_LOGIN_WINDOW_SECONDS)
    user = db.scalar(
        select(UserProfile)
        .where(func.lower(UserProfile.name) == normalized_username)
        .order_by(UserProfile.id.asc())
        .limit(1)
    )
    if not user or not verify_password(payload.password, user.password_hash):
        register_failure(limit_key, window_seconds=AUTH_LOGIN_WINDOW_SECONDS)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    clear_failures(limit_key)
    return _create_session(db, user, response)


@router.post("/session/register", response_model=SessionOut)
def register(payload: RegisterIn, response: Response, db: Session = Depends(get_db)) -> SessionOut:
    if not _registration_allowed(db):
        raise HTTPException(status_code=403, detail="Admin setup must be completed before creating accounts")
    normalized_username = _normalize_username(payload.username, strict=True)
    if normalized_username == "admin":
        raise HTTPException(status_code=409, detail="Username already exists")
    existing = db.scalar(select(UserProfile).where(func.lower(UserProfile.name) == normalized_username))
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    display_name = (payload.display_name or "").strip() or normalized_username
    user = UserProfile(
        name=normalized_username,
        display_name=display_name,
        accent_color="#7ea6d6",
        password_hash=hash_password(_validate_password(payload.password)),
        pin_hash=hash_password(_validate_pin(payload.pin)),
        avatar_url=(payload.avatar_url.strip() if payload.avatar_url else DEFAULT_USER_AVATAR),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _create_session(db, user, response)


@router.post("/session/switch", response_model=SessionOut)
def switch_session(payload: SwitchSessionIn, response: Response, db: Session = Depends(get_db)) -> SessionOut:
    token = resolve_session_token(db, payload.session_token)
    if not token or not token.user:
        raise HTTPException(status_code=401, detail="Session token not recognized")
    response.set_cookie(
        settings.session_cookie_name,
        payload.session_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=60 * 60 * 24 * 365,
    )
    return SessionOut(user=_current_user_out(token.user), session_token=payload.session_token)


@router.post("/session/logout")
def logout(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    db: Session = Depends(get_db),
) -> dict:
    if session_token:
        token = resolve_session_token(db, session_token)
        if token:
            db.delete(token)
            db.commit()
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


@router.post("/session/admin/setup", response_model=SessionUserOut)
def complete_admin_setup(
    payload: AdminSetupIn,
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_admin_user),
) -> SessionUserOut:
    if not current_user.requires_admin_setup:
        raise HTTPException(status_code=400, detail="Admin setup is already complete")
    _apply_admin_password_change(current_user, password=payload.password, clear_setup=True)
    _revoke_other_sessions(db, current_user.id, session_token)
    db.commit()
    clear_bootstrap_admin_credentials()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.post("/session/admin/password", response_model=SessionUserOut)
def change_admin_password(
    payload: AdminPasswordChangeIn,
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SessionUserOut:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    _apply_admin_password_change(current_user, password=payload.password)
    _revoke_other_sessions(db, current_user.id, session_token)
    db.commit()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.post("/session/admin/recovery-reset", response_model=SessionUserOut)
def reset_admin_password_from_settings(
    payload: AdminRecoveryIn,
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_admin_user),
) -> SessionUserOut:
    if not verify_recovery_phrase(payload.recovery_phrase, current_user.recovery_phrase_hash):
        raise HTTPException(status_code=401, detail="Recovery phrase not recognized")
    _apply_admin_password_change(current_user, password=payload.password, clear_setup=True)
    _revoke_other_sessions(db, current_user.id, session_token)
    db.commit()
    clear_bootstrap_admin_credentials()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.post("/session/admin/recover", response_model=SessionOut)
def recover_admin_account(payload: AdminRecoveryIn, response: Response, request: Request, db: Session = Depends(get_db)) -> SessionOut:
    limit_key = _auth_limit_key("admin-recover", request=request, username=DEFAULT_ADMIN_USERNAME)
    _enforce_auth_rate_limit(limit_key, limit=AUTH_ADMIN_RECOVERY_LIMIT, window_seconds=AUTH_ADMIN_RECOVERY_WINDOW_SECONDS)
    admin_user = _admin_user(db)
    if not admin_user or not verify_recovery_phrase(payload.recovery_phrase, admin_user.recovery_phrase_hash):
        register_failure(limit_key, window_seconds=AUTH_ADMIN_RECOVERY_WINDOW_SECONDS)
        raise HTTPException(status_code=401, detail="Recovery phrase not recognized")
    clear_failures(limit_key)
    _apply_admin_password_change(admin_user, password=payload.password, clear_setup=True)
    for token in db.scalars(select(SessionToken).where(SessionToken.user_id == admin_user.id)).all():
        db.delete(token)
    db.flush()
    clear_bootstrap_admin_credentials()
    return _create_session(db, admin_user, response)


@router.post("/session/password", response_model=SessionUserOut)
def change_password(
    payload: UserPasswordChangeIn,
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> SessionUserOut:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    current_user.password_hash = hash_password(_validate_password(payload.password))
    _revoke_other_sessions(db, current_user.id, session_token)
    db.commit()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.post("/session/password/reset", response_model=SessionOut)
def reset_password_by_pin(payload: UserPasswordResetByPinIn, response: Response, request: Request, db: Session = Depends(get_db)) -> SessionOut:
    normalized_username = _normalize_username(payload.username, strict=False)
    limit_key = _auth_limit_key("password-reset", request=request, username=normalized_username)
    _enforce_auth_rate_limit(limit_key, limit=AUTH_RESET_LIMIT, window_seconds=AUTH_RESET_WINDOW_SECONDS)
    user = db.scalar(
        select(UserProfile)
        .where(func.lower(UserProfile.name) == normalized_username)
        .order_by(UserProfile.id.asc())
        .limit(1)
    )
    if not user or not user.pin_hash or not verify_password(_validate_pin(payload.pin), user.pin_hash):
        register_failure(limit_key, window_seconds=AUTH_RESET_WINDOW_SECONDS)
        raise HTTPException(status_code=401, detail="Username or PIN not recognized")
    clear_failures(limit_key)
    user.password_hash = hash_password(_validate_password(payload.password))
    for token in db.scalars(select(SessionToken).where(SessionToken.user_id == user.id)).all():
        db.delete(token)
    db.flush()
    return _create_session(db, user, response)


@router.post("/session/pin", response_model=SessionUserOut)
def set_account_pin(
    payload: UserPinSetIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> SessionUserOut:
    if current_user.pin_hash:
        raise HTTPException(status_code=400, detail="Account PIN is already set and cannot be changed")
    current_user.pin_hash = hash_password(_validate_pin(payload.pin))
    db.commit()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.put("/profile/me", response_model=SessionUserOut)
def update_profile_me(payload: ProfileUpdateIn, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> SessionUserOut:
    current_user.display_name = payload.display_name.strip() or current_user.display_name
    current_user.avatar_url = payload.avatar_url.strip() if payload.avatar_url else None
    db.commit()
    db.refresh(current_user)
    return _current_user_out(current_user)


@router.get("/library/roots", response_model=list[LibraryRootOut])
def list_roots(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> list[LibraryRootOut]:
    del current_user
    roots = db.scalars(select(LibraryRoot).order_by(LibraryRoot.id)).all()
    selected_counts = _selected_folder_counts_by_root(db)

    items: list[LibraryRootOut] = []
    for root in roots:
        normalized_root_path = root.path.rstrip("\\/")
        item_count = db.scalar(
            select(func.count(func.distinct(VideoFile.video_id)))
            .join(Video, Video.id == VideoFile.video_id)
            .where(
                Video.is_available.is_(True),
                VideoFile.absolute_path.like(f"{normalized_root_path}%"),
            )
        ) or 0
        items.append(
            LibraryRootOut(
                id=root.id,
                label=root.label,
                path=root.path,
                is_available=root.is_available,
                selected_count=selected_counts.get(root.id, 0),
                item_count=item_count,
            )
        )
    return items


@router.get("/library/storage")
def library_storage(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    content_roots = _selected_storage_roots(db)
    total_library_bytes = _indexed_library_storage_bytes(db, content_roots)
    volume_usage: dict[int, tuple[int, int]] = {}

    for root in content_roots:
        try:
            stat = root.stat()
            usage = shutil.disk_usage(root)
            volume_usage[int(stat.st_dev)] = (int(usage.total), int(usage.free))
        except OSError:
            continue

    total_bytes = sum(total for total, _ in volume_usage.values())
    free_bytes = sum(free for _, free in volume_usage.values())
    return {
        "library_bytes": total_library_bytes,
        "available_bytes": free_bytes,
        "total_bytes": total_bytes,
        "root_count": len(content_roots),
    }


@router.get("/library/selected-folders", response_model=list[SelectedFolderOut])
def list_selected_folders(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> list[SelectedFolderOut]:
    del current_user
    return _effective_selected_folders(db)


@router.post("/library/selected-folders", response_model=SelectedFolderOut)
def add_selected_folder(
    payload: SelectedFolderIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SelectedFolder:
    del current_user
    root = db.get(LibraryRoot, payload.root_id)
    if not root:
        raise HTTPException(status_code=404, detail="Root not found")
    existing = db.scalar(
        select(SelectedFolder).where(SelectedFolder.root_id == payload.root_id, SelectedFolder.relative_path == payload.relative_path)
    )
    if existing:
        return existing
    folder = SelectedFolder(root_id=payload.root_id, relative_path=payload.relative_path.strip("/"))
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


@router.delete("/library/selected-folders/{folder_id}")
def delete_selected_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    folder = db.get(SelectedFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Selected folder not found")
    db.delete(folder)
    db.commit()
    return {"ok": True}


@router.get("/library/browse")
def browse_root(
    root_id: int,
    relative_path: str = "",
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="Root not found")
    absolute = Path(root.path) / relative_path
    if not absolute.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    directories = []
    files = []
    for child in sorted(absolute.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        item = {"name": child.name, "relative_path": (Path(relative_path) / child.name).as_posix()}
        if child.is_dir():
            directories.append(item)
        else:
            files.append(item)
    normalized_root_path = root.path.rstrip("\\/")
    item_count = db.scalar(
        select(func.count(func.distinct(VideoFile.video_id)))
        .join(Video, Video.id == VideoFile.video_id)
        .where(
            Video.is_available.is_(True),
            VideoFile.absolute_path.like(f"{normalized_root_path}%"),
        )
    ) or 0
    selected_count = _selected_folder_counts_by_root(db).get(root.id, 0)
    return {
        "root": LibraryRootOut(
            id=root.id,
            label=root.label,
            path=root.path,
            is_available=root.is_available,
            selected_count=selected_count,
            item_count=item_count,
        ),
        "relative_path": relative_path,
        "directories": directories,
        "files": files,
    }


@router.post("/library/scan", response_model=JobOut)
def trigger_scan(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> ScanJob:
    del current_user
    return scan_selected_folders(db, settings.mounted_roots, trigger="manual")


@router.get("/library/videos", response_model=list[VideoSummary])
def list_videos(
    user_id: int | None = None,
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> list[VideoSummary]:
    active_user_id = user_id or current_user.id
    progress_map = {item.video_id: item for item in db.scalars(select(WatchProgress).where(WatchProgress.user_id == active_user_id)).all()}
    query = (
        _video_query()
        .outerjoin(YouTubeMatch, Video.id == YouTubeMatch.video_id)
        .outerjoin(YouTubeVideoSnapshot, YouTubeMatch.youtube_video_id == YouTubeVideoSnapshot.youtube_video_id)
        .order_by(_video_recency_order_clause().desc(), Video.id.desc())
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    videos = db.scalars(query).unique().all()
    return [summarize_video(video, progress_map.get(video.id), db=db) for video in videos]


@router.get("/library/explore")
def explore_videos(
    offset: int = 0,
    limit: int = 30,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    bounded_offset = max(0, offset)
    bounded_limit = max(1, min(limit, 60))
    progress_map = {item.video_id: item for item in db.scalars(select(WatchProgress).where(WatchProgress.user_id == current_user.id)).all()}
    ranked_videos = _build_explore_ranked_videos(db, current_user, progress_map)
    total = len(ranked_videos)
    videos = ranked_videos[bounded_offset : bounded_offset + bounded_limit]
    return {
        "items": [summarize_video(video, progress_map.get(video.id), db=db) for video in videos],
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": total,
        "has_more": bounded_offset + len(videos) < total,
    }


@router.get("/library/suggested")
def suggested_videos(
    offset: int = 0,
    limit: int = 30,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    bounded_offset = max(0, offset)
    bounded_limit = max(1, min(limit, 60))
    ranked_videos, progress_map = build_suggested_feed(db, current_user.id)
    total = len(ranked_videos)
    videos = ranked_videos[bounded_offset : bounded_offset + bounded_limit]
    return {
        "items": [summarize_video(video, progress_map.get(video.id), db=db) for video in videos],
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": total,
        "has_more": bounded_offset + len(videos) < total,
    }


@router.get("/home", response_model=list[FeedSection])
def home_feed(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> list[FeedSection]:
    return build_home_feed(db, current_user.id)


@router.get("/live", response_model=LiveOverviewOut)
async def live_overview(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> LiveOverviewOut:
    del current_user
    await _refresh_live_if_due(db)
    return _live_overview_payload(db)


@router.get("/live/{youtube_video_id}", response_model=LiveStreamOut)
async def live_stream_detail(
    youtube_video_id: str,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> LiveStreamOut:
    del current_user
    await _refresh_live_if_due(
        db,
        youtube_video_id=youtube_video_id,
    )
    stream = db.scalar(
        select(YouTubeLiveStreamSnapshot)
        .options(joinedload(YouTubeLiveStreamSnapshot.channel))
        .where(
            YouTubeLiveStreamSnapshot.youtube_video_id == youtube_video_id,
            YouTubeLiveStreamSnapshot.is_live.is_(True),
        )
    )
    if not stream:
        raise HTTPException(status_code=404, detail="Live stream not found")
    payload = _serialize_live_stream(db, stream)
    chat_enabled = await _detect_live_chat_enabled(youtube_video_id)
    playback = await _resolve_live_playback(youtube_video_id)
    if playback.get("playback_mode") != "youtube-embed":
        chat_enabled = False
    return payload.model_copy(update={"chat_enabled": chat_enabled, **playback})


@router.get("/search")
def search_library(q: str, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    query = q.strip().lower()
    if len(query) < 2:
        return {"videos": [], "channels": []}

    video_progress = {item.video_id: item for item in db.scalars(select(WatchProgress).where(WatchProgress.user_id == current_user.id)).all()}
    all_videos = db.scalars(_video_query().order_by(Video.created_at.desc())).unique().all()
    video_matches = [
        video
        for video in all_videos
        if tokens_match_query(
            " ".join(
                part
                for part in (
                    video.title,
                    video.channel.name if video.channel else "",
                    video.series.name if video.series else "",
                )
                if part
            ),
            query,
        )
    ][:18]

    all_channels = db.execute(select(Channel).options(joinedload(Channel.videos)).order_by(Channel.name)).unique().scalars().all()
    channel_matches = [
        channel
        for channel in all_channels
        if channel.slug != "unknown-channel"
        if tokens_match_query(
            " ".join(
                part
                for part in (
                    channel.name,
                    channel.description or "",
                    " ".join(video.title for video in channel.videos[:8]) if channel.videos else "",
                )
                if part
            ),
            query,
        )
    ][:8]

    subscribed_ids = set(db.scalars(select(Subscription.channel_id).where(Subscription.user_id == current_user.id)).all())
    return {
        "videos": [summarize_video(video, video_progress.get(video.id), db=db) for video in video_matches],
        "channels": [_channel_out(db, channel, subscribed=channel.id in subscribed_ids) for channel in channel_matches],
    }


@router.get("/videos/{video_ref}")
def get_video(
    video_ref: str,
    request: Request,
    client_profile: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    video = _resolve_video_ref(db, video_ref)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    video_id = video.id

    progress = db.scalar(select(WatchProgress).where(WatchProgress.user_id == current_user.id, WatchProgress.video_id == video_id))
    reaction = db.scalar(select(VideoReaction).where(VideoReaction.user_id == current_user.id, VideoReaction.video_id == video_id))
    saved_video = db.scalar(select(SavedVideo).where(SavedVideo.user_id == current_user.id, SavedVideo.video_id == video_id))
    playback = resolve_playback(video, client_profile=_resolved_playback_profile(request, client_profile))
    primary_file = video.files[0] if video.files else None
    primary_path = Path(primary_file.absolute_path) if primary_file else None
    source_available = bool(primary_path and primary_path.exists())
    media_info = probe_media(primary_path) if source_available and primary_path else {}
    caption_tracks = []
    if source_available and primary_path:
        for index, track in enumerate(find_caption_tracks(primary_path)):
            caption_tracks.append(
                {
                    "id": index,
                    "label": track["label"],
                    "format": track["format"],
                    "url": f"/api/videos/{video.id}/captions/{index}",
                }
            )

    comments = []
    youtube_snapshot = None
    channel_snapshot = _channel_snapshot_for_channel(db, video.channel_id) if video.channel_id else None
    channel_display_name = channel_snapshot.title if channel_snapshot and channel_snapshot.title else (video.channel.name if video.channel else None)
    next_up = _next_up_for_video(db, video)
    suggested_page = _watch_suggestions_page(
        db,
        video=video,
        current_user=current_user,
        mode="suggested",
        offset=0,
        limit=10,
        next_up=next_up,
        channel_display_name=channel_display_name,
    )
    subscribed = (
        db.scalar(select(Subscription).where(Subscription.user_id == current_user.id, Subscription.channel_id == video.channel_id)) is not None
        if video.channel_id
        else False
    )
    authoritative_match = _authoritative_youtube_match(video)
    if authoritative_match and authoritative_match.youtube_video_id:
        youtube_snapshot = db.scalar(
            select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == authoritative_match.youtube_video_id)
        )
        comments = db.scalars(
            select(YouTubeCommentSnapshot)
            .where(YouTubeCommentSnapshot.youtube_video_id == authoritative_match.youtube_video_id)
            .order_by(YouTubeCommentSnapshot.id.asc())
        ).all()
    comment_replies_by_parent: dict[int, list[YouTubeCommentReplySnapshot]] = {}
    if comments:
        reply_rows = db.scalars(
            select(YouTubeCommentReplySnapshot)
            .where(YouTubeCommentReplySnapshot.parent_comment_id.in_([item.id for item in comments]))
            .order_by(
                YouTubeCommentReplySnapshot.parent_comment_id.asc(),
                YouTubeCommentReplySnapshot.position.asc(),
                YouTubeCommentReplySnapshot.id.asc(),
            )
        ).all()
        for reply in reply_rows:
            comment_replies_by_parent.setdefault(reply.parent_comment_id, []).append(reply)

    return {
        "video": (
            lambda summary: summary.model_copy(
                update={
                    "user_reaction": reaction.reaction if reaction else None,
                    "user_saved": saved_video is not None,
                }
            )
        )(_with_channel_display(summarize_video(video, progress, db=db), channel_display_name)),
        "watch_ref": _video_ref_for(db, video),
        "playback": playback,
        "media_info": {
            **media_info,
            "source_path": primary_file.absolute_path if primary_file else None,
            "transcoding": playback["requires_transcode"],
            "source_missing": bool(primary_file and not source_available),
        },
        "captions": caption_tracks,
        "resume_point": _resume_point_for(progress, video.duration_seconds),
        "channel": (
            ChannelOut(
                id=video.channel.id,
                name=channel_display_name or video.channel.name,
                slug=video.channel.slug,
                description=video.channel.description or (channel_snapshot.description if channel_snapshot else None),
                avatar_url=_channel_image_proxy(
                    video.channel.id,
                    "avatar",
                    video.channel.avatar_url or (channel_snapshot.avatar_url if channel_snapshot else None),
                ),
                banner_url=_channel_image_proxy(
                    video.channel.id,
                    "banner",
                    video.channel.banner_url or (channel_snapshot.banner_url if channel_snapshot else None),
                ),
                video_count=db.scalar(select(func.count(Video.id)).where(Video.channel_id == video.channel_id)) or 0,
                subscribed=subscribed,
                subscriber_count=channel_snapshot.subscriber_count if channel_snapshot else None,
                view_count=channel_snapshot.view_count if channel_snapshot else None,
                youtube_video_count=channel_snapshot.video_count if channel_snapshot else None,
                youtube_fetched_at=channel_snapshot.fetched_at if channel_snapshot else None,
            ).model_dump()
            if video.channel
            else None
        ),
        "next_up": _with_channel_display(summarize_video(next_up, db=db) if next_up else None, channel_display_name if next_up and next_up.channel_id == video.channel_id else None),
        "suggested": suggested_page["items"],
        "suggested_total": suggested_page["total"],
        "suggested_has_more": suggested_page["has_more"],
        "youtube": {
            "match": {
                "status": video.youtube_match.status,
                "confidence": video.youtube_match.confidence,
                "reasons": video.youtube_match.reasons,
            }
            if video.youtube_match
            else None,
            "snapshot": {
                "title": youtube_snapshot.title,
                "description": youtube_snapshot.description,
                "published_at": _trusted_snapshot_published_at(youtube_snapshot),
                "view_count": youtube_snapshot.view_count,
                "like_count": youtube_snapshot.like_count,
                "dislike_count": youtube_snapshot.dislike_count,
                "rating": youtube_snapshot.rating,
                "duration_seconds": youtube_snapshot.duration_seconds,
                "fetched_at": youtube_snapshot.fetched_at,
            }
            if youtube_snapshot
            else None,
            "comments": [
                {
                    "id": item.id,
                    "youtube_comment_id": item.youtube_comment_id,
                    "author_name": item.author_name,
                    "body": item.body,
                    "like_count": item.like_count,
                    "reply_count": item.reply_count,
                    "published_at": item.published_at,
                    "replies": [
                        {
                            "id": reply.id,
                            "youtube_reply_id": reply.youtube_reply_id,
                            "author_name": reply.author_name,
                            "body": reply.body,
                            "like_count": reply.like_count,
                            "published_at": reply.published_at,
                        }
                        for reply in comment_replies_by_parent.get(item.id, [])
                    ],
                }
                for item in comments
            ],
        },
    }


@router.get("/videos/{video_ref}/suggestions")
def get_video_suggestions(
    video_ref: str,
    mode: str = Query("suggested"),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=25),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    video = _resolve_video_ref(db, video_ref)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    channel_snapshot = _channel_snapshot_for_channel(db, video.channel_id) if video.channel_id else None
    channel_display_name = (
        channel_snapshot.title
        if channel_snapshot and channel_snapshot.title
        else (video.channel.name if video.channel else None)
    )
    next_up = _next_up_for_video(db, video)
    return _watch_suggestions_page(
        db,
        video=video,
        current_user=current_user,
        mode=mode,
        offset=offset,
        limit=limit,
        next_up=next_up,
        channel_display_name=channel_display_name,
    )


@router.post("/videos/{video_id}/progress")
def update_progress(
    video_id: int,
    payload: ProgressIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    video = _video_by_id(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    progress = db.scalar(select(WatchProgress).where(WatchProgress.user_id == current_user.id, WatchProgress.video_id == video_id))
    if not progress:
        progress = WatchProgress(user_id=current_user.id, video_id=video_id)
        db.add(progress)
    normalized_position, completed = _normalized_watch_progress(
        payload.position_seconds,
        duration_seconds=video.duration_seconds,
        completed=payload.completed,
    )
    history_position = max(0, int(payload.position_seconds))
    if video.duration_seconds and video.duration_seconds > 0:
        history_position = min(history_position, video.duration_seconds)
    progress.position_seconds = normalized_position
    progress.completed = completed
    db.add(WatchHistory(user_id=current_user.id, video_id=video_id, position_seconds=history_position))
    db.commit()
    return {"ok": True}


@router.post("/videos/{video_id}/reaction")
def set_reaction(video_id: int, payload: ReactionIn, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    reaction = db.scalar(select(VideoReaction).where(VideoReaction.user_id == current_user.id, VideoReaction.video_id == video_id))
    if payload.reaction not in {"like", "dislike", None}:
        raise HTTPException(status_code=400, detail="Unsupported reaction")
    if payload.reaction is None:
        if reaction:
            db.delete(reaction)
            db.commit()
        return {"reaction": None}
    if not reaction:
        reaction = VideoReaction(user_id=current_user.id, video_id=video_id, reaction=payload.reaction)
        db.add(reaction)
    else:
        reaction.reaction = payload.reaction
    db.commit()
    return {"reaction": payload.reaction}


@router.post("/videos/{video_id}/save")
def toggle_saved_video(video_id: int, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    video = _video_by_id(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    saved = db.scalar(select(SavedVideo).where(SavedVideo.user_id == current_user.id, SavedVideo.video_id == video_id))
    if saved:
        db.delete(saved)
        db.commit()
        return {"saved": False}
    db.add(SavedVideo(user_id=current_user.id, video_id=video_id))
    db.commit()
    return {"saved": True}


@router.post("/playlists/{playlist_id}/save")
def set_playlist_saved_state(
    playlist_id: int,
    payload: CollectionSaveIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    playlist = db.get(Playlist, playlist_id)
    if not playlist or playlist.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Playlist not found")
    video_ids = _playlist_video_ids(db, playlist_id)
    if not video_ids:
        return {"saved": payload.saved, "count": 0}
    existing_ids = _saved_video_id_set(db, current_user.id, video_ids)
    if payload.saved:
        for video_id in video_ids:
            if video_id not in existing_ids:
                db.add(SavedVideo(user_id=current_user.id, video_id=video_id))
    else:
        db.query(SavedVideo).filter(
            SavedVideo.user_id == current_user.id,
            SavedVideo.video_id.in_(video_ids),
        ).delete(synchronize_session=False)
    db.commit()
    return {"saved": payload.saved, "count": len(video_ids)}


@router.post("/series/{series_id}/save")
def set_series_saved_state(
    series_id: int,
    payload: CollectionSaveIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    video_ids = _series_video_ids(db, series_id)
    if not video_ids:
        return {"saved": payload.saved, "count": 0}
    existing_ids = _saved_video_id_set(db, current_user.id, video_ids)
    if payload.saved:
        for video_id in video_ids:
            if video_id not in existing_ids:
                db.add(SavedVideo(user_id=current_user.id, video_id=video_id))
    else:
        db.query(SavedVideo).filter(
            SavedVideo.user_id == current_user.id,
            SavedVideo.video_id.in_(video_ids),
        ).delete(synchronize_session=False)
    db.commit()
    return {"saved": payload.saved, "count": len(video_ids)}


@router.post("/videos/{video_id}/watch-state")
def set_watch_state(
    video_id: int,
    payload: WatchStateIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    video = _video_by_id(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    progress = db.scalar(select(WatchProgress).where(WatchProgress.user_id == current_user.id, WatchProgress.video_id == video_id))
    state = (payload.state or "").strip().lower()
    if state == "watched":
        if not progress:
            progress = WatchProgress(user_id=current_user.id, video_id=video_id)
            db.add(progress)
        progress.position_seconds = 0
        progress.completed = True
        db.add(WatchHistory(user_id=current_user.id, video_id=video_id, position_seconds=video.duration_seconds or 0))
    elif state == "unwatched":
        if progress:
            db.delete(progress)
        db.query(WatchHistory).filter(
            WatchHistory.user_id == current_user.id,
            WatchHistory.video_id == video_id,
        ).delete(synchronize_session=False)
    else:
        raise HTTPException(status_code=400, detail="Unsupported watch state")
    db.commit()
    return {"ok": True}


@router.get("/videos/{video_id}/stream")
def stream_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    video = _video_by_id(db, video_id)
    if not video or not video.files:
        raise HTTPException(status_code=404, detail="File not found")
    source_path = Path(video.files[0].absolute_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video source is unavailable")
    return FileResponse(source_path)


@router.get("/videos/{video_id}/compatible")
def compatible_stream(
    video_id: int,
    request: Request,
    client_profile: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    video = _video_by_id(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    playback = resolve_playback(video, client_profile=_resolved_playback_profile(request, client_profile))
    if playback.get("source_missing"):
        raise HTTPException(status_code=404, detail="Video source is unavailable")
    profile = playback.get("transcode_profile")
    if profile not in {"remux-webm", "remux-mp4-copy", "remux-mp4-aac", "transcode-mp4-mobile", "transcode-mp4-android"}:
        raise HTTPException(status_code=400, detail="Video does not require compatible-stream processing")
    try:
        output_path = ensure_compatible_stream(db, video, settings.cache_dir, profile)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileResponse(output_path)


@router.get("/videos/{video_id}/hls/{segment_path:path}")
def hls_stream(
    video_id: int,
    segment_path: str,
    request: Request,
    client_profile: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    video = _video_by_id(db, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    playback = resolve_playback(video, client_profile=_resolved_playback_profile(request, client_profile))
    if playback.get("source_missing"):
        raise HTTPException(status_code=404, detail="Video source is unavailable")
    if not playback["requires_transcode"]:
        raise HTTPException(status_code=400, detail="Video can direct play")

    try:
        playlist_path = ensure_hls_transcode(db, video, settings.cache_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    target = playlist_path.parent / segment_path
    if segment_path in ("", ".", "/"):
        target = playlist_path
    elif segment_path == "index.m3u8":
        target = playlist_path

    if target == playlist_path and not target.exists():
        wait_for_transcode_playlist(playlist_path)
    elif not target.exists():
        wait_for_transcode_target(target)

    if not target.exists():
        raise HTTPException(status_code=404, detail="HLS segment not found")
    return FileResponse(target)


@router.get("/videos/{video_id}/thumbnail")
def video_thumbnail(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    authoritative_match = _authoritative_youtube_match(video)
    if (not video.thumbnail_path or not Path(video.thumbnail_path).exists()) and authoritative_match and authoritative_match.youtube_video_id:
        snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == authoritative_match.youtube_video_id))
        thumbnail_url = snapshot.thumbnail_url if snapshot else None
        if thumbnail_url:
            fingerprint = video.files[0].fingerprint if video.files else f"yt-{authoritative_match.youtube_video_id}"
            downloaded = download_thumbnail(thumbnail_url, settings.cache_dir, fingerprint, force_replace=True)
            if downloaded:
                video.thumbnail_path = downloaded
                db.commit()
    source_path = Path(video.files[0].absolute_path) if video.files else None
    if (
        (not video.thumbnail_path or not Path(video.thumbnail_path).exists())
        and video.files
        and source_path
        and source_path.exists()
    ):
        generated = generate_thumbnail(source_path, settings.cache_dir, video.files[0].fingerprint)
        if generated:
            video.thumbnail_path = generated
            db.commit()
    if not video.thumbnail_path or not Path(video.thumbnail_path).exists():
        return Response(content=placeholder_thumbnail_svg(video.title, video.channel.name if video.channel else None), media_type="image/svg+xml")
    return FileResponse(video.thumbnail_path)


@router.get("/videos/{video_id}/preview")
def video_preview(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    video = db.get(Video, video_id)
    if not video or not video.files:
        raise HTTPException(status_code=404, detail="Video not found")
    source_path = Path(video.files[0].absolute_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video source is unavailable")
    preview_path = generate_preview_clip(source_path, settings.cache_dir, video.files[0].fingerprint)
    if not preview_path or not Path(preview_path).exists():
        raise HTTPException(status_code=404, detail="Preview not available")
    return FileResponse(preview_path)


@router.get("/videos/{video_id}/captions/{track_index}")
def caption_track(
    video_id: int,
    track_index: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> Response:
    video = _video_by_id(db, video_id)
    if not video or not video.files:
        raise HTTPException(status_code=404, detail="Video not found")
    source_path = Path(video.files[0].absolute_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Video source is unavailable")
    tracks = find_caption_tracks(source_path)
    if track_index < 0 or track_index >= len(tracks):
        raise HTTPException(status_code=404, detail="Caption track not found")
    track = tracks[track_index]
    content = Path(track["path"]).read_text(encoding="utf-8", errors="ignore")
    if track["format"] == "srt":
        content = srt_to_vtt(content)
    return Response(content=content, media_type="text/vtt; charset=utf-8")


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> list[ChannelOut]:
    subscribed_ids = set(db.scalars(select(Subscription.channel_id).where(Subscription.user_id == current_user.id)).all())
    channels = db.scalars(select(Channel).where(Channel.slug != "unknown-channel").order_by(Channel.name)).all()
    return [
        _channel_out(db, channel, user=current_user, subscribed=channel.id in subscribed_ids)
        for channel in channels
    ]


@router.get("/channels/{channel_ref}")
def get_channel(channel_ref: str, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    channel = _resolve_channel_ref(db, channel_ref)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.slug == "unknown-channel":
        raise HTTPException(status_code=404, detail="Channel not found")
    channel_id = channel.id
    videos = db.scalars(
        _video_query()
        .where(Video.channel_id == channel_id)
        .order_by(
            Video.published_at.desc().nullslast(),
            Video.created_at.desc(),
            Video.id.desc(),
        )
    ).unique().all()
    subscribed = db.scalar(select(Subscription).where(Subscription.user_id == current_user.id, Subscription.channel_id == channel_id)) is not None
    snapshot = _channel_snapshot_for_channel(db, channel_id)
    channel_display_name = snapshot.title if snapshot and snapshot.title else channel.name
    video_summaries = _apply_user_saved_flags(
        db,
        current_user,
        [_with_channel_display(summarize_video(video, db=db), channel_display_name) for video in videos],
    )
    for video, summary in zip(videos, video_summaries):
        if not video.series or not summary.series_id:
            continue
        if _series_matches_channel_name(video.series.name, video.channel.name if video.channel else None):
            summary.series_id = None
            summary.series_name = None
    fresh_cutoff = datetime.utcnow() - timedelta(seconds=LIVE_STALE_AFTER_SECONDS)
    live_stream = db.scalar(
        select(YouTubeLiveStreamSnapshot)
        .options(joinedload(YouTubeLiveStreamSnapshot.channel))
        .where(
            YouTubeLiveStreamSnapshot.channel_id == channel_id,
            YouTubeLiveStreamSnapshot.is_live.is_(True),
            YouTubeLiveStreamSnapshot.last_seen_at >= fresh_cutoff,
        )
        .order_by(
            YouTubeLiveStreamSnapshot.concurrent_viewers.desc().nullslast(),
            YouTubeLiveStreamSnapshot.actual_start_at.desc().nullslast(),
            YouTubeLiveStreamSnapshot.last_seen_at.desc(),
        )
    )
    return {
        "channel": _channel_out(db, channel, user=current_user, subscribed=subscribed, video_count=len(videos)),
        "videos": video_summaries,
        "live_stream": _serialize_live_stream(db, live_stream) if live_stream else None,
    }


@router.get("/channels/{channel_id}/{kind}-image")
def channel_image(
    channel_id: int,
    kind: str,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> FileResponse:
    if kind not in {"avatar", "banner"}:
        raise HTTPException(status_code=404, detail="Image not found")
    channel = db.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    snapshot = _channel_snapshot_for_channel(db, channel_id)
    remote_url = channel.avatar_url if kind == "avatar" else channel.banner_url
    if snapshot:
        remote_url = remote_url or (snapshot.avatar_url if kind == "avatar" else snapshot.banner_url)
    if not remote_url:
        raise HTTPException(status_code=404, detail="Image not found")
    if remote_url.startswith("http"):
        fingerprint = hashlib.sha1(remote_url.encode("utf-8")).hexdigest()[:12]
        cached = download_thumbnail(remote_url, settings.cache_dir, f"channel-{channel_id}-{kind}-{fingerprint}")
        if not cached:
            raise HTTPException(status_code=404, detail="Image not found")
        return FileResponse(cached)
    if not Path(remote_url).exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(remote_url)


@router.post("/channels/{channel_ref}/subscribe")
def toggle_subscription(channel_ref: str, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    channel = _resolve_channel_ref(db, channel_ref)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    channel_id = channel.id
    existing = db.scalar(select(Subscription).where(Subscription.user_id == current_user.id, Subscription.channel_id == channel_id))
    if existing:
        db.delete(existing)
        action = "unsubscribed"
    else:
        db.add(Subscription(user_id=current_user.id, channel_id=channel_id))
        action = "subscribed"
    db.commit()
    return {"status": action}


@router.get("/series", response_model=list[SeriesOut])
def list_series(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> list[SeriesOut]:
    series_items = db.scalars(select(Series).order_by(Series.name)).all()
    output: list[SeriesOut] = []
    for item in series_items:
        saved_count, all_videos_saved = _series_saved_summary(db, item.id, current_user.id)
        output.append(
            SeriesOut(
                id=item.id,
                name=item.name,
                slug=item.slug,
                description=item.description,
                video_count=db.scalar(select(func.count(Video.id)).where(Video.series_id == item.id)) or 0,
                preview_thumbnails=_series_preview_thumbnails(db, item.id),
                saved_video_count=saved_count,
                all_videos_saved=all_videos_saved,
            )
        )
    return output


@router.get("/series/{series_id}")
def get_series(series_id: int, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    videos = db.scalars(_video_query().where(Video.series_id == series_id).order_by(Video.episode_number.asc().nullslast(), Video.id.asc())).unique().all()
    saved_count, all_videos_saved = _series_saved_summary(db, series_id, current_user.id)
    return {
        "series": SeriesOut(
            id=series.id,
            name=series.name,
            slug=series.slug,
            description=series.description,
            video_count=len(videos),
            preview_thumbnails=_series_preview_thumbnails(db, series_id),
            saved_video_count=saved_count,
            all_videos_saved=all_videos_saved,
        ),
        "videos": _apply_user_saved_flags(db, current_user, [summarize_video(video, db=db) for video in videos]),
    }


@router.get("/playlists", response_model=list[PlaylistOut])
def list_playlists(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> list[PlaylistOut]:
    playlists = db.scalars(select(Playlist).where(Playlist.user_id == current_user.id).order_by(Playlist.name)).all()
    return [_playlist_out(db, item, current_user.id) for item in playlists]


@router.get("/playlists/{playlist_id}")
def get_playlist_detail(playlist_id: int, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    playlist, videos = _playlist_video_lookup(db, playlist_id)
    if not playlist or playlist.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {
        "playlist": _playlist_out(db, playlist, current_user.id),
        "videos": _apply_user_saved_flags(db, current_user, videos),
    }


@router.post("/playlists", response_model=PlaylistOut)
def create_playlist(
    payload: PlaylistCreateIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> PlaylistOut:
    playlist = Playlist(user_id=current_user.id, name=payload.name, description=payload.description)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist, current_user.id)


@router.post("/playlists/{playlist_id}/items")
def add_playlist_item(
    playlist_id: int,
    payload: QueueItemIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    playlist = db.get(Playlist, playlist_id)
    if not playlist or playlist.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Playlist not found")
    max_position = db.scalar(select(func.max(PlaylistItem.position)).where(PlaylistItem.playlist_id == playlist_id))
    db.add(PlaylistItem(playlist_id=playlist_id, video_id=payload.video_id, position=(max_position or 0) + 1))
    db.commit()
    return {"ok": True}


@router.put("/playlists/{playlist_id}/items/reorder")
def reorder_playlist_items(playlist_id: int, payload: dict, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    playlist = db.get(Playlist, playlist_id)
    if not playlist or playlist.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Playlist not found")
    ordered_ids = [int(video_id) for video_id in payload.get("video_ids", []) if isinstance(video_id, int) or str(video_id).isdigit()]
    items = db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.position.asc())).all()
    item_by_video_id = {item.video_id: item for item in items}
    if set(ordered_ids) != set(item_by_video_id.keys()):
        raise HTTPException(status_code=400, detail="Playlist reorder did not include the full playlist")
    for index, video_id in enumerate(ordered_ids, start=1):
        item_by_video_id[video_id].position = index
    db.commit()
    return {"ok": True}


@router.get("/queue")
def list_queue(db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    queue = db.scalars(select(QueueItem).where(QueueItem.user_id == current_user.id).order_by(QueueItem.position)).all()
    video_ids = [item.video_id for item in queue]
    videos = {video.id: video for video in db.scalars(_video_query().where(Video.id.in_(video_ids)) if video_ids else _video_query().where(Video.id == -1)).unique().all()}
    return {
        "items": [
            {
                "id": item.id,
                "position": item.position,
                "video": summarize_video(videos[item.video_id], db=db),
            }
            for item in queue
            if item.video_id in videos
        ]
    }


@router.post("/queue")
def add_queue_item(payload: QueueItemIn, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    max_position = db.scalar(select(func.max(QueueItem.position)).where(QueueItem.user_id == current_user.id))
    item = QueueItem(user_id=current_user.id, video_id=payload.video_id, position=(max_position or 0) + 1)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "position": item.position}


@router.post("/queue/bulk")
def replace_queue(payload: QueueBulkIn, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    video_ids = [video_id for video_id in payload.video_ids if isinstance(video_id, int)]
    if payload.reset:
        db.query(QueueItem).filter(QueueItem.user_id == current_user.id).delete()
        start_position = 0
    else:
        start_position = db.scalar(select(func.max(QueueItem.position)).where(QueueItem.user_id == current_user.id)) or 0
    for offset, video_id in enumerate(video_ids, start=1):
        db.add(QueueItem(user_id=current_user.id, video_id=video_id, position=start_position + offset))
    db.commit()
    return {"ok": True, "count": len(video_ids)}


@router.delete("/queue/{item_id}")
def delete_queue_item(item_id: int, db: Session = Depends(get_db), current_user: UserProfile = Depends(get_current_user)) -> dict:
    item = db.scalar(select(QueueItem).where(QueueItem.id == item_id, QueueItem.user_id == current_user.id))
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.post("/admin/metadata-overrides")
def upsert_override(
    payload: MetadataOverrideIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    override = db.scalar(
        select(MetadataOverride).where(MetadataOverride.target_type == payload.target_type, MetadataOverride.target_id == payload.target_id)
    )
    if not override:
        override = MetadataOverride(target_type=payload.target_type, target_id=payload.target_id)
        db.add(override)
    override.payload = payload.payload
    db.commit()
    return {"ok": True}


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> list[JobOut]:
    del current_user
    scan_jobs = db.scalars(select(ScanJob)).all()
    sync_jobs = [reconcile_sync_job(db, job) for job in db.scalars(select(SyncJob)).all()]
    transcode_jobs = db.scalars(select(TranscodeJob)).all()
    combined = [
        *[
            {
                "id": job.id,
                "scope": job.scope,
                "status": job.status,
                "details": job.details or {},
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "created_at": job.created_at,
            }
            for job in [*scan_jobs, *sync_jobs]
        ],
        *[
            {
                "id": job.id,
                "scope": "transcode",
                "status": reconcile_transcode_job(db, job).status,
                "details": {
                    "profile": job.profile,
                    "output_path": job.output_path,
                    "title": db.scalar(select(Video.title).where(Video.id == job.video_id)),
                    "video_id": job.video_id,
                    "throttled": transcode_is_throttled(job),
                },
                "started_at": job.created_at,
                "finished_at": job.updated_at if job.status in {"completed", "failed"} else None,
                "created_at": job.created_at,
            }
            for job in transcode_jobs
        ],
    ]
    combined.sort(key=lambda item: item["created_at"], reverse=True)
    return [
        {
            "id": item["id"],
            "scope": item["scope"],
            "status": item["status"],
            "details": item["details"],
            "started_at": item["started_at"],
            "finished_at": item["finished_at"],
        }
        for item in combined[:25]
    ]


@router.get("/jobs/status")
def job_status(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
) -> dict:
    del current_user
    active_scans = db.scalars(select(ScanJob).where(ScanJob.status == "running").order_by(ScanJob.created_at.desc())).all()
    active_syncs = [reconcile_sync_job(db, job) for job in db.scalars(select(SyncJob).where(SyncJob.status == "running").order_by(SyncJob.created_at.desc())).all()]
    active_transcodes = db.scalars(select(TranscodeJob).where(TranscodeJob.status == "running").order_by(TranscodeJob.created_at.desc())).all()
    items = []
    for job in [*active_scans, *active_syncs]:
        if job.status != "running":
            continue
        items.append(
            {
                "id": job.id,
                "scope": job.scope,
                "status": job.status,
                "percent": (job.details or {}).get("percent"),
                "details": job.details or {},
            }
        )
    for job in active_transcodes:
        job = reconcile_transcode_job(db, job)
        if job.status != "running":
            continue
        video = db.get(Video, job.video_id)
        items.append(
            {
                "id": job.id,
                "scope": "transcode",
                "status": job.status,
                "percent": None,
                "details": {
                    "profile": job.profile,
                    "output_path": job.output_path,
                    "title": video.title if video else None,
                    "video_id": job.video_id,
                    "throttled": transcode_is_throttled(job),
                },
            }
        )
    return {"items": items}


@router.get("/transcodes")
def list_transcodes(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    transcodes = db.scalars(select(TranscodeJob).order_by(TranscodeJob.created_at.desc()).limit(25)).all()
    return {
        "items": [
            {
                "id": job.id,
                "video_id": job.video_id,
                "title": db.scalar(select(Video.title).where(Video.id == job.video_id)),
                "profile": job.profile,
                "status": reconcile_transcode_job(db, job).status,
                "output_path": job.output_path,
                "pid": job.pid,
                "throttled": transcode_is_throttled(job),
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
            for job in transcodes
        ]
    }


@router.post("/transcodes/{job_id}/stop")
def halt_transcode(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    job = db.get(TranscodeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Transcode job not found")
    stopped = stop_transcode_job(db, job)
    return {"ok": True, "stopped": stopped}


@router.get("/logs")
def app_logs(
    limit: int = 20,
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    bounded_limit = max(1, min(limit, 1000))
    return {"lines": read_log_lines(bounded_limit)}


@router.get("/sync/settings", response_model=SyncSettingsOut)
def get_sync_settings(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncSettings:
    del current_user
    changed = False
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row:
        settings_row = SyncSettings(
            comment_limit=100,
            automatic_detection_enabled=True,
            scan_interval_seconds=max(5, min(settings.scan_interval_seconds, 3600)),
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    elif settings_row.comment_limit == 10:
        settings_row.comment_limit = 100
        db.commit()
        db.refresh(settings_row)
    if settings_row.automatic_detection_enabled is None:
        settings_row.automatic_detection_enabled = True
        db.commit()
        db.refresh(settings_row)
    if settings_row.live_tab_enabled is None:
        settings_row.live_tab_enabled = True
        db.commit()
        db.refresh(settings_row)
    if settings_row.subtitle_generation_enabled is None:
        settings_row.subtitle_generation_enabled = False
        db.commit()
        db.refresh(settings_row)
    if not settings_row.scan_interval_seconds:
        settings_row.scan_interval_seconds = max(5, min(settings.scan_interval_seconds, 3600))
        db.commit()
        db.refresh(settings_row)
    if settings_row.max_replies_per_comment is None:
        settings_row.max_replies_per_comment = 3
        db.commit()
        db.refresh(settings_row)
    if not settings_row.requests_per_second:
        settings_row.requests_per_second = 3
        db.commit()
        db.refresh(settings_row)
    if settings_row.allow_fallback_art is None:
        settings_row.allow_fallback_art = False
        db.commit()
        db.refresh(settings_row)
    if settings_row.prefer_high_res_banners is None:
        settings_row.prefer_high_res_banners = False
        db.commit()
        db.refresh(settings_row)
    if not settings_row.youtube_api_key and settings.youtube_api_key:
        settings_row.youtube_api_key = settings.youtube_api_key
        changed = True
    changed = normalize_youtube_api_quota(settings_row) or changed
    if changed:
        db.commit()
        db.refresh(settings_row)
    return _sync_settings_payload(db, settings_row)


@router.post("/sync/youtube-cookies", response_model=SyncSettingsOut)
async def upload_youtube_cookies(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncSettingsOut:
    del current_user
    filename = (file.filename or "").strip().lower()
    if filename and not filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Upload a cookies.txt file")
    content = await file.read()
    await file.close()
    if not content.strip():
        raise HTTPException(status_code=400, detail="Uploaded cookies file is empty")
    if len(content) > YOUTUBE_COOKIES_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded cookies file is too large")
    cookie_path = _youtube_cookies_path()
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cookie_path.with_suffix(".upload")
    if temp_path.exists():
        temp_path.unlink()
    temp_path.write_bytes(content)
    temp_path.replace(cookie_path)
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row:
        settings_row = SyncSettings()
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    return _sync_settings_payload(db, settings_row)


@router.delete("/sync/youtube-cookies", response_model=SyncSettingsOut)
async def delete_youtube_cookies(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncSettingsOut:
    del current_user
    cookie_path = _youtube_cookies_path()
    if cookie_path.exists():
        cookie_path.unlink()
    settings_row = db.scalar(select(SyncSettings))
    if not settings_row:
        settings_row = SyncSettings()
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    return _sync_settings_payload(db, settings_row)


@router.put("/sync/settings", response_model=SyncSettingsOut)
async def update_sync_settings(
    payload: SyncSettingsIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncSettings:
    del current_user
    settings_row = db.scalar(select(SyncSettings))
    previous_live_tab_enabled = (
        bool(settings_row.live_tab_enabled)
        if settings_row and settings_row.live_tab_enabled is not None
        else True
    )
    previous_monitored_ids = monitored_live_channel_ids(db)
    previous_api_key = _active_youtube_api_key(db)
    previous_subtitle_generation_enabled = bool(settings_row.subtitle_generation_enabled) if settings_row else False
    if not settings_row:
        settings_row = SyncSettings()
        db.add(settings_row)
    settings_row.automatic_detection_enabled = payload.automatic_detection_enabled
    settings_row.automatic_sync_enabled = payload.automatic_sync_enabled
    settings_row.live_tab_enabled = payload.live_tab_enabled
    settings_row.subtitle_generation_enabled = payload.subtitle_generation_enabled
    settings_row.scan_interval_seconds = max(5, min(payload.scan_interval_seconds, 3600))
    settings_row.allow_fallback_art = payload.allow_fallback_art
    settings_row.prefer_high_res_banners = payload.prefer_high_res_banners
    settings_row.comment_limit = payload.comment_limit
    settings_row.max_replies_per_comment = max(0, int(payload.max_replies_per_comment))
    settings_row.requests_per_second = max(1, min(payload.requests_per_second, 10))
    if payload.clear_youtube_api_key:
        settings_row.youtube_api_key = None
    elif payload.youtube_api_key and payload.youtube_api_key.strip():
        settings_row.youtube_api_key = payload.youtube_api_key.strip()
    requested_channel_ids = sorted(
        {
            int(channel_id)
            for channel_id in payload.live_monitored_channel_ids
            if isinstance(channel_id, int) and channel_id > 0
        }
    )
    db.query(LiveMonitoredChannel).delete(synchronize_session=False)
    db.flush()
    if requested_channel_ids:
        valid_channel_ids = db.scalars(
            select(Channel.id).where(
                Channel.id.in_(requested_channel_ids),
                Channel.slug != "unknown-channel",
            )
        ).all()
        for channel_id in sorted({int(channel_id) for channel_id in valid_channel_ids if channel_id}):
            db.add(LiveMonitoredChannel(channel_id=channel_id))
    normalize_youtube_api_quota(settings_row)
    db.commit()
    db.refresh(settings_row)
    if (
        not previous_subtitle_generation_enabled
        and settings_row.subtitle_generation_enabled
    ):
        create_subtitle_backfill_job(db)
    current_monitored_ids = monitored_live_channel_ids(db)
    current_api_key = _active_youtube_api_key(db)
    live_config_changed = (
        previous_live_tab_enabled != bool(payload.live_tab_enabled)
        or previous_monitored_ids != current_monitored_ids
        or previous_api_key != current_api_key
    )
    if payload.live_tab_enabled and current_api_key and live_config_changed:
        try:
            await refresh_live_streams(
                db,
                current_api_key,
                settings_row.requests_per_second,
            )
            db.refresh(settings_row)
        except Exception as exc:
            logger.exception("Live refresh after sync settings save failed: %s", exc)
    return _sync_settings_payload(db, settings_row)


@router.post("/sync/subtitles", response_model=JobOut)
def sync_subtitles(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return create_subtitle_backfill_job(db)


@router.get("/retention/settings")
def get_retention_settings_route(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    return _retention_overview(db)


@router.put("/retention/settings")
def update_retention_settings_route(
    payload: RetentionSettingsIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    settings_row = get_or_create_retention_settings(db)
    try:
        validated_staging_folder = validate_retention_staging_folder_path(payload.staging_folder_path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    settings_row.enabled = payload.enabled
    settings_row.retention_days = max(1, min(payload.retention_days, 3650))
    settings_row.staging_folder_path = str(validated_staging_folder) if validated_staging_folder else None
    settings_row.auto_schedule_kind = payload.auto_schedule_kind.strip().lower() if payload.auto_schedule_kind else "interval"
    settings_row.auto_interval_minutes = payload.auto_interval_minutes
    settings_row.auto_time_hour = payload.auto_time_hour
    settings_row.auto_time_minute = payload.auto_time_minute
    settings_row.auto_weekday = payload.auto_weekday
    settings_row.auto_timezone = normalize_timezone_name(payload.auto_timezone) or server_timezone_name()
    if settings_row.auto_schedule_kind not in {"interval", "daily", "weekly"}:
        raise HTTPException(status_code=400, detail="Unsupported retention frequency")
    if settings_row.auto_schedule_kind == "interval":
        settings_row.auto_interval_minutes = max(5, min(payload.auto_interval_minutes, 10080))
    settings_row.auto_time_hour = max(0, min(payload.auto_time_hour, 23))
    settings_row.auto_time_minute = max(0, min(payload.auto_time_minute, 59))
    settings_row.auto_weekday = max(0, min(payload.auto_weekday, 6))
    db.commit()
    db.refresh(settings_row)
    return _retention_overview(db, settings_row)


@router.post("/retention/run")
def run_retention_route(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    try:
        result = run_retention_cycle(db, trigger="manual", force=True)
    except Exception as error:
        db.rollback()
        result = record_retention_failure(db, trigger="manual", message=str(error))
        raise HTTPException(status_code=500, detail=result["message"])
    return {
        "result": result,
        **_retention_overview(db),
    }


@router.post("/retention/revert")
def revert_retention_route(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    try:
        result = revert_last_retention_run(db)
    except Exception as error:
        db.rollback()
        result = record_retention_failure(db, trigger="manual-revert", message=str(error))
        raise HTTPException(status_code=500, detail=result["message"])
    return {
        "result": result,
        **_retention_overview(db),
    }


@router.post("/retention/delete")
def delete_retention_route(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    try:
        result = delete_pending_retention_items(db)
    except Exception as error:
        db.rollback()
        result = record_retention_failure(db, trigger="manual-delete", message=str(error))
        raise HTTPException(status_code=500, detail=result["message"])
    return {
        "result": result,
        **_retention_overview(db),
    }


@router.get("/retention/folders")
def browse_retention_folders_route(
    path: str = "",
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del db, current_user
    try:
        return browse_retention_folders(path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/retention/folders")
def create_retention_folder_route(
    payload: RetentionFolderCreateIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del db, current_user
    try:
        created_path = create_retention_folder(payload.parent_path, payload.name)
        return {
            "created_path": str(created_path),
            "browser": browse_retention_folders(str(created_path)),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/retention/lookup")
def retention_lookup_route(
    q: str,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    if len(q.strip()) < 2:
        return {"channels": [], "series": [], "videos": []}
    return retention_lookup(db, q)


@router.post("/retention/exclusions")
def create_retention_exclusion(
    payload: RetentionExclusionIn,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    target_type = payload.target_type.strip().lower()
    if target_type not in {"video", "series", "channel"}:
        raise HTTPException(status_code=400, detail="Unsupported retention exclusion type")
    existing = db.scalar(
        select(RetentionExclusion).where(
            RetentionExclusion.target_type == target_type,
            RetentionExclusion.target_id == payload.target_id,
        )
    )
    if existing:
        return _retention_exclusion_out(db, existing)
    exclusion = RetentionExclusion(target_type=target_type, target_id=payload.target_id)
    db.add(exclusion)
    db.commit()
    db.refresh(exclusion)
    return _retention_exclusion_out(db, exclusion)


@router.delete("/retention/exclusions/{exclusion_id}")
def delete_retention_exclusion(
    exclusion_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> dict:
    del current_user
    exclusion = db.get(RetentionExclusion, exclusion_id)
    if not exclusion:
        raise HTTPException(status_code=404, detail="Retention exclusion not found")
    db.delete(exclusion)
    db.commit()
    return {"ok": True}


async def _run_sync(
    scope: str,
    target_id: int | None,
    db: Session,
    *,
    force: bool = False,
    prefer_high_res_banners_override: bool | None = None,
) -> SyncJob:
    api_key = _active_youtube_api_key(db)
    return await sync_scope(
        db,
        scope=scope,
        target_id=target_id,
        api_key=api_key,
        force=force,
        prefer_high_res_banners_override=prefer_high_res_banners_override,
    )


@router.post("/sync/library", response_model=JobOut)
async def sync_library(
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return await _run_sync("library", None, db, force=force)


@router.post("/sync/orphans", response_model=JobOut)
async def sync_orphans(
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return await _run_sync("orphans", None, db, force=force)


@router.post("/sync/channel/{channel_id}", response_model=JobOut)
async def sync_channel(
    channel_id: int,
    force: bool = Query(default=False),
    high_res_banner: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return await _run_sync("channel", channel_id, db, force=force, prefer_high_res_banners_override=high_res_banner or None)


@router.post("/sync/series/{series_id}", response_model=JobOut)
async def sync_series(
    series_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return await _run_sync("series", series_id, db)


@router.post("/sync/video/{video_id}", response_model=JobOut)
async def sync_video_scope(
    video_id: int,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_configured_admin_user),
) -> SyncJob:
    del current_user
    return await _run_sync("video", video_id, db, force=force)


