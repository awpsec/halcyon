import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  api,
  type ChannelSummary,
  type Preferences,
  type Profile,
  type RetentionFolderBrowser,
  type RetentionLookupItem,
  type RetentionRun,
  type TranscodeItem,
  type UpdateStatus,
  type VideoSummary,
} from "../api/client";
import { AvatarImage } from "../components/AvatarImage";
import { Modal } from "../components/Modal";
import { SettingsPageSkeleton } from "../components/PageSkeletons";
import { SyncReviewPanel } from "../components/SyncReviewPanel";
import { useAsyncData } from "../hooks/useAsyncData";
import { formatAbsoluteDateTime, formatRelativeDate, parseApiDate } from "../lib/format";
import { pushToast } from "../lib/notifications";

declare const __APP_VERSION__: string;

type Props = {
  profile: Profile | null;
  preferences: Preferences;
  onPreferencesChange: (preferences: Preferences) => void;
  onProfileChange: (profile: Profile) => void;
};

type SettingsTab = "user" | "server" | "retention" | "logs" | "admin";
const INITIAL_UPLOAD_BATCH = 10;
const UPLOAD_BATCH_SIZE = 5;
const DEFAULT_AVATAR_ZOOM = 1.12;
const RETENTION_SCHEDULE_OPTIONS: Array<SettingsSelectOption<RetentionScheduleKind>> = [
  { value: "interval", label: "Every N minutes" },
  { value: "daily", label: "Every day at" },
  { value: "weekly", label: "Every week on" },
];
const RETENTION_WEEKDAY_OPTIONS: Array<SettingsSelectOption<RetentionWeekdayValue>> = [
  { value: "0", label: "Monday" },
  { value: "1", label: "Tuesday" },
  { value: "2", label: "Wednesday" },
  { value: "3", label: "Thursday" },
  { value: "4", label: "Friday" },
  { value: "5", label: "Saturday" },
  { value: "6", label: "Sunday" },
];
const FALLBACK_RETENTION_TIMEZONE_LABEL = "Server local time";
const LEGACY_RETENTION_TIMEZONE_LABELS = new Set(["", "UTC", "Etc/UTC", "GMT", FALLBACK_RETENTION_TIMEZONE_LABEL]);

type AvatarCropDraft = {
  zoom: number;
  offsetX: number;
  offsetY: number;
};

type SettingsSelectOption<Value extends string> = {
  value: Value;
  label: string;
};

type RetentionScheduleKind = "interval" | "daily" | "weekly";
type RetentionWeekdayValue = "0" | "1" | "2" | "3" | "4" | "5" | "6";

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function clampAvatarCropDraft(draft: AvatarCropDraft): AvatarCropDraft {
  return {
    zoom: clamp(draft.zoom, 1, 2.6),
    offsetX: clamp(draft.offsetX, -100, 100),
    offsetY: clamp(draft.offsetY, -100, 100),
  };
}

function isNearBottom(node: HTMLDivElement) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 28;
}

function isUsefulChannel(value: string | null | undefined) {
  const normalized = (value ?? "").trim().toLowerCase();
  return normalized.length > 0 && normalized !== "unknown channel" && normalized !== "offline library";
}

function detectBrowserTimeZone() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone?.trim();
  return zone ? zone : "";
}

function uploadMetadataStatus(video: VideoSummary) {
  const flags = {
    channel: isUsefulChannel(video.channel_name),
    views: video.youtube_view_count != null,
    likes: video.youtube_like_count != null,
    dislikes: video.youtube_dislike_count != null,
    comments: (video.youtube_comment_count ?? 0) > 0,
    date: Boolean(video.published_at),
    description: Boolean(video.description?.trim()),
  };
  const collected = Object.values(flags).filter(Boolean).length;
  return {
    percent: Math.round((collected / Object.keys(flags).length) * 100),
    flags,
  };
}

function formatSyncScore(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return null;
  const normalized = clamp(value, 0, 1);
  return `${(normalized * 100).toFixed(2)}% match`;
}

function describeMatchedFields(raw: string | null | undefined) {
  const labels = (raw ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      if (item === "channel") return "channel";
      if (item === "title") return "title";
      if (item === "description") return "description";
      if (item === "likes") return "likes";
      if (item === "comments") return "comments";
      if (item === "uploaded") return "uploaded";
      if (item === "views") return "views";
      return item;
    });
  return labels.length ? `Matched ${labels.join(" - ")}.` : "Matched metadata.";
}

function formatRetentionTrigger(value: string | null | undefined) {
  if (!value) return "Never";
  if (value === "auto") return "Automatic";
  if (value === "manual") return "Manual";
  if (value === "manual-delete") return "Delete now";
  if (value === "manual-revert") return "Manual revert";
  return value;
}

function formatRetentionTargetType(value: RetentionLookupItem["target_type"]) {
  if (value === "channel") return "Channel";
  if (value === "series") return "Series";
  return "Video";
}

function formatRetentionStatus(value: string | null | undefined) {
  if (!value) return "idle";
  if (value === "failed") return "Failed";
  if (value === "completed") return "Completed";
  if (value === "skipped") return "Skipped";
  if (value === "idle") return "Idle";
  return value;
}

function retentionRunDetails(
  item: Pick<RetentionRun, "status" | "message" | "details" | "marked_count" | "deleted_count" | "reverted_count">,
) {
  const sections: Array<{ key: string; title: string; entries: string[] }> = [];
  const detailPayload = item.details ?? {};
  const hadMeaningfulSummary =
    item.marked_count > 0 || item.deleted_count > 0 || item.reverted_count > 0;

  const fileSections = [
    { key: "marked", title: "Marked files", entries: detailPayload.marked_files ?? [] },
    { key: "deleted", title: "Deleted files", entries: detailPayload.deleted_files ?? [] },
    { key: "reverted", title: "Reverted files", entries: detailPayload.reverted_files ?? [] },
  ];
  for (const section of fileSections) {
    if (section.entries.length > 0) {
      sections.push(section);
    }
  }

  const notes: string[] = [];
  const message = item.message?.trim() ?? "";
  const summaryMatch = message.match(/^Marked \d+, deleted \d+, reverted \d+(?:,\s*(.+))?$/i);
  const summaryTail = summaryMatch?.[1]?.trim();
  if (summaryTail) {
    const normalizedTail = summaryTail.charAt(0).toUpperCase() + summaryTail.slice(1);
    notes.push(normalizedTail.endsWith(".") ? normalizedTail : `${normalizedTail}.`);
  } else if (item.status === "failed" && message) {
    notes.push(message);
  } else if (
    message &&
    !summaryMatch &&
    !/^Reverted \d+ pending deletions$/i.test(message)
  ) {
    notes.push(message);
  }

  if (!sections.length && !notes.length && hadMeaningfulSummary) {
    notes.push("This run was recorded before detailed file logs were enabled.");
  }

  if (!sections.length && !notes.length) {
    notes.push("No interesting details.");
  }

  return { sections, notes };
}

function formatRetentionTime(hour: number, minute: number) {
  const normalizedHour = Number.isFinite(hour) ? Math.max(0, Math.min(23, hour)) : 0;
  const normalizedMinute = Number.isFinite(minute) ? Math.max(0, Math.min(59, minute)) : 0;
  return `${String(normalizedHour).padStart(2, "0")}:${String(normalizedMinute).padStart(2, "0")}`;
}

function parseRetentionTime(value: string) {
  const match = value.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) {
    return { hour: 4, minute: 0 };
  }
  return {
    hour: clamp(Number(match[1]) || 0, 0, 23),
    minute: clamp(Number(match[2]) || 0, 0, 59),
  };
}

function formatRetentionCountdown(value: string, nowMs: number) {
  const target = parseApiDate(value);
  if (Number.isNaN(target.getTime())) return "Timer unavailable";

  const remainingSeconds = Math.max(0, Math.ceil((target.getTime() - nowMs) / 1000));
  const hours = Math.floor(remainingSeconds / 3600);
  const minutes = Math.floor((remainingSeconds % 3600) / 60);
  const seconds = remainingSeconds % 60;

  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function extractApiErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    return parsed.detail?.trim() || error.message;
  } catch {
    return error.message || fallback;
  }
}

function matchesChannelSearch(channel: ChannelSummary, query: string) {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return true;
  return (
    channel.name.toLowerCase().includes(normalizedQuery)
    || (channel.slug ?? "").toLowerCase().includes(normalizedQuery)
  );
}

function formatBytes(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const precision = index === 0 ? 0 : size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(precision)} ${units[index]}`;
}

function storageFreeTone(value: number | null | undefined) {
  const freeBytes = value ?? 0;
  const fiftyGb = 50 * 1024 * 1024 * 1024;
  const twoHundredGb = 200 * 1024 * 1024 * 1024;
  if (freeBytes <= fiftyGb) return "is-critical";
  if (freeBytes <= twoHundredGb) return "is-warning";
  return "";
}

async function copyTextToClipboard(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "true");
  field.style.position = "absolute";
  field.style.left = "-9999px";
  document.body.appendChild(field);
  field.select();
  document.execCommand("copy");
  document.body.removeChild(field);
}

function retentionTargetHref(targetType: "video" | "series" | "channel", targetId: number, subtitle?: string | null) {
  if (targetType === "channel") return `/channels/${subtitle || targetId}`;
  if (targetType === "series") return `/series/${targetId}`;
  return `/video/${targetId}`;
}

function settingLabelClass(tooltip?: string | null) {
  return tooltip ? "settings-tooltip-target" : undefined;
}

function normalizeSettingsTab(value: string | null, isAdmin: boolean): SettingsTab {
  if (isAdmin && (value === "server" || value === "retention" || value === "logs" || value === "admin")) {
    return value;
  }
  return "user";
}

function SettingsMenuSelect<Value extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: Value;
  options: Array<SettingsSelectOption<Value>>;
  onChange: (value: Value) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selected = options.find((option) => option.value === value) ?? options[0];

  useEffect(() => {
    if (!open) return undefined;
    function handlePointerDown(event: MouseEvent) {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="settings-menu-select" ref={rootRef}>
      <button
        type="button"
        className={`settings-menu-select-trigger ${open ? "is-open" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{selected?.label ?? label}</span>
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <path
            d="m5.5 7.5 4.5 5 4.5-5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open ? (
        <div className="settings-menu-select-list" role="listbox" aria-label={label}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`settings-menu-select-option ${option.value === value ? "is-selected" : ""}`}
              role="option"
              aria-selected={option.value === value}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function readFileAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function cropAvatarDataUrl(
  source: string,
  options: AvatarCropDraft,
) {
  const image = await new Promise<HTMLImageElement>((resolve, reject) => {
    const next = new Image();
    next.onload = () => resolve(next);
    next.onerror = () => reject(new Error("Unable to load avatar image"));
    next.src = source;
  });
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 512;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Unable to crop avatar image");

  const cropSize = Math.max(32, Math.floor(Math.min(image.width, image.height) / options.zoom));
  const availableX = Math.max(0, image.width - cropSize);
  const availableY = Math.max(0, image.height - cropSize);
  const sx = Math.min(
    availableX,
    Math.max(0, availableX / 2 + (options.offsetX / 100) * (availableX / 2)),
  );
  const sy = Math.min(
    availableY,
    Math.max(0, availableY / 2 + (options.offsetY / 100) * (availableY / 2)),
  );

  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  context.clearRect(0, 0, 512, 512);
  context.drawImage(image, sx, sy, cropSize, cropSize, 0, 0, 512, 512);
  return canvas.toDataURL("image/png");
}

