import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
} from "react";
import { createPortal } from "react-dom";
import type Plyr from "plyr";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  api,
  type PlaylistSummary,
  type Preferences,
  type Profile,
  type VideoSummary,
} from "../api/client";
import { AvatarImage } from "../components/AvatarImage";
import { LinkifiedText } from "../components/LinkifiedText";
import { MetadataEditorModal } from "../components/MetadataEditorModal";
import { Modal } from "../components/Modal";
import { PlaylistCreateModal } from "../components/PlaylistCreateModal";
import { HalcyonPlayer } from "../components/HalcyonPlayer";
import { useAsyncData } from "../hooks/useAsyncData";
import {
  formatAbsoluteDateTime,
  formatCount,
  formatRelativeDate,
  normalizeImportedText,
} from "../lib/format";
import {
  readPlaybackContext,
  writePlaybackContext,
} from "../lib/playbackContext";
import { pushToast } from "../lib/notifications";

const WATCH_COMPLETION_THRESHOLD = 0.95;
const SUGGESTION_LOADING_MIN_MS = 180;
const PLAYER_MODE_STORAGE_KEY = "halcyon.playerMode";
const STANDARD_COMMENT_PREVIEW_COUNT = 4;
const COMPACT_COMMENT_PREVIEW_COUNT = 2;
const PROGRESS_CHECKPOINT_INTERVAL_SECONDS = 15;
const PROGRESS_CHECK_INTERVAL_MS = 5_000;
const PROGRESS_SAVE_MIN_DELTA_SECONDS = 2;
const PLAYER_OVERLAY_HIDE_DELAY_MS = 2200;

function detectsTouchOverlayDevice() {
  if (typeof window === "undefined") return false;
  const userAgent = navigator.userAgent || "";
  const mobileUserAgent =
    /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
      userAgent,
    );
  if (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches
  ) {
    return true;
  }
  return mobileUserAgent || "ontouchstart" in window || navigator.maxTouchPoints > 0;
}

type ParsedChapter = {
  startSeconds: number;
  label: string;
};

function readStoredPlayerMode(): "default" | "theater" {
  try {
    const stored = localStorage.getItem(PLAYER_MODE_STORAGE_KEY);
    return stored === "theater" ? "theater" : "default";
  } catch {
    return "default";
  }
}

function resolvePlayerModePreference(
  preference: Preferences["defaultPlayerMode"],
): "default" | "theater" {
  if (preference === "theater") return "theater";
  if (preference === "default") return "default";
  return readStoredPlayerMode();
}

function parseChapterSeconds(match: RegExpMatchArray) {
  const hours = match[1] ? Number(match[1]) : 0;
  const minutes = Number(match[2]);
  const seconds = Number(match[3]);
  return hours * 3600 + minutes * 60 + seconds;
}

function parseDescriptionChapters(description: string): ParsedChapter[] {
  const seen = new Set<number>();
  const parsed = description
    .replace(/\r/g, "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(
        /^(?:[-*•]\s*)?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\s+|[-–—|:]\s*)(.+)$/,
      );
      if (!match) return null;
      const startSeconds = parseChapterSeconds(match);
      const label = match[4].replace(/^[\s\-–—|:]+/, "").trim();
      if (!label || seen.has(startSeconds)) return null;
      seen.add(startSeconds);
      return { startSeconds, label };
    })
    .filter((chapter): chapter is ParsedChapter => chapter !== null)
    .sort((left, right) => left.startSeconds - right.startSeconds);
  return parsed.length >= 2 ? parsed : [];
}

