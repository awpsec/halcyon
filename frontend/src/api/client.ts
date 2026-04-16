export type FeedSection = {
  key: string;
  title: string;
  items: FeedCard[];
};

export type FeedCard = {
  id: number;
  watch_ref?: string;
  title: string;
  channel: string | null;
  channel_slug?: string | null;
  series: string | null;
  channel_avatar_url?: string | null;
  duration_seconds: number;
  thumbnail_url?: string | null;
  watched: boolean;
  progress_seconds: number;
  reason: string;
  channel_id?: number | null;
  series_id?: number | null;
  published_at?: string | null;
  youtube_view_count?: number | null;
  youtube_like_count?: number | null;
  youtube_dislike_count?: number | null;
  youtube_rating?: number | null;
  youtube_comment_count?: number | null;
};

export type Profile = {
  id: number;
  name: string;
  display_name: string;
  accent_color: string;
  avatar_url?: string | null;
  is_admin?: boolean;
  has_pin?: boolean;
  requires_admin_setup?: boolean;
  admin_setup_recovery_phrase?: string[] | null;
};

export type LibraryRoot = {
  id: number;
  label: string;
  path: string;
  is_available: boolean;
  selected_count: number;
  item_count: number;
};

export type SelectedFolder = {
  id: number;
  root_id: number;
  relative_path: string;
  is_enabled: boolean;
};

export type LibraryStorage = {
  library_bytes: number;
  available_bytes: number;
  total_bytes: number;
  root_count: number;
};

export type Preferences = {
  theme: "light" | "dark";
  autoplay: boolean;
  captionsEnabled: boolean;
  preferMpv: boolean;
  mousewheelVolumeControl: boolean;
  density: "relaxed" | "comfortable" | "compact";
  defaultPlayerMode: "last-used" | "default" | "theater";
};

export type SessionResponse = {
  user: Profile;
  session_token: string;
};

export type AuthBootstrapStatus = {
  admin_username: string;
  admin_setup_required: boolean;
  allow_registration: boolean;
};

export type UpdateStatus = {
  current_version: string;
  latest_version: string;
  update_available: boolean;
  repository_url?: string | null;
  update_command: string;
  checked_at?: string | null;
  error?: string | null;
};

export type VideoSummary = {
  id: number;
  watch_ref: string;
  title: string;
  reason?: string | null;
  channel_id: number | null;
  channel_name: string | null;
  channel_slug?: string | null;
  channel_avatar_url?: string | null;
  series_id: number | null;
  series_name: string | null;
  episode_number: number | null;
  duration_seconds: number;
  description: string | null;
  created_at?: string | null;
  published_at: string | null;
  thumbnail_url?: string | null;
  watched: boolean;
  progress_seconds: number;
  youtube_view_count?: number | null;
  youtube_like_count?: number | null;
  youtube_dislike_count?: number | null;
  youtube_rating?: number | null;
  youtube_comment_count?: number | null;
  youtube_match_status?: string | null;
  youtube_match_confidence?: number | null;
  user_reaction?: "like" | "dislike" | null;
  user_saved?: boolean;
};

export type ChannelSummary = {
  id: number;
  name: string;
  slug: string;
  description?: string | null;
  avatar_url?: string | null;
  banner_url?: string | null;
  video_count: number;
  subscribed?: boolean;
  subscriber_count?: number | null;
  view_count?: number | null;
  youtube_video_count?: number | null;
  youtube_fetched_at?: string | null;
  joined_at?: string | null;
  canonical_url?: string | null;
  links?: Array<{ title: string; url: string }>;
  has_new_video?: boolean;
};

export type PlaylistSummary = {
  id: number;
  name: string;
  description: string | null;
  item_count: number;
  preview_thumbnails?: string[];
  all_videos_saved?: boolean;
  saved_video_count?: number;
};

export type PlaylistDetail = {
  playlist: PlaylistSummary;
  videos: VideoSummary[];
};

export type SeriesSummary = {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  video_count: number;
  preview_thumbnails?: string[];
  all_videos_saved?: boolean;
  saved_video_count?: number;
};

export type ProfileSummary = {
  profile: Profile;
  playlists: PlaylistSummary[];
  subscriptions: ChannelSummary[];
  recently_watched: VideoSummary[];
  liked_videos: VideoSummary[];
  saved_videos: VideoSummary[];
  queue: { id: number; position: number; video: VideoSummary }[];
};

export type ProfileSavedResponse = {
  profile: Profile;
  items: VideoSummary[];
};

export type QueueResponse = {
  items: {
    id: number;
    position: number;
    video: VideoSummary;
  }[];
};

