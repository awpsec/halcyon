from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserProfileOut(OrmModel):
    id: int
    name: str
    display_name: str
    accent_color: str
    avatar_url: str | None = None
    is_admin: bool = False
    has_pin: bool = False


class SessionUserOut(UserProfileOut):
    is_admin: bool = False
    requires_admin_setup: bool = False
    admin_setup_recovery_phrase: list[str] | None = None


class SessionOut(BaseModel):
    user: SessionUserOut
    session_token: str


class AuthBootstrapOut(BaseModel):
    admin_username: str = "admin"
    admin_setup_required: bool = False
    allow_registration: bool = True


class UpdateStatusOut(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool = False
    repository_url: str | None = None
    update_command: str = "halcyon update"
    checked_at: datetime | None = None
    error: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    pin: str
    display_name: str | None = None
    avatar_url: str | None = None


class ProfileUpdateIn(BaseModel):
    display_name: str
    avatar_url: str | None = None


class SwitchSessionIn(BaseModel):
    session_token: str


class AdminSetupIn(BaseModel):
    password: str


class AdminPasswordChangeIn(BaseModel):
    current_password: str
    password: str


class UserPasswordChangeIn(BaseModel):
    current_password: str
    password: str


class AdminRecoveryIn(BaseModel):
    recovery_phrase: str
    password: str


class UserPasswordResetByPinIn(BaseModel):
    username: str
    pin: str
    password: str


class AdminUserPermissionIn(BaseModel):
    is_admin: bool


class UserPinSetIn(BaseModel):
    pin: str


class LibraryRootOut(OrmModel):
    id: int
    label: str
    path: str
    is_available: bool
    selected_count: int = 0
    item_count: int = 0


class SelectedFolderIn(BaseModel):
    root_id: int
    relative_path: str


class SelectedFolderOut(OrmModel):
    id: int
    root_id: int
    relative_path: str
    is_enabled: bool


class FeedCard(BaseModel):
    id: int
    watch_ref: str
    title: str
    channel: str | None
    channel_slug: str | None = None
    series: str | None
    channel_id: int | None = None
    series_id: int | None = None
    channel_avatar_url: str | None = None
    duration_seconds: int
    thumbnail_url: str | None
    watched: bool
    progress_seconds: int
    reason: str
    published_at: datetime | None = None
    youtube_view_count: int | None = None
    youtube_like_count: int | None = None
    youtube_dislike_count: int | None = None
    youtube_rating: float | None = None
    youtube_comment_count: int | None = None


class FeedSection(BaseModel):
    key: str
    title: str
    items: list[FeedCard]


class VideoSummary(BaseModel):
    id: int
    watch_ref: str
    title: str
    channel_id: int | None
    channel_name: str | None
    channel_slug: str | None = None
    channel_avatar_url: str | None = None
    series_id: int | None
    series_name: str | None
    episode_number: int | None
    duration_seconds: int
    description: str | None
    created_at: datetime | None = None
    published_at: datetime | None
    thumbnail_url: str | None
    watched: bool = False
    progress_seconds: int = 0
    youtube_view_count: int | None = None
    youtube_like_count: int | None = None
    youtube_dislike_count: int | None = None
    youtube_rating: float | None = None
    youtube_comment_count: int | None = None
    youtube_match_status: str | None = None
    youtube_match_confidence: float | None = None
    user_reaction: str | None = None
    user_saved: bool = False


class ChannelOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    avatar_url: str | None
    banner_url: str | None
    video_count: int
    subscribed: bool = False
    subscriber_count: int | None = None
    view_count: int | None = None
    youtube_video_count: int | None = None
    youtube_fetched_at: datetime | None = None
    joined_at: datetime | None = None
    canonical_url: str | None = None
    links: list[dict] = []
    has_new_video: bool = False


class SeriesOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    video_count: int
    all_videos_saved: bool = False
    saved_video_count: int = 0
    preview_thumbnails: list[str] = []


class PlaylistOut(BaseModel):
    id: int
    name: str
    description: str | None
    item_count: int
    preview_thumbnails: list[str] = []
    all_videos_saved: bool = False
    saved_video_count: int = 0


class PlaylistCreateIn(BaseModel):
    user_id: int
    name: str
    description: str | None = None


class QueueItemIn(BaseModel):
    video_id: int


class QueueBulkIn(BaseModel):
    video_ids: list[int]
    reset: bool = True


class CollectionSaveIn(BaseModel):
    saved: bool = True


class ProgressIn(BaseModel):
    user_id: int
    position_seconds: int
    completed: bool = False


class ReactionIn(BaseModel):
    reaction: str | None = None


class WatchStateIn(BaseModel):
    state: str


class MetadataOverrideIn(BaseModel):
    target_type: str
    target_id: int
    payload: dict


class SyncSettingsIn(BaseModel):
    automatic_detection_enabled: bool
    automatic_sync_enabled: bool
    live_tab_enabled: bool = True
    subtitle_generation_enabled: bool = False
    live_monitored_channel_ids: list[int] = Field(default_factory=list)
    scan_interval_seconds: int = 30
    allow_fallback_art: bool = False
    prefer_high_res_banners: bool = False
    comment_limit: int
    max_replies_per_comment: int = 3
    requests_per_second: int = 3
    youtube_api_key: str | None = None
    clear_youtube_api_key: bool = False


class SyncSettingsOut(OrmModel):
    id: int
    automatic_detection_enabled: bool
    automatic_sync_enabled: bool
    live_tab_enabled: bool = True
    subtitle_generation_enabled: bool = False
    live_monitored_channel_ids: list[int] = Field(default_factory=list)
    scan_interval_seconds: int
    allow_fallback_art: bool
    prefer_high_res_banners: bool
    comment_limit: int
    max_replies_per_comment: int = 3
    requests_per_second: int
    last_library_sync_at: datetime | None
    last_live_sync_at: datetime | None = None
    last_subtitle_sync_at: datetime | None = None
    youtube_api_key_configured: bool = False
    youtube_api_quota_daily_limit: int = 10_000
    youtube_api_quota_used_units: int = 0
    youtube_api_quota_remaining_units: int = 10_000
    youtube_api_quota_remaining_percent: float = 100.0
    youtube_api_quota_estimated: bool = True


class RetentionSettingsIn(BaseModel):
    enabled: bool
    retention_days: int = 30
    staging_folder_path: str | None = None
    auto_schedule_kind: str = "interval"
    auto_interval_minutes: int = 15
    auto_time_hour: int = 4
    auto_time_minute: int = 0
    auto_weekday: int = 0
    auto_timezone: str | None = None


class RetentionSettingsOut(OrmModel):
    id: int
    enabled: bool
    retention_days: int
    staging_folder_path: str | None = None
    auto_schedule_kind: str
    auto_interval_minutes: int
    auto_time_hour: int
    auto_time_minute: int
    auto_weekday: int
    auto_timezone: str
    last_auto_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_trigger: str | None = None
    last_run_status: str | None = None
    last_run_message: str | None = None
    last_run_marked_count: int
    last_run_deleted_count: int
    last_run_reverted_count: int
    last_run_token: str | None = None


class RetentionExclusionIn(BaseModel):
    target_type: str
    target_id: int


class RetentionExclusionOut(OrmModel):
    id: int
    target_type: str
    target_id: int
    created_at: datetime
    updated_at: datetime


class RetentionPendingItemOut(BaseModel):
    id: int
    video_id: int
    video_title: str
    channel_name: str | None = None
    thumbnail_url: str | None = None
    marked_at: datetime
    delete_after_at: datetime
    run_token: str | None = None


class RetentionRunOut(OrmModel):
    id: int
    trigger: str
    status: str
    message: str | None = None
    details: dict = Field(default_factory=dict)
    marked_count: int
    deleted_count: int
    reverted_count: int
    run_token: str | None = None
    created_at: datetime
    updated_at: datetime


class RetentionStatsOut(BaseModel):
    reclaimed_bytes: int


class RetentionLookupItem(BaseModel):
    id: int
    label: str
    subtitle: str | None = None
    target_type: str


class RetentionFolderCreateIn(BaseModel):
    parent_path: str
    name: str


class LiveStreamOut(BaseModel):
    youtube_video_id: str
    youtube_channel_id: str
    title: str
    description: str | None = None
    thumbnail_url: str | None = None
    channel_id: int | None = None
    channel_name: str | None = None
    channel_slug: str | None = None
    channel_avatar_url: str | None = None
    channel_banner_url: str | None = None
    scheduled_start_at: datetime | None = None
    actual_start_at: datetime | None = None
    concurrent_viewers: int | None = None
    is_live: bool = True
    last_seen_at: datetime
    fetched_at: datetime
    watch_url: str
    embed_url: str
    chat_enabled: bool = True


class LiveOverviewOut(BaseModel):
    enabled: bool = True
    api_key_configured: bool = False
    last_live_sync_at: datetime | None = None
    items: list[LiveStreamOut] = Field(default_factory=list)


class JobOut(OrmModel):
    id: int
    scope: str
    status: str
    details: dict
    started_at: datetime | None
    finished_at: datetime | None