export function SettingsPage({ profile, preferences, onPreferencesChange, onProfileChange }: Props) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const isAdmin = Boolean(profile?.is_admin);
  const requestedTab = searchParams.get("tab");
  const activeTab = normalizeSettingsTab(requestedTab, isAdmin);
  const syncState = useAsyncData(() => (isAdmin ? api.syncSettings() : Promise.resolve(null)), [isAdmin]);
  const rootsState = useAsyncData(() => (isAdmin ? api.libraryRoots() : Promise.resolve([])), [isAdmin]);
  const selectedState = useAsyncData(() => (isAdmin ? api.selectedFolders() : Promise.resolve([])), [isAdmin]);
  const storageState = useAsyncData(() => (isAdmin ? api.libraryStorage() : Promise.resolve(null)), [isAdmin]);
  const jobsState = useAsyncData(() => (isAdmin ? api.jobs() : Promise.resolve([])), [isAdmin]);
  const logsState = useAsyncData(() => (isAdmin ? api.logs(1200) : Promise.resolve(null)), [isAdmin]);
  const transcodesState = useAsyncData(() => (isAdmin ? api.transcodes() : Promise.resolve(null)), [isAdmin]);
  const updateStatusState = useAsyncData<UpdateStatus | null>(() => (isAdmin ? api.updateStatus() : Promise.resolve(null)), [isAdmin]);
  const uploadsState = useAsyncData(() => (isAdmin ? api.videos({ offset: 0, limit: INITIAL_UPLOAD_BATCH }) : Promise.resolve([])), [isAdmin]);
  const retentionState = useAsyncData(() => (isAdmin ? api.retentionSettings() : Promise.resolve(null)), [isAdmin]);
  const profilesState = useAsyncData(() => (isAdmin ? api.profiles() : Promise.resolve([])), [isAdmin]);
  const channelsState = useAsyncData(() => (isAdmin ? api.channels() : Promise.resolve([])), [isAdmin]);
  const [browserRootId, setBrowserRootId] = useState<number | null>(null);
  const browserState = useAsyncData(
    () => (isAdmin && browserRootId ? api.browse(browserRootId, "") : Promise.resolve(null)),
    [browserRootId, isAdmin],
  );
  const [accountDisplayName, setAccountDisplayName] = useState(profile?.display_name ?? "");
  const [accountAvatarUrl, setAccountAvatarUrl] = useState(profile?.avatar_url ?? "");
  const [avatarCropSource, setAvatarCropSource] = useState<string | null>(null);
  const [avatarCropDraft, setAvatarCropDraft] = useState<AvatarCropDraft>({
    zoom: DEFAULT_AVATAR_ZOOM,
    offsetX: 0,
    offsetY: 0,
  });
  const [avatarCropDragging, setAvatarCropDragging] = useState(false);
  const [syncDraft, setSyncDraft] = useState({
    automatic_detection_enabled: true,
    automatic_sync_enabled: false,
    subtitle_generation_enabled: false,
    scan_interval_seconds: 30,
    allow_fallback_art: false,
    prefer_high_res_banners: false,
    live_tab_enabled: true,
    live_monitored_channel_ids: [] as number[],
    comment_limit: 100,
    max_replies_per_comment: 3,
    requests_per_second: 3,
    youtube_api_key: "",
  });
  const [youtubeApiKeyConfigured, setYoutubeApiKeyConfigured] = useState(false);
  const [clearYoutubeApiKeyRequested, setClearYoutubeApiKeyRequested] = useState(false);
  const [retentionDraft, setRetentionDraft] = useState({
    enabled: false,
    retention_days: 30,
    staging_folder_path: "",
    auto_schedule_kind: "interval" as RetentionScheduleKind,
    auto_interval_minutes: 15,
    auto_time_hour: 4,
    auto_time_minute: 0,
    auto_weekday: 0,
    auto_timezone: detectBrowserTimeZone(),
  });
  const [syncDirty, setSyncDirty] = useState(false);
  const [syncSaving, setSyncSaving] = useState(false);
  const [retentionDirty, setRetentionDirty] = useState(false);
  const [retentionSaving, setRetentionSaving] = useState(false);
  const [retentionRunning, setRetentionRunning] = useState(false);
  const [retentionDeleting, setRetentionDeleting] = useState(false);
  const [retentionReverting, setRetentionReverting] = useState(false);
  const youtubeQuotaSummary = useMemo(() => {
    const dailyLimit = syncState.data?.youtube_api_quota_daily_limit ?? 10_000;
    const usedUnits = syncState.data?.youtube_api_quota_used_units ?? 0;
    const remainingUnits = syncState.data?.youtube_api_quota_remaining_units ?? Math.max(0, dailyLimit - usedUnits);
    const remainingPercent = Math.max(0, Math.min(100, syncState.data?.youtube_api_quota_remaining_percent ?? ((remainingUnits / dailyLimit) * 100)));
    return {
      dailyLimit,
      usedUnits,
      remainingUnits,
      remainingPercent,
      estimated: syncState.data?.youtube_api_quota_estimated ?? true,
    };
  }, [syncState.data]);
  const subtitleBackfillActive = useMemo(
    () =>
      Boolean(
        (jobsState.data ?? []).some(
          (item: { scope?: string; status?: string }) =>
            item.scope === "subtitles" && (item.status === "pending" || item.status === "running"),
        ),
      ),
    [jobsState.data],
  );
  const subtitleBackfillJob = useMemo(
    () =>
      (jobsState.data ?? []).find(
        (item: { scope?: string; status?: string }) =>
          item.scope === "subtitles" && (item.status === "pending" || item.status === "running"),
      ) as { details?: Record<string, unknown> } | undefined,
    [jobsState.data],
  );
  const subtitleBackfillLabel = useMemo(() => {
    if (!subtitleBackfillJob?.details) return null;
    const processed = Number(subtitleBackfillJob.details.processed ?? 0);
    const total = Number(subtitleBackfillJob.details.total ?? 0);
    const generated = Number(subtitleBackfillJob.details.generated ?? 0);
    const skipped = Number(subtitleBackfillJob.details.skipped ?? 0);
    const failed = Number(subtitleBackfillJob.details.failed ?? 0);
    if (!total) return "Backfilling subtitles...";
    return `Backfilling subtitles ${processed}/${total} • ${generated} generated • ${skipped} skipped • ${failed} failed`;
  }, [subtitleBackfillJob]);
  const [savingAccount, setSavingAccount] = useState(false);
  const [scanPending, setScanPending] = useState(false);
  const [syncPending, setSyncPending] = useState(false);
  const [forceSyncPending, setForceSyncPending] = useState(false);
  const [channelSyncPending, setChannelSyncPending] = useState(false);
  const [orphanSyncPending, setOrphanSyncPending] = useState(false);
  const [stoppingTranscodeId, setStoppingTranscodeId] = useState<number | null>(null);
  const [visibleSyncActivityCount, setVisibleSyncActivityCount] = useState(5);
  const activityLogRef = useRef<HTMLDivElement | null>(null);
  const syncLogRef = useRef<HTMLDivElement | null>(null);
  const syncActivityRef = useRef<HTMLDivElement | null>(null);
  const uploadsPanelRef = useRef<HTMLDivElement | null>(null);
  const avatarInputRef = useRef<HTMLInputElement | null>(null);
  const avatarCropStageRef = useRef<HTMLDivElement | null>(null);
  const avatarCropDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    startOffsetX: number;
    startOffsetY: number;
    startZoom: number;
  } | null>(null);
  const activityFollowRef = useRef(true);
  const syncFollowRef = useRef(true);
  const [uploadsHasMore, setUploadsHasMore] = useState(true);
  const [uploadsLoadingMore, setUploadsLoadingMore] = useState(false);
  const [uploadSyncPending, setUploadSyncPending] = useState<Record<number, "sync" | "force" | "review" | undefined>>({});
  const [retentionLookupQuery, setRetentionLookupQuery] = useState("");
  const [liveMonitorQuery, setLiveMonitorQuery] = useState("");
  const [retentionLookupResults, setRetentionLookupResults] = useState<{
    channels: RetentionLookupItem[];
    series: RetentionLookupItem[];
    videos: RetentionLookupItem[];
  }>({ channels: [], series: [], videos: [] });
  const [retentionLookupPending, setRetentionLookupPending] = useState(false);
  const [retentionExclusionPending, setRetentionExclusionPending] = useState<string | null>(null);
  const [retentionExclusionDeletingId, setRetentionExclusionDeletingId] = useState<number | null>(null);
  const [showRetentionHistory, setShowRetentionHistory] = useState(false);
  const [expandedRetentionRunIds, setExpandedRetentionRunIds] = useState<number[]>([]);
  const [retentionCountdownNow, setRetentionCountdownNow] = useState(() => Date.now());
  const [retentionFolderBrowser, setRetentionFolderBrowser] = useState<RetentionFolderBrowser | null>(null);
  const [retentionFolderBrowserOpen, setRetentionFolderBrowserOpen] = useState(false);
  const [retentionFolderBrowserPending, setRetentionFolderBrowserPending] = useState(false);
  const [retentionFolderBrowserError, setRetentionFolderBrowserError] = useState<string | null>(null);
  const [retentionFolderCreating, setRetentionFolderCreating] = useState(false);
  const [retentionNewFolderOpen, setRetentionNewFolderOpen] = useState(false);
  const [retentionNewFolderName, setRetentionNewFolderName] = useState("");
  const liveMonitorChannels = useMemo(
    () =>
      (channelsState.data ?? [])
        .filter((channel: ChannelSummary) => channel.slug !== "unknown-channel")
        .sort((left: ChannelSummary, right: ChannelSummary) => left.name.localeCompare(right.name, undefined, { sensitivity: "base" })),
    [channelsState.data],
  );
  const selectedLiveMonitorChannels = useMemo(() => {
    const selectedIds = new Set(syncDraft.live_monitored_channel_ids);
    return liveMonitorChannels.filter((channel) => selectedIds.has(channel.id));
  }, [liveMonitorChannels, syncDraft.live_monitored_channel_ids]);
  const liveMonitorSearchResults = useMemo(() => {
    const selectedIds = new Set(syncDraft.live_monitored_channel_ids);
    return liveMonitorChannels
      .filter((channel) => !selectedIds.has(channel.id))
      .filter((channel) => matchesChannelSearch(channel, liveMonitorQuery))
      .slice(0, 8);
  }, [liveMonitorChannels, liveMonitorQuery, syncDraft.live_monitored_channel_ids]);
  const retentionFolderBrowserRef = useRef<HTMLDivElement | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [updateModalOpen, setUpdateModalOpen] = useState(false);
  const [profilePermissionDrafts, setProfilePermissionDrafts] = useState<Record<number, "user" | "admin">>({});
  const [profilePermissionsSaving, setProfilePermissionsSaving] = useState(false);
  const [deletingProfileId, setDeletingProfileId] = useState<number | null>(null);
  const [pendingDeleteProfile, setPendingDeleteProfile] = useState<Profile | null>(null);
  const adminProfiles = useMemo(
    () => [...(profilesState.data ?? [])].sort((left, right) => left.display_name.localeCompare(right.display_name)),
    [profilesState.data],
  );
  const profilePermissionsDirty = useMemo(
    () =>
      adminProfiles.some((item) => {
        const draft = profilePermissionDrafts[item.id];
        return draft != null && draft !== (item.is_admin ? "admin" : "user");
      }),
    [adminProfiles, profilePermissionDrafts],
  );

  function setActiveTab(nextTab: SettingsTab) {
    const next = new URLSearchParams(searchParams);
    if (nextTab === "user") {
      next.delete("tab");
    } else {
      next.set("tab", nextTab);
    }
    setSearchParams(next, { replace: true });
  }

  useEffect(() => {
    if (isAdmin || !requestedTab) return;
    navigate("/settings", { replace: true });
  }, [isAdmin, navigate, requestedTab]);

  useEffect(() => {
    setAccountDisplayName(profile?.display_name ?? "");
    setAccountAvatarUrl(profile?.avatar_url ?? "");
  }, [profile]);

  useEffect(() => {
    const profiles = profilesState.data ?? [];
    if (!profiles.length) return;
    setProfilePermissionDrafts((current) => {
      const next: Record<number, "user" | "admin"> = {};
      for (const item of profiles) {
        next[item.id] = current[item.id] ?? (item.is_admin ? "admin" : "user");
      }
      return next;
    });
  }, [profilesState.data]);

  useEffect(() => {
    if (!browserRootId && rootsState.data?.length) {
      setBrowserRootId(rootsState.data[0].id);
    }
  }, [browserRootId, rootsState.data]);

  useEffect(() => {
    if (!isAdmin) return undefined;
    const timer = window.setInterval(() => {
      void api.libraryStorage().then(storageState.setData).catch(() => undefined);
    }, 15000);
    return () => window.clearInterval(timer);
  }, [isAdmin, storageState.setData]);

  useEffect(() => {
    if (!syncState.data || syncDirty || syncSaving) return;
    setSyncDraft({
      automatic_detection_enabled: syncState.data.automatic_detection_enabled ?? true,
      automatic_sync_enabled: syncState.data.automatic_sync_enabled,
      subtitle_generation_enabled: syncState.data.subtitle_generation_enabled ?? false,
      scan_interval_seconds: syncState.data.scan_interval_seconds ?? 30,
      allow_fallback_art: syncState.data.allow_fallback_art ?? false,
      prefer_high_res_banners: syncState.data.prefer_high_res_banners ?? false,
      live_tab_enabled: syncState.data.live_tab_enabled ?? true,
      live_monitored_channel_ids: syncState.data.live_monitored_channel_ids ?? [],
      comment_limit: syncState.data.comment_limit,
      max_replies_per_comment: syncState.data.max_replies_per_comment ?? 3,
      requests_per_second: syncState.data.requests_per_second ?? 3,
      youtube_api_key: "",
    });
    setYoutubeApiKeyConfigured(syncState.data.youtube_api_key_configured ?? false);
    setClearYoutubeApiKeyRequested(false);
  }, [syncDirty, syncSaving, syncState.data]);

  useEffect(() => {
    if (!retentionState.data || retentionDirty || retentionSaving) return;
    setRetentionDraft({
      enabled: retentionState.data.settings.enabled,
      retention_days: retentionState.data.settings.retention_days ?? 30,
      staging_folder_path: retentionState.data.settings.staging_folder_path ?? "",
      auto_schedule_kind: retentionState.data.settings.auto_schedule_kind ?? "interval",
      auto_interval_minutes: retentionState.data.settings.auto_interval_minutes ?? 15,
      auto_time_hour: retentionState.data.settings.auto_time_hour ?? 4,
      auto_time_minute: retentionState.data.settings.auto_time_minute ?? 0,
      auto_weekday: retentionState.data.settings.auto_weekday ?? 0,
      auto_timezone: retentionState.data.settings.auto_timezone ?? detectBrowserTimeZone(),
    });
  }, [retentionDirty, retentionSaving, retentionState.data]);

  useEffect(() => {
    if (!isAdmin || retentionDirty || retentionSaving || !retentionState.data) return;
    const detectedTimezone = detectBrowserTimeZone();
    const currentTimezone = (retentionState.data.settings.auto_timezone ?? "").trim();
    if (!detectedTimezone || detectedTimezone === currentTimezone || !LEGACY_RETENTION_TIMEZONE_LABELS.has(currentTimezone)) return;
    void api.updateRetentionSettings({
      enabled: retentionState.data.settings.enabled,
      retention_days: retentionState.data.settings.retention_days ?? 30,
      staging_folder_path: retentionState.data.settings.staging_folder_path ?? null,
      auto_schedule_kind: retentionState.data.settings.auto_schedule_kind ?? "interval",
      auto_interval_minutes: retentionState.data.settings.auto_interval_minutes ?? 15,
      auto_time_hour: retentionState.data.settings.auto_time_hour ?? 4,
      auto_time_minute: retentionState.data.settings.auto_time_minute ?? 0,
      auto_weekday: retentionState.data.settings.auto_weekday ?? 0,
      auto_timezone: detectedTimezone,
    })
      .then((next) => retentionState.setData(next))
      .catch(() => undefined);
  }, [isAdmin, retentionDirty, retentionSaving, retentionState.data, retentionState.setData]);

  useEffect(() => {
    if (!(retentionState.data?.pending_items.length ?? 0)) return undefined;
    const timer = window.setInterval(() => {
      setRetentionCountdownNow(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, [retentionState.data?.pending_items.length]);

  useEffect(() => {
    if (!isAdmin) return undefined;
    const uploadLimit = Math.max(INITIAL_UPLOAD_BATCH, uploadsState.data?.length ?? INITIAL_UPLOAD_BATCH);
    const interval = window.setInterval(() => {
      void api.jobs().then(jobsState.setData).catch(() => undefined);
      void api.logs(1200).then(logsState.setData).catch(() => undefined);
      void api.transcodes().then(transcodesState.setData).catch(() => undefined);
      void api.videos({ offset: 0, limit: uploadLimit })
        .then((items) => {
          uploadsState.setData(items);
          setUploadsHasMore(items.length >= uploadLimit);
        })
        .catch(() => undefined);
      void api.libraryRoots().then(rootsState.setData).catch(() => undefined);
      void api.selectedFolders().then(selectedState.setData).catch(() => undefined);
      if (!syncDirty && !syncSaving) {
        void api.syncSettings().then(syncState.setData).catch(() => undefined);
      }
      if (!retentionDirty && !retentionSaving && !retentionRunning && !retentionDeleting && !retentionReverting) {
        void api.retentionSettings().then(retentionState.setData).catch(() => undefined);
      }
    }, 4000);
    return () => window.clearInterval(interval);
  }, [isAdmin, retentionDeleting, retentionDirty, retentionReverting, retentionRunning, retentionSaving, syncDirty, syncSaving, uploadsState.data?.length]);

  useEffect(() => {
    const query = retentionLookupQuery.trim();
    if (query.length < 2) {
      setRetentionLookupResults({ channels: [], series: [], videos: [] });
      setRetentionLookupPending(false);
      return;
    }
    let cancelled = false;
    setRetentionLookupPending(true);
    const timer = window.setTimeout(() => {
      void api.retentionLookup(query)
        .then((results) => {
          if (!cancelled) {
            setRetentionLookupResults(results);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setRetentionLookupResults({ channels: [], series: [], videos: [] });
          }
        })
        .finally(() => {
          if (!cancelled) {
            setRetentionLookupPending(false);
          }
        });
    }, 220);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [retentionLookupQuery]);

  useEffect(() => {
    if (!retentionFolderBrowserOpen) return undefined;
    let cancelled = false;
    setRetentionFolderBrowserPending(true);
    const timer = window.setTimeout(() => {
      void api.retentionFolders(retentionDraft.staging_folder_path)
        .then((browser) => {
          if (cancelled) return;
          setRetentionFolderBrowser(browser);
          setRetentionFolderBrowserError(null);
        })
        .catch((error) => {
          if (cancelled) return;
          setRetentionFolderBrowser(null);
          setRetentionFolderBrowserError(extractApiErrorMessage(error, "Unable to browse retention folders"));
        })
        .finally(() => {
          if (!cancelled) {
            setRetentionFolderBrowserPending(false);
          }
        });
    }, 140);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [retentionDraft.staging_folder_path, retentionFolderBrowserOpen]);

  useEffect(() => {
    if (!retentionFolderBrowserOpen) return undefined;
    function handlePointerDown(event: MouseEvent) {
      if (retentionFolderBrowserRef.current?.contains(event.target as Node)) return;
      setRetentionFolderBrowserOpen(false);
      setRetentionNewFolderOpen(false);
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setRetentionFolderBrowserOpen(false);
        setRetentionNewFolderOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [retentionFolderBrowserOpen]);

  useEffect(() => {
    const node = activityLogRef.current;
    if (node && activityFollowRef.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [logsState.data?.lines]);

  const selectedByRoot = useMemo(() => {
    const map = new Map<number, Array<{ id: number; relative_path: string }>>();
    for (const folder of selectedState.data ?? []) {
      const items = map.get(folder.root_id) ?? [];
      items.push(folder);
      map.set(folder.root_id, items);
    }
    return map;
  }, [selectedState.data]);

  const activeTranscodes = useMemo(
    () => (transcodesState.data?.items ?? []).filter((item) => item.status === "running"),
    [transcodesState.data?.items],
  );
  const syncLogLines = useMemo(
    () =>
      (logsState.data?.lines ?? [])
        .filter((line) => {
          const lowered = line.toLowerCase();
          return lowered.includes("sync") || lowered.includes("scan") || lowered.includes("subtitle") || lowered.includes("whisper");
        })
        .slice(-320),
    [logsState.data?.lines],
  );
  const activityLines = useMemo(() => (logsState.data?.lines ?? []).slice(-500), [logsState.data?.lines]);
  const activeSyncJobs = useMemo(
    () => (jobsState.data ?? []).filter((job: any) => job.scope !== "transcode" && job.status === "running"),
    [jobsState.data],
  );
  const recentUploads = uploadsState.data ?? [];
  const libraryStorageSegments = useMemo(() => {
    if (!storageState.data) return null;
    const libraryBytes = Math.max(0, storageState.data.library_bytes ?? 0);
    const availableBytes = Math.max(0, storageState.data.available_bytes ?? 0);
    const totalBytes = Math.max(0, storageState.data.total_bytes ?? 0);
    const otherBytes = Math.max(0, totalBytes - libraryBytes - availableBytes);
    const safeTotal = totalBytes || Math.max(1, libraryBytes + availableBytes + otherBytes);
    return {
      libraryBytes,
      availableBytes,
      otherBytes,
      libraryPercent: (libraryBytes / safeTotal) * 100,
      otherPercent: (otherBytes / safeTotal) * 100,
      availablePercent: (availableBytes / safeTotal) * 100,
    };
  }, [storageState.data]);

  const syncActivityItems = useMemo(() => {
    return syncLogLines
      .map((line) => {
        const timestamp = line.match(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3}/)?.[0] ?? "";
        const body = line.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3}\s+\w+\s+/, "");
        const query = body.match(/query=(.+?)(?:\s+\w+=|$)/)?.[1]?.trim();
        const title = body.match(/title=(.+?)(?:\s+\w+=|$)/)?.[1]?.trim();
        const warning = body.match(/error=(.+)$/)?.[1]?.trim();
        const status = body.match(/status=(\w+)/)?.[1]?.trim();
        if (body.startsWith("Sync started")) return { timestamp, title: "Sync started", detail: "Starting sync.", tone: "warning" };
        if (body.startsWith("Sync video start")) return { timestamp, title: "Matching video", detail: title ?? "Preparing metadata lookup.", tone: "warning" };
        if (body.startsWith("Sync youtube api query=")) return { timestamp, title: "Trying YouTube API", detail: query ?? "Searching with the API.", tone: "warning" };
        if (body.startsWith("Sync api fallback")) return { timestamp, title: "API fallback", detail: "Switching to manual web matching.", tone: "warning" };
        if (body.startsWith("Sync google dork query=")) return { timestamp, title: "Searching the web", detail: query ?? "Running fallback search.", tone: "warning" };
        if (body.startsWith("Sync youtube web query=")) return { timestamp, title: "Searching YouTube", detail: query ?? "Checking YouTube search results.", tone: "warning" };
        if (body.startsWith("Sync watch page fetch")) return { timestamp, title: "Inspecting candidate", detail: "Inspecting watch page.", tone: "warning" };
        if (body.startsWith("Sync matched")) {
          const localTitle = body.match(/title=(.+?)(?:\s+youtube_video_id=|\s+matched_title=|$)/)?.[1]?.trim();
          const matchedTitle = body.match(/matched_title=(.+?)(?:\s+matched_channel=|$)/)?.[1]?.trim();
          const matchedChannel = body.match(/matched_channel=(.+?)(?:\s+confidence=|$)/)?.[1]?.trim();
          const confidence = body.match(/confidence=([0-9.]+)/)?.[1]?.trim();
          const fields = body.match(/fields=(.+?)(?:\s+reasons=|$)/)?.[1]?.trim();
          return {
            timestamp,
            title: `Match found for "${localTitle ?? matchedTitle ?? "video"}"`,
            detail: `${matchedChannel ?? "Unknown channel"} - ${matchedTitle ?? localTitle ?? "Matched title"}. ${describeMatchedFields(fields)}`,
            tone: "success",
            score: confidence ? formatSyncScore(Number(confidence)) : null,
          };
        }
        if (body.startsWith("Sync unmatched")) return { timestamp, title: "No match found", detail: title ?? "This item still needs review.", tone: "warning" };
        if (body.startsWith("Sync warning")) return { timestamp, title: "Sync warning", detail: warning ?? "A recoverable warning occurred.", tone: "warning" };
        if (body.startsWith("Sync finished")) {
          return {
            timestamp,
            title: status === "partial" ? "Sync finished with issues" : "Sync finished",
            detail: status === "partial" ? "Some items still need review or a retry." : "Metadata refresh completed.",
            tone: status === "partial" ? "warning" : "success",
          };
        }
        if (body.startsWith("Sync crashed") || body.startsWith("Sync failed")) return { timestamp, title: "Sync failed", detail: "The sync job crashed before finishing.", tone: "error" };
        if (body.startsWith("Auto scan started")) return { timestamp, title: "Auto scan started", detail: "Background library scan is running.", tone: "warning" };
        if (body.startsWith("Manual scan started")) return { timestamp, title: "Manual scan started", detail: "Library scan was triggered from settings.", tone: "warning" };
        if (body.startsWith("Auto scan progress")) {
          const percent = body.match(/percent=(\d+)/)?.[1]?.trim();
          return { timestamp, title: "Auto scan progress", detail: percent ? `${percent}% complete.` : "Updating library progress.", tone: "warning" };
        }
        if (body.startsWith("Manual scan progress")) {
          const percent = body.match(/percent=(\d+)/)?.[1]?.trim();
          return { timestamp, title: "Manual scan progress", detail: percent ? `${percent}% complete.` : "Updating library progress.", tone: "warning" };
        }
        if (body.startsWith("Auto scan completed")) return { timestamp, title: "Auto scan completed", detail: "Background library scan finished.", tone: "success" };
        if (body.startsWith("Manual scan completed")) return { timestamp, title: "Manual scan completed", detail: "Library scan finished.", tone: "success" };
        if (body.startsWith("Auto scan failed")) return { timestamp, title: "Auto scan failed", detail: warning ?? "Background library scan hit an error.", tone: "error" };
        if (body.startsWith("Manual scan failed")) return { timestamp, title: "Manual scan failed", detail: warning ?? "Library scan hit an error.", tone: "error" };
        return null;
      })
      .filter(Boolean)
      .slice(-120) as Array<{ timestamp: string; title: string; detail: string; tone: string; score?: string | null }>;
  }, [syncLogLines]);
  const visibleSyncActivityItems = useMemo(
    () => [...syncActivityItems].reverse().slice(0, visibleSyncActivityCount),
    [syncActivityItems, visibleSyncActivityCount],
  );

  useEffect(() => {
    const node = syncLogRef.current;
    if (node && syncFollowRef.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [syncActivityItems]);

  useEffect(() => {
    if (activeTab !== "logs") return;
    activityFollowRef.current = true;
    syncFollowRef.current = true;
    requestAnimationFrame(() => {
      if (activityLogRef.current) {
        activityLogRef.current.scrollTop = activityLogRef.current.scrollHeight;
      }
      if (syncLogRef.current) {
        syncLogRef.current.scrollTop = syncLogRef.current.scrollHeight;
      }
    });
  }, [activeTab]);

  useEffect(() => {
    setVisibleSyncActivityCount(5);
  }, [syncLogLines]);

  useEffect(() => {
    if (uploadsState.data && uploadsState.data.length < INITIAL_UPLOAD_BATCH) {
      setUploadsHasMore(false);
    }
  }, [uploadsState.data]);

  function handleActivityScroll() {
    const node = activityLogRef.current;
    if (!node) return;
    activityFollowRef.current = isNearBottom(node);
  }

  function handleSyncScroll() {
    const node = syncLogRef.current;
    if (!node) return;
    syncFollowRef.current = isNearBottom(node);
  }

  function handleSyncActivityScroll() {
    const node = syncActivityRef.current;
    if (!node) return;
    if (node.scrollTop + node.clientHeight >= node.scrollHeight - 24) {
      setVisibleSyncActivityCount((current) => Math.min(syncActivityItems.length, current + 5));
    }
  }

  function handleUploadsScroll() {
    const node = uploadsPanelRef.current;
    if (!node || uploadsLoadingMore || !uploadsHasMore) return;
    if (node.scrollTop + node.clientHeight >= node.scrollHeight - 24) {
      void loadMoreUploads();
    }
  }

  async function refreshServerState() {
    if (!isAdmin) return;
    rootsState.setData(await api.libraryRoots());
    selectedState.setData(await api.selectedFolders());
    storageState.setData(await api.libraryStorage());
    jobsState.setData(await api.jobs());
    logsState.setData(await api.logs(1200));
    transcodesState.setData(await api.transcodes());
    const uploadLimit = Math.max(INITIAL_UPLOAD_BATCH, recentUploads.length || INITIAL_UPLOAD_BATCH);
    const refreshedUploads = await api.videos({ offset: 0, limit: uploadLimit });
    uploadsState.setData(refreshedUploads);
    setUploadsHasMore(refreshedUploads.length >= uploadLimit);
  }

  async function refreshRetentionState() {
    if (!isAdmin) return;
    retentionState.setData(await api.retentionSettings());
  }

  async function navigateRetentionFolder(path: string) {
    setRetentionDraft((current) => ({ ...current, staging_folder_path: path }));
    setRetentionDirty(true);
    setRetentionFolderBrowserOpen(true);
  }

  async function createRetentionDirectory() {
    const parentPath = retentionFolderBrowser?.create_parent_path;
    const name = retentionNewFolderName.trim();
    if (!parentPath || !name) return;
    setRetentionFolderCreating(true);
    try {
      const next = await api.createRetentionFolder({ parent_path: parentPath, name });
      setRetentionFolderBrowser(next.browser);
      setRetentionDraft((current) => ({ ...current, staging_folder_path: next.created_path }));
      setRetentionDirty(true);
      setRetentionNewFolderName("");
      setRetentionNewFolderOpen(false);
      setRetentionFolderBrowserError(null);
    } catch (error) {
      pushToast("error", "Unable to create retention folder", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionFolderCreating(false);
    }
  }

  async function loadMoreUploads() {
    if (uploadsLoadingMore || !uploadsHasMore) return;
    setUploadsLoadingMore(true);
    try {
      const next = await api.videos({
        offset: recentUploads.length,
        limit: UPLOAD_BATCH_SIZE,
      });
      uploadsState.setData([...(recentUploads ?? []), ...next]);
      setUploadsHasMore(next.length >= UPLOAD_BATCH_SIZE);
    } catch {
      pushToast("error", "Unable to load more uploads");
    } finally {
      setUploadsLoadingMore(false);
    }
  }

  async function triggerVideoSync(videoId: number, force = false) {
    setUploadSyncPending((current) => ({ ...current, [videoId]: force ? "force" : "sync" }));
    try {
      const result: any = await api.syncVideo(videoId, force ? { force: true } : undefined);
      await refreshServerState();
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast(
          "error",
          force ? "Forced video sync completed with issues" : "Video sync completed with issues",
          result?.details?.warning ?? result?.details?.error ?? "Check logs",
        );
      } else {
        pushToast("success", force ? "Forced video sync finished" : "Video sync finished");
      }
    } catch (error) {
      pushToast(
        "error",
        force ? "Forced video sync failed" : "Video sync failed",
        error instanceof Error ? error.message : "Unknown sync error",
      );
    } finally {
      setUploadSyncPending((current) => ({ ...current, [videoId]: undefined }));
    }
  }

  async function triggerVideoReview(videoId: number) {
    setUploadSyncPending((current) => ({ ...current, [videoId]: "review" }));
    try {
      await api.sendVideoToReview(videoId);
      await refreshServerState();
      pushToast(
        "success",
        "Sent to review",
        "Open the sync review queue to approve or manually re-match it.",
        { href: "/sync-review" },
      );
    } catch (error) {
      pushToast(
        "error",
        "Unable to send to review",
        error instanceof Error ? error.message : "Unknown review error",
      );
    } finally {
      setUploadSyncPending((current) => ({ ...current, [videoId]: undefined }));
    }
  }

  async function saveRetentionSettings(options?: { silent?: boolean }) {
    setRetentionSaving(true);
    try {
      const next = await api.updateRetentionSettings({
        enabled: retentionDraft.enabled,
        retention_days: retentionDraft.retention_days,
        staging_folder_path: retentionDraft.staging_folder_path.trim() || null,
        auto_schedule_kind: retentionDraft.auto_schedule_kind,
        auto_interval_minutes: retentionDraft.auto_interval_minutes,
        auto_time_hour: retentionDraft.auto_time_hour,
        auto_time_minute: retentionDraft.auto_time_minute,
        auto_weekday: retentionDraft.auto_weekday,
        auto_timezone: retentionDraft.auto_timezone || detectBrowserTimeZone() || null,
      });
      retentionState.setData(next);
      setRetentionDraft({
        enabled: next.settings.enabled,
        retention_days: next.settings.retention_days,
        staging_folder_path: next.settings.staging_folder_path ?? "",
        auto_schedule_kind: next.settings.auto_schedule_kind ?? "interval",
        auto_interval_minutes: next.settings.auto_interval_minutes ?? 15,
        auto_time_hour: next.settings.auto_time_hour ?? 4,
        auto_time_minute: next.settings.auto_time_minute ?? 0,
        auto_weekday: next.settings.auto_weekday ?? 0,
        auto_timezone: next.settings.auto_timezone ?? detectBrowserTimeZone(),
      });
      setRetentionDirty(false);
      if (!options?.silent) {
        pushToast("success", "Retention settings saved");
      }
      return next;
    } catch (error) {
      if (!options?.silent) {
        pushToast("error", "Unable to save retention settings", error instanceof Error ? error.message : "Unknown error");
      }
      throw error;
    } finally {
      setRetentionSaving(false);
    }
  }

  async function runRetention() {
    setRetentionRunning(true);
    try {
      if (retentionDirty) {
        const saved = await saveRetentionSettings({ silent: true });
        retentionState.setData(saved);
      }
      const next = await api.runRetention();
      await refreshRetentionState();
      pushToast("success", "Retention wizard finished", next.result.message);
    } catch (error) {
      await refreshRetentionState().catch(() => undefined);
      pushToast("error", "Retention wizard failed", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionRunning(false);
    }
  }

  async function deleteRetention() {
    setRetentionDeleting(true);
    try {
      const next = await api.deleteRetention();
      await refreshRetentionState();
      pushToast("success", "Pending deletions removed", next.result.message);
    } catch (error) {
      await refreshRetentionState().catch(() => undefined);
      pushToast("error", "Unable to delete pending items", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionDeleting(false);
    }
  }

  async function revertRetention() {
    setRetentionReverting(true);
    try {
      const next = await api.revertRetention();
      await refreshRetentionState();
      pushToast("success", "Pending deletions reverted", next.result.message);
    } catch (error) {
      pushToast("error", "Unable to revert retention run", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionReverting(false);
    }
  }

  async function addRetentionExclusion(item: RetentionLookupItem) {
    const pendingKey = `${item.target_type}:${item.id}`;
    setRetentionExclusionPending(pendingKey);
    try {
      await api.addRetentionExclusion({ target_type: item.target_type, target_id: item.id });
      await refreshRetentionState();
      setRetentionLookupQuery("");
      setRetentionLookupResults({ channels: [], series: [], videos: [] });
      pushToast("success", "Retention exclusion added");
    } catch (error) {
      pushToast("error", "Unable to add exclusion", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionExclusionPending(null);
    }
  }

  async function removeRetentionExclusion(exclusionId: number) {
    setRetentionExclusionDeletingId(exclusionId);
    try {
      await api.deleteRetentionExclusion(exclusionId);
      await refreshRetentionState();
      pushToast("success", "Retention exclusion removed");
    } catch (error) {
      pushToast("error", "Unable to remove exclusion", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setRetentionExclusionDeletingId(null);
    }
  }

  async function saveSyncSettings() {
    setSyncSaving(true);
    try {
      const trimmedApiKey = syncDraft.youtube_api_key.trim();
      const next = await api.updateSyncSettings({
        ...syncDraft,
        youtube_api_key: trimmedApiKey || undefined,
        clear_youtube_api_key: clearYoutubeApiKeyRequested,
      });
      syncState.setData(next);
      setSyncDraft({
        automatic_detection_enabled: next.automatic_detection_enabled ?? true,
        automatic_sync_enabled: next.automatic_sync_enabled,
        subtitle_generation_enabled: next.subtitle_generation_enabled ?? false,
        scan_interval_seconds: next.scan_interval_seconds ?? 30,
        allow_fallback_art: next.allow_fallback_art ?? false,
        prefer_high_res_banners: next.prefer_high_res_banners ?? false,
        live_tab_enabled: next.live_tab_enabled ?? true,
        live_monitored_channel_ids: next.live_monitored_channel_ids ?? [],
        comment_limit: next.comment_limit,
        max_replies_per_comment: next.max_replies_per_comment ?? 3,
        requests_per_second: next.requests_per_second ?? 3,
        youtube_api_key: "",
      });
      setYoutubeApiKeyConfigured(next.youtube_api_key_configured ?? false);
      setClearYoutubeApiKeyRequested(false);
      setSyncDirty(false);
      pushToast("success", "Sync settings saved");
    } catch (error) {
      pushToast("error", "Unable to save sync settings", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setSyncSaving(false);
    }
  }

  function addLiveMonitorChannel(channel: ChannelSummary) {
    setSyncDraft((current) => {
      if (current.live_monitored_channel_ids.includes(channel.id)) {
        return current;
      }
      return {
        ...current,
        live_monitored_channel_ids: [...current.live_monitored_channel_ids, channel.id].sort((left, right) => left - right),
      };
    });
    setLiveMonitorQuery("");
    setSyncDirty(true);
  }

  function removeLiveMonitorChannel(channelId: number) {
    setSyncDraft((current) => ({
      ...current,
      live_monitored_channel_ids: current.live_monitored_channel_ids.filter((value) => value !== channelId),
    }));
    setSyncDirty(true);
  }

  async function saveAccount() {
    if (!profile) return;
    setSavingAccount(true);
    try {
      const next = await api.updateProfile({
        display_name: accountDisplayName,
        avatar_url: accountAvatarUrl || null,
      });
      onProfileChange(next);
      pushToast("success", "Account updated");
    } catch (error) {
      pushToast("error", "Unable to save account", error instanceof Error ? error.message : "Unknown account error");
    } finally {
      setSavingAccount(false);
    }
  }

  async function saveAccountPassword() {
    setPasswordSaving(true);
    try {
      const next = await api.changePassword({
        current_password: currentPassword,
        password: newPassword,
      });
      onProfileChange(next);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      pushToast("success", "Password updated");
    } catch (error) {
      pushToast("error", "Unable to update password", extractApiErrorMessage(error, "Unknown password error"));
    } finally {
      setPasswordSaving(false);
    }
  }

  async function saveProfilePermissions() {
    const updates = adminProfiles.filter((item) => {
      const draft = profilePermissionDrafts[item.id];
      return draft != null && draft !== (item.is_admin ? "admin" : "user");
    });
    if (!updates.length) return;

    setProfilePermissionsSaving(true);
    try {
      let refreshedProfile = profile;
      for (const item of updates) {
        const updated = await api.updateProfilePermissions(item.id, {
          is_admin: profilePermissionDrafts[item.id] === "admin",
        });
        if (profile?.id === updated.id) {
          refreshedProfile = updated;
        }
      }
      profilesState.setData(await api.profiles());
      if (refreshedProfile && profile?.id === refreshedProfile.id) {
        onProfileChange(refreshedProfile);
      }
      pushToast("success", "User permissions updated");
    } catch (error) {
      pushToast("error", "Unable to update user permissions", extractApiErrorMessage(error, "Unknown permissions error"));
    } finally {
      setProfilePermissionsSaving(false);
    }
  }

  async function deleteUserProfile(userId: number) {
    setDeletingProfileId(userId);
    try {
      await api.deleteProfile(userId);
      profilesState.setData(await api.profiles());
      setProfilePermissionDrafts((current) => {
        const next = { ...current };
        delete next[userId];
        return next;
      });
      if (pendingDeleteProfile?.id === userId) {
        setPendingDeleteProfile(null);
      }
      pushToast("success", "User deleted");
    } catch (error) {
      pushToast("error", "Unable to delete user", extractApiErrorMessage(error, "Unknown delete user error"));
    } finally {
      setDeletingProfileId(null);
    }
  }

  async function triggerScan() {
    setScanPending(true);
    pushToast("info", "Library scan started");
    try {
      await api.scan();
      await refreshServerState();
      pushToast("success", "Library scan finished");
    } catch (error) {
      pushToast("error", "Library scan failed", error instanceof Error ? error.message : "Unknown scan error");
    } finally {
      setScanPending(false);
    }
  }

  async function triggerSync() {
    setSyncPending(true);
    pushToast("info", "Library sync started");
    try {
      const result: any = await api.syncLibrary();
      await refreshServerState();
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast("error", "Library sync completed with issues", result?.details?.warning ?? result?.details?.error ?? "Check logs");
      } else {
        pushToast("success", "Library sync finished");
      }
    } catch (error) {
      pushToast("error", "Library sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setSyncPending(false);
    }
  }

  async function triggerForceSync() {
    setForceSyncPending(true);
    pushToast("info", "Forced library sync started");
    try {
      const result: any = await api.syncLibrary({ force: true });
      await refreshServerState();
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast("error", "Forced sync completed with issues", result?.details?.warning ?? result?.details?.error ?? "Check logs");
      } else {
        pushToast("success", "Forced library sync finished");
      }
    } catch (error) {
      pushToast("error", "Forced library sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setForceSyncPending(false);
    }
  }

  async function triggerChannelSync() {
    setChannelSyncPending(true);
    pushToast("info", "Channel sync started");
    try {
      const channels = await api.channels();
      const eligibleChannels = channels.filter((channel: any) => channel.slug !== "unknown-channel");
      for (const channel of eligibleChannels) {
        await api.syncChannel(channel.id);
      }
      await refreshServerState();
      pushToast("success", `Channel sync finished for ${eligibleChannels.length} channel${eligibleChannels.length === 1 ? "" : "s"}`);
    } catch (error) {
      pushToast("error", "Channel sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setChannelSyncPending(false);
    }
  }

  async function triggerOrphanSync() {
    setOrphanSyncPending(true);
    pushToast("info", "Orphan sync started");
    try {
      const result: any = await api.syncOrphans();
      await refreshServerState();
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast("error", "Orphan sync completed with issues", result?.details?.warning ?? result?.details?.error ?? "Check logs");
      } else {
        pushToast("success", "Orphan sync finished");
      }
    } catch (error) {
      pushToast("error", "Orphan sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setOrphanSyncPending(false);
    }
  }

  async function stopTranscode(jobId: number) {
    setStoppingTranscodeId(jobId);
    try {
      await api.stopTranscode(jobId);
      await refreshServerState();
      pushToast("success", "Transcode stopped");
    } catch (error) {
      pushToast("error", "Unable to stop transcode", error instanceof Error ? error.message : "Unknown transcode error");
    } finally {
      setStoppingTranscodeId(null);
    }
  }

  async function uploadAvatar(file: File | null) {
    if (!file) return;
    try {
      const result = await readFileAsDataUrl(file);
      setAvatarCropDraft({ zoom: DEFAULT_AVATAR_ZOOM, offsetX: 0, offsetY: 0 });
      setAvatarCropSource(result);
    } catch (error) {
      pushToast("error", "Unable to read avatar image", error instanceof Error ? error.message : "Unknown avatar error");
    }
  }

  function handleAvatarCropPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const node = avatarCropStageRef.current;
    if (!node) return;
    avatarCropDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startOffsetX: avatarCropDraft.offsetX,
      startOffsetY: avatarCropDraft.offsetY,
      startZoom: avatarCropDraft.zoom,
    };
    setAvatarCropDragging(true);
    node.setPointerCapture(event.pointerId);
  }

  function handleAvatarCropPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = avatarCropDragRef.current;
    const node = avatarCropStageRef.current;
    if (!drag || !node || drag.pointerId !== event.pointerId) return;

    const rect = node.getBoundingClientRect();
    const movableWidth = Math.max(1, rect.width / 2);
    const movableHeight = Math.max(1, rect.height / 2);
    const deltaX = event.clientX - drag.startX;
    const deltaY = event.clientY - drag.startY;

    setAvatarCropDraft((current) =>
      clampAvatarCropDraft({
        ...current,
        offsetX: drag.startOffsetX - (deltaX / movableWidth) * 100,
        offsetY: drag.startOffsetY - (deltaY / movableHeight) * 100,
      }),
    );
  }

  function handleAvatarCropWheel(event: ReactWheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const delta = event.deltaY < 0 ? 0.08 : -0.08;
    setAvatarCropDraft((current) =>
      clampAvatarCropDraft({
        ...current,
        zoom: Number((current.zoom + delta).toFixed(2)),
      }),
    );
  }

  function endAvatarCropDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const node = avatarCropStageRef.current;
    const drag = avatarCropDragRef.current;
    if (node && drag && drag.pointerId === event.pointerId && node.hasPointerCapture(event.pointerId)) {
      node.releasePointerCapture(event.pointerId);
    }
    avatarCropDragRef.current = null;
    setAvatarCropDragging(false);
  }

  async function applyAvatarCrop() {
    if (!avatarCropSource) return;
    try {
      const cropped = await cropAvatarDataUrl(avatarCropSource, avatarCropDraft);
      setAccountAvatarUrl(cropped);
      setAvatarCropSource(null);
      if (avatarInputRef.current) {
        avatarInputRef.current.value = "";
      }
    } catch (error) {
      pushToast("error", "Unable to crop avatar", error instanceof Error ? error.message : "Unknown crop error");
    }
  }

  if (!profile) {
    return <SettingsPageSkeleton />;
  }

  const updateStatus = updateStatusState.data;
  const updateBadgeVisible = Boolean(isAdmin && updateStatus?.update_available);
  const updateCommand = updateStatus?.update_command?.trim() || "halcyon update";
  const isManualUpdateCommand =
    updateCommand.includes("git pull") || updateCommand.includes("docker compose");

  async function handleUpdateAction() {
    try {
      await copyTextToClipboard(updateCommand);
      pushToast(
        "info",
        "Update command copied",
        isManualUpdateCommand
          ? `From the halcyon root, run "${updateCommand}".`
          : `Run "${updateCommand}" in a terminal on the machine hosting halcyon.`,
      );
    } catch (error) {
      pushToast("error", "Unable to copy update command", error instanceof Error ? error.message : "Unknown clipboard error");
    }
  }

  return (
    <div className="page-stack settings-page">
      <div className="settings-shell">
        <aside className="settings-sidebar" aria-label="Settings navigation">
          <div className="settings-sidebar-rail">
            <div className="settings-sidebar-title">Settings</div>
            <div className="settings-tabs" role="tablist" aria-label="Settings sections">
              <button className={`settings-tab ${activeTab === "user" ? "active" : ""}`} type="button" onClick={() => setActiveTab("user")}>
                User settings
              </button>
              {isAdmin ? (
                <>
                  <button className={`settings-tab ${activeTab === "admin" ? "active" : ""}`} type="button" onClick={() => setActiveTab("admin")}>
                    Admin settings
                  </button>
                  <button className={`settings-tab ${activeTab === "server" ? "active" : ""}`} type="button" onClick={() => setActiveTab("server")}>
                    Server settings
                  </button>
                  <button className={`settings-tab ${activeTab === "retention" ? "active" : ""}`} type="button" onClick={() => setActiveTab("retention")}>
                    <span>Retention</span>
                    <span className="settings-beta-badge">Beta</span>
                  </button>
                  <button className={`settings-tab ${activeTab === "logs" ? "active" : ""}`} type="button" onClick={() => setActiveTab("logs")}>
                    Logs
                  </button>
                </>
              ) : null}
            </div>
            <button
              type="button"
              className={`settings-version-note ${updateBadgeVisible ? "has-update" : ""}`}
              onClick={() => setUpdateModalOpen(true)}
              data-tooltip={updateBadgeVisible ? "Update available" : "Version and update info"}
              aria-label={updateBadgeVisible ? "Open update details" : "Open version details"}
            >
              <span>Version {__APP_VERSION__}</span>
              {updateBadgeVisible ? (
                <span className="settings-update-badge" aria-hidden="true">
                  !
                </span>
              ) : null}
            </button>
          </div>
        </aside>
        <div className="settings-main">

      {activeTab === "user" ? (
        <div className="settings-sections">
          <section className="settings-section">
            <div className="section-heading">
              <h2>User settings</h2>
            </div>
            <div className="settings-form-grid">
              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong className={settingLabelClass("Continue into the next queued or suggested video.")} data-tooltip="Continue into the next queued or suggested video." tabIndex={0}>Autoplay</strong>
                  </div>
                </div>
                <button
                  type="button"
                  className={`switch switch-button ${preferences.autoplay ? "is-on" : ""}`}
                  role="switch"
                  aria-checked={preferences.autoplay}
                  aria-label="Toggle autoplay"
                  onClick={() => onPreferencesChange({ ...preferences, autoplay: !preferences.autoplay })}
                >
                  <span className="switch-slider" />
                </button>
              </div>

              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong className={settingLabelClass("Adjust player volume with the mouse wheel while hovering the video.")} data-tooltip="Adjust player volume with the mouse wheel while hovering the video." tabIndex={0}>Mousewheel volume control</strong>
                  </div>
                </div>
                <button
                  type="button"
                  className={`switch switch-button ${preferences.mousewheelVolumeControl ? "is-on" : ""}`}
                  role="switch"
                  aria-checked={preferences.mousewheelVolumeControl}
                  aria-label="Toggle mousewheel volume control"
                  onClick={() => onPreferencesChange({ ...preferences, mousewheelVolumeControl: !preferences.mousewheelVolumeControl })}
                >
                  <span className="switch-slider" />
                </button>
              </div>

              <div className="settings-inline-grid settings-inline-grid-fields account-preferences-grid">
                <label className="settings-field">
                  <span>Density</span>
                  <SettingsMenuSelect
                    label="Density"
                    value={preferences.density}
                    options={[
                      { value: "relaxed", label: "Relaxed" },
                      { value: "comfortable", label: "Comfortable" },
                      { value: "compact", label: "Compact" },
                    ]}
                    onChange={(next) =>
                      onPreferencesChange({
                        ...preferences,
                        density: next,
                      })
                    }
                  />
                </label>

                <label className="settings-field">
                  <span>Default player setting</span>
                  <SettingsMenuSelect
                    label="Default player setting"
                    value={preferences.defaultPlayerMode}
                    options={[
                      { value: "last-used", label: "Last used" },
                      { value: "default", label: "Default view" },
                      { value: "theater", label: "Theater" },
                    ]}
                    onChange={(next) =>
                      onPreferencesChange({
                        ...preferences,
                        defaultPlayerMode: next,
                      })
                    }
                  />
                </label>
              </div>
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Account</h2>
            </div>
            <div className="account-settings-flat">
              <div className="settings-account-preview">
                <div className="settings-avatar-editor">
                  <button
                    className="settings-avatar-button"
                    type="button"
                    onClick={() => avatarInputRef.current?.click()}
                    aria-label="Upload avatar image"
                  >
                    <AvatarImage
                      src={accountAvatarUrl}
                      seed={profile?.name ?? "user"}
                      alt={accountDisplayName || profile?.display_name || "User"}
                      fallbackText={(accountDisplayName || profile?.display_name || "US").slice(0, 2).toUpperCase()}
                      className="settings-avatar-image"
                    />
                    <span className="settings-avatar-upload">
                      <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                      </svg>
                    </span>
                  </button>
                  <input
                    ref={avatarInputRef}
                    className="settings-avatar-input"
                    type="file"
                    accept="image/*"
                    onChange={(event) => void uploadAvatar(event.target.files?.[0] ?? null)}
                  />
                </div>
                <div className="menu-account-copy">
                  <strong>{accountDisplayName || profile?.display_name || "User"}</strong>
                  <small>@{profile?.name ?? "unknown"}</small>
                </div>
              </div>
              <div className="settings-form-grid">
                <label className="settings-field">
                  <span>Display name</span>
                  <input value={accountDisplayName} onChange={(event) => setAccountDisplayName(event.target.value)} />
                </label>
              </div>
              <div className="settings-actions-row">
                <button
                  className="ghost-button settings-utility-button"
                  disabled={savingAccount || !accountDisplayName.trim()}
                  onClick={() => void saveAccount()}
                >
                  {savingAccount ? "Saving..." : "Save user settings"}
                </button>
              </div>
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Security</h2>
            </div>
            <p className="settings-section-note">
              Change your account password here. Password resets still use the permanent account PIN that was chosen when this account was created.
            </p>
            <div className="settings-form-grid">
              <label className="settings-field account-security-current-field">
                <span>Current password</span>
                <input
                  type="password"
                  value={currentPassword}
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  placeholder="Current password"
                />
              </label>
              <div className="settings-inline-grid settings-inline-grid-fields account-security-grid">
                <label className="settings-field">
                  <span>New password</span>
                  <input
                    type="password"
                    value={newPassword}
                    onChange={(event) => setNewPassword(event.target.value)}
                    placeholder="New password"
                  />
                </label>
                <label className="settings-field">
                  <span>Confirm new password</span>
                  <input
                    type="password"
                    value={confirmPassword}
                    onChange={(event) => setConfirmPassword(event.target.value)}
                    placeholder="Confirm new password"
                  />
                </label>
              </div>
            </div>
            <div className="settings-actions-row">
              <button
                className="ghost-button settings-utility-button"
                type="button"
                disabled={passwordSaving || !currentPassword || newPassword.length < 8 || confirmPassword !== newPassword}
                onClick={() => void saveAccountPassword()}
              >
                {passwordSaving ? "Saving..." : "Update password"}
              </button>
              <small className="muted-copy">
                {confirmPassword && confirmPassword !== newPassword
                  ? "Confirm password must match exactly"
                  : "Passwords must be at least 8 characters"}
              </small>
            </div>
          </section>
        </div>
      ) : null}

      {activeTab === "server" ? (
        <div className="settings-sections">
          <section className="settings-section">
            <div className="section-heading">
              <div className="settings-heading-copy">
                <h2>Library</h2>
                {storageState.data ? (
                  <>
                    {libraryStorageSegments ? (
                      <>
                        <div className="library-storage-bar" aria-hidden="true">
                          <span
                            className="library-storage-segment is-halcyon"
                            style={{ width: `${libraryStorageSegments.libraryPercent}%` }}
                          />
                          <span
                            className="library-storage-segment is-other"
                            style={{ width: `${libraryStorageSegments.otherPercent}%` }}
                          />
                          <span
                            className="library-storage-segment is-free"
                            style={{ width: `${libraryStorageSegments.availablePercent}%` }}
                          />
                        </div>
                        <div className="library-storage-legend">
                          <span><i className="library-storage-dot is-halcyon" />halcyon {formatBytes(libraryStorageSegments.libraryBytes)}</span>
                          <span><i className="library-storage-dot is-other" />Other {formatBytes(libraryStorageSegments.otherBytes)}</span>
                          <span><i className="library-storage-dot is-free" />Free {formatBytes(libraryStorageSegments.availableBytes)}</span>
                        </div>
                      </>
                    ) : null}
                  </>
                ) : null}
              </div>
              <button className="ghost-button settings-utility-button" onClick={() => void triggerScan()} disabled={scanPending}>
                {scanPending ? "Scanning..." : "Scan library"}
              </button>
            </div>
            <div className="root-stack settings-flat-stack">
              {(rootsState.data ?? []).map((root) => {
                const folders = selectedByRoot.get(root.id) ?? [];
                const browserOpen = browserRootId === root.id;
                const hasImplicitRootSelection = folders.some((item) => item.id < 0 && item.relative_path === "");
                return (
                  <div className="library-root-flat" key={root.id}>
                    <div className="library-root-header">
                      <div className="library-root-copy">
                        <strong>{root.path}</strong>
                        <small className="muted-copy">
                          {root.selected_count} folder{root.selected_count === 1 ? "" : "s"} selected • {root.item_count} item{root.item_count === 1 ? "" : "s"} loaded
                        </small>
                      </div>
                      {!hasImplicitRootSelection ? (
                        <button className="icon-button" type="button" aria-label="Browse root" onClick={() => setBrowserRootId(browserOpen ? null : root.id)}>
                          <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
                            <path d={browserOpen ? "M7 14l5-5 5 5" : "M7 10l5 5 5-5"} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </button>
                      ) : null}
                    </div>
                    {hasImplicitRootSelection ? (
                      <div className="selected-folder-list">
                        <small className="muted-copy">This mounted root is active as the entire library by default.</small>
                      </div>
                    ) : browserOpen ? (
                      <div className="library-picker-grid">
                        {(browserState.data?.directories ?? []).map((directory: { name: string; relative_path: string }) => {
                          const selected = folders.some((item) => item.relative_path === directory.relative_path);
                          return (
                            <button
                              key={directory.relative_path}
                              className={`library-folder-chip ${selected ? "active-chip" : ""}`}
                              disabled={selected}
                              onClick={async () => {
                                await api.addSelectedFolder({ root_id: root.id, relative_path: directory.relative_path });
                                await refreshServerState();
                              }}
                            >
                              {directory.name}
                            </button>
                          );
                        })}
                      </div>
                    ) : null}
                    <div className="selected-folder-list">
                      {folders.map((folder) => (
                        <div className="selected-folder-row" key={folder.id}>
                          <span>{folder.relative_path || "Entire root"}</span>
                          {folder.id > 0 ? (
                            <button
                              className="linkish"
                              onClick={async () => {
                                await api.deleteSelectedFolder(folder.id);
                                await refreshServerState();
                              }}
                            >
                              Remove
                            </button>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Sync</h2>
              <div className="settings-actions-row settings-heading-actions">
                <button className="ghost-button settings-utility-button" onClick={() => void triggerSync()} disabled={syncPending}>
                  {syncPending ? "Syncing..." : "Sync now"}
                </button>
                <button className="ghost-button settings-utility-button" onClick={() => void triggerChannelSync()} disabled={channelSyncPending}>
                  {channelSyncPending ? "Syncing..." : "Sync channels"}
                </button>
                <button className="ghost-button settings-utility-button" onClick={() => void triggerOrphanSync()} disabled={orphanSyncPending}>
                  {orphanSyncPending ? "Syncing..." : "Sync orphans"}
                </button>
              </div>
            </div>
              <div className="settings-form-grid">
              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong className={settingLabelClass("Detect added or deleted library files and sync new orphan videos automatically.")} data-tooltip="Detect added or deleted library files and sync new orphan videos automatically." tabIndex={0}>Automatic onboarding</strong>
                  </div>
                </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.automatic_detection_enabled ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.automatic_detection_enabled}
                    aria-label="Toggle automatic onboarding"
                    onClick={() => {
                      setSyncDraft((current) => ({ ...current, automatic_detection_enabled: !current.automatic_detection_enabled }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong className={settingLabelClass("Keep matched items refreshed when the worker runs.")} data-tooltip="Keep matched items refreshed when the worker runs." tabIndex={0}>Automatic sync</strong>
                  </div>
                </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.automatic_sync_enabled ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.automatic_sync_enabled}
                    aria-label="Toggle automatic sync"
                    onClick={() => {
                      setSyncDraft((current) => ({ ...current, automatic_sync_enabled: !current.automatic_sync_enabled }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong
                      className={settingLabelClass("Generate AI subtitles for newly onboarded videos in the background. Existing caption files are always preferred over generated ones.")}
                      data-tooltip="Generate AI subtitles for newly onboarded videos in the background. Existing caption files are always preferred over generated ones."
                      tabIndex={0}
                    >
                      Generate subtitles during onboarding
                    </strong>
                  </div>
                  <small className="muted-copy">
                    {syncState.data?.last_subtitle_sync_at
                      ? `Last subtitle pass ${formatRelativeDate(syncState.data.last_subtitle_sync_at) ?? "recently"}`
                      : "Uses the bundled Whisper sidecar and writes .vtt files next to videos that do not already have captions. Turning this on also backfills older videos automatically."}
                  </small>
                </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.subtitle_generation_enabled ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.subtitle_generation_enabled}
                    aria-label="Toggle subtitle generation during onboarding"
                    onClick={() => {
                      setSyncDraft((current) => ({ ...current, subtitle_generation_enabled: !current.subtitle_generation_enabled }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
                <label className="settings-field settings-subfield-block">
                  <span className={settingLabelClass("How often halcyon polls selected folders for new, changed, or removed files. Lower values increase disk activity. Default is 30 seconds.")} data-tooltip="How often halcyon polls selected folders for new, changed, or removed files. Lower values increase disk activity. Default is 30 seconds." tabIndex={0}>
                    halcyon scan interval
                  </span>
                  <input
                    type="number"
                    min={5}
                    max={3600}
                    value={syncDraft.scan_interval_seconds}
                    onChange={(event) => {
                      setSyncDraft((current) => ({
                        ...current,
                        scan_interval_seconds: Number(event.target.value) || 30,
                      }));
                      setSyncDirty(true);
                    }}
                  />
                </label>
                <div className="switch-row">
                  <div className="settings-copy">
                    <div className="settings-label-row">
                      <strong className={settingLabelClass("Allow the non-API fallback sync path to guess missing channel avatars and banners. This can pull incorrect channel art.")} data-tooltip="Allow the non-API fallback sync path to guess missing channel avatars and banners. This can pull incorrect channel art." tabIndex={0}>Allow fallback art path</strong>
                    </div>
                  </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.allow_fallback_art ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.allow_fallback_art}
                    aria-label="Toggle fallback art path"
                    onClick={() => {
                      setSyncDraft((current) => ({ ...current, allow_fallback_art: !current.allow_fallback_art }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
                <div className="switch-row">
                  <div className="settings-copy">
                    <strong>Prefer high resolution banners</strong>
                  </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.prefer_high_res_banners ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.prefer_high_res_banners}
                    aria-label="Toggle high resolution banners"
                    onClick={() => {
                      setSyncDraft((current) => ({ ...current, prefer_high_res_banners: !current.prefer_high_res_banners }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
                <div className="switch-row">
                  <div className="settings-copy">
                    <div className="settings-label-row">
                      <strong
                        className={settingLabelClass("Show a Live tab and only check selected channels for active livestreams.")}
                        data-tooltip="Show a Live tab and only check selected channels for active livestreams."
                        tabIndex={0}
                      >
                        Live tab
                      </strong>
                    </div>
                    <small className="muted-copy">
                      {syncState.data?.last_live_sync_at
                        ? `Last live check ${formatRelativeDate(syncState.data.last_live_sync_at) ?? "recently"}`
                        : "Enabled by default. Select which channels Halcyon should monitor for streams."}
                    </small>
                  </div>
                  <button
                    type="button"
                    className={`switch switch-button ${syncDraft.live_tab_enabled ? "is-on" : ""}`}
                    role="switch"
                    aria-checked={syncDraft.live_tab_enabled}
                    aria-label="Toggle live tab"
                    onClick={() => {
                      setSyncDraft((current) => ({
                        ...current,
                        live_tab_enabled: !current.live_tab_enabled,
                      }));
                      setSyncDirty(true);
                    }}
                  >
                    <span className="switch-slider" />
                  </button>
                </div>
                {syncDraft.live_tab_enabled ? (
                  <div className="settings-live-monitor-panel">
                    <div className="settings-live-monitor-header">
                      <strong>Monitored livestream channels</strong>
                      <span>{syncDraft.live_monitored_channel_ids.length} selected</span>
                    </div>
                    <small className="muted-copy">
                      Only selected channels are checked for livestreams. Saving this list runs a fresh live check right away. Channels still need a valid YouTube match before they can appear in Live.
                    </small>
                    {channelsState.loading ? (
                      <div className="search-results-empty">Loading channels...</div>
                    ) : liveMonitorChannels.length ? (
                      <>
                        <label className="settings-field settings-live-monitor-search-field">
                          <span>Add channel</span>
                          <input
                            type="text"
                            value={liveMonitorQuery}
                            onChange={(event) => setLiveMonitorQuery(event.target.value)}
                            placeholder="Search channels to monitor for livestreams"
                          />
                        </label>
                        {liveMonitorQuery.trim() ? (
                          liveMonitorSearchResults.length ? (
                            <div className="settings-live-monitor-search-results">
                              {liveMonitorSearchResults.map((channel) => (
                                <button
                                  key={channel.id}
                                  type="button"
                                  className="settings-live-monitor-item"
                                  onClick={() => addLiveMonitorChannel(channel)}
                                >
                                  <AvatarImage
                                    src={channel.avatar_url ?? null}
                                    alt={channel.name}
                                    fallbackText={channel.name}
                                    className="settings-live-monitor-avatar"
                                  />
                                  <span className="settings-live-monitor-copy">
                                    <strong>{channel.name}</strong>
                                    <small>{channel.video_count.toLocaleString()} videos indexed</small>
                                  </span>
                                  <span className="settings-live-monitor-action">Add</span>
                                </button>
                              ))}
                            </div>
                          ) : (
                            <div className="search-results-empty">No channels match that search.</div>
                          )
                        ) : (
                          <div className="settings-field-hint">Search for a channel above, then add it to the monitored list.</div>
                        )}
                        {selectedLiveMonitorChannels.length ? (
                          <div className="settings-live-monitor-selected-wrap">
                            <div className="settings-live-monitor-list">
                              {selectedLiveMonitorChannels.map((channel) => (
                                <div className="settings-live-monitor-item is-selected" key={channel.id}>
                                  <AvatarImage
                                    src={channel.avatar_url ?? null}
                                    alt={channel.name}
                                    fallbackText={channel.name}
                                    className="settings-live-monitor-avatar"
                                  />
                                  <span className="settings-live-monitor-copy">
                                    <strong>{channel.name}</strong>
                                    <small>{channel.video_count.toLocaleString()} videos indexed</small>
                                  </span>
                                  <button
                                    type="button"
                                    className="ghost-button settings-live-monitor-remove"
                                    onClick={() => removeLiveMonitorChannel(channel.id)}
                                  >
                                    Remove
                                  </button>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : (
                          <div className="search-results-empty">No channels selected yet.</div>
                        )}
                      </>
                    ) : (
                      <div className="search-results-empty">Add at least one channel to Halcyon before selecting livestream monitoring.</div>
                    )}
                  </div>
                ) : null}
                <label className="settings-field">
                  <span>YouTube API key</span>
                  <input
                    type="text"
                    name="halcyon-youtube-api-key"
                    autoComplete="off"
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    data-1p-ignore="true"
                    data-lpignore="true"
                    value={syncDraft.youtube_api_key}
                    placeholder={youtubeApiKeyConfigured && !clearYoutubeApiKeyRequested ? "API key is set." : "Paste a YouTube Data API key"}
                    onChange={(event) => {
                      setSyncDraft((current) => ({ ...current, youtube_api_key: event.target.value }));
                      setClearYoutubeApiKeyRequested(false);
                      setSyncDirty(true);
                    }}
                  />
                </label>
                {youtubeApiKeyConfigured && !clearYoutubeApiKeyRequested ? (
                  <div className="settings-field-hint-row">
                    <span className="settings-field-hint">A key is stored. Enter a new key to replace it.</span>
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={() => {
                        setSyncDraft((current) => ({ ...current, youtube_api_key: "" }));
                        setClearYoutubeApiKeyRequested(true);
                        setSyncDirty(true);
                      }}
                    >
                      Clear saved key
                    </button>
                  </div>
                ) : null}
                <div className="settings-quota-meter" aria-label="Estimated YouTube API usage">
                  <div className="settings-quota-meter-header">
                    <strong>Estimated daily YouTube API quota</strong>
                    <span>{youtubeQuotaSummary.remainingUnits.toLocaleString()} remaining</span>
                  </div>
                  <div className="settings-quota-meter-bar" aria-hidden="true">
                    <div
                      className={`settings-quota-meter-fill ${youtubeQuotaSummary.remainingPercent >= 100 ? "is-full" : "is-partial"}`}
                      style={{ width: `${youtubeQuotaSummary.remainingPercent}%` }}
                    />
                  </div>
                  <div className="settings-quota-meter-meta">
                    <span>
                      {youtubeQuotaSummary.usedUnits.toLocaleString()} used today --{" "}
                      {youtubeQuotaSummary.estimated ? "estimated from halcyon requests. Resets daily." : "live quota usage."}
                    </span>
                  </div>
                </div>
                <div className="settings-inline-grid settings-inline-grid-fields settings-sync-inline-grid">
                  <label className="settings-field">
                    <span>Max Comments</span>
                    <input type="number" min={1} max={100} value={syncDraft.comment_limit} onChange={(event) => { setSyncDraft((current) => ({ ...current, comment_limit: Number(event.target.value) || 1 })); setSyncDirty(true); }} />
                  </label>
                  <label className="settings-field">
                    <span className={settingLabelClass("How many replies to pull per top-level comment. Higher values may require extra YouTube API requests. Use 0 to skip replies.")} data-tooltip="How many replies to pull per top-level comment. Higher values may require extra YouTube API requests. Use 0 to skip replies." tabIndex={0}>Max Replies</span>
                    <input type="number" min={0} value={syncDraft.max_replies_per_comment} onChange={(event) => { setSyncDraft((current) => ({ ...current, max_replies_per_comment: Math.max(0, Number(event.target.value) || 0) })); setSyncDirty(true); }} />
                  </label>
                  <label className="settings-field">
                    <span className={settingLabelClass("Going above the default can cause rate limits on your network.")} data-tooltip="Going above the default can cause rate limits on your network." tabIndex={0}>Requests/sec</span>
                    <input type="number" min={1} max={10} value={syncDraft.requests_per_second} onChange={(event) => { setSyncDraft((current) => ({ ...current, requests_per_second: Number(event.target.value) || 1 })); setSyncDirty(true); }} />
                  </label>
                </div>
                <div className="settings-actions-row">
                  <button className="ghost-button settings-utility-button" disabled={!syncDirty || syncSaving} onClick={() => void saveSyncSettings()}>
                    {syncSaving ? "Saving..." : "Save sync settings"}
                  </button>
                  {syncDirty || subtitleBackfillActive ? (
                    <small className="muted-copy">
                      {syncDirty
                        ? "Unsaved changes"
                        : subtitleBackfillActive
                          ? (subtitleBackfillLabel ?? "Backfilling subtitles for existing videos...")
                          : null}
                    </small>
                  ) : null}
                </div>
              </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Transcodes</h2>
              <small className="muted-copy">{activeTranscodes.length} active</small>
            </div>
            <div className="transcode-list">
              {activeTranscodes.map((item: TranscodeItem) => (
                <div className="transcode-row" key={item.id}>
                  <div className="transcode-copy">
                    <strong>
                      {item.title ?? `Video ${item.video_id}`}
                      {item.throttled ? <span className="transcode-warning-inline">throttled</span> : null}
                    </strong>
                    <small>{item.profile} • PID {item.pid ?? "n/a"}</small>
                  </div>
                  <button className="ghost-button" disabled={stoppingTranscodeId === item.id} onClick={() => void stopTranscode(item.id)}>
                    {stoppingTranscodeId === item.id ? "Stopping..." : "Stop"}
                  </button>
                </div>
              ))}
              {!activeTranscodes.length ? <div className="search-results-empty">No active transcodes.</div> : null}
            </div>
          </section>
        </div>
      ) : null}

      {activeTab === "logs" ? (
        <div className="settings-sections">
          <section className="settings-section">
            <div className="section-heading">
              <h2>Sync activity</h2>
              {activeSyncJobs.length ? <small className="muted-copy">Sync in progress</small> : null}
            </div>
            <div className="logs-stack-panel sync-activity-panel">
              <div className="sync-activity-list sync-activity-scroll" ref={syncActivityRef} onScroll={handleSyncActivityScroll}>
                {visibleSyncActivityItems.map((item) => (
                  <div className={`sync-activity-card tone-${item.tone}`} key={`${item.timestamp}-${item.title}-${item.detail}`}>
                    <div className="sync-activity-card-top">
                      <strong>{item.title}</strong>
                      {item.score ? <span className="sync-activity-score">{item.score}</span> : null}
                    </div>
                    <div className="sync-activity-card-meta">
                      <small>{item.detail}</small>
                      <span className="muted-copy sync-activity-timestamp">{item.timestamp}</span>
                    </div>
                  </div>
                ))}
                {!syncActivityItems.length ? <div className="search-results-empty">No sync activity yet.</div> : null}
              </div>
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Sync raw logs</h2>
            </div>
            <div className="activity-log terminal-log logs-terminal" ref={syncLogRef} onScroll={handleSyncScroll}>
              {syncLogLines.map((line) => (
                <div key={line}>{line}</div>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Activity logs</h2>
            </div>
            <div className="activity-log terminal-log logs-terminal" ref={activityLogRef} onScroll={handleActivityScroll}>
              {activityLines.map((line) => (
                <div key={line}>{line}</div>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Uploads</h2>
            </div>
            <div className="new-uploads-panel" ref={uploadsPanelRef} onScroll={handleUploadsScroll}>
              {recentUploads.map((video) => {
                const status = uploadMetadataStatus(video);
                const syncScore = formatSyncScore(video.youtube_match_confidence);
                return (
                  <article className="new-upload-card" key={video.id}>
                    <img className="new-upload-thumb" src={video.thumbnail_url ?? `/api/videos/${video.id}/thumbnail`} alt="" />
                    <div className="new-upload-copy">
                      <div className="new-upload-title-row">
                        <strong>{video.title}</strong>
                        {syncScore ? <span className="new-upload-match-score">{syncScore}</span> : null}
                      </div>
                      <small>
                        {(video.channel_name && isUsefulChannel(video.channel_name) ? video.channel_name : "Unknown channel")}
                        {video.published_at ? ` • ${formatRelativeDate(video.published_at)}` : ""}
                      </small>
                      <div className="new-upload-actions">
                        <button
                          className="ghost-button settings-utility-button"
                          type="button"
                          disabled={uploadSyncPending[video.id] != null}
                          onClick={() => void triggerVideoSync(video.id)}
                        >
                          {uploadSyncPending[video.id] === "sync" ? "Syncing..." : "Sync"}
                        </button>
                        <button
                          className="ghost-button settings-utility-button"
                          type="button"
                          disabled={uploadSyncPending[video.id] != null}
                          onClick={() => void triggerVideoSync(video.id, true)}
                        >
                          {uploadSyncPending[video.id] === "force" ? "Syncing..." : "Force sync"}
                        </button>
                        <button
                          className="ghost-button settings-utility-button"
                          type="button"
                          disabled={uploadSyncPending[video.id] != null}
                          onClick={() => void triggerVideoReview(video.id)}
                        >
                          {uploadSyncPending[video.id] === "review" ? "Sending..." : "Send to review"}
                        </button>
                      </div>
                      <div className="new-upload-progress-row">
                        <span className="new-upload-progress-label">Sync {status.percent}%</span>
                        <div className="new-upload-progress-track" aria-hidden="true">
                          <span className="new-upload-progress-fill" style={{ width: `${status.percent}%` }} />
                        </div>
                      </div>
                      <div className="metadata-badge-row">
                        {Object.entries(status.flags).map(([key, value]) => (
                          <span key={key} className={`metadata-badge ${value ? "is-complete" : "is-pending"}`}>
                            {key === "likes"
                              ? "Like count"
                              : key === "dislikes"
                                ? "Dislike count"
                                : key.charAt(0).toUpperCase() + key.slice(1)}
                          </span>
                        ))}
                      </div>
                    </div>
                  </article>
                );
              })}
              {uploadsLoadingMore ? <div className="search-results-empty">Loading more uploads...</div> : null}
              {!recentUploads.length ? <div className="search-results-empty">No uploads indexed yet.</div> : null}
            </div>
          </section>
        </div>
      ) : null}
      {activeTab === "retention" ? (
        <div className="settings-sections">
          <section className="settings-section">
            <div className="section-heading">
              <div className="settings-heading-copy">
                <h2>Retention</h2>
                <small className="muted-copy">
                  Reclaimed {formatBytes(retentionState.data?.stats.reclaimed_bytes ?? 0)} so far
                </small>
              </div>
              <div className="settings-actions-row settings-heading-actions">
                <button
                  className="ghost-button settings-utility-button"
                  type="button"
                  disabled={retentionRunning || retentionDeleting || retentionReverting}
                  onClick={() => void runRetention()}
                >
                  {retentionRunning ? "Running..." : "Run retention wizard"}
                </button>
              </div>
            </div>
            <p className="settings-section-note">
              Retention stages videos based on when they were added to your library, not when they were uploaded. Eligible files are moved into the pre-delete retention folder, held there for one hour, and then permanently deleted unless you revert the run first.
            </p>
            <div className="settings-form-grid">
              <div className="switch-row">
                <div className="settings-copy">
                  <div className="settings-label-row">
                    <strong>Enable retention</strong>
                  </div>
                  <small className="muted-copy">Saved videos and saved series are automatically exempt.</small>
                </div>
                <button
                  type="button"
                  className={`switch switch-button ${retentionDraft.enabled ? "is-on" : ""}`}
                  role="switch"
                  aria-checked={retentionDraft.enabled}
                  aria-label="Toggle retention"
                  onClick={() => {
                    setRetentionDraft((current) => ({ ...current, enabled: !current.enabled }));
                    setRetentionDirty(true);
                  }}
                >
                  <span className="switch-slider" />
                </button>
              </div>
              <label className="settings-field">
                <span>Retention period</span>
                <div className="settings-inline-unit">
                  <input
                    type="number"
                    min={1}
                    max={3650}
                    value={retentionDraft.retention_days}
                    onChange={(event) => {
                      setRetentionDraft((current) => ({
                        ...current,
                        retention_days: Number(event.target.value) || 1,
                      }));
                      setRetentionDirty(true);
                    }}
                  />
                  <span className="settings-inline-unit-label">day(s)</span>
                </div>
              </label>
              <div className="settings-field">
                <span>Retention frequency</span>
                <div className="retention-frequency-editor">
                  <SettingsMenuSelect<RetentionScheduleKind>
                    value={retentionDraft.auto_schedule_kind}
                    options={RETENTION_SCHEDULE_OPTIONS}
                    onChange={(value) => {
                      setRetentionDraft((current) => ({ ...current, auto_schedule_kind: value }));
                      setRetentionDirty(true);
                    }}
                    label="Retention frequency"
                  />
                  {retentionDraft.auto_schedule_kind === "interval" ? (
                    <div className="settings-inline-unit retention-frequency-inline">
                      <input
                        type="number"
                        min={5}
                        max={10080}
                        step={5}
                        value={retentionDraft.auto_interval_minutes}
                        onChange={(event) => {
                          setRetentionDraft((current) => ({
                            ...current,
                            auto_interval_minutes: Number(event.target.value) || 5,
                          }));
                          setRetentionDirty(true);
                        }}
                      />
                      <span className="settings-inline-unit-label">minute(s)</span>
                    </div>
                  ) : null}
                  {retentionDraft.auto_schedule_kind === "daily" ? (
                    <input
                      className="retention-time-input"
                      type="time"
                      value={formatRetentionTime(retentionDraft.auto_time_hour, retentionDraft.auto_time_minute)}
                      onChange={(event) => {
                        const next = parseRetentionTime(event.target.value);
                        setRetentionDraft((current) => ({
                          ...current,
                          auto_time_hour: next.hour,
                          auto_time_minute: next.minute,
                        }));
                        setRetentionDirty(true);
                      }}
                    />
                  ) : null}
                  {retentionDraft.auto_schedule_kind === "weekly" ? (
                    <div className="retention-frequency-weekly">
                      <SettingsMenuSelect<RetentionWeekdayValue>
                        value={String(retentionDraft.auto_weekday) as RetentionWeekdayValue}
                        options={RETENTION_WEEKDAY_OPTIONS}
                        onChange={(value) => {
                          setRetentionDraft((current) => ({
                            ...current,
                            auto_weekday: Number(value),
                          }));
                          setRetentionDirty(true);
                        }}
                        label="Retention weekday"
                      />
                      <input
                        className="retention-time-input"
                        type="time"
                        value={formatRetentionTime(retentionDraft.auto_time_hour, retentionDraft.auto_time_minute)}
                        onChange={(event) => {
                          const next = parseRetentionTime(event.target.value);
                          setRetentionDraft((current) => ({
                            ...current,
                            auto_time_hour: next.hour,
                            auto_time_minute: next.minute,
                          }));
                          setRetentionDirty(true);
                        }}
                      />
                    </div>
                  ) : null}
                </div>
                <small className="muted-copy retention-frequency-note">
                  Uses time zone: {(retentionState.data?.settings.auto_timezone ?? retentionDraft.auto_timezone) || FALLBACK_RETENTION_TIMEZONE_LABEL}
                </small>
              </div>
              <div className="settings-field">
                <span className={settingLabelClass("Files staged by the retention wizard are moved here for one hour before they are permanently deleted. Reverting the last retention run moves them back.")} data-tooltip="Files staged by the retention wizard are moved here for one hour before they are permanently deleted. Reverting the last retention run moves them back." tabIndex={0}>Pre-delete retention folder</span>
                <div className="retention-folder-picker" ref={retentionFolderBrowserRef}>
                  <div className="retention-folder-input-row">
                    <input
                      value={retentionDraft.staging_folder_path}
                      placeholder={retentionState.data?.effective_staging_folder ?? ""}
                      onFocus={() => setRetentionFolderBrowserOpen(true)}
                      onChange={(event) => {
                        setRetentionDraft((current) => ({
                          ...current,
                          staging_folder_path: event.target.value,
                        }));
                        setRetentionDirty(true);
                        setRetentionFolderBrowserOpen(true);
                      }}
                    />
                  </div>
                  {retentionFolderBrowserOpen ? (
                    <div className="retention-folder-browser">
                      <div className="retention-folder-browser-head">
                        <small className="muted-copy">
                          {retentionFolderBrowser?.browse_path || retentionFolderBrowser?.root_path || "Mounted root"}
                        </small>
                      </div>
                      <div className="retention-folder-browser-list">
                        {retentionFolderBrowserPending ? <div className="search-results-empty">Loading folders...</div> : null}
                        {!retentionFolderBrowserPending && retentionFolderBrowser?.parent_path ? (
                          <button
                            className="retention-folder-browser-item"
                            type="button"
                            onClick={() => void navigateRetentionFolder(retentionFolderBrowser.parent_path ?? "")}
                          >
                            <strong>...</strong>
                            <small>Go up one folder</small>
                          </button>
                        ) : null}
                        {!retentionFolderBrowserPending &&
                        (retentionFolderBrowser?.directories ?? []).map((directory) => (
                          <button
                            className="retention-folder-browser-item"
                            type="button"
                            key={directory.path}
                            onClick={() => void navigateRetentionFolder(directory.path)}
                          >
                            <strong>{directory.name}</strong>
                            <small>{directory.path}</small>
                          </button>
                        ))}
                        {!retentionFolderBrowserPending &&
                        !retentionFolderBrowserError &&
                        !(retentionFolderBrowser?.directories.length) ? (
                          <div className="search-results-empty">No folders here yet.</div>
                        ) : null}
                        {retentionFolderBrowserError ? <div className="search-results-empty">{retentionFolderBrowserError}</div> : null}
                      </div>
                      <div className="retention-folder-browser-actions">
                        {retentionNewFolderOpen ? (
                          <div className="retention-folder-create-row">
                            <input
                              value={retentionNewFolderName}
                              placeholder="New directory name"
                              onChange={(event) => setRetentionNewFolderName(event.target.value)}
                            />
                            <button
                              className="ghost-button settings-utility-button"
                              type="button"
                              disabled={retentionFolderCreating || !retentionNewFolderName.trim()}
                              onClick={() => void createRetentionDirectory()}
                            >
                              {retentionFolderCreating ? "Creating..." : "Create"}
                            </button>
                          </div>
                        ) : null}
                        <button
                          className="ghost-button settings-utility-button retention-folder-create-button"
                          type="button"
                          onClick={() => setRetentionNewFolderOpen((current) => !current)}
                        >
                          {retentionNewFolderOpen ? "Cancel new directory" : "New directory..."}
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              </div>
              <div className="retention-last-run-card">
                <div className="retention-last-run-row">
                  <strong>Last retention run</strong>
                  <div className="retention-last-run-actions">
                    <button
                      className="ghost-button settings-utility-button retention-history-toggle"
                      type="button"
                      onClick={() => setShowRetentionHistory((current) => !current)}
                    >
                      {showRetentionHistory ? "Hide history" : "Retention history"}
                    </button>
                    <span className={`retention-status-pill tone-${retentionState.data?.settings.last_run_status ?? "idle"}`}>
                      {retentionState.data?.settings.last_run_status ?? "idle"}
                    </span>
                  </div>
                </div>
                <small className="muted-copy">
                  {retentionState.data?.settings.last_run_at
                    ? `${formatRetentionTrigger(retentionState.data.settings.last_run_trigger)} • ${formatRelativeDate(retentionState.data.settings.last_run_at)}`
                    : "Retention has not run yet."}
                </small>
                <div className="retention-run-stats">
                  <span className="retention-stat-pill tone-marked">Marked <strong>{retentionState.data?.settings.last_run_marked_count ?? 0}</strong></span>
                  <span className="retention-stat-pill tone-deleted">Deleted <strong>{retentionState.data?.settings.last_run_deleted_count ?? 0}</strong></span>
                  <span className="retention-stat-pill tone-reverted">Reverted <strong>{retentionState.data?.settings.last_run_reverted_count ?? 0}</strong></span>
                </div>
                {showRetentionHistory ? (
                  <div className="retention-history-list">
                    {(retentionState.data?.history ?? []).map((item) => (
                      <article
                        className={`retention-history-row ${expandedRetentionRunIds.includes(item.id) ? "is-expanded" : ""} tone-${item.status ?? "idle"}`}
                        key={item.id}
                      >
                        <div className="retention-history-row-top">
                          <div className="retention-history-copy">
                            <strong>{formatRetentionTrigger(item.trigger)}</strong>
                            <small>{formatRetentionStatus(item.status)}</small>
                          </div>
                          <div className="retention-history-badges">
                            <span className="retention-history-stat tone-marked"><strong>{item.marked_count}</strong> marked</span>
                            <span className="retention-history-stat tone-deleted"><strong>{item.deleted_count}</strong> deleted</span>
                            <span className="retention-history-stat tone-reverted"><strong>{item.reverted_count}</strong> reverted</span>
                            <span className={`retention-status-pill tone-${item.status ?? "idle"}`}>
                              {formatRetentionStatus(item.status)}
                            </span>
                          </div>
                        </div>
                        <div className="retention-history-footer">
                          <button
                            className="ghost-button retention-history-details"
                            type="button"
                            onClick={() =>
                              setExpandedRetentionRunIds((current) =>
                                current.includes(item.id)
                                  ? current.filter((value) => value !== item.id)
                                  : [...current, item.id],
                              )
                            }
                          >
                            <span>Details</span>
                            <svg
                              viewBox="0 0 16 16"
                              aria-hidden="true"
                              className={`retention-history-caret ${expandedRetentionRunIds.includes(item.id) ? "is-open" : ""}`}
                            >
                              <path
                                d="M4.25 6.25 8 10l3.75-3.75"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="1.6"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </button>
                          <small className="muted-copy retention-history-timestamp">
                            {formatAbsoluteDateTime(item.created_at) ?? formatRelativeDate(item.created_at) ?? "just now"}
                          </small>
                        </div>
                        {expandedRetentionRunIds.includes(item.id) ? (() => {
                          const details = retentionRunDetails(item);
                          return (
                            <div className="retention-history-panel">
                              {details.sections.map((section) => (
                                <div className="retention-history-detail-block" key={`${item.id}-${section.key}`}>
                                  <strong>{section.title}</strong>
                                  <ol className="retention-history-detail-list">
                                    {section.entries.map((entry, entryIndex) => (
                                      <li key={`${item.id}-${section.key}-${entryIndex}`}>{entry}</li>
                                    ))}
                                  </ol>
                                </div>
                              ))}
                              {details.notes.map((line, noteIndex) => (
                                <p key={`${item.id}-note-${noteIndex}`}>{line}</p>
                              ))}
                            </div>
                          );
                        })() : null}
                      </article>
                    ))}
                    {!(retentionState.data?.history.length) ? (
                      <div className="search-results-empty">No retention runs have been recorded yet.</div>
                    ) : null}
                  </div>
                ) : null}
              </div>
              <div className="settings-actions-row">
                <button
                  className="ghost-button settings-utility-button"
                  disabled={!retentionDirty || retentionSaving || retentionDeleting || retentionRunning || retentionReverting}
                  onClick={() => void saveRetentionSettings()}
                >
                  {retentionSaving ? "Saving..." : "Save retention settings"}
                </button>
                <small className="muted-copy">
                  {retentionDirty ? "Unsaved changes" : "Retention settings saved"}
                </small>
              </div>
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Exclusions</h2>
            </div>
            <p className="settings-section-note">
              Exclusions are server-wide and protect matching videos from retention. Saved videos and saved series are also treated as automatic exemptions.
            </p>
            <div className="retention-exclusion-stack">
              <label className="settings-field retention-lookup-field">
                <span className={settingLabelClass("Add exclusions before the wizard runs. If something is already pending deletion, revert the run first, add the exclusion, then let retention run again.")} data-tooltip="Add exclusions before the wizard runs. If something is already pending deletion, revert the run first, add the exclusion, then let retention run again." tabIndex={0}>Add exclusion</span>
                <input
                  value={retentionLookupQuery}
                  placeholder="Search channels, series, or videos"
                  onChange={(event) => setRetentionLookupQuery(event.target.value)}
                />
              </label>
              {(retentionLookupPending || retentionLookupQuery.trim().length >= 2) ? (
                <div className="retention-lookup-results">
                  {(["channels", "series", "videos"] as const).map((groupKey) =>
                    retentionLookupResults[groupKey].map((item) => {
                      const pendingKey = `${item.target_type}:${item.id}`;
                      return (
                        <button
                          key={pendingKey}
                          className="retention-lookup-item"
                          type="button"
                          disabled={retentionExclusionPending === pendingKey}
                          onClick={() => void addRetentionExclusion(item)}
                        >
                          <div className="retention-lookup-copy">
                            <strong>{item.label}</strong>
                            <small>
                              {formatRetentionTargetType(item.target_type)}
                              {item.subtitle ? ` • ${item.subtitle}` : ""}
                            </small>
                          </div>
                          <span className="retention-lookup-action">
                            {retentionExclusionPending === pendingKey ? "Adding..." : "Exclude"}
                          </span>
                        </button>
                      );
                    }),
                  )}
                  {!retentionLookupPending &&
                  retentionLookupQuery.trim().length >= 2 &&
                  !retentionLookupResults.channels.length &&
                  !retentionLookupResults.series.length &&
                  !retentionLookupResults.videos.length ? (
                    <div className="search-results-empty">No matching channels, series, or videos.</div>
                  ) : null}
                </div>
              ) : null}
              <div className="retention-scroll-wrap retention-exclusion-list">
                {(retentionState.data?.exclusions ?? []).map((item) => (
                  <div
                    className="retention-exclusion-row retention-clickable-row"
                    key={item.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(retentionTargetHref(item.target_type, item.target_id, item.subtitle))}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        navigate(retentionTargetHref(item.target_type, item.target_id, item.subtitle));
                      }
                    }}
                  >
                    <img
                      className={`retention-exclusion-thumb ${item.target_type === "channel" ? "is-channel" : "is-media"}`}
                      src={item.image_url ?? "/assets/branding/default_avi.png"}
                      alt=""
                    />
                    <div className="retention-exclusion-copy">
                      <strong>{item.label}</strong>
                      <small>
                        {formatRetentionTargetType(item.target_type)}
                        {item.subtitle ? ` • ${item.subtitle}` : ""}
                      </small>
                    </div>
                    <button
                      className="retention-exclusion-remove"
                      type="button"
                      disabled={retentionExclusionDeletingId === item.id}
                      onClick={(event) => {
                        event.stopPropagation();
                        void removeRetentionExclusion(item.id);
                      }}
                    >
                      {retentionExclusionDeletingId === item.id ? "Removing..." : "Remove"}
                    </button>
                  </div>
                ))}
                {!(retentionState.data?.exclusions.length) ? (
                  <div className="search-results-empty">No manual exclusions yet.</div>
                ) : null}
              </div>
            </div>
          </section>

          <section className="settings-section">
            <div className="section-heading">
              <h2>Pending deletion</h2>
              <small className="muted-copy retention-section-count">{retentionState.data?.pending_items.length ?? 0} items</small>
              <div className="settings-actions-row settings-heading-actions">
                <button
                  className="ghost-button settings-utility-button"
                  type="button"
                  disabled={retentionDeleting || !(retentionState.data?.pending_items.length)}
                  onClick={() => void deleteRetention()}
                >
                  {retentionDeleting ? "Deleting..." : "Delete now"}
                </button>
                <button
                  className="ghost-button settings-utility-button"
                  type="button"
                  disabled={retentionReverting || retentionDeleting || !(retentionState.data?.pending_items.length)}
                  onClick={() => void revertRetention()}
                >
                  {retentionReverting ? "Reverting..." : "Revert pending deletions"}
                </button>
              </div>
            </div>
              <div className="retention-scroll-wrap retention-pending-list">
                {(retentionState.data?.pending_items ?? []).map((item) => (
                  <article
                    className="retention-pending-card retention-clickable-row"
                    key={item.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(`/video/${item.video_id}`)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        navigate(`/video/${item.video_id}`);
                      }
                    }}
                  >
                    <img className="retention-pending-thumb" src={item.thumbnail_url ?? `/api/videos/${item.video_id}/thumbnail`} alt="" />
                    <div className="retention-pending-copy">
                      <strong>{item.video_title}</strong>
                      <small>
                        {item.channel_name ?? "Unknown channel"}
                        {` • queued for deletion ${formatRelativeDate(item.delete_after_at) ?? "soon"}`}
                      </small>
                      <div className="retention-pending-meta">
                        <span className="retention-pending-timer">
                          Deletes in {formatRetentionCountdown(item.delete_after_at, retentionCountdownNow)}
                        </span>
                        <small className="muted-copy">
                          Marked {formatRelativeDate(item.marked_at) ?? "recently"}
                        </small>
                      </div>
                    </div>
                  </article>
                ))}
              {!(retentionState.data?.pending_items.length) ? (
                <div className="search-results-empty">Nothing is staged for deletion right now.</div>
              ) : null}
            </div>
          </section>
        </div>
      ) : null}
      {activeTab === "admin" ? (
        <div className="settings-sections">
          <section className="settings-section">
            <div className="section-heading">
              <h2>User permissions</h2>
            </div>
            <p className="settings-section-note">
              Promote trusted accounts to admin when they should be able to manage server settings, sync, retention, and logs. Everyone else stays a regular user.
            </p>
            <div className="admin-user-permissions-list retention-scroll-wrap">
              {adminProfiles.map((item) => (
                <div className="admin-user-permission-row" key={item.id}>
                  <div className="admin-user-permission-copy">
                    <strong>{item.display_name}</strong>
                    <small>
                      @{item.name}
                      {profile?.id === item.id ? " - you" : ""}
                    </small>
                  </div>
                  <div className="admin-user-permission-actions">
                    <div className="admin-user-permission-control">
                      <div className="admin-user-permission-meta">
                        <small className="muted-copy">Current: {item.is_admin ? "Admin" : "User"}</small>
                        <button
                          className="admin-user-delete-button"
                          type="button"
                          disabled={deletingProfileId === item.id || profile?.id === item.id || item.name === "guest"}
                          aria-label={`Delete ${item.display_name}`}
                          onClick={() => setPendingDeleteProfile(item)}
                        >
                          <svg viewBox="0 0 20 20" aria-hidden="true">
                            <path
                              d="M6.75 4.75h6.5M5.5 6.5h9M7.35 6.5l.42 7.55c.03.52.46.93.98.93h2.5c.52 0 .95-.41.98-.93l.42-7.55M8.75 8.75v4.25m2.5-4.25v4.25"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="1.6"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                      </div>
                      <SettingsMenuSelect<"user" | "admin">
                        value={profilePermissionDrafts[item.id] ?? (item.is_admin ? "admin" : "user")}
                        options={[
                          { value: "user", label: "User" },
                          { value: "admin", label: "Admin" },
                        ]}
                        onChange={(value) =>
                          setProfilePermissionDrafts((current) => ({
                            ...current,
                            [item.id]: value,
                          }))
                        }
                        label={`Permissions for ${item.display_name}`}
                      />
                    </div>
                  </div>
                </div>
              ))}
              {!adminProfiles.length ? <div className="search-results-empty">No registered users yet.</div> : null}
            </div>
            <div className="settings-actions-row">
              <button
                className="ghost-button settings-utility-button"
                type="button"
                disabled={profilePermissionsSaving || !profilePermissionsDirty}
                onClick={() => void saveProfilePermissions()}
              >
                {profilePermissionsSaving ? "Saving..." : "Save permissions"}
              </button>
              <small className="muted-copy">
                {profilePermissionsDirty
                  ? "Unsaved changes"
                  : retentionState.data?.settings.auto_timezone
                    ? `Retention time zone: ${retentionState.data.settings.auto_timezone}`
                    : "Permissions saved"}
              </small>
            </div>
          </section>

          <section className="settings-section">
            <SyncReviewPanel
              title="Sync review"
              note="When the scanner lands in the middle, approve the current candidate, reject it, or paste the exact YouTube URL/ID you want halcyon to use."
            />
          </section>
        </div>
      ) : null}
        </div>
      </div>
      {updateModalOpen ? (
        <Modal
          title={updateStatus?.update_available ? "Update available" : "Version details"}
          onClose={() => setUpdateModalOpen(false)}
        >
          <div className="update-modal">
            <p className="update-modal-copy">
              {updateStatus?.update_available
                ? "A newer halcyon build is available for this server."
                : "halcyon is already on the newest known build."}
            </p>
            <div className="update-modal-status">
              <div>
                <small className="muted-copy">Current version</small>
                <strong>{updateStatus?.current_version ?? __APP_VERSION__}</strong>
              </div>
              <div>
                <small className="muted-copy">Newest version</small>
                <strong>{updateStatus?.latest_version ?? __APP_VERSION__}</strong>
              </div>
            </div>
            {updateStatus?.error ? (
              <p className="update-modal-warning">{updateStatus.error}</p>
            ) : (
              <p className="update-modal-note">
                {isManualUpdateCommand ? (
                  <>
                    From the halcyon root, run <code>{updateCommand}</code>.
                    {" "}This install updates from the checked-out source, not a prebuilt app image.
                    {" "}If you want simpler updates later, run <code>./halcyon status</code> once to bootstrap the reusable <code>halcyon</code> command.
                  </>
                ) : (
                  <>
                    Run <code>{updateCommand}</code> on the machine hosting halcyon.
                    {" "}The app does not control Docker directly from inside the container.
                  </>
                )}
              </p>
            )}
            <div className="settings-actions-row">
              <button className="ghost-button settings-utility-button" type="button" onClick={() => setUpdateModalOpen(false)}>
                Close
              </button>
              <button
                className="action-button"
                type="button"
                onClick={() => void handleUpdateAction()}
                disabled={Boolean(updateStatus?.error)}
              >
                {updateStatus?.update_available ? "Update" : "Copy command"}
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
      {pendingDeleteProfile ? (
        <Modal
          title="Delete user"
          onClose={() => {
            if (deletingProfileId === pendingDeleteProfile.id) return;
            setPendingDeleteProfile(null);
          }}
        >
          <div className="delete-user-modal">
            <p className="delete-user-modal-copy">
              Delete <strong>{pendingDeleteProfile.display_name}</strong>? This permanently removes the account profile,
              watch history, playlists, queue, and saved state for that user.
            </p>
            <p className="delete-user-modal-warning">This action cannot be undone.</p>
            <div className="settings-actions-row">
              <button
                className="ghost-button settings-utility-button"
                type="button"
                onClick={() => setPendingDeleteProfile(null)}
                disabled={deletingProfileId === pendingDeleteProfile.id}
              >
                Cancel
              </button>
              <button
                className="settings-danger-button"
                type="button"
                onClick={() => void deleteUserProfile(pendingDeleteProfile.id)}
                disabled={deletingProfileId === pendingDeleteProfile.id}
              >
                {deletingProfileId === pendingDeleteProfile.id ? "Deleting..." : "Delete user"}
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
      {avatarCropSource ? (
        <Modal title="Crop avatar" onClose={() => {
          setAvatarCropSource(null);
          if (avatarInputRef.current) {
            avatarInputRef.current.value = "";
          }
        }}>
          <div className="avatar-crop-modal">
            <div
              ref={avatarCropStageRef}
              className={`avatar-crop-stage ${avatarCropDragging ? "is-dragging" : ""}`}
              onPointerDown={handleAvatarCropPointerDown}
              onPointerMove={handleAvatarCropPointerMove}
              onPointerUp={endAvatarCropDrag}
              onPointerCancel={endAvatarCropDrag}
              onWheel={handleAvatarCropWheel}
            >
              <div
                className="avatar-crop-image"
                style={{
                  backgroundImage: `url(${avatarCropSource})`,
                  backgroundPosition: `${50 + avatarCropDraft.offsetX * 0.5}% ${50 + avatarCropDraft.offsetY * 0.5}%`,
                  backgroundSize: `${avatarCropDraft.zoom * 100}%`,
                }}
              />
              <div className="avatar-crop-mask" />
            </div>
            <div className="avatar-crop-controls">
              <small className="muted-copy">Drag to move the focus. Use the mouse wheel to zoom.</small>
            </div>
            <div className="settings-actions-row">
              <button
                className="ghost-button settings-utility-button"
                type="button"
                onClick={() => {
                  setAvatarCropSource(null);
                  if (avatarInputRef.current) {
                    avatarInputRef.current.value = "";
                  }
                }}
              >
                Cancel
              </button>
              <button className="action-button" type="button" onClick={() => void applyAvatarCrop()}>
                Use avatar
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