export type JobStatusItem = {
  id: number;
  scope: string;
  status: string;
  percent: number | null;
  details: Record<string, unknown>;
};

export type LogResponse = {
  lines: string[];
};

export type SyncSettings = {
  id: number;
  automatic_detection_enabled: boolean;
  automatic_sync_enabled: boolean;
  subtitle_generation_enabled: boolean;
  scan_interval_seconds: number;
  allow_fallback_art: boolean;
  prefer_high_res_banners: boolean;
  live_tab_enabled: boolean;
  live_monitored_channel_ids: number[];
  comment_limit: number;
  max_replies_per_comment: number;
  requests_per_second: number;
  last_library_sync_at: string | null;
  last_live_sync_at: string | null;
  last_subtitle_sync_at: string | null;
  youtube_api_key_configured: boolean;
  youtube_api_quota_daily_limit: number;
  youtube_api_quota_used_units: number;
  youtube_api_quota_remaining_units: number;
  youtube_api_quota_remaining_percent: number;
  youtube_api_quota_estimated: boolean;
};

export type LiveStream = {
  youtube_video_id: string;
  youtube_channel_id: string;
  title: string;
  description: string | null;
  thumbnail_url: string | null;
  channel_id: number | null;
  channel_name: string | null;
  channel_slug: string | null;
  channel_avatar_url: string | null;
  channel_banner_url: string | null;
  scheduled_start_at: string | null;
  actual_start_at: string | null;
  concurrent_viewers: number | null;
  is_live: boolean;
  last_seen_at: string;
  fetched_at: string;
  watch_url: string;
  embed_url: string;
  chat_enabled: boolean;
};

export type LiveOverview = {
  enabled: boolean;
  api_key_configured: boolean;
  last_live_sync_at: string | null;
  items: LiveStream[];
};

export type RetentionSettings = {
  id: number;
  enabled: boolean;
  retention_days: number;
  staging_folder_path?: string | null;
  auto_schedule_kind: "interval" | "daily" | "weekly";
  auto_interval_minutes: number;
  auto_time_hour: number;
  auto_time_minute: number;
  auto_weekday: number;
  auto_timezone: string;
  last_auto_run_at?: string | null;
  last_run_at?: string | null;
  last_run_trigger?: string | null;
  last_run_status?: string | null;
  last_run_message?: string | null;
  last_run_marked_count: number;
  last_run_deleted_count: number;
  last_run_reverted_count: number;
  last_run_token?: string | null;
};

export type RetentionExclusion = {
  id: number;
  target_type: "video" | "series" | "channel";
  target_id: number;
  label: string;
  subtitle?: string | null;
  image_url?: string | null;
  created_at: string;
  updated_at: string;
};

export type RetentionPendingItem = {
  id: number;
  video_id: number;
  video_title: string;
  channel_name?: string | null;
  thumbnail_url?: string | null;
  marked_at: string;
  delete_after_at: string;
  run_token?: string | null;
};

export type RetentionRun = {
  id: number;
  trigger: string;
  status: string;
  message?: string | null;
  details?: Record<string, string[]> | null;
  marked_count: number;
  deleted_count: number;
  reverted_count: number;
  run_token?: string | null;
  created_at: string;
  updated_at: string;
};

export type RetentionStats = {
  reclaimed_bytes: number;
};

export type RetentionLookupItem = {
  id: number;
  label: string;
  subtitle?: string | null;
  target_type: "video" | "series" | "channel";
};

export type RetentionFolderBrowser = {
  roots: string[];
  root_path: string;
  browse_path: string;
  input_path: string;
  prefix: string;
  parent_path?: string | null;
  create_parent_path: string;
  directories: Array<{ name: string; path: string }>;
};

export type RetentionOverview = {
  settings: RetentionSettings;
  effective_staging_folder: string;
  exclusions: RetentionExclusion[];
  pending_items: RetentionPendingItem[];
  history: RetentionRun[];
  stats: RetentionStats;
};

export type SearchResults = {
  videos: VideoSummary[];
  channels: ChannelSummary[];
};

export type ExploreResponse = {
  items: VideoSummary[];
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
};

export type WatchSuggestionsResponse = {
  mode: "suggested" | "related";
  items: VideoSummary[];
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
};

export type TranscodeItem = {
  id: number;
  video_id: number;
  title: string | null;
  profile: string;
  status: string;
  output_path: string | null;
  pid: number | null;
  throttled?: boolean;
  created_at: string;
  updated_at: string;
};

function browserTimeZone(): string | null {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone?.trim();
  return zone ? zone : null;
}