function ThumbIcon({
  type,
  active,
}: {
  type: "like" | "dislike";
  active?: boolean;
}) {
  if (type === "like") {
    return (
      <svg
        viewBox="0 0 24 24"
        className={`reaction-icon ${active ? "active-like" : ""}`}
        aria-hidden="true"
      >
        <path
          d="M9 22H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h4m0 11V11m0 11h7.3a2 2 0 0 0 1.95-1.55l1.55-7A2 2 0 0 0 17.85 11H13V7.5a3.5 3.5 0 0 0-3.5-3.5L9 11"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  return (
    <svg
      viewBox="0 0 24 24"
      className={`reaction-icon ${active ? "active-dislike" : ""}`}
      aria-hidden="true"
    >
      <path
        d="M15 2h4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-4M15 2v11m0-11H7.7a2 2 0 0 0-1.95 1.55l-1.55 7A2 2 0 0 0 6.15 13H11v3.5A3.5 3.5 0 0 0 14.5 20L15 13"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ShareIcon() {
  return (
    <svg viewBox="0 0 24 24" className="reaction-icon" aria-hidden="true">
      <path
        d="M14 5l5 5-5 5M19 10H9a4 4 0 0 0-4 4v5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function BookmarkIcon({ active }: { active?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className={`reaction-icon ${active ? "active-like" : ""}`} aria-hidden="true">
      <path
        d="M7 4.5h10a1.5 1.5 0 0 1 1.5 1.5V20l-6.5-3.9L5.5 20V6A1.5 1.5 0 0 1 7 4.5Z"
        fill={active ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 20 20" className="reaction-icon" aria-hidden="true">
      <path
        d="m4.5 10.5 3.2 3.2 7.8-7.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

type WatchSuggestionItem = {
  id: number;
  watch_ref?: string;
  title: string;
  channel_name?: string | null;
  channel_slug?: string | null;
  channel_id?: number | null;
  series_name?: string | null;
  series_id?: number | null;
  duration_seconds?: number | null;
  progress_seconds?: number | null;
  thumbnail_url?: string | null;
  created_at?: string | null;
  published_at?: string | null;
  youtube_view_count?: number | null;
  watched?: boolean;
  completed?: boolean;
};

type WatchCommentReply = {
  id: number;
  youtube_reply_id?: string | null;
  author_name: string;
  body: string;
  like_count: number;
  published_at?: string | null;
};

type WatchComment = {
  id: number;
  youtube_comment_id?: string | null;
  author_name: string;
  body: string;
  like_count: number;
  reply_count: number;
  published_at?: string | null;
  replies: WatchCommentReply[];
};

type SuggestionFilter = "suggested" | "related";

type SuggestionFeedState = {
  items: WatchSuggestionItem[];
  total: number;
  hasMore: boolean;
  initialized: boolean;
  loadingInitial: boolean;
  loadingMore: boolean;
};

function createSuggestionFeedState(): SuggestionFeedState {
  return {
    items: [],
    total: 0,
    hasMore: false,
    initialized: false,
    loadingInitial: false,
    loadingMore: false,
  };
}

function GhostWatchPage({
  displayMode,
  message = "File was not found in filesystem.",
}: {
  displayMode: "default" | "theater";
  message?: string;
}) {
  return (
    <div
      className={`page-stack watch-page watch-page-ghost ${displayMode === "theater" ? "watch-page-theater" : ""}`}
    >
      <section className="watch-layout">
        <div className="watch-stage-row">
          <div className="watch-main-column">
            <div className="watch-player-slot">
              <div className="video-frame advanced-player watch-player-frame watch-ghost-player-frame">
                <div className="watch-ghost-player-topbar" aria-hidden="true">
                  <span className="watch-ghost-pill" />
                  <span className="watch-ghost-pill short" />
                </div>
                <div className="watch-ghost-player-copy">
                  <small>Playback unavailable</small>
                  <strong>Well this is awkward...</strong>
                  <p>{message}</p>
                </div>
                <div className="watch-ghost-player-controls" aria-hidden="true">
                  <span className="watch-ghost-control wide" />
                  <span className="watch-ghost-control" />
                  <span className="watch-ghost-control" />
                  <span className="watch-ghost-control narrow" />
                </div>
              </div>
            </div>

            <div className="watch-meta-slot">
              <section className="watch-meta watch-ghost-panel" aria-hidden="true">
                <div className="watch-ghost-line watch-ghost-line-title" />
                <div className="watch-ghost-line watch-ghost-line-meta" />
                <div className="watch-ghost-chip-row">
                  <span className="watch-ghost-chip" />
                  <span className="watch-ghost-chip" />
                  <span className="watch-ghost-chip short" />
                </div>
                <div className="watch-ghost-block" />
              </section>
            </div>

            <div className="watch-comments-slot">
              <section className="watch-comments watch-ghost-panel" aria-hidden="true">
                <div className="watch-ghost-line watch-ghost-line-heading" />
                <div className="watch-ghost-comment">
                  <span className="watch-ghost-avatar" />
                  <div className="watch-ghost-comment-copy">
                    <div className="watch-ghost-line watch-ghost-line-meta short" />
                    <div className="watch-ghost-line watch-ghost-line-comment" />
                    <div className="watch-ghost-line watch-ghost-line-comment short" />
                  </div>
                </div>
                <div className="watch-ghost-comment">
                  <span className="watch-ghost-avatar" />
                  <div className="watch-ghost-comment-copy">
                    <div className="watch-ghost-line watch-ghost-line-meta short" />
                    <div className="watch-ghost-line watch-ghost-line-comment" />
                    <div className="watch-ghost-line watch-ghost-line-comment short" />
                  </div>
                </div>
              </section>
            </div>
          </div>

          <aside className="watch-sidebar">
            <section className="watch-sidebar-section watch-ghost-panel" aria-hidden="true">
              <div className="watch-ghost-chip-row">
                <span className="watch-ghost-chip" />
                <span className="watch-ghost-chip short" />
              </div>
              {Array.from({ length: 5 }).map((_, index) => (
                <div className="suggestion-row suggestion-skeleton watch-ghost-suggestion" key={index}>
                  <div className="suggestion-thumb" />
                  <div className="suggestion-copy suggestion-copy-skeleton">
                    <div className="watch-skeleton-line suggestion-skeleton-title" />
                    <div className="watch-skeleton-line suggestion-skeleton-meta" />
                    <div className="watch-skeleton-line suggestion-skeleton-meta short" />
                  </div>
                </div>
              ))}
            </section>
          </aside>
        </div>
      </section>
    </div>
  );
}

type AutoplayTarget = WatchSuggestionItem & {
  watch_ref: string;
};

function normalizeSuggestionItems(
  items: VideoSummary[] | WatchSuggestionItem[] | null | undefined,
  nextUpId: number | null,
): WatchSuggestionItem[] {
  if (!items?.length) return [];
  const seen = new Set<number>();
  return items.filter((item) => {
    if (!item || item.id === nextUpId || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function toAutoplayTarget(
  item: VideoSummary | WatchSuggestionItem | null | undefined,
): AutoplayTarget | null {
  if (!item?.id || !item.title) return null;
  const watchRef = item.watch_ref ? String(item.watch_ref) : String(item.id);
  return {
    ...item,
    watch_ref: watchRef,
  };
}

function WatchSuggestionRow({
  item,
  profile,
  onRefresh,
}: {
  item: WatchSuggestionItem;
  profile: Profile | null;
  onRefresh?: () => Promise<void> | void;
}) {
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [playlistsOpen, setPlaylistsOpen] = useState(false);
  const [playlistsLoading, setPlaylistsLoading] = useState(false);
  const [playlists, setPlaylists] = useState<PlaylistSummary[]>([]);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);
  const [createPlaylistPending, setCreatePlaylistPending] = useState(false);
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const playlistsCloseTimerRef = useRef<number | null>(null);
  const displayTitle = normalizeImportedText(item.title) ?? item.title;
  const displayChannel =
    normalizeImportedText(item.channel_name) ??
    item.channel_name ??
    "Unknown channel";
  const displaySeries =
    normalizeImportedText(item.series_name) ?? item.series_name;
  const progressPercent =
    item.duration_seconds && item.duration_seconds > 0
      ? Math.min(
          100,
          ((item.progress_seconds ?? 0) / item.duration_seconds) * 100,
        )
      : 0;
  const isWatched = item.completed ?? item.watched ?? progressPercent >= 99.5;
  const canMarkUnwatched = isWatched || (item.progress_seconds ?? 0) > 0;
  const publishedAt = item.published_at ?? null;
  const isNew =
    publishedAt !== null &&
    Date.now() - new Date(publishedAt).getTime() <= 48 * 60 * 60 * 1000;
  const statsLine = [
    formatCount(item.youtube_view_count)
      ? `${formatCount(item.youtube_view_count)} views`
      : null,
    publishedAt ? formatRelativeDate(publishedAt) : null,
  ]
    .filter(Boolean)
    .join(" • ");

  const menuPlacement = useMemo(() => {
    if (!menuAnchor || typeof window === "undefined") return null;
    const width = 188;
    const submenuWidth = 224;
    const estimatedHeight = 204;
    const viewportPadding = 12;
    const preferredLeft = Math.max(
      viewportPadding,
      Math.min(menuAnchor.left, window.innerWidth - width - viewportPadding),
    );
    const fallbackLeft = Math.max(
      viewportPadding,
      Math.min(
        menuAnchor.right - width,
        window.innerWidth - width - viewportPadding,
      ),
    );
    const left =
      preferredLeft + width > window.innerWidth - viewportPadding
        ? fallbackLeft
        : preferredLeft;
    const top = Math.max(
      viewportPadding,
      Math.min(
        menuAnchor.bottom + 8,
        window.innerHeight - estimatedHeight - viewportPadding,
      ),
    );
    const submenuSide =
      left + width + submenuWidth + 8 > window.innerWidth - viewportPadding
        ? "left"
        : "right";
    return {
      style: { top: `${top}px`, left: `${left}px` },
      submenuSide,
    };
  }, [menuAnchor]);

  useEffect(
    () => () => {
      if (playlistsCloseTimerRef.current) {
        window.clearTimeout(playlistsCloseTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (!menuOpen) return undefined;

    function handlePointerDown(event: globalThis.MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (menuRef.current?.contains(target)) return;
      if (target.closest(".suggestion-kebab")) return;
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  async function ensurePlaylists() {
    if (playlists.length || playlistsLoading) return;
    setPlaylistsLoading(true);
    try {
      setPlaylists(await api.playlists());
    } catch (error) {
      pushToast(
        "error",
        "Unable to load playlists",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    } finally {
      setPlaylistsLoading(false);
    }
  }

  function closeMenus() {
    setMenuOpen(false);
    setMenuAnchor(null);
    setPlaylistsOpen(false);
  }

  async function addToPlaylist(playlistId: number) {
    try {
      await api.addPlaylistItem(playlistId, item.id);
      pushToast("success", "Added to playlist", displayTitle, {
        href: `/playlists/${playlistId}`,
      });
      closeMenus();
    } catch (error) {
      pushToast(
        "error",
        "Unable to add to playlist",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    }
  }

  async function createPlaylistAndAdd(playlistName: string) {
    setCreatePlaylistPending(true);
    try {
      const currentProfile = profile ?? (await api.me());
      const created: any = await api.createPlaylist({
        user_id: currentProfile.id,
        name: playlistName,
      });
      const nextPlaylists = await api.playlists();
      setPlaylists(nextPlaylists);
      const createdPlaylist =
        nextPlaylists.find((playlist) => playlist.id === created?.id) ??
        nextPlaylists.find((playlist) => playlist.name === playlistName);

      if (!createdPlaylist) {
        throw new Error("Playlist created but could not be loaded.");
      }

      await api.addPlaylistItem(createdPlaylist.id, item.id);
      pushToast("success", "Added to playlist", displayTitle, {
        href: `/playlists/${createdPlaylist.id}`,
      });
      setCreatePlaylistOpen(false);
      closeMenus();
    } catch (error) {
      pushToast(
        "error",
        "Unable to create playlist",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    } finally {
      setCreatePlaylistPending(false);
    }
  }

  async function addToQueue() {
    try {
      await api.addQueueItem(item.id);
      pushToast("success", "Added to queue", displayTitle, { href: "/?topic=queue" });
    } catch (error) {
      pushToast(
        "error",
        "Unable to add to queue",
        error instanceof Error ? error.message : "Unknown queue error",
      );
    } finally {
      closeMenus();
    }
  }

  async function markWatchState(state: "watched" | "unwatched") {
    try {
      await api.setWatchState(item.id, state);
      pushToast(
        "success",
        state === "watched" ? "Marked as watched" : "Marked as unwatched",
        displayTitle,
      );
      await onRefresh?.();
    } catch (error) {
      pushToast(
        "error",
        "Unable to update watch state",
        error instanceof Error ? error.message : "Unknown watch state error",
      );
    } finally {
      closeMenus();
    }
  }

  function openPlaylistsMenu() {
    if (playlistsCloseTimerRef.current) {
      window.clearTimeout(playlistsCloseTimerRef.current);
      playlistsCloseTimerRef.current = null;
    }
    void ensurePlaylists();
    setPlaylistsOpen(true);
  }

  function closePlaylistsMenu(event?: FocusEvent<HTMLDivElement>) {
    const nextTarget = event?.relatedTarget as Node | null;
    if (nextTarget && event?.currentTarget.contains(nextTarget)) return;
    if (playlistsCloseTimerRef.current) {
      window.clearTimeout(playlistsCloseTimerRef.current);
    }
    playlistsCloseTimerRef.current = window.setTimeout(() => {
      setPlaylistsOpen(false);
      playlistsCloseTimerRef.current = null;
    }, 180);
  }

  return (
    <div
      className="suggestion-row-shell"
      onClick={(event) => event.stopPropagation()}
    >
        <div className={`suggestion-row ${isWatched ? "is-watched" : ""}`}>
          <Link to={`/video/${item.watch_ref ?? item.id}`} className="suggestion-thumb">
          <img
            src={item.thumbnail_url ?? `/api/videos/${item.id}/thumbnail`}
            alt={displayTitle}
          />
          {isWatched ? <span className="watched-badge-overlay">Watched</span> : null}
          </Link>
          <span className="suggestion-copy">
            <Link to={`/video/${item.watch_ref ?? item.id}`} className="suggestion-title-link">
              <strong>{displayTitle}</strong>
            </Link>
            <Link
              to={`/channels/${item.channel_slug ?? item.channel_id ?? ""}`}
              className={`suggestion-channel-link ${!item.channel_slug && !item.channel_id ? "is-disabled" : ""}`}
              onClick={(event) => {
                if (!item.channel_slug && !item.channel_id) {
                  event.preventDefault();
                }
                event.stopPropagation();
              }}
            >
              <small className="suggestion-channel">{displayChannel}</small>
            </Link>
            {statsLine || displaySeries || isNew ? (
              <span className="suggestion-meta-stack">
                <small className="suggestion-meta">{statsLine || displaySeries}</small>
                {isNew ? <span className="suggestion-fresh-badge">New</span> : null}
              </span>
            ) : null}
          </span>
        </div>
      <button
        className="suggestion-kebab"
        aria-label={`Actions for ${displayTitle}`}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          const rect = event.currentTarget.getBoundingClientRect();
          setMenuAnchor(rect);
          setMenuOpen((current) => !current);
        }}
        type="button"
      >
        <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
          <circle cx="12" cy="5" r="2.15" fill="currentColor" />
          <circle cx="12" cy="12" r="2.15" fill="currentColor" />
          <circle cx="12" cy="19" r="2.15" fill="currentColor" />
        </svg>
      </button>
      {menuOpen && menuPlacement
        ? createPortal(
            <div
              ref={menuRef}
              className="card-menu"
              data-submenu-side={menuPlacement.submenuSide}
              style={menuPlacement.style}
              onClick={(event) => event.stopPropagation()}
            >
              {item.channel_id || item.channel_slug ? (
                <button
                  className="menu-item"
                  onClick={() => {
                    closeMenus();
                    navigate(
                      `/channels/${item.channel_slug ?? item.channel_id}`,
                    );
                  }}
                >
                  Go to channel
                </button>
              ) : null}
              <div
                className={`menu-item-group ${playlistsOpen ? "is-open" : ""}`}
                onMouseEnter={openPlaylistsMenu}
                onMouseLeave={() => closePlaylistsMenu()}
                onFocusCapture={openPlaylistsMenu}
                onBlurCapture={closePlaylistsMenu}
              >
                <button
                  className="menu-item menu-item-with-arrow"
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={playlistsOpen}
                  onClick={async () => {
                    await ensurePlaylists();
                    setPlaylistsOpen((current) => !current);
                  }}
                >
                  <span>Add to playlist</span>
                  <svg
                    viewBox="0 0 20 20"
                    className="menu-caret"
                    aria-hidden="true"
                  >
                    <path
                      d="m7 4 6 6-6 6"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
                {playlistsOpen ? (
                  <div className="menu-submenu playlist-submenu">
                    <button
                      className="menu-item playlist-create-item"
                      onClick={() => {
                        closeMenus();
                        setCreatePlaylistOpen(true);
                      }}
                    >
                      + New playlist
                    </button>
                    <div className="menu-section-label">Playlists</div>
                    <div className="playlist-submenu-scroll">
                      {playlistsLoading ? (
                        <div className="search-results-empty">
                          Loading playlists...
                        </div>
                      ) : null}
                      {!playlistsLoading && playlists.length
                        ? playlists.map((playlist) => (
                            <button
                              key={playlist.id}
                              className="menu-item"
                              onClick={() => void addToPlaylist(playlist.id)}
                            >
                              {normalizeImportedText(playlist.name) ??
                                playlist.name}
                            </button>
                          ))
                        : null}
                      {!playlistsLoading && !playlists.length ? (
                        <div className="search-results-empty">
                          No playlists yet.
                        </div>
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </div>
              <button className="menu-item" onClick={() => void addToQueue()}>
                Add to queue
              </button>
              {!isWatched ? (
                <button
                  className="menu-item"
                  onClick={() => void markWatchState("watched")}
                >
                  Mark as watched
                </button>
              ) : null}
              {canMarkUnwatched ? (
                <button
                  className="menu-item"
                  onClick={() => void markWatchState("unwatched")}
                >
                  Mark as unwatched
                </button>
              ) : null}
              {item.series_id ? (
                <button
                  className="menu-item"
                  onClick={() => {
                    closeMenus();
                    navigate(`/series/${item.series_id}`);
                  }}
                >
                  Go to series
                </button>
              ) : null}
            </div>,
            document.body,
          )
        : null}
      {createPlaylistOpen ? (
        <PlaylistCreateModal
          pending={createPlaylistPending}
          onClose={() => {
            if (!createPlaylistPending) {
              setCreatePlaylistOpen(false);
            }
          }}
          onCreate={(name) => createPlaylistAndAdd(name)}
        />
      ) : null}
    </div>
  );
}

function WatchSuggestionSkeletonRow() {
  return (
    <div className="suggestion-row suggestion-skeleton" aria-hidden="true">
      <div className="suggestion-thumb" />
      <div className="suggestion-copy suggestion-copy-skeleton">
        <div className="watch-skeleton-line suggestion-skeleton-title" />
        <div className="watch-skeleton-line suggestion-skeleton-meta" />
        <div className="watch-skeleton-line suggestion-skeleton-meta short" />
      </div>
    </div>
  );
}

function WatchSuggestionEmptyState({
  message,
}: {
  message: string;
}) {
  return (
    <div className="watch-suggestion-empty-state">
      <p className="muted-copy">{message}</p>
      <div className="suggestion-stack is-empty-state" aria-hidden="true">
        {Array.from({ length: 3 }).map((_, index) => (
          <WatchSuggestionSkeletonRow key={index} />
        ))}
      </div>
    </div>
  );
}

export function VideoPage({
  profile,
  preferences,
  onCaptionsPreferenceChange,
}: {
  profile: Profile | null;
  preferences: Preferences;
  onCaptionsPreferenceChange: (enabled: boolean) => void;
}) {
  const params = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const canManageVideo = Boolean(profile?.is_admin);
  const videoRef = params.videoId ?? "";
  const { data, loading, error, setData } = useAsyncData(
    () => api.video(videoRef),
    [videoRef],
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareAtTimestamp, setShareAtTimestamp] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [playerLoading, setPlayerLoading] = useState(true);
  const [playerError, setPlayerError] = useState<string | null>(null);
  const [descriptionExpanded, setDescriptionExpanded] = useState(false);
  const [displayMode, setDisplayMode] = useState<"default" | "theater">(() =>
    resolvePlayerModePreference(preferences.defaultPlayerMode),
  );
  const [liveStats, setLiveStats] = useState({
    bufferAhead: 0,
    droppedFrames: 0,
  });
  const [subscriptionPending, setSubscriptionPending] = useState(false);
  const [reactionPending, setReactionPending] = useState<
    "like" | "dislike" | null
  >(null);
  const [savePending, setSavePending] = useState(false);
  const [autoplayTarget, setAutoplayTarget] = useState<AutoplayTarget | null>(null);
  const [autoplayCountdown, setAutoplayCountdown] = useState<number | null>(null);
  const [commentReactions, setCommentReactions] = useState<
    Record<string, "like" | "dislike" | null>
  >({});
  const [suggestionFilter, setSuggestionFilter] =
    useState<SuggestionFilter>("suggested");
  const [suggestionFeeds, setSuggestionFeeds] = useState<
    Record<SuggestionFilter, SuggestionFeedState>
  >(() => ({
    suggested: createSuggestionFeedState(),
    related: createSuggestionFeedState(),
  }));
  const [commentsExpanded, setCommentsExpanded] = useState(false);
  const [expandedReplies, setExpandedReplies] = useState<Record<number, boolean>>(
    {},
  );
  const [playlistMenuOpen, setPlaylistMenuOpen] = useState(false);
  const [playlistLoading, setPlaylistLoading] = useState(false);
  const [playlists, setPlaylists] = useState<PlaylistSummary[]>([]);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);
  const [createPlaylistPending, setCreatePlaylistPending] = useState(false);
  const [touchOverlayDevice] = useState(() => detectsTouchOverlayDevice());
  const [playerOverlayVisible, setPlayerOverlayVisible] = useState(
    () => !detectsTouchOverlayDevice(),
  );
  const videoNodeRef = useRef<HTMLVideoElement | null>(null);
  const statsRef = useRef<number | null>(null);
  const playerOverlayTimerRef = useRef<number | null>(null);
  const playlistMenuCloseTimerRef = useRef<number | null>(null);
  const autoplayCountdownTimerRef = useRef<number | null>(null);
  const autoplayNavigateTimerRef = useRef<number | null>(null);
  const suggestionsSentinelRef = useRef<HTMLDivElement | null>(null);
  const suggestionFeedsRef = useRef(suggestionFeeds);
  const suggestionRequestTokenRef = useRef(0);
  const autoplayTransitionRef = useRef(false);
  const lastPersistedProgressRef = useRef(0);
  const lastPersistedCompletedRef = useRef(false);
  const lastBackgroundProgressRef = useRef<{
    positionSeconds: number;
    completed: boolean;
  } | null>(null);
  const videoId = data?.video.id ?? null;
  const currentVideoRef = data?.video.watch_ref ?? videoRef;
  const startAtSeconds = useMemo(() => {
    const raw = new URLSearchParams(location.search).get("t");
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }, [location.search]);

  useEffect(() => {
    setPlayerLoading(true);
    setPlayerError(null);
  }, [data?.playback.stream_url, videoId]);

  useEffect(() => {
    lastPersistedProgressRef.current = Math.max(
      0,
      Math.floor(data?.resume_point ?? data?.video.progress_seconds ?? 0),
    );
    lastPersistedCompletedRef.current = Boolean(
      data?.video?.watched ?? (data as any)?.video?.completed,
    );
    lastBackgroundProgressRef.current = null;
  }, [
    data?.resume_point,
    data?.video.progress_seconds,
    data?.video.watched,
    (data as any)?.video?.completed,
    videoId,
  ]);

  useEffect(() => {
    setCommentsExpanded(false);
    setExpandedReplies({});
  }, [videoId]);

  useEffect(() => {
    function clearOverlayTimer() {
      if (playerOverlayTimerRef.current != null) {
        window.clearTimeout(playerOverlayTimerRef.current);
        playerOverlayTimerRef.current = null;
      }
    }

    if (!touchOverlayDevice) {
      clearOverlayTimer();
      setPlayerOverlayVisible(true);
      return undefined;
    }

    if (statsOpen || menuOpen || playlistMenuOpen) {
      clearOverlayTimer();
      setPlayerOverlayVisible(true);
      return undefined;
    }

    clearOverlayTimer();
    setPlayerOverlayVisible(true);
    playerOverlayTimerRef.current = window.setTimeout(() => {
      playerOverlayTimerRef.current = null;
      setPlayerOverlayVisible(false);
    }, PLAYER_OVERLAY_HIDE_DELAY_MS);

    return () => clearOverlayTimer();
  }, [menuOpen, playlistMenuOpen, statsOpen, touchOverlayDevice, videoId]);

  useEffect(() => {
    setDisplayMode(resolvePlayerModePreference(preferences.defaultPlayerMode));
  }, [preferences.defaultPlayerMode, videoId]);

  useEffect(() => {
    try {
      localStorage.setItem(PLAYER_MODE_STORAGE_KEY, displayMode);
    } catch {
      // Ignore storage failures for local preference-only state.
    }
  }, [displayMode]);

  useEffect(() => {
    autoplayTransitionRef.current = false;
    if (autoplayCountdownTimerRef.current) {
      window.clearInterval(autoplayCountdownTimerRef.current);
      autoplayCountdownTimerRef.current = null;
    }
    if (autoplayNavigateTimerRef.current) {
      window.clearTimeout(autoplayNavigateTimerRef.current);
      autoplayNavigateTimerRef.current = null;
    }
    setAutoplayTarget(null);
    setAutoplayCountdown(null);
  }, [videoId]);

  useEffect(() => {
    suggestionFeedsRef.current = suggestionFeeds;
  }, [suggestionFeeds]);

  useEffect(() => {
    if (!statsOpen) {
      if (statsRef.current) window.clearInterval(statsRef.current);
      statsRef.current = null;
      return;
    }

    statsRef.current = window.setInterval(() => {
      const node = videoNodeRef.current;
      if (!node) return;
      const quality =
        "getVideoPlaybackQuality" in node
          ? node.getVideoPlaybackQuality()
          : null;
      const bufferAhead = node.buffered.length
        ? Math.max(
            0,
            node.buffered.end(node.buffered.length - 1) - node.currentTime,
          )
        : 0;
      setLiveStats({
        bufferAhead,
        droppedFrames: quality?.droppedVideoFrames ?? 0,
      });
    }, 500);

    return () => {
      if (statsRef.current) window.clearInterval(statsRef.current);
    };
  }, [statsOpen]);

  useEffect(
    () => () => {
      if (playlistMenuCloseTimerRef.current) {
        window.clearTimeout(playlistMenuCloseTimerRef.current);
      }
      if (autoplayCountdownTimerRef.current) {
        window.clearInterval(autoplayCountdownTimerRef.current);
      }
      if (autoplayNavigateTimerRef.current) {
        window.clearTimeout(autoplayNavigateTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    const context = readPlaybackContext();
    if (!videoId || !context || !context.videoIds.includes(videoId)) return;
    if (!context.queueApplied) {
      const currentIndex = context.videoIds.indexOf(videoId);
      const remaining = context.videoIds.slice(currentIndex + 1);
      void api.bulkQueue(remaining, true).catch(() => undefined);
      writePlaybackContext({
        ...context,
        activeVideoId: videoId,
        queueApplied: true,
      });
      return;
    }
    if (context.activeVideoId !== videoId) {
      writePlaybackContext({ ...context, activeVideoId: videoId });
    }
  }, [videoId]);

  const descriptionSource =
    data?.video.description || data?.youtube.snapshot?.description || "";
  const displayVideoTitle =
    normalizeImportedText(data?.video.title) ?? data?.video.title ?? "";
  const displayChannelName =
    normalizeImportedText(data?.channel?.name ?? data?.video.channel_name) ??
    data?.channel?.name ??
    data?.video.channel_name ??
    "Unknown channel";
  const displaySeriesName =
    normalizeImportedText(data?.video.series_name) ?? data?.video.series_name;
  const description = useMemo(
    () =>
      descriptionSource
        .replace(/\r/g, "")
        .split("\n")
        .filter((line: string) => line.trim() !== "...")
        .join("\n")
        .replace(/(?:\n)?\s*\.\.\.\s*$/, "")
        .trim(),
    [descriptionSource],
  );
  const chapters = useMemo(
    () => parseDescriptionChapters(descriptionSource),
    [descriptionSource],
  );
  const canExpandDescription =
    description.trim().length > 220 || description.includes("\n");
  const publishedAtLabel = useMemo(() => {
    if (!data) return null;
    const published =
      data.youtube.snapshot?.published_at ?? data.video.published_at;
    return published ? formatRelativeDate(published) : null;
  }, [data]);
  const addedAtLabel = useMemo(() => {
    if (!data?.video.created_at) return null;
    return formatAbsoluteDateTime(data.video.created_at);
  }, [data?.video.created_at]);
  const viewCountLabel = useMemo(() => {
    if (!data) return null;
    const viewCount = formatCount(
      data.youtube.snapshot?.view_count ?? data.video.youtube_view_count,
    );
    return viewCount ? `${viewCount} views` : null;
  }, [data]);

  const statsRows = useMemo(
    () => [
      ["Container", data?.media_info.container ?? "unknown"],
      ["Video codec", data?.media_info.codec_summary ?? "unknown"],
      ["Audio codec", data?.media_info.audio_codec ?? "unknown"],
      ["Resolution", data?.media_info.resolution ?? "unknown"],
      ["FPS", data?.media_info.fps ?? "unknown"],
      [
        "Bitrate",
        data?.media_info.bitrate_kbps
          ? `${data.media_info.bitrate_kbps} kb/s`
          : "unknown",
      ],
      ["Mode", data?.media_info.transcoding ? "transcoding" : "direct play"],
      ["Buffered", `${liveStats.bufferAhead.toFixed(1)}s`],
      ["Dropped", String(liveStats.droppedFrames)],
    ],
    [data, liveStats],
  );
  const displayedLikeCount = useMemo(() => {
    if (!data) return null;
    const base =
      data.youtube.snapshot?.like_count ?? data.video.youtube_like_count;
    if (base == null) return null;
    return data.video.user_reaction === "like" ? base + 1 : base;
  }, [data]);
  const displayedDislikeCount = useMemo(() => {
    if (!data) return null;
    const base =
      data.youtube.snapshot?.dislike_count ?? data.video.youtube_dislike_count;
    if (base == null) return null;
    return data.video.user_reaction === "dislike" ? base + 1 : base;
  }, [data]);
  const engagementRatio = useMemo(() => {
    if (!data) return null;
    const likes = displayedLikeCount;
    const dislikes = displayedDislikeCount;
    if (likes != null && dislikes != null && likes + dislikes > 0) {
      return Math.max(0, Math.min(1, likes / (likes + dislikes)));
    }
    const rating = data.youtube.snapshot?.rating ?? data.video.youtube_rating;
    if (rating != null) {
      return Math.max(0, Math.min(1, rating / 5));
    }
    return null;
  }, [data, displayedDislikeCount, displayedLikeCount]);
  const engagementTooltip = useMemo(() => {
    if (engagementRatio == null) return null;
    const likePercent = (engagementRatio * 100).toFixed(1);
    const dislikePercent = (100 - engagementRatio * 100).toFixed(1);
    return `${likePercent}% like • ${dislikePercent}% dislike`;
  }, [engagementRatio]);
  const playerAspectRatio = "16 / 9";
  const videoWatched = Boolean(data?.video?.watched ?? (data as any)?.video?.completed);
  const canMarkVideoUnwatched = videoWatched || (data?.video?.progress_seconds ?? data?.resume_point ?? 0) > 0;
  const resumablePosition = useMemo(() => {
    if (!data?.resume_point || data.resume_point <= 0) return null;
    if (videoWatched) return null;
    const durationSeconds = data.video.duration_seconds;
    if (
      durationSeconds &&
      durationSeconds > 0 &&
      data.resume_point / durationSeconds >= WATCH_COMPLETION_THRESHOLD
    ) {
      return null;
    }
    return data.resume_point;
  }, [data, videoWatched]);
  const activeSuggestionFeed = suggestionFeeds[suggestionFilter];
  const visibleSuggestedItems = activeSuggestionFeed.items;
  const showSuggestionLoadingState =
    activeSuggestionFeed.loadingInitial ||
    (suggestionFilter === "related" && !activeSuggestionFeed.initialized);
  const totalComments = (data?.youtube.comments ?? []) as WatchComment[];
  const usesCompactCommentPreview = preferences.density === "compact";
  const commentPreviewCount = usesCompactCommentPreview
    ? COMPACT_COMMENT_PREVIEW_COUNT
    : STANDARD_COMMENT_PREVIEW_COUNT;
  const canCollapseComments = totalComments.length > commentPreviewCount;
  const previewedComments = useMemo(
    () =>
      commentsExpanded || !canCollapseComments
        ? totalComments
        : totalComments.slice(0, commentPreviewCount + 1),
    [canCollapseComments, commentPreviewCount, commentsExpanded, totalComments],
  );
  const previewOverflowCommentIndex =
    canCollapseComments && !commentsExpanded
      ? commentPreviewCount
      : -1;
  const showCollapsedCommentPreview = canCollapseComments && !commentsExpanded;

  function toggleCommentsExpanded() {
    if (!canCollapseComments) return;
    setCommentsExpanded((current) => !current);
  }

  function toggleReplies(commentId: number) {
    setExpandedReplies((current) => ({
      ...current,
      [commentId]: !current[commentId],
    }));
  }

  useEffect(() => {
    if (!profile || !videoId) return undefined;

    const intervalId = window.setInterval(() => {
      const node = videoNodeRef.current;
      if (
        !node ||
        node.paused ||
        node.ended ||
        playerLoading ||
        Boolean(playerError)
      ) {
        return;
      }
      const currentTimeSeconds = normalizeProgressPosition(node.currentTime);
      if (currentTimeSeconds <= 0) return;
      const progressedEnough =
        currentTimeSeconds - lastPersistedProgressRef.current >=
        PROGRESS_CHECKPOINT_INTERVAL_SECONDS;
      const rewoundEnough =
        currentTimeSeconds <
        lastPersistedProgressRef.current - PROGRESS_SAVE_MIN_DELTA_SECONDS;
      if (!progressedEnough && !rewoundEnough) return;
      void persistProgress(currentTimeSeconds, false);
    }, PROGRESS_CHECK_INTERVAL_MS);

    return () => window.clearInterval(intervalId);
  }, [playerError, playerLoading, profile, videoId]);

  useEffect(() => {
    if (!profile || !videoId) return undefined;

    function flushCurrentProgress() {
      const node = videoNodeRef.current;
      if (!node || node.ended) return;
      const currentTimeSeconds = normalizeProgressPosition(node.currentTime);
      if (currentTimeSeconds <= 0) return;
      persistProgressInBackground(currentTimeSeconds, false);
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        flushCurrentProgress();
      }
    }

    window.addEventListener("pagehide", flushCurrentProgress);
    window.addEventListener("beforeunload", flushCurrentProgress);
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      flushCurrentProgress();
      window.removeEventListener("pagehide", flushCurrentProgress);
      window.removeEventListener("beforeunload", flushCurrentProgress);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [profile, videoId]);

  useEffect(() => {
    suggestionRequestTokenRef.current += 1;
    const nextUpId = data?.next_up?.id ?? null;
    const seededSuggested = normalizeSuggestionItems(
      data?.suggested,
      nextUpId,
    );
    const suggestedTotal = Math.max(
      data?.suggested_total ?? seededSuggested.length,
      seededSuggested.length,
    );
    const suggestedHasMore = Boolean(
      data?.suggested_has_more ?? suggestedTotal > seededSuggested.length,
    );
    setSuggestionFeeds({
      suggested: {
        items: seededSuggested,
        total: suggestedTotal,
        hasMore: suggestedHasMore,
        initialized: true,
        loadingInitial: false,
        loadingMore: false,
      },
      related: createSuggestionFeedState(),
    });
  }, [videoId, data?.suggested, data?.suggested_total, data?.suggested_has_more, data?.next_up?.id]);

  async function loadSuggestionBatch(
    mode: SuggestionFilter,
    batchSize: number,
    options?: { reset?: boolean },
  ) {
    const reset = options?.reset ?? false;
    const currentRef = currentVideoRef;
    if (!currentRef) return;
    const currentFeed = suggestionFeedsRef.current[mode];
    if (!reset) {
      if (
        currentFeed.loadingInitial ||
        currentFeed.loadingMore ||
        !currentFeed.hasMore
      ) {
        return;
      }
    }

    const offset = reset ? 0 : currentFeed.items.length;
    const requestToken = suggestionRequestTokenRef.current;
    const loadingStartedAt = performance.now();
    setSuggestionFeeds((current) => ({
      ...current,
      [mode]: {
        ...current[mode],
        initialized: true,
        loadingInitial: reset,
        loadingMore: !reset,
      },
    }));

    try {
      const page = await api.videoSuggestions(currentRef, mode, offset, batchSize);
      const remainingDelay =
        SUGGESTION_LOADING_MIN_MS - (performance.now() - loadingStartedAt);
      if (remainingDelay > 0) {
        await new Promise((resolve) => window.setTimeout(resolve, remainingDelay));
      }
      if (
        requestToken !== suggestionRequestTokenRef.current ||
        currentRef !== currentVideoRef
      ) {
        return;
      }
      const nextUpId = data?.next_up?.id ?? null;
      const normalizedItems = normalizeSuggestionItems(page.items, nextUpId);
      setSuggestionFeeds((current) => {
        const existing =
          reset || offset === 0 ? [] : current[mode].items;
        const merged = normalizeSuggestionItems(
          [...existing, ...normalizedItems],
          nextUpId,
        );
        return {
          ...current,
          [mode]: {
            items: merged,
            total: page.total,
            hasMore: page.has_more,
            initialized: true,
            loadingInitial: false,
            loadingMore: false,
          },
        };
      });
    } catch {
      const remainingDelay =
        SUGGESTION_LOADING_MIN_MS - (performance.now() - loadingStartedAt);
      if (remainingDelay > 0) {
        await new Promise((resolve) => window.setTimeout(resolve, remainingDelay));
      }
      if (requestToken !== suggestionRequestTokenRef.current) return;
      setSuggestionFeeds((current) => ({
        ...current,
        [mode]: {
          ...current[mode],
          initialized: true,
          loadingInitial: false,
          loadingMore: false,
        },
      }));
    }
  }

  useEffect(() => {
    if (!videoId || suggestionFilter !== "related") return;
    if (suggestionFeeds.related.initialized) return;
    void loadSuggestionBatch("related", 10, { reset: true });
  }, [videoId, suggestionFilter, suggestionFeeds.related.initialized]);

  useEffect(() => {
    const node = suggestionsSentinelRef.current;
    if (
      !node ||
      activeSuggestionFeed.loadingInitial ||
      activeSuggestionFeed.loadingMore ||
      !activeSuggestionFeed.hasMore
    ) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        void loadSuggestionBatch(suggestionFilter, 5);
      },
      { root: null, rootMargin: "320px 0px", threshold: 0.01 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [
    activeSuggestionFeed.hasMore,
    activeSuggestionFeed.items.length,
    activeSuggestionFeed.loadingInitial,
    activeSuggestionFeed.loadingMore,
    suggestionFilter,
    videoId,
  ]);
  const shareUrl = useMemo(() => {
    if (typeof window === "undefined") return "";
    const url = new URL(`/video/${currentVideoRef}`, window.location.origin);
    if (shareAtTimestamp && videoNodeRef.current) {
      const seconds = Math.floor(videoNodeRef.current.currentTime);
      if (seconds > 0) {
        url.searchParams.set("t", String(seconds));
      }
    }
    return url.toString();
  }, [
    shareAtTimestamp,
    currentVideoRef,
    playerLoading,
    data?.playback.stream_url,
  ]);

  async function copyShareUrl() {
    try {
      await navigator.clipboard.writeText(shareUrl);
      pushToast("success", "Link copied", shareUrl);
    } catch (nextError) {
      pushToast(
        "error",
        "Copy failed",
        nextError instanceof Error ? nextError.message : "Could not copy link",
      );
    }
  }

  function openChannel() {
    const ref =
      data?.channel?.slug ??
      data?.video.channel_slug ??
      data?.channel?.id ??
      data?.video.channel_id;
    if (!ref) return;
    navigate(`/channels/${ref}`);
  }

  function openSeries() {
    if (!data?.video.series_id) return;
    navigate(`/series/${data.video.series_id}`);
  }

  function closePlayerMenus() {
    setMenuOpen(false);
    setPlaylistMenuOpen(false);
  }

  async function refreshWatchPage() {
    setData(await api.video(currentVideoRef));
  }

  function setCommentReaction(key: string, nextReaction: "like" | "dislike") {
    setCommentReactions((current) => ({
      ...current,
      [key]: current[key] === nextReaction ? null : nextReaction,
    }));
  }

  function normalizeProgressPosition(positionSeconds: number) {
    if (!Number.isFinite(positionSeconds)) return 0;
    return Math.max(0, Math.floor(positionSeconds));
  }

  function shouldPersistProgress(
    positionSeconds: number,
    completed: boolean,
    force = false,
  ) {
    if (force) return true;
    if (completed !== lastPersistedCompletedRef.current) return true;
    if (completed) return false;
    return (
      Math.abs(positionSeconds - lastPersistedProgressRef.current) >=
      PROGRESS_SAVE_MIN_DELTA_SECONDS
    );
  }

  async function persistProgress(
    positionSeconds: number,
    completed: boolean,
    options?: { force?: boolean },
  ) {
    if (!profile || !videoId) return;
    const safePosition = normalizeProgressPosition(positionSeconds);
    if (!shouldPersistProgress(safePosition, completed, options?.force)) return;
    await api.updateProgress(videoId, {
      user_id: profile.id,
      position_seconds: safePosition,
      completed,
    });
    lastPersistedProgressRef.current = safePosition;
    lastPersistedCompletedRef.current = completed;
  }

  function persistProgressInBackground(
    positionSeconds: number,
    completed: boolean,
    options?: { force?: boolean },
  ) {
    if (!profile || !videoId) return;
    const safePosition = normalizeProgressPosition(positionSeconds);
    if (!shouldPersistProgress(safePosition, completed, options?.force)) return;
    if (
      lastBackgroundProgressRef.current?.positionSeconds === safePosition &&
      lastBackgroundProgressRef.current?.completed === completed
    ) {
      return;
    }
    lastBackgroundProgressRef.current = {
      positionSeconds: safePosition,
      completed,
    };
    lastPersistedProgressRef.current = safePosition;
    lastPersistedCompletedRef.current = completed;
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone?.trim();
    void fetch(`/api/videos/${videoId}/progress`, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        ...(timezone ? { "X-Halcyon-Timezone": timezone } : {}),
      },
      body: JSON.stringify({
        user_id: profile.id,
        position_seconds: safePosition,
        completed,
      }),
    }).catch(() => undefined);
  }

  async function handleSync(force = false) {
    if (!videoId) return;
    setSyncing(true);
    pushToast(
      "info",
      force ? "Forced resync started" : "Video sync started",
      displayVideoTitle,
    );
    try {
      const result: any = await api.syncVideo(videoId, { force });
      setData(await api.video(currentVideoRef));
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast(
          "error",
          force
            ? "Forced resync completed with issues"
            : "Video sync completed with issues",
          result?.details?.warning ??
            result?.details?.error ??
            displayVideoTitle,
        );
      } else {
        pushToast(
          "success",
          force ? "Forced resync finished" : "Video sync finished",
          displayVideoTitle,
        );
      }
    } catch (nextError) {
      pushToast(
        "error",
        force ? "Forced resync failed" : "Video sync failed",
        nextError instanceof Error ? nextError.message : "Unknown sync error",
      );
    } finally {
      setSyncing(false);
      setMenuOpen(false);
    }
  }

  async function handleSendToReview() {
    if (!data) return;
    setReviewing(true);
    pushToast(
      "info",
      "Sending to review",
      `${displayVideoTitle} is being re-scored for manual review.`,
    );
    try {
      await api.sendVideoToReview(data.video.id);
      setData(await api.video(currentVideoRef));
      pushToast(
        "success",
        "Sent to review",
        "Open the sync review queue to approve or manually re-match it.",
        { href: "/sync-review" },
      );
    } catch (nextError) {
      pushToast(
        "error",
        "Unable to send to review",
        nextError instanceof Error ? nextError.message : "Unknown review error",
      );
    } finally {
      setReviewing(false);
      closePlayerMenus();
    }
  }

  async function toggleSubscription() {
    if (!data?.channel?.slug) return;
    setSubscriptionPending(true);
    try {
      await api.toggleSubscription(data.channel.slug);
      setData(await api.video(currentVideoRef));
    } finally {
      setSubscriptionPending(false);
    }
  }

  async function ensurePlaylists() {
    if (playlists.length || playlistLoading) return;
    setPlaylistLoading(true);
    try {
      setPlaylists(await api.playlists());
    } catch (error) {
      pushToast(
        "error",
        "Unable to load playlists",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    } finally {
      setPlaylistLoading(false);
    }
  }

  async function addVideoToPlaylist(playlistId: number) {
    if (!videoId) return;
    try {
      await api.addPlaylistItem(playlistId, videoId);
      pushToast("success", "Added to playlist", displayVideoTitle, {
        href: `/playlists/${playlistId}`,
      });
      setPlaylistMenuOpen(false);
      setMenuOpen(false);
    } catch (error) {
      pushToast(
        "error",
        "Unable to add to playlist",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    }
  }

  async function createPlaylistAndAddVideo(playlistName: string) {
    if (!videoId) return;
    setCreatePlaylistPending(true);
    try {
      const currentProfile = profile ?? (await api.me());
      const created: any = await api.createPlaylist({
        user_id: currentProfile.id,
        name: playlistName,
      });
      const nextPlaylists = await api.playlists();
      setPlaylists(nextPlaylists);
      const createdPlaylist =
        nextPlaylists.find((playlist) => playlist.id === created?.id) ??
        nextPlaylists.find((playlist) => playlist.name === playlistName);

      if (!createdPlaylist) {
        throw new Error("Playlist created but could not be loaded.");
      }

      await api.addPlaylistItem(createdPlaylist.id, videoId);
      pushToast("success", "Added to playlist", displayVideoTitle, {
        href: `/playlists/${createdPlaylist.id}`,
      });
      setCreatePlaylistOpen(false);
      setPlaylistMenuOpen(false);
      setMenuOpen(false);
    } catch (error) {
      pushToast(
        "error",
        "Unable to create playlist",
        error instanceof Error ? error.message : "Unknown playlist error",
      );
    } finally {
      setCreatePlaylistPending(false);
    }
  }

  async function markWatchState(state: "watched" | "unwatched") {
    if (!videoId) return;
    try {
      await api.setWatchState(videoId, state);
      setData(await api.video(currentVideoRef));
      pushToast(
        "success",
        state === "watched" ? "Marked as watched" : "Marked as unwatched",
        displayVideoTitle,
      );
    } catch (error) {
      pushToast(
        "error",
        "Unable to update watch state",
        error instanceof Error ? error.message : "Unknown watch state error",
      );
    } finally {
      closePlayerMenus();
    }
  }

  async function addVideoToQueue() {
    if (!videoId) return;
    try {
      await api.addQueueItem(videoId);
      pushToast("success", "Added to queue", displayVideoTitle, { href: "/?topic=queue" });
    } catch (error) {
      pushToast(
        "error",
        "Unable to add to queue",
        error instanceof Error ? error.message : "Unknown queue error",
      );
    } finally {
      closePlayerMenus();
    }
  }

  async function resolveAutoplayTarget() {
    try {
      const queueState: any = await api.queue();
      const queueItems = Array.isArray(queueState?.items)
        ? queueState.items
        : Array.isArray(queueState?.queue)
          ? queueState.queue
        : Array.isArray(queueState)
          ? queueState
          : [];
      const currentIndex = queueItems.findIndex(
        (entry: any) => {
          const queuedVideo = entry?.video ?? entry;
          return (
            queuedVideo?.id === videoId ||
            queuedVideo?.watch_ref === currentVideoRef
          );
        },
      );
      const queueTarget = (
        currentIndex >= 0 ? queueItems.slice(currentIndex + 1) : queueItems
      )
        .map((entry: any) => entry?.video ?? entry)
        .find((video: any) => video && video.id !== videoId);
      const nextFromQueue = toAutoplayTarget(queueTarget);
      if (nextFromQueue) return nextFromQueue;
    } catch {
      // Fall back to next up or suggested videos if queue lookup fails.
    }

    return (
      toAutoplayTarget(
        data?.next_up && data.next_up.id !== videoId ? data.next_up : null,
      ) ??
      toAutoplayTarget(
        visibleSuggestedItems.find((item: any) => item.id !== videoId),
      )
    );
  }

  function clearAutoplayTimers() {
    if (autoplayCountdownTimerRef.current) {
      window.clearInterval(autoplayCountdownTimerRef.current);
      autoplayCountdownTimerRef.current = null;
    }
    if (autoplayNavigateTimerRef.current) {
      window.clearTimeout(autoplayNavigateTimerRef.current);
      autoplayNavigateTimerRef.current = null;
    }
  }

  function cancelAutoplayCountdown() {
    clearAutoplayTimers();
    autoplayTransitionRef.current = false;
    setAutoplayTarget(null);
    setAutoplayCountdown(null);
  }

  function playAutoplayNow() {
    if (!autoplayTarget) return;
    clearAutoplayTimers();
    navigate(`/video/${autoplayTarget.watch_ref}`);
  }

  function startAutoplayCountdown(target: AutoplayTarget) {
    clearAutoplayTimers();
    setAutoplayTarget(target);
    setAutoplayCountdown(5);
    autoplayCountdownTimerRef.current = window.setInterval(() => {
      setAutoplayCountdown((current) => {
        if (current == null) return current;
        if (current <= 1) {
          if (autoplayCountdownTimerRef.current) {
            window.clearInterval(autoplayCountdownTimerRef.current);
            autoplayCountdownTimerRef.current = null;
          }
          return 0;
        }
        return current - 1;
      });
    }, 1000);
    autoplayNavigateTimerRef.current = window.setTimeout(() => {
      navigate(`/video/${target.watch_ref}`);
    }, 5000);
  }

  function openPlaylistMenu() {
    if (playlistMenuCloseTimerRef.current) {
      window.clearTimeout(playlistMenuCloseTimerRef.current);
      playlistMenuCloseTimerRef.current = null;
    }
    void ensurePlaylists();
    setPlaylistMenuOpen(true);
  }

  function closePlaylistMenu(event?: FocusEvent<HTMLDivElement>) {
    const nextTarget = event?.relatedTarget as Node | null;
    if (nextTarget && event?.currentTarget.contains(nextTarget)) return;
    if (playlistMenuCloseTimerRef.current) {
      window.clearTimeout(playlistMenuCloseTimerRef.current);
    }
    playlistMenuCloseTimerRef.current = window.setTimeout(() => {
      setPlaylistMenuOpen(false);
      playlistMenuCloseTimerRef.current = null;
    }, 180);
  }

  async function setReaction(nextReaction: "like" | "dislike") {
    if (!data || !videoId) return;
    const current = data.video.user_reaction ?? null;
    const resolved = current === nextReaction ? null : nextReaction;
    setReactionPending(nextReaction);
    try {
      await api.setReaction(videoId, resolved);
      setData((currentData: any) =>
        currentData
          ? {
              ...currentData,
              video: {
                ...currentData.video,
                user_reaction: resolved,
              },
            }
          : currentData,
      );
    } finally {
      setReactionPending(null);
    }
  }

  async function toggleSavedVideo() {
    if (!data || !videoId || savePending) return;
    setSavePending(true);
    try {
      const result = await api.toggleSavedVideo(videoId);
      setData((currentData: any) =>
        currentData
          ? {
              ...currentData,
              video: {
                ...currentData.video,
                user_saved: result.saved,
              },
            }
          : currentData,
      );
      pushToast(
        "success",
        result.saved ? "Saved video" : "Removed saved video",
        displayVideoTitle,
        result.saved && profile ? { href: `/profile/${profile.name}/saved` } : undefined,
      );
    } finally {
      setSavePending(false);
    }
  }

  if (loading) {
    return (
      <div className="page-stack watch-page">
        <section className="watch-layout watch-layout-loading">
          <div className="watch-stage-row">
            <div className="watch-main-column">
              <div className="watch-player-slot">
                <div className="watch-skeleton-player">
                  <span className="loading-dots" aria-label="Loading video">
                    <span>.</span>
                    <span>.</span>
                    <span>.</span>
                  </span>
                </div>
              </div>
              <div className="watch-meta-slot">
                <div className="watch-skeleton-line watch-skeleton-title" />
                <div className="watch-skeleton-line watch-skeleton-meta" />
                <div className="watch-skeleton-line watch-skeleton-meta short" />
              </div>
            </div>
            <aside className="watch-sidebar">
              {Array.from({ length: 6 }).map((_, index) => (
                <div className="suggestion-row suggestion-skeleton" key={index}>
                  <span className="suggestion-thumb" />
                  <span className="suggestion-copy">
                    <span className="watch-skeleton-line" />
                    <span className="watch-skeleton-line short" />
                  </span>
                </div>
              ))}
            </aside>
          </div>
        </section>
      </div>
    );
  }
  const normalizedError = (error ?? "").trim().toLowerCase();
  const isInternalServerError =
    normalizedError === "internal server error" ||
    normalizedError.includes("500");
  if (error || !data) {
    if (isInternalServerError) {
      return (
        <GhostWatchPage
          displayMode={displayMode}
          message="Internal server error."
        />
      );
    }
    return <div className="panel error">{error ?? "Video not found"}</div>;
  }

  const sourceUnavailable =
    !data.playback.stream_url || Boolean(data.media_info?.source_missing);
  if (sourceUnavailable) {
    return (
      <GhostWatchPage
        displayMode={displayMode}
        message="File was not found in filesystem."
      />
    );
  }

  return (
    <div
      className={`page-stack watch-page ${displayMode === "theater" ? "watch-page-theater" : ""}`}
    >
      <section className="watch-layout">
        <div className="watch-stage-row">
          <div className="watch-main-column">
              <div className="watch-player-slot">
              <div
                className={`video-frame advanced-player watch-player-frame ${
                  touchOverlayDevice ? "touch-overlay-device" : ""
                } ${playerOverlayVisible ? "player-overlay-visible" : ""} ${
                  menuOpen || playlistMenuOpen ? "player-menu-open" : ""
                }`}
                onPointerDown={() => {
                  if (!touchOverlayDevice) return;
                  setPlayerOverlayVisible(true);
                }}
              >
                <HalcyonPlayer
                  source={data.playback.stream_url}
                  autoplay={preferences.autoplay}
                  captions={data.captions ?? []}
                  captionsEnabled={preferences.captionsEnabled}
                  chapters={chapters}
                  mousewheelVolumeControl={preferences.mousewheelVolumeControl}
                  aspectRatio={playerAspectRatio}
                  mode={displayMode}
                  onCaptionsChange={onCaptionsPreferenceChange}
                  onLoadingChange={(next) => {
                    setPlayerLoading(next);
                    if (next) setPlayerError(null);
                  }}
                  onFatalError={(message) => setPlayerError(message)}
                  onReady={(video, player: Plyr | null) => {
                    videoNodeRef.current = video;
                    setPlayerError(null);
                    const targetPosition = startAtSeconds ?? resumablePosition;
                    if (
                      targetPosition &&
                      Math.abs(video.currentTime - targetPosition) > 5
                    ) {
                      video.currentTime = targetPosition;
                    }
                    if (preferences.autoplay) {
                      const playTarget = player ?? video;
                      Promise.resolve(playTarget.play()).catch(() => undefined);
                    }
                  }}
                  onPause={(video) => {
                    void persistProgress(Math.floor(video.currentTime), false);
                  }}
                  onEnded={async (video) => {
                    if (autoplayTransitionRef.current) return;
                    autoplayTransitionRef.current = true;
                    let navigated = false;
                    try {
                      await persistProgress(
                        Math.floor(video.duration || data.video.duration_seconds),
                        true,
                      );
                      const nextAutoplayTarget =
                        preferences.autoplay
                          ? await resolveAutoplayTarget()
                          : null;
                      if (nextAutoplayTarget) {
                        navigated = true;
                        startAutoplayCountdown(nextAutoplayTarget);
                        return;
                      }
                      setData(await api.video(currentVideoRef));
                    } finally {
                      if (!navigated) {
                        autoplayTransitionRef.current = false;
                      }
                    }
                  }}
                />
                {playerLoading ? (
                  <div className="player-loading-overlay">
                    <span className="loading-dots" aria-label="Loading video">
                      <span>.</span>
                      <span>.</span>
                      <span>.</span>
                    </span>
                  </div>
                ) : null}
                {playerError ? (
                  <div className="player-error-banner">{playerError}</div>
                ) : null}
                {autoplayTarget && autoplayCountdown !== null ? (
                  <div className="player-autoplay-overlay" role="status" aria-live="polite">
                    <div className="player-autoplay-card">
                      <div className="player-autoplay-kicker">Up next</div>
                      <div className="player-autoplay-preview">
                        {autoplayTarget.thumbnail_url ? (
                          <img
                            className="player-autoplay-thumb"
                            src={autoplayTarget.thumbnail_url}
                            alt=""
                          />
                        ) : (
                          <div className="player-autoplay-thumb player-autoplay-thumb-fallback">
                            Next
                          </div>
                        )}
                        <div className="player-autoplay-copy">
                          <strong>{normalizeImportedText(autoplayTarget.title) ?? autoplayTarget.title}</strong>
                          <span>
                            {normalizeImportedText(autoplayTarget.channel_name) ??
                              autoplayTarget.channel_name ??
                              "Unknown channel"}
                          </span>
                          <span>
                            Playing in {autoplayCountdown}...
                            {autoplayTarget.published_at
                              ? ` ${formatRelativeDate(autoplayTarget.published_at)}`
                              : ""}
                          </span>
                        </div>
                      </div>
                      <div className="player-autoplay-actions">
                        <button
                          type="button"
                          className="ghost-button player-autoplay-action is-primary"
                          onClick={playAutoplayNow}
                        >
                          Play now
                        </button>
                        <button
                          type="button"
                          className="ghost-button player-autoplay-action"
                          onClick={cancelAutoplayCountdown}
                        >
                          Stay here
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}

                <div className="player-topbar">
                  <div className="player-topbar-right">
                    <button
                      className={`icon-button floating-control ${statsOpen ? "active-chip" : ""}`}
                      onClick={() => {
                        setPlayerOverlayVisible(true);
                        setStatsOpen((current) => !current);
                      }}
                      aria-label="Playback stats"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        className="icon-button-svg"
                        aria-hidden="true"
                      >
                        <path
                          d="M5 18V9m7 9V5m7 13v-7"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                        />
                      </svg>
                    </button>
                    <button
                      className={`icon-button floating-control floating-control-fades-when-active ${displayMode === "theater" ? "active-chip" : ""}`}
                      onClick={() => {
                        setPlayerOverlayVisible(true);
                        setDisplayMode((current) =>
                          current === "default" ? "theater" : "default",
                        );
                      }}
                      aria-label="Toggle theater mode"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        className="icon-button-svg"
                        aria-hidden="true"
                      >
                        <rect
                          x="4.5"
                          y="6.5"
                          width="15"
                          height="11"
                          rx="1.8"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                        />
                        <path
                          d="M8 9.5h8"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                        />
                      </svg>
                    </button>
                    <button
                      className="icon-button floating-control"
                      onClick={(event) => {
                        event.stopPropagation();
                        setPlayerOverlayVisible(true);
                        setMenuOpen((current) => !current);
                      }}
                      aria-label="Player actions"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        className="icon-button-svg"
                        aria-hidden="true"
                      >
                        <circle cx="12" cy="5" r="2.15" fill="currentColor" />
                        <circle cx="12" cy="12" r="2.15" fill="currentColor" />
                        <circle cx="12" cy="19" r="2.15" fill="currentColor" />
                      </svg>
                    </button>
                    {menuOpen ? (
                      <div className="player-menu">
                        {(data.channel?.slug ??
                        data.video.channel_slug ??
                        data.video.channel_id) ? (
                          <button
                            className="menu-item"
                            onClick={() => {
                              closePlayerMenus();
                              openChannel();
                            }}
                          >
                            Go to channel
                          </button>
                        ) : null}
                        <div
                          className={`menu-item-group ${playlistMenuOpen ? "is-open" : ""}`}
                          onMouseEnter={openPlaylistMenu}
                          onMouseLeave={() => closePlaylistMenu()}
                          onFocusCapture={openPlaylistMenu}
                          onBlurCapture={closePlaylistMenu}
                        >
                          <button
                            className="menu-item menu-item-with-arrow"
                            type="button"
                            aria-haspopup="menu"
                            aria-expanded={playlistMenuOpen}
                            onClick={async () => {
                              await ensurePlaylists();
                              setPlaylistMenuOpen((current) => !current);
                            }}
                          >
                            <span>Add to playlist</span>
                            <svg
                              viewBox="0 0 20 20"
                              className="menu-caret"
                              aria-hidden="true"
                            >
                              <path
                                d="m7 4 6 6-6 6"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="1.8"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </button>
                    {playlistMenuOpen ? (
                      <div className="menu-submenu player-playlist-submenu">
                        <button
                          className="menu-item playlist-create-item"
                          onClick={() => {
                            closePlayerMenus();
                            setCreatePlaylistOpen(true);
                          }}
                        >
                          + New playlist
                        </button>
                              <div className="menu-section-label">
                                Playlists
                              </div>
                              <div className="playlist-submenu-scroll">
                                {playlistLoading ? (
                                  <div className="search-results-empty">
                                    Loading playlists...
                                  </div>
                                ) : null}
                                {!playlistLoading && playlists.length
                                  ? playlists.map((playlist) => (
                                      <button
                                        key={playlist.id}
                                        className="menu-item"
                                        onClick={() =>
                                          void addVideoToPlaylist(playlist.id)
                                        }
                                      >
                                        {normalizeImportedText(playlist.name) ??
                                          playlist.name}
                                      </button>
                                    ))
                                  : null}
                                {!playlistLoading && !playlists.length ? (
                                  <div className="search-results-empty">
                                    No playlists yet.
                                  </div>
                                ) : null}
                              </div>
                            </div>
                          ) : null}
                        </div>
                        <button
                          className="menu-item"
                          onClick={() => void addVideoToQueue()}
                        >
                          Add to queue
                        </button>
                        {!videoWatched ? (
                          <button
                            className="menu-item"
                            onClick={() => void markWatchState("watched")}
                          >
                            Mark as watched
                          </button>
                        ) : null}
                        {canMarkVideoUnwatched ? (
                          <button
                            className="menu-item"
                            onClick={() => void markWatchState("unwatched")}
                          >
                            Mark as unwatched
                          </button>
                        ) : null}
                        {canManageVideo ? (
                          <>
                            <button
                              className="menu-item"
                              onClick={() => void handleSync()}
                              disabled={syncing || reviewing}
                            >
                              {syncing ? "Syncing..." : "Sync"}
                            </button>
                            <button
                              className="menu-item"
                              onClick={() => void handleSync(true)}
                              disabled={syncing || reviewing}
                            >
                              {syncing ? "Syncing..." : "Force sync"}
                            </button>
                            <button
                              className="menu-item"
                              onClick={() => void handleSendToReview()}
                              disabled={syncing || reviewing}
                            >
                              {reviewing ? "Sending..." : "Send to review"}
                            </button>
                            <button
                              className="menu-item"
                              onClick={() => {
                                closePlayerMenus();
                                setEditOpen(true);
                              }}
                            >
                              Edit metadata
                            </button>
                          </>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </div>

                {statsOpen ? (
                  <div className="stats-overlay">
                    <div className="stats-grid">
                      {statsRows.map(([label, value]) => (
                        <div key={label} className="stats-row">
                          <span>{label}</span>
                          <strong>{value}</strong>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </div>

            <div className="watch-meta-slot">
              <section className="watch-meta">
                <h1 className="watch-title">{displayVideoTitle}</h1>
                <div className="watch-action-row">
                  <div className="watch-channel-row">
                    <div className="watch-channel-block">
                      <button
                        className="watch-channel-avatar watch-channel-avatar-button"
                        onClick={openChannel}
                        type="button"
                      >
                        <AvatarImage
                          src={data.channel?.avatar_url}
                          alt={displayChannelName}
                          seed={displayChannelName || "channel"}
                          fallbackText={displayChannelName || "??"}
                        />
                      </button>
                      <span className="watch-channel-copy">
                        <button
                          className="watch-channel-name-button"
                          onClick={openChannel}
                          type="button"
                        >
                          <strong>{displayChannelName}</strong>
                        </button>
                        {data.channel?.subscriber_count ? (
                          <small>
                            {formatCount(data.channel.subscriber_count)}{" "}
                            subscribers
                          </small>
                        ) : null}
                        {displaySeriesName && data.video.series_id ? (
                          <button
                            className="watch-series-link"
                            onClick={openSeries}
                            type="button"
                          >
                            {displaySeriesName}
                          </button>
                        ) : null}
                      </span>
                    </div>
                    {data.channel?.id ? (
                      <button
                        className={`watch-subscribe ${data.channel.subscribed ? "is-subscribed is-compact" : ""} ${subscriptionPending ? "is-pending is-compact" : ""}`}
                        disabled={subscriptionPending}
                        onClick={() => void toggleSubscription()}
                        type="button"
                        aria-label={
                          subscriptionPending
                            ? "Updating subscription"
                            : data.channel.subscribed
                              ? "Subscribed"
                              : "Subscribe"
                        }
                      >
                        {subscriptionPending
                          ? "..."
                          : data.channel.subscribed
                            ? <CheckIcon />
                            : "Subscribe"}
                      </button>
                    ) : null}
                  </div>

                  <div className="watch-engagement-row">
                    <button
                      className={`pill-button reaction-button is-like ${data.video.user_reaction === "like" ? "is-selected" : ""}`}
                      disabled={reactionPending === "like"}
                      onClick={() => void setReaction("like")}
                      type="button"
                    >
                      <ThumbIcon
                        type="like"
                        active={data.video.user_reaction === "like"}
                      />
                      <span>Like</span>
                      {displayedLikeCount != null ? (
                        <strong>{formatCount(displayedLikeCount)}</strong>
                      ) : null}
                    </button>
                    <button
                      className={`pill-button reaction-button is-dislike ${data.video.user_reaction === "dislike" ? "is-selected" : ""}`}
                      disabled={reactionPending === "dislike"}
                      onClick={() => void setReaction("dislike")}
                      type="button"
                    >
                      <ThumbIcon
                        type="dislike"
                        active={data.video.user_reaction === "dislike"}
                      />
                      <span>Dislike</span>
                      {displayedDislikeCount != null ? (
                        <strong>{formatCount(displayedDislikeCount)}</strong>
                      ) : null}
                    </button>
                    <button
                      className="pill-button"
                      onClick={() => setShareOpen(true)}
                      type="button"
                    >
                      <ShareIcon />
                      Share
                    </button>
                    <button
                      className={`pill-button save-video-button ${data.video.user_saved ? "is-selected" : ""}`}
                      disabled={savePending}
                      onClick={() => void toggleSavedVideo()}
                      type="button"
                      aria-label={data.video.user_saved ? "Remove saved video" : "Save video"}
                    >
                      <BookmarkIcon active={data.video.user_saved} />
                    </button>
                  </div>
                </div>

                <div
                  className={`watch-description ${descriptionExpanded ? "expanded" : ""} ${canExpandDescription ? "is-expandable" : ""}`}
                  onClick={() => {
                    if (!descriptionExpanded && canExpandDescription) {
                      setDescriptionExpanded(true);
                    }
                  }}
                >
                  {viewCountLabel || publishedAtLabel || engagementRatio != null ? (
                    <div className="watch-description-head">
                      <div className="watch-description-meta">
                        {viewCountLabel ? <strong>{viewCountLabel}</strong> : null}
                        {publishedAtLabel ? <span>{publishedAtLabel}</span> : null}
                      </div>
                      <div
                        className="watch-rating-wrap"
                        role="img"
                        aria-label={engagementTooltip ?? "No like or dislike data"}
                      >
                        <div
                          className={`watch-rating-bar ${
                            engagementRatio == null ? "is-empty" : ""
                          }`}
                        >
                          <span
                            className="watch-rating-like"
                            style={{
                              width: engagementRatio == null ? "100%" : `${engagementRatio * 100}%`,
                            }}
                          />
                          {engagementRatio != null ? (
                            <span
                              className="watch-rating-dislike"
                              style={{ width: `${(1 - engagementRatio) * 100}%` }}
                            />
                          ) : null}
                        </div>
                        {engagementTooltip ? (
                          <span className="watch-rating-tooltip" role="tooltip">
                            {engagementTooltip}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  ) : null}
                  <LinkifiedText
                    text={description || "No description yet."}
                    className="watch-description-copy linkified-text"
                  />
                  {canExpandDescription || addedAtLabel ? (
                    <div className="watch-description-footer">
                      {canExpandDescription && !descriptionExpanded ? (
                        <button
                          className="description-text-button description-ellipsis-button"
                          onClick={(event) => {
                            event.stopPropagation();
                            setDescriptionExpanded(true);
                          }}
                          type="button"
                        >
                          ...
                        </button>
                      ) : canExpandDescription ? (
                        <button
                          className="description-text-button"
                          onClick={(event) => {
                            event.stopPropagation();
                            setDescriptionExpanded(false);
                          }}
                          type="button"
                        >
                          Show less
                        </button>
                      ) : null}
                      {addedAtLabel && (descriptionExpanded || !canExpandDescription) ? (
                        <small className="watch-added-at">Added on {addedAtLabel}</small>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </section>
            </div>
            <div className="watch-comments-slot">
              <section
                className={`watch-comments ${
                  canCollapseComments ? "is-collapsible" : ""
                } ${commentsExpanded ? "is-expanded" : "is-collapsed"} ${
                  usesCompactCommentPreview ? "is-compact-preview" : "is-standard-preview"
                }`}
              >
                <div
                  className={`section-heading ${
                    canCollapseComments ? "watch-comments-summary is-clickable" : ""
                  }`}
                  onClick={() => {
                    if (canCollapseComments) {
                      toggleCommentsExpanded();
                    }
                  }}
                  role={canCollapseComments ? "button" : undefined}
                  tabIndex={canCollapseComments ? 0 : undefined}
                  onKeyDown={(event) => {
                    if (!canCollapseComments) return;
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggleCommentsExpanded();
                    }
                  }}
                  aria-expanded={canCollapseComments ? commentsExpanded : undefined}
                >
                  <div className="watch-comments-heading">
                    <h2>Comments</h2>
                    {canCollapseComments ? (
                      <button
                        className={`watch-comments-toggle ${
                          commentsExpanded ? "is-expanded" : ""
                        }`}
                        aria-expanded={commentsExpanded}
                        aria-label={
                          commentsExpanded
                            ? "Collapse comments"
                            : "Expand comments"
                        }
                        onClick={(event) => {
                          event.stopPropagation();
                          toggleCommentsExpanded();
                        }}
                        type="button"
                      >
                        <svg viewBox="0 0 20 20" aria-hidden="true">
                          <path
                            d="m8 5 5 5-5 5"
                            fill="none"
                            stroke="currentColor"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth="1.8"
                          />
                        </svg>
                      </button>
                    ) : null}
                  </div>
                  <span>{totalComments.length}</span>
                </div>
                <div
                  className={`comment-stack ${
                    canCollapseComments && !commentsExpanded ? "is-preview" : ""
                  } ${
                    !usesCompactCommentPreview && canCollapseComments && !commentsExpanded
                      ? "is-standard-preview"
                      : ""
                  } ${
                    usesCompactCommentPreview && canCollapseComments && !commentsExpanded
                      ? "is-compact-preview"
                      : ""
                  }`}
                >
                  {previewedComments.length ? (
                    showCollapsedCommentPreview ? (
                      previewedComments.map((comment, index: number) => (
                        <article
                          key={comment.id}
                          className={`watch-comment-preview-row ${
                            index === previewOverflowCommentIndex
                              ? "is-faded-preview"
                              : ""
                          }`}
                        >
                          <span className="comment-avatar">
                            <AvatarImage
                              src={null}
                              alt={comment.author_name}
                              seed={`${comment.id}-${comment.author_name}`}
                              fallbackText={comment.author_name}
                            />
                          </span>
                          <div className="watch-comment-preview-copy">
                            <strong>{comment.author_name}</strong>
                            <p>{comment.body}</p>
                          </div>
                        </article>
                      ))
                    ) : (
                      previewedComments.map((comment, index: number) => {
                        const reactionKey = comment.youtube_comment_id ?? String(comment.id);
                        const visibleReplies = comment.replies ?? [];
                        const repliesExpanded = Boolean(expandedReplies[comment.id]);
                        return (
                          <article
                            key={comment.id}
                            className={`comment-card watch-comment-card ${
                              index === previewOverflowCommentIndex
                                ? "is-faded-preview"
                                : ""
                            }`}
                          >
                            <span className="comment-avatar">
                              <AvatarImage
                                src={null}
                                alt={comment.author_name}
                                seed={`${comment.id}-${comment.author_name}`}
                                fallbackText={comment.author_name}
                              />
                            </span>
                            <div className="comment-body">
                              <strong>{comment.author_name}</strong>
                              <p>{comment.body}</p>
                              <div className="comment-actions">
                                <button
                                  className={`comment-reaction-button is-like ${commentReactions[reactionKey] === "like" ? "is-selected" : ""}`}
                                  onClick={() =>
                                    setCommentReaction(
                                      reactionKey,
                                      "like",
                                    )
                                  }
                                  type="button"
                                >
                                  <ThumbIcon
                                    type="like"
                                    active={
                                      commentReactions[
                                        reactionKey
                                      ] === "like"
                                    }
                                  />
                                  <span>
                                    {formatCount(comment.like_count) || "0"}
                                  </span>
                                </button>
                                <button
                                  className={`comment-reaction-button is-dislike ${commentReactions[reactionKey] === "dislike" ? "is-selected" : ""}`}
                                  onClick={() =>
                                    setCommentReaction(
                                      reactionKey,
                                      "dislike",
                                    )
                                  }
                                  type="button"
                                >
                                  <ThumbIcon
                                    type="dislike"
                                    active={
                                      commentReactions[
                                        reactionKey
                                      ] === "dislike"
                                    }
                                  />
                                </button>
                              </div>
                              {visibleReplies.length ? (
                                <div className="watch-comment-replies">
                                  <button
                                    className={`watch-comment-replies-header ${
                                      repliesExpanded ? "is-expanded" : ""
                                    }`}
                                    onClick={() => toggleReplies(comment.id)}
                                    type="button"
                                  >
                                    <strong>
                                      {comment.reply_count === 1 ? "1 reply" : `${comment.reply_count} replies`}
                                    </strong>
                                    {repliesExpanded && comment.reply_count > visibleReplies.length ? (
                                      <small>
                                        Showing {visibleReplies.length} of {comment.reply_count}
                                      </small>
                                    ) : null}
                                    <span className="watch-comment-replies-toggle">
                                      {repliesExpanded ? "Hide" : "Show"}
                                    </span>
                                  </button>
                                  {repliesExpanded ? (
                                    <div className="watch-comment-replies-list">
                                      {visibleReplies.map((reply) => (
                                        <article className="watch-comment-reply" key={reply.id}>
                                          <span className="comment-avatar is-reply-avatar">
                                            <AvatarImage
                                              src={null}
                                              alt={reply.author_name}
                                              seed={`${reply.id}-${reply.author_name}`}
                                              fallbackText={reply.author_name}
                                            />
                                          </span>
                                          <div className="watch-comment-reply-copy">
                                            <strong>{reply.author_name}</strong>
                                            <p>{reply.body}</p>
                                          </div>
                                        </article>
                                      ))}
                                    </div>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          </article>
                        );
                      })
                    )
                  ) : totalComments.length === 0 ? (
                    <p className="muted-copy">No synced comments yet.</p>
                  ) : null}
                </div>
                {canCollapseComments &&
                !commentsExpanded &&
                !usesCompactCommentPreview ? (
                  <button
                    className="watch-comments-preview-action"
                    onClick={() => setCommentsExpanded(true)}
                    type="button"
                  >
                    Show all comments
                  </button>
                ) : null}
              </section>
            </div>
          </div>
          <aside className="watch-sidebar">
            {data.next_up ? (
              <section className="watch-sidebar-section">
                <div className="section-heading">
                  <h2>Up Next</h2>
                </div>
                <WatchSuggestionRow
                  item={data.next_up}
                  onRefresh={refreshWatchPage}
                  profile={profile}
                />
              </section>
            ) : null}

            <section className="watch-sidebar-section">
              <div className="watch-filter-row">
                <button
                  className={`topic-chip ${suggestionFilter === "suggested" ? "active" : ""}`}
                  onClick={() => setSuggestionFilter("suggested")}
                  type="button"
                >
                  Suggested
                </button>
                <button
                  className={`topic-chip ${suggestionFilter === "related" ? "active" : ""}`}
                  onClick={() => setSuggestionFilter("related")}
                  type="button"
                >
                  Explore
                </button>
              </div>
              {showSuggestionLoadingState ? (
                <div className="suggestion-stack">
                  {Array.from({ length: 5 }).map((_, index) => (
                    <WatchSuggestionSkeletonRow
                      key={`suggestion-skeleton-${suggestionFilter}-${index}`}
                    />
                  ))}
                </div>
              ) : visibleSuggestedItems.length ? (
                <div className="suggestion-stack">
                  {visibleSuggestedItems.map((item: any) => (
                    <WatchSuggestionRow
                      key={item.id}
                      item={item}
                      onRefresh={refreshWatchPage}
                      profile={profile}
                    />
                  ))}
                  {activeSuggestionFeed.loadingMore
                    ? Array.from({ length: 2 }).map((_, index) => (
                        <WatchSuggestionSkeletonRow
                          key={`suggestion-more-${suggestionFilter}-${index}`}
                        />
                      ))
                    : null}
                  {activeSuggestionFeed.hasMore ? (
                    <div
                      ref={suggestionsSentinelRef}
                      className="suggestion-load-sentinel"
                      aria-hidden="true"
                    />
                  ) : null}
                </div>
              ) : (
                <WatchSuggestionEmptyState
                  message={
                    suggestionFilter === "suggested"
                      ? "Nothing to suggest! You've won!"
                      : "Nothing to explore! You've seen it all!"
                  }
                />
              )}
            </section>
          </aside>
        </div>
      </section>

      {editOpen ? (
        <MetadataEditorModal
          videoId={videoId ?? data.video.id}
          initialTitle={displayVideoTitle}
          initialDescription={description}
          onClose={() => setEditOpen(false)}
          onSaved={async () => {
            setData(await api.video(currentVideoRef));
          }}
        />
      ) : null}
      {shareOpen ? (
        <Modal title="Share video" onClose={() => setShareOpen(false)}>
          <div className="modal-form share-modal">
            <label className="settings-field">
              <span>Link</span>
              <input value={shareUrl} readOnly />
            </label>
            <label className="share-timestamp-row">
              <input
                type="checkbox"
                checked={shareAtTimestamp}
                onChange={(event) => setShareAtTimestamp(event.target.checked)}
              />
              <span>Start at current time</span>
            </label>
            <div className="row-actions">
              <button
                className="ghost-button"
                onClick={() => setShareOpen(false)}
                type="button"
              >
                Close
              </button>
              <button
                className="action-button"
                onClick={() => void copyShareUrl()}
                type="button"
              >
                Copy link
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
      {createPlaylistOpen ? (
        <PlaylistCreateModal
          pending={createPlaylistPending}
          onClose={() => {
            if (!createPlaylistPending) {
              setCreatePlaylistOpen(false);
            }
          }}
          onCreate={(name) => createPlaylistAndAddVideo(name)}
        />
      ) : null}
    </div>
  );
}
