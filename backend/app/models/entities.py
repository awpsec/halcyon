from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def utc_now() -> datetime:
    return datetime.utcnow()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class UserProfile(TimestampMixin, Base):
    __tablename__ = "user_profiles"

    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    accent_color: Mapped[str] = mapped_column(String(32), default="#f97316")
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    pin_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_admin_setup: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_phrase_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    recovery_phrase_pending: Mapped[str | None] = mapped_column(String(255), default=None)
    last_subscription_seen_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    sessions: Mapped[list["SessionToken"]] = relationship(back_populates="user")


class SessionToken(TimestampMixin, Base):
    __tablename__ = "session_tokens"

    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped["UserProfile"] = relationship(back_populates="sessions")


class LibraryRoot(TimestampMixin, Base):
    __tablename__ = "library_roots"

    label: Mapped[str] = mapped_column(String(120), unique=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

    selected_folders: Mapped[list["SelectedFolder"]] = relationship(back_populates="root")


class SelectedFolder(TimestampMixin, Base):
    __tablename__ = "selected_folders"
    __table_args__ = (UniqueConstraint("root_id", "relative_path", name="uq_selected_folder"),)

    root_id: Mapped[int] = mapped_column(ForeignKey("library_roots.id"), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    root: Mapped["LibraryRoot"] = relationship(back_populates="selected_folders")


class Channel(TimestampMixin, Base):
    __tablename__ = "channels"

    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    inferred_from_path: Mapped[bool] = mapped_column(Boolean, default=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    banner_url: Mapped[str | None] = mapped_column(String(1024), default=None)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel")


class Series(TimestampMixin, Base):
    __tablename__ = "series"

    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    videos: Mapped[list["Video"]] = relationship(back_populates="series")


class Video(TimestampMixin, Base):
    __tablename__ = "videos"

    title: Mapped[str] = mapped_column(String(512), index=True)
    slug: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), default=None, index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None, index=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    metadata_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

    channel: Mapped["Channel | None"] = relationship(back_populates="videos")
    series: Mapped["Series | None"] = relationship(back_populates="videos")
    files: Mapped[list["VideoFile"]] = relationship(back_populates="video")
    watch_progress: Mapped[list["WatchProgress"]] = relationship(back_populates="video")
    youtube_match: Mapped["YouTubeMatch | None"] = relationship(back_populates="video", uselist=False)


class VideoFile(TimestampMixin, Base):
    __tablename__ = "video_files"

    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    absolute_path: Mapped[str] = mapped_column(String(2048), unique=True)
    relative_path: Mapped[str] = mapped_column(String(2048), index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    codec_summary: Mapped[str | None] = mapped_column(String(255), default=None)
    resolution: Mapped[str | None] = mapped_column(String(64), default=None)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    video: Mapped["Video"] = relationship(back_populates="files")


class WatchProgress(TimestampMixin, Base):
    __tablename__ = "watch_progress"
    __table_args__ = (UniqueConstraint("user_id", "video_id", name="uq_watch_progress"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    position_seconds: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)

    video: Mapped["Video"] = relationship(back_populates="watch_progress")


class WatchHistory(TimestampMixin, Base):
    __tablename__ = "watch_history"

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    watched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    position_seconds: Mapped[int] = mapped_column(Integer, default=0)


class VideoReaction(TimestampMixin, Base):
    __tablename__ = "video_reactions"
    __table_args__ = (UniqueConstraint("user_id", "video_id", name="uq_video_reaction"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    reaction: Mapped[str] = mapped_column(String(16), default="like")


class SavedVideo(TimestampMixin, Base):
    __tablename__ = "saved_videos"
    __table_args__ = (UniqueConstraint("user_id", "video_id", name="uq_saved_video"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_subscription"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)


class Playlist(TimestampMixin, Base):
    __tablename__ = "playlists"

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)


class PlaylistItem(TimestampMixin, Base):
    __tablename__ = "playlist_items"
    __table_args__ = (UniqueConstraint("playlist_id", "position", name="uq_playlist_position"),)

    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, index=True)


class QueueItem(TimestampMixin, Base):
    __tablename__ = "queue_items"
    __table_args__ = (UniqueConstraint("user_id", "position", name="uq_queue_position"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, index=True)


class MetadataOverride(TimestampMixin, Base):
    __tablename__ = "metadata_overrides"

    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ScanJob(TimestampMixin, Base):
    __tablename__ = "scan_jobs"

    scope: Mapped[str] = mapped_column(String(64), default="library")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class TranscodeJob(TimestampMixin, Base):
    __tablename__ = "transcode_jobs"

    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    profile: Mapped[str] = mapped_column(String(64), default="hls-default")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    output_path: Mapped[str | None] = mapped_column(String(2048), default=None)
    pid: Mapped[int | None] = mapped_column(Integer, default=None)


class YouTubeMatch(TimestampMixin, Base):
    __tablename__ = "youtube_matches"
    __table_args__ = (UniqueConstraint("video_id", name="uq_video_match"),)

    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="unmatched")
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)

    video: Mapped["Video"] = relationship(back_populates="youtube_match")


class YouTubeChannelSnapshot(TimestampMixin, Base):
    __tablename__ = "youtube_channel_snapshots"

    youtube_channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    banner_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    canonical_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    links: Mapped[list[dict]] = mapped_column(JSON, default=list)
    subscriber_count: Mapped[int | None] = mapped_column(Integer, default=None)
    video_count: Mapped[int | None] = mapped_column(Integer, default=None)
    view_count: Mapped[int | None] = mapped_column(Integer, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class YouTubeVideoSnapshot(TimestampMixin, Base):
    __tablename__ = "youtube_video_snapshots"

    youtube_video_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    published_at_source: Mapped[str | None] = mapped_column(String(32), default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    category_id: Mapped[str | None] = mapped_column(String(32), default=None)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    view_count: Mapped[int | None] = mapped_column(Integer, default=None)
    like_count: Mapped[int | None] = mapped_column(Integer, default=None)
    dislike_count: Mapped[int | None] = mapped_column(Integer, default=None)
    rating: Mapped[float | None] = mapped_column(Float, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class YouTubeCommentSnapshot(TimestampMixin, Base):
    __tablename__ = "youtube_comment_snapshots"

    youtube_video_id: Mapped[str] = mapped_column(String(32), index=True)
    author_name: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)


class SyncJob(TimestampMixin, Base):
    __tablename__ = "sync_jobs"

    scope: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class SyncSettings(TimestampMixin, Base):
    __tablename__ = "sync_settings"

    automatic_detection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automatic_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    allow_fallback_art: Mapped[bool] = mapped_column(Boolean, default=False)
    prefer_high_res_banners: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_limit: Mapped[int] = mapped_column(Integer, default=100)
    requests_per_second: Mapped[int] = mapped_column(Integer, default=3)
    last_library_sync_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    youtube_api_key: Mapped[str | None] = mapped_column(String(255), default=None)


class RetentionSettings(TimestampMixin, Base):
    __tablename__ = "retention_settings"

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    staging_folder_path: Mapped[str | None] = mapped_column(String(2048), default=None)
    auto_schedule_kind: Mapped[str] = mapped_column(String(32), default="interval")
    auto_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    auto_time_hour: Mapped[int] = mapped_column(Integer, default=4)
    auto_time_minute: Mapped[int] = mapped_column(Integer, default=0)
    auto_weekday: Mapped[int] = mapped_column(Integer, default=0)
    auto_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    last_auto_run_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_run_trigger: Mapped[str | None] = mapped_column(String(32), default=None)
    last_run_status: Mapped[str | None] = mapped_column(String(32), default=None)
    last_run_message: Mapped[str | None] = mapped_column(String(1024), default=None)
    last_run_marked_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_reverted_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_token: Mapped[str | None] = mapped_column(String(64), default=None)


class RetentionExclusion(TimestampMixin, Base):
    __tablename__ = "retention_exclusions"
    __table_args__ = (UniqueConstraint("target_type", "target_id", name="uq_retention_exclusion"),)

    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)


class RetentionItem(TimestampMixin, Base):
    __tablename__ = "retention_items"

    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), default=None, index=True)
    video_file_id: Mapped[int | None] = mapped_column(ForeignKey("video_files.id"), default=None, unique=True, index=True)
    original_absolute_path: Mapped[str] = mapped_column(String(2048))
    staged_absolute_path: Mapped[str] = mapped_column(String(2048), unique=True)
    original_relative_path: Mapped[str | None] = mapped_column(String(2048), default=None)
    original_video_created_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_fingerprint: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    marked_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    delete_after_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(32), default="staged", index=True)
    run_token: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), default=None)


class RetentionRun(TimestampMixin, Base):
    __tablename__ = "retention_runs"

    trigger: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(String(1024), default=None)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    marked_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    reverted_count: Mapped[int] = mapped_column(Integer, default=0)
    run_token: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
