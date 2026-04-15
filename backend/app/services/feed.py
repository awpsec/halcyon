from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.entities import QueueItem, Subscription, Video, WatchProgress, YouTubeChannelSnapshot, YouTubeCommentSnapshot, YouTubeMatch, YouTubeVideoSnapshot
from app.schemas.common import FeedCard, FeedSection, VideoSummary
from app.services.overrides import apply_video_override
from app.services.utils import parse_episode_number

TRUSTED_PUBLISHED_AT_SOURCES = {"youtube-api", "watch-page"}


def _authoritative_youtube_match(video: Video) -> YouTubeMatch | None:
    match = video.youtube_match
    if match and match.status == "matched":
        return match
    return None


def _watch_ref(video: Video) -> str:
    match = _authoritative_youtube_match(video)
    if match and match.youtube_video_id:
        return match.youtube_video_id
    return str(video.id)


def _thumbnail_url(video: Video) -> str:
    version_source = video.updated_at or video.created_at or datetime.utcnow()
    return f"/api/videos/{video.id}/thumbnail?v={int(version_source.timestamp())}"


def _video_feed_query():
    return select(Video).options(joinedload(Video.channel), joinedload(Video.series), joinedload(Video.youtube_match)).where(Video.is_available.is_(True))


def _stable_suggested_jitter(user_id: int, video_id: int) -> float:
    digest = hashlib.sha1(f"suggested:{user_id}:{video_id}".encode("utf-8")).hexdigest()
    return int(digest[:10], 16) / float(0xFFFFFFFFFF)


def _resolved_channel_snapshot(video: Video, db: Session) -> YouTubeChannelSnapshot | None:
    match = _authoritative_youtube_match(video)
    youtube_channel_id = match.youtube_channel_id if match and match.youtube_channel_id else None
    if not youtube_channel_id and video.channel_id:
        youtube_channel_ids = [
            item
            for item in db.scalars(
                select(YouTubeMatch.youtube_channel_id)
                .join(Video, Video.id == YouTubeMatch.video_id)
                .where(
                    Video.channel_id == video.channel_id,
                    YouTubeMatch.status == "matched",
                    YouTubeMatch.youtube_channel_id.is_not(None),
                )
                .distinct()
                .limit(2)
            ).all()
            if item
        ]
        if len(youtube_channel_ids) == 1:
            youtube_channel_id = youtube_channel_ids[0]
    if not youtube_channel_id:
        return None
    return db.scalar(select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == youtube_channel_id))


def _channel_display(video: Video, db: Session) -> tuple[str | None, str | None]:
    channel_name = video.channel.name if video.channel else None
    channel_avatar_url = video.channel.avatar_url if video.channel else None
    channel_snapshot = _resolved_channel_snapshot(video, db)
    if channel_snapshot:
        channel_name = channel_snapshot.title or channel_name
        channel_avatar_url = channel_snapshot.avatar_url or channel_avatar_url
    if channel_avatar_url and video.channel_id:
        fingerprint = hashlib.sha1(channel_avatar_url.encode("utf-8")).hexdigest()[:12]
        channel_avatar_url = f"/api/channels/{video.channel_id}/avatar-image?v={fingerprint}"
    return channel_name, channel_avatar_url


def _visible_progress_seconds(progress: WatchProgress | None) -> int:
    if not progress or progress.completed:
        return 0
    return max(0, progress.position_seconds or 0)


def _trusted_snapshot_published_at(snapshot: YouTubeVideoSnapshot | None) -> datetime | None:
    if not snapshot or not snapshot.published_at:
        return None
    if snapshot.published_at_source not in TRUSTED_PUBLISHED_AT_SOURCES:
        return None
    return snapshot.published_at


def _trusted_published_at_by_video_id(videos: list[Video], db: Session) -> dict[int, datetime]:
    youtube_video_ids_by_video_id = {
        video.id: match.youtube_video_id
        for video in videos
        if (match := _authoritative_youtube_match(video)) and match.youtube_video_id
    }
    youtube_video_ids = list({youtube_video_id for youtube_video_id in youtube_video_ids_by_video_id.values() if youtube_video_id})
    if not youtube_video_ids:
        return {}
    snapshots = db.execute(
        select(
            YouTubeVideoSnapshot.youtube_video_id,
            YouTubeVideoSnapshot.published_at,
            YouTubeVideoSnapshot.published_at_source,
        ).where(YouTubeVideoSnapshot.youtube_video_id.in_(youtube_video_ids))
    ).all()
    trusted_by_youtube_id = {
        youtube_video_id: published_at
        for youtube_video_id, published_at, published_at_source in snapshots
        if published_at and published_at_source in TRUSTED_PUBLISHED_AT_SOURCES
    }
    return {
        video_id: trusted_by_youtube_id[youtube_video_id]
        for video_id, youtube_video_id in youtube_video_ids_by_video_id.items()
        if youtube_video_id in trusted_by_youtube_id
    }


def _video_recency_timestamp(video: Video, trusted_published_at_by_video_id: dict[int, datetime]) -> datetime:
    return trusted_published_at_by_video_id.get(video.id) or video.published_at or video.created_at or datetime.min