function playbackClientProfile(): "default" | "mobile" | "android" {
  if (typeof navigator === "undefined") return "default";
  const userAgent = navigator.userAgent || "";
  if (/Android/i.test(userAgent)) return "android";
  if (/webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(userAgent)) {
    return "mobile";
  }
  return "default";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timezone = browserTimeZone();
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(timezone ? { "X-Halcyon-Timezone": timezone } : {}),
      ...(init?.headers ?? {})
    },
    ...init
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed: ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  bootstrapStatus: () => request<AuthBootstrapStatus>("/api/session/bootstrap"),
  updateStatus: () => request<UpdateStatus>("/api/app/update-status"),
  profiles: () => request<Profile[]>("/api/session/profiles"),
  updateProfilePermissions: (userId: number, payload: { is_admin: boolean }) =>
    request<Profile>(`/api/session/profiles/${userId}/permissions`, { method: "PUT", body: JSON.stringify(payload) }),
  me: () => request<Profile>("/api/session/me"),
  profileSummary: (username?: string) =>
    request<ProfileSummary>(username ? `/api/profile/${encodeURIComponent(username)}/summary` : "/api/profile/summary"),
  profileSaved: (username: string) =>
    request<ProfileSavedResponse>(`/api/profile/${encodeURIComponent(username)}/saved`),
  selectProfile: (userId: number) => request<SessionResponse>(`/api/session/select/${userId}`, { method: "POST" }),
  login: (payload: { username: string; password: string }) =>
    request<SessionResponse>("/api/session/login", { method: "POST", body: JSON.stringify(payload) }),
  register: (payload: { username: string; password: string; pin: string; display_name?: string | null }) =>
    request<SessionResponse>("/api/session/register", { method: "POST", body: JSON.stringify(payload) }),
  switchSession: (sessionToken: string) =>
    request<SessionResponse>("/api/session/switch", { method: "POST", body: JSON.stringify({ session_token: sessionToken }) }),
  logout: () => request("/api/session/logout", { method: "POST" }),
  completeAdminSetup: (payload: { password: string }) =>
    request<Profile>("/api/session/admin/setup", { method: "POST", body: JSON.stringify(payload) }),
  changeAdminPassword: (payload: { current_password: string; password: string }) =>
    request<Profile>("/api/session/admin/password", { method: "POST", body: JSON.stringify(payload) }),
  resetAdminPasswordFromSettings: (payload: { recovery_phrase: string; password: string }) =>
    request<Profile>("/api/session/admin/recovery-reset", { method: "POST", body: JSON.stringify(payload) }),
  recoverAdminAccount: (payload: { recovery_phrase: string; password: string }) =>
    request<SessionResponse>("/api/session/admin/recover", { method: "POST", body: JSON.stringify(payload) }),
  changePassword: (payload: { current_password: string; password: string }) =>
    request<Profile>("/api/session/password", { method: "POST", body: JSON.stringify(payload) }),
  resetPasswordByPin: (payload: { username: string; pin: string; password: string }) =>
    request<SessionResponse>("/api/session/password/reset", { method: "POST", body: JSON.stringify(payload) }),
  setAccountPin: (payload: { pin: string }) =>
    request<Profile>("/api/session/pin", { method: "POST", body: JSON.stringify(payload) }),
  updateProfile: (payload: { display_name: string; avatar_url?: string | null }) =>
    request<Profile>("/api/profile/me", { method: "PUT", body: JSON.stringify(payload) }),
  deleteProfile: (userId: number) => request(`/api/session/profiles/${userId}`, { method: "DELETE" }),
  home: () => request<FeedSection[]>("/api/home"),
  search: (query: string) => request<SearchResults>(`/api/search?q=${encodeURIComponent(query)}`),
  libraryRoots: () => request<LibraryRoot[]>("/api/library/roots"),
  libraryStorage: () => request<LibraryStorage>("/api/library/storage"),
  selectedFolders: () => request<SelectedFolder[]>("/api/library/selected-folders"),
  addSelectedFolder: (payload: { root_id: number; relative_path: string }) =>
    request("/api/library/selected-folders", { method: "POST", body: JSON.stringify(payload) }),
  deleteSelectedFolder: (folderId: number) => request(`/api/library/selected-folders/${folderId}`, { method: "DELETE" }),
  browse: (rootId: number, relativePath = "") =>
    request<any>(`/api/library/browse?root_id=${rootId}&relative_path=${encodeURIComponent(relativePath)}`),
  scan: () => request("/api/library/scan", { method: "POST" }),
  videos: (options?: { user_id?: number; offset?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (options?.user_id != null) params.set("user_id", String(options.user_id));
    if (options?.offset != null) params.set("offset", String(options.offset));
    if (options?.limit != null) params.set("limit", String(options.limit));
    const query = params.toString();
    return request<VideoSummary[]>(`/api/library/videos${query ? `?${query}` : ""}`);
  },
  explore: (offset = 0, limit = 30) => request<ExploreResponse>(`/api/library/explore?offset=${offset}&limit=${limit}`),
  suggested: (offset = 0, limit = 30) => request<ExploreResponse>(`/api/library/suggested?offset=${offset}&limit=${limit}`),
  video: (videoRef: number | string) => {
    const profile = playbackClientProfile();
    const params = new URLSearchParams();
    if (profile !== "default") {
      params.set("client_profile", profile);
    }
    const query = params.toString();
    return request<any>(`/api/videos/${encodeURIComponent(String(videoRef))}${query ? `?${query}` : ""}`);
  },
  videoSuggestions: (
    videoRef: number | string,
    mode: "suggested" | "related",
    offset = 0,
    limit = 10,
  ) =>
    request<WatchSuggestionsResponse>(
      `/api/videos/${encodeURIComponent(String(videoRef))}/suggestions?mode=${mode}&offset=${offset}&limit=${limit}`,
    ),
  updateProgress: (id: number, payload: { user_id: number; position_seconds: number; completed: boolean }) =>
    request(`/api/videos/${id}/progress`, { method: "POST", body: JSON.stringify(payload) }),
  setWatchState: (id: number, state: "watched" | "unwatched") =>
    request(`/api/videos/${id}/watch-state`, { method: "POST", body: JSON.stringify({ state }) }),
  setReaction: (id: number, reaction: "like" | "dislike" | null) =>
    request<{ reaction: "like" | "dislike" | null }>(`/api/videos/${id}/reaction`, { method: "POST", body: JSON.stringify({ reaction }) }),
  toggleSavedVideo: (id: number) =>
    request<{ saved: boolean }>(`/api/videos/${id}/save`, { method: "POST" }),
  liveOverview: () => request<LiveOverview>("/api/live"),
  liveStream: (youtubeVideoId: string) =>
    request<LiveStream>(`/api/live/${encodeURIComponent(youtubeVideoId)}`),
  setPlaylistSaved: (id: number, saved: boolean) =>
    request<{ saved: boolean; count: number }>(`/api/playlists/${id}/save`, { method: "POST", body: JSON.stringify({ saved }) }),
  channels: () => request<any[]>("/api/channels"),
  channel: (channelRef: number | string) => request<any>(`/api/channels/${encodeURIComponent(String(channelRef))}`),
  toggleSubscription: (channelRef: number | string) => request(`/api/channels/${encodeURIComponent(String(channelRef))}/subscribe`, { method: "POST" }),
  series: () => request<SeriesSummary[]>("/api/series"),
  setSeriesSaved: (id: number, saved: boolean) =>
    request<{ saved: boolean; count: number }>(`/api/series/${id}/save`, { method: "POST", body: JSON.stringify({ saved }) }),
  seriesDetail: (id: number) => request<any>(`/api/series/${id}`),
  playlists: () => request<PlaylistSummary[]>("/api/playlists"),
  playlistDetail: (id: number) => request<PlaylistDetail>(`/api/playlists/${id}`),
  createPlaylist: (payload: { user_id: number; name: string; description?: string | null }) =>
    request("/api/playlists", { method: "POST", body: JSON.stringify(payload) }),
  addPlaylistItem: (playlistId: number, videoId: number) =>
    request(`/api/playlists/${playlistId}/items`, { method: "POST", body: JSON.stringify({ video_id: videoId }) }),
  reorderPlaylistItems: (playlistId: number, videoIds: number[]) =>
    request(`/api/playlists/${playlistId}/items/reorder`, { method: "PUT", body: JSON.stringify({ video_ids: videoIds }) }),
  queue: () => request<QueueResponse>("/api/queue"),
  addQueueItem: (videoId: number) => request("/api/queue", { method: "POST", body: JSON.stringify({ video_id: videoId }) }),
  bulkQueue: (videoIds: number[], reset = true) =>
    request("/api/queue/bulk", { method: "POST", body: JSON.stringify({ video_ids: videoIds, reset }) }),
  deleteQueueItem: (itemId: number) => request(`/api/queue/${itemId}`, { method: "DELETE" }),
  updateMetadataOverride: (payload: { target_type: string; target_id: number; payload: Record<string, unknown> }) =>
    request("/api/admin/metadata-overrides", { method: "POST", body: JSON.stringify(payload) }),
  syncSettings: () => request<SyncSettings>("/api/sync/settings"),
  updateSyncSettings: (payload: { automatic_detection_enabled: boolean; automatic_sync_enabled: boolean; subtitle_generation_enabled: boolean; scan_interval_seconds: number; allow_fallback_art: boolean; prefer_high_res_banners: boolean; live_tab_enabled: boolean; live_monitored_channel_ids: number[]; comment_limit: number; max_replies_per_comment: number; requests_per_second: number; youtube_api_key?: string | null; clear_youtube_api_key?: boolean }) =>
    request<SyncSettings>("/api/sync/settings", { method: "PUT", body: JSON.stringify(payload) }),
  syncSubtitles: () => request("/api/sync/subtitles", { method: "POST" }),
  retentionSettings: () => request<RetentionOverview>("/api/retention/settings"),
  updateRetentionSettings: (payload: {
    enabled: boolean;
    retention_days: number;
    staging_folder_path?: string | null;
    auto_schedule_kind: "interval" | "daily" | "weekly";
    auto_interval_minutes: number;
    auto_time_hour: number;
    auto_time_minute: number;
    auto_weekday: number;
    auto_timezone?: string | null;
  }) =>
    request<RetentionOverview>("/api/retention/settings", { method: "PUT", body: JSON.stringify(payload) }),
  runRetention: () => request<{ result: { status: string; message: string; marked: number; deleted: number; reverted: number; run_token?: string | null }; settings: RetentionSettings; pending_items: RetentionPendingItem[]; history: RetentionRun[] }>("/api/retention/run", { method: "POST" }),
  revertRetention: () => request<{ result: { status: string; message: string; reverted: number }; settings: RetentionSettings; pending_items: RetentionPendingItem[]; history: RetentionRun[] }>("/api/retention/revert", { method: "POST" }),
  deleteRetention: () => request<{ result: { status: string; message: string; deleted: number }; settings: RetentionSettings; pending_items: RetentionPendingItem[]; history: RetentionRun[] }>("/api/retention/delete", { method: "POST" }),
  retentionLookup: (query: string) =>
    request<{ channels: RetentionLookupItem[]; series: RetentionLookupItem[]; videos: RetentionLookupItem[] }>(`/api/retention/lookup?q=${encodeURIComponent(query)}`),
  retentionFolders: (path = "") =>
    request<RetentionFolderBrowser>(`/api/retention/folders?path=${encodeURIComponent(path)}`),
  createRetentionFolder: (payload: { parent_path: string; name: string }) =>
    request<{ created_path: string; browser: RetentionFolderBrowser }>("/api/retention/folders", { method: "POST", body: JSON.stringify(payload) }),
  addRetentionExclusion: (payload: { target_type: "video" | "series" | "channel"; target_id: number }) =>
    request<RetentionExclusion>("/api/retention/exclusions", { method: "POST", body: JSON.stringify(payload) }),
  deleteRetentionExclusion: (exclusionId: number) =>
    request<{ ok: boolean }>(`/api/retention/exclusions/${exclusionId}`, { method: "DELETE" }),
  syncLibrary: (options?: { force?: boolean }) =>
    request(`/api/sync/library${options?.force ? "?force=true" : ""}`, { method: "POST" }),
  syncOrphans: (options?: { force?: boolean }) =>
    request(`/api/sync/orphans${options?.force ? "?force=true" : ""}`, { method: "POST" }),
  syncChannel: (id: number, options?: { force?: boolean; high_res_banner?: boolean }) => {
    const params = new URLSearchParams();
    if (options?.force) params.set("force", "true");
    if (options?.high_res_banner) params.set("high_res_banner", "true");
    const query = params.toString();
    return request(`/api/sync/channel/${id}${query ? `?${query}` : ""}`, { method: "POST" });
  },
  syncSeries: (id: number) => request(`/api/sync/series/${id}`, { method: "POST" }),
  syncVideo: (id: number, options?: { force?: boolean }) =>
    request(`/api/sync/video/${id}${options?.force ? "?force=true" : ""}`, { method: "POST" }),
  jobs: () => request<any[]>("/api/jobs"),
  jobsStatus: () => request<{ items: JobStatusItem[] }>("/api/jobs/status"),
  transcodes: () => request<{ items: TranscodeItem[] }>("/api/transcodes"),
  stopTranscode: (jobId: number) => request<{ ok: boolean; stopped: boolean }>(`/api/transcodes/${jobId}/stop`, { method: "POST" }),
  logs: (limit = 20) => request<LogResponse>(`/api/logs?limit=${limit}`),
  clearSubscriptionMarkers: () => request("/api/subscriptions/clear-new", { method: "POST" }),
};