def _video_to_card(video: Video, progress_by_video: dict[int, WatchProgress], reason: str, db: Session) -> FeedCard:
    video = apply_video_override(db, video)
    match = _authoritative_youtube_match(video)
    progress = progress_by_video.get(video.id)
    comment_count = None
    like_count = None
    dislike_count = None
    rating = None
    view_count = None
    published_at = None if match and match.youtube_video_id else video.published_at
    channel_name, channel_avatar_url = _channel_display(video, db)
    if match and match.youtube_video_id:
        snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == match.youtube_video_id))
        if snapshot:
            like_count = snapshot.like_count
            dislike_count = snapshot.dislike_count
            rating = snapshot.rating
            view_count = snapshot.view_count
            published_at = _trusted_snapshot_published_at(snapshot) or published_at
            comment_count = db.scalar(
                select(func.count(YouTubeCommentSnapshot.id)).where(YouTubeCommentSnapshot.youtube_video_id == snapshot.youtube_video_id)
            ) or None
    return FeedCard(
        id=video.id,
        watch_ref=_watch_ref(video),
        title=video.title,
        channel=channel_name,
        channel_slug=video.channel.slug if video.channel else None,
        series=video.series.name if video.series else None,
        channel_id=video.channel_id,
        series_id=video.series_id,
        channel_avatar_url=channel_avatar_url,
        duration_seconds=video.duration_seconds,
        thumbnail_url=_thumbnail_url(video),
        watched=bool(progress and progress.completed),
        progress_seconds=_visible_progress_seconds(progress),
        reason=reason,
        published_at=published_at,
        youtube_view_count=view_count,
        youtube_like_count=like_count,
        youtube_dislike_count=dislike_count,
        youtube_rating=rating,
        youtube_comment_count=comment_count,
    )


def build_home_feed(db: Session, user_id: int) -> list[FeedSection]:
    progress_entries = db.scalars(select(WatchProgress).where(WatchProgress.user_id == user_id)).all()
    progress_by_video = {item.video_id: item for item in progress_entries}
    watched_video_ids = {item.video_id for item in progress_entries if item.completed}
    continue_cutoff = datetime.utcnow() - timedelta(days=30)
    queue_video_ids = db.scalars(select(QueueItem.video_id).where(QueueItem.user_id == user_id).order_by(QueueItem.position)).all()

    all_videos = db.scalars(_video_feed_query()).unique().all()
    trusted_published_at_by_video_id = _trusted_published_at_by_video_id(all_videos, db)
    in_progress = [
        video
        for video in all_videos
        if progress_by_video.get(video.id)
        and not progress_by_video[video.id].completed
        and progress_by_video[video.id].updated_at
        and progress_by_video[video.id].updated_at >= continue_cutoff
    ]
    queue = [video for video in all_videos if video.id in queue_video_ids]
    random_pool = [video for video in all_videos if not progress_by_video.get(video.id)]
    recently_added = sorted(
        [video for video in all_videos if video.id not in watched_video_ids],
        key=lambda video: _video_recency_timestamp(video, trusted_published_at_by_video_id),
        reverse=True,
    )
    in_progress_ids = {video.id for video in in_progress}
    queue_ids = set(queue_video_ids)

    def episode_value(video: Video) -> int | None:
        return video.episode_number or parse_episode_number(video.title or "")

    def series_sort_key(video: Video) -> tuple[int, int, datetime, int]:
        episode = episode_value(video)
        return (
            0 if episode is not None else 1,
            episode if episode is not None else 10**9,
            video.published_at or video.created_at or datetime.min,
            video.id,
        )

    series_groups: dict[int, list[Video]] = {}
    for video in all_videos:
        if not video.series_id:
            continue
        series_groups.setdefault(video.series_id, []).append(video)

    next_in_series_candidates: list[tuple[datetime, Video]] = []
    for series_videos in series_groups.values():
        ordered_series = sorted(series_videos, key=series_sort_key)
        if any(video.id in in_progress_ids for video in ordered_series):
            continue

        completed_videos = [
            video
            for video in ordered_series
            if progress_by_video.get(video.id) and progress_by_video[video.id].completed
        ]
        if not completed_videos:
            continue

        candidate = next(
            (video for video in ordered_series if not (progress_by_video.get(video.id) and progress_by_video[video.id].completed)),
            None,
        )
        if candidate is None:
            continue

        if candidate.id in queue_ids:
            continue
        last_completed_video = max(
            completed_videos,
            key=lambda video: (
                progress_by_video[video.id].updated_at or video.published_at or video.created_at or datetime.min,
                video.id,
            ),
        )
        last_completed_progress = progress_by_video.get(last_completed_video.id)
        activity_at = (
            last_completed_progress.updated_at
            if last_completed_progress and last_completed_progress.updated_at
            else last_completed_video.published_at or last_completed_video.created_at or datetime.min
        )
        next_in_series_candidates.append((activity_at, candidate))

    next_in_series = [
        video
        for _, video in sorted(
            next_in_series_candidates,
            key=lambda item: (item[0], item[1].published_at or item[1].created_at or datetime.min, item[1].id),
            reverse=True,
        )
    ]
    next_in_series_ids = {video.id for video in next_in_series}

    suggested_videos = sorted(
        [video for video in random_pool if video.id not in queue_ids],
        key=lambda video: (_stable_suggested_jitter(user_id, video.id), video.id),
    )
    suggested_with_series: list[Video] = []
    seen_suggested_ids: set[int] = set()
    for video in [*next_in_series, *suggested_videos]:
        if video.id in seen_suggested_ids:
            continue
        seen_suggested_ids.add(video.id)
        suggested_with_series.append(video)
    longform_videos = sorted(
        [video for video in all_videos if video.duration_seconds >= 3600 and video.id not in watched_video_ids],
        key=lambda video: video.published_at or video.created_at or datetime.min,
        reverse=True,
    )

    sections = [
        FeedSection(key="recent", title="Recently Added", items=[_video_to_card(video, progress_by_video, "recently-added", db) for video in recently_added[:48]]),
        FeedSection(key="continue", title="Continue Watching", items=[_video_to_card(video, progress_by_video, "continue-watching", db) for video in in_progress[:10]]),
        FeedSection(key="queue", title="Queue", items=[_video_to_card(video, progress_by_video, "queued", db) for video in queue[:10]]),
        FeedSection(key="longform", title="Long-form", items=[_video_to_card(video, progress_by_video, "longform", db) for video in longform_videos[:48]]),
        FeedSection(
            key="random",
            title="Suggested",
            items=[
                _video_to_card(
                    video,
                    progress_by_video,
                    "next-up" if video.id in next_in_series_ids else "suggested",
                    db,
                )
                for video in suggested_with_series[:12]
            ],
        ),
    ]
    return [section for section in sections if section.items or section.key == "queue"]


def build_suggested_feed(db: Session, user_id: int) -> tuple[list[Video], dict[int, WatchProgress]]:
    progress_entries = db.scalars(select(WatchProgress).where(WatchProgress.user_id == user_id)).all()
    progress_by_video = {item.video_id: item for item in progress_entries}
    queue_video_ids = set(
        db.scalars(select(QueueItem.video_id).where(QueueItem.user_id == user_id).order_by(QueueItem.position)).all()
    )
    all_videos = db.scalars(_video_feed_query()).unique().all()
    suggested_videos = sorted(
        [
            video
            for video in all_videos
            if video.id not in progress_by_video and video.id not in queue_video_ids
        ],
        key=lambda video: (_stable_suggested_jitter(user_id, video.id), video.id),
    )
    return suggested_videos, progress_by_video


def summarize_video(video: Video, progress: WatchProgress | None = None, db: Session | None = None) -> VideoSummary:
    overridden = apply_video_override(db, video) if db else video
    comment_count = None
    like_count = None
    dislike_count = None
    rating = None
    view_count = None
    published_at = None if overridden.youtube_match and overridden.youtube_match.youtube_video_id else overridden.published_at
    description = overridden.description
    channel_name = overridden.channel.name if overridden.channel else None
    channel_avatar_url = overridden.channel.avatar_url if overridden.channel else None
    if db and overridden.youtube_match and overridden.youtube_match.youtube_video_id:
        snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == overridden.youtube_match.youtube_video_id))
        if snapshot:
            like_count = snapshot.like_count
            dislike_count = snapshot.dislike_count
            rating = snapshot.rating
            view_count = snapshot.view_count
            published_at = _trusted_snapshot_published_at(snapshot) or published_at
            description = snapshot.description or description
            comment_count = db.scalar(
                select(func.count(YouTubeCommentSnapshot.id)).where(YouTubeCommentSnapshot.youtube_video_id == snapshot.youtube_video_id)
            ) or None
    if db:
        channel_name, channel_avatar_url = _channel_display(overridden, db)
    return VideoSummary(
        id=overridden.id,
        watch_ref=_watch_ref(overridden),
        title=overridden.title,
        channel_id=overridden.channel_id,
        channel_name=channel_name,
        channel_slug=overridden.channel.slug if overridden.channel else None,
        channel_avatar_url=channel_avatar_url,
        series_id=overridden.series_id,
        series_name=overridden.series.name if overridden.series else None,
        episode_number=overridden.episode_number,
        duration_seconds=overridden.duration_seconds,
        description=description,
        created_at=overridden.created_at,
        published_at=published_at,
        thumbnail_url=_thumbnail_url(overridden),
        watched=bool(progress and progress.completed),
        progress_seconds=_visible_progress_seconds(progress),
        youtube_view_count=view_count,
        youtube_like_count=like_count,
        youtube_dislike_count=dislike_count,
        youtube_rating=rating,
        youtube_comment_count=comment_count,
        youtube_match_status=overridden.youtube_match.status if overridden.youtube_match else None,
        youtube_match_confidence=overridden.youtube_match.confidence if overridden.youtube_match else None,
        user_reaction=None,
    )
