import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type MouseEvent,
} from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router-dom";
import { api, type PlaylistSummary } from "../api/client";
import { AvatarImage } from "./AvatarImage";
import { pushToast } from "../lib/notifications";
import { EmptyState } from "./EmptyState";
import { MetadataEditorModal } from "./MetadataEditorModal";
import { PlaylistCreateModal } from "./PlaylistCreateModal";
import {
  formatCount,
  formatDuration,
  formatRelativeDate,
  normalizeImportedText,
} from "../lib/format";

export type CardItem = {
  id: number;
  watch_ref?: string;
  title: string;
  reason?: string | null;
  channel?: string | null;
  channel_slug?: string | null;
  channel_avatar_url?: string | null;
  series?: string | null;
  channel_id?: number | null;
  series_id?: number | null;
  duration_seconds: number;
  thumbnail_url?: string | null;
  progress_seconds: number;
  watched?: boolean;
  created_at?: string | null;
  published_at?: string | null;
  youtube_view_count?: number | null;
  youtube_like_count?: number | null;
  youtube_comment_count?: number | null;
};

type Props = {
  title?: string;
  titleHref?: string;
  titleCount?: number | string;
  items: CardItem[];
  onRefresh?: () => Promise<void> | void;
  layout?: "shelf" | "grid";
  emptyMessage?: string;
  getNavigationState?: (item: CardItem) => unknown;
  beforeNavigate?: (item: CardItem) => void;
  canManageVideo?: boolean;
};

export function VideoCard({
  item,
  onRefresh,
  compact = false,
  navigationState,
  beforeNavigate,
  canManageVideo = false,
}: {
  item: CardItem;
  onRefresh?: () => Promise<void> | void;
  compact?: boolean;
  navigationState?: unknown;
  beforeNavigate?: (item: CardItem) => void;
  canManageVideo?: boolean;
}) {
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [playlistsOpen, setPlaylistsOpen] = useState(false);
  const [playlistsLoading, setPlaylistsLoading] = useState(false);
  const [playlists, setPlaylists] = useState<PlaylistSummary[]>([]);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);
  const [createPlaylistPending, setCreatePlaylistPending] = useState(false);
  const [previewSource, setPreviewSource] = useState<string | null>(null);
  const [previewMuted, setPreviewMuted] = useState(true);
  const [previewError, setPreviewError] = useState(false);
  const [localProgressSeconds, setLocalProgressSeconds] = useState(
    item.progress_seconds,
  );
  const [localWatched, setLocalWatched] = useState(item.watched);
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const hoverTimerRef = useRef<number | null>(null);
  const playlistsCloseTimerRef = useRef<number | null>(null);
  const progressPercent =
    item.duration_seconds > 0
      ? Math.min(100, (localProgressSeconds / item.duration_seconds) * 100)
      : 0;
  const isWatched = localWatched ?? progressPercent >= 99.5;
  const canMarkUnwatched = isWatched || localProgressSeconds > 0;
  const displayTitle = normalizeImportedText(item.title) ?? item.title;
  const displayChannel =
    normalizeImportedText(item.channel) ?? item.channel ?? "Unknown channel";
  const displaySeries = normalizeImportedText(item.series) ?? item.series;
  const statsLine = [
    formatCount(item.youtube_view_count)
      ? `${formatCount(item.youtube_view_count)} views`
      : null,
    formatRelativeDate(item.published_at),
  ]
    .filter(Boolean)
    .join(" • ");
  const isNew =
    !isWatched &&
    item.reason !== "recently-added" &&
    item.published_at != null &&
    Date.now() - new Date(item.published_at).getTime() <= 48 * 60 * 60 * 1000;

  const menuPlacement = useMemo(() => {
    if (!menuAnchor || typeof window === "undefined") return null;
    const width = 176;
    const submenuWidth = 224;
    const estimatedHeight = 170;
    const viewportPadding = 12;
    const left = Math.max(
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
    const resolvedLeft =
      left + width > window.innerWidth - viewportPadding ? fallbackLeft : left;
    const top = Math.max(
      viewportPadding,
      Math.min(
        menuAnchor.top - estimatedHeight - 8,
        window.innerHeight - estimatedHeight - viewportPadding,
      ),
    );
    const submenuSide =
      resolvedLeft + width + submenuWidth + 8 >
      window.innerWidth - viewportPadding
        ? "left"
        : "right";
    return {
      style: { top: `${top}px`, left: `${resolvedLeft}px` },
      submenuSide,
    };
  }, [menuAnchor]);

  function navigateToChannel(event: MouseEvent) {
    event.stopPropagation();
    const target = item.channel_slug ?? item.channel_id;
    if (!target) return;
    navigate(`/channels/${target}`);
  }

  useEffect(() => {
    const node = videoRef.current;
    if (!node) return;
    node.volume = 0.5;
    if (hovered && previewSource && !previewError) {
      void node.play().catch(() => undefined);
      return;
    }
    node.pause();
    node.currentTime = 0;
  }, [hovered, previewError, previewSource]);

  useEffect(() => {
    setPreviewMuted(true);
    setPreviewError(false);
    setPreviewSource(null);
  }, [item.id]);

  useEffect(() => {
    setLocalProgressSeconds(item.progress_seconds);
    setLocalWatched(item.watched);
  }, [item.id, item.progress_seconds, item.watched]);

  useEffect(() => {
    const node = videoRef.current;
    if (!node) return;
    node.volume = 0.5;
  }, [previewMuted, previewSource]);

  useEffect(
    () => () => {
      if (hoverTimerRef.current) {
        window.clearTimeout(hoverTimerRef.current);
      }
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
      if (target.closest(".kebab-button")) return;
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

  async function handleSync(force = false) {
    setSyncing(true);
    pushToast(
      "info",
      force ? "Forced resync started" : "Sync started",
      displayTitle,
    );
    try {
      const result: any = await api.syncVideo(item.id, { force });
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast(
          "error",
          force
            ? "Forced resync completed with issues"
            : "Sync completed with issues",
          result?.details?.warning ?? result?.details?.error ?? displayTitle,
        );
      } else {
        pushToast(
          "success",
          force ? "Forced resync finished" : "Sync finished",
          displayTitle,
        );
      }
      await onRefresh?.();
    } catch (error) {
      pushToast(
        "error",
        force ? "Forced resync failed" : "Sync failed",
        error instanceof Error ? error.message : "Video sync failed",
      );
    } finally {
      setSyncing(false);
      setMenuOpen(false);
    }
  }

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

  async function addToPlaylist(playlistId: number) {
    try {
      await api.addPlaylistItem(playlistId, item.id);
      pushToast("success", "Added to playlist", displayTitle, {
        href: `/playlists/${playlistId}`,
      });
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
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
      const profile = await api.me();
      const created: any = await api.createPlaylist({
        user_id: profile.id,
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
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
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
    try {
      await api.setWatchState(item.id, state);
      if (state === "watched") {
        setLocalWatched(true);
        setLocalProgressSeconds(item.duration_seconds);
      } else {
        setLocalWatched(false);
        setLocalProgressSeconds(0);
      }
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
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
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
      setMenuOpen(false);
      setMenuAnchor(null);
      setPlaylistsOpen(false);
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
    <article
      className={`video-tile ${compact ? "compact-tile" : "grid-tile"} ${isWatched ? "is-watched" : ""}`}
      onMouseEnter={() => {
        if (hoverTimerRef.current) {
          window.clearTimeout(hoverTimerRef.current);
        }
        hoverTimerRef.current = window.setTimeout(() => {
          setHovered(true);
          if (!previewSource) {
            setPreviewSource(`/api/videos/${item.id}/preview`);
          }
        }, 180);
      }}
      onMouseLeave={() => {
        if (hoverTimerRef.current) {
          window.clearTimeout(hoverTimerRef.current);
          hoverTimerRef.current = null;
        }
        setHovered(false);
        setPreviewMuted(true);
      }}
      onClick={() => {
        beforeNavigate?.(item);
        navigate(
          `/video/${item.watch_ref ?? item.id}`,
          navigationState ? { state: navigationState } : undefined,
        );
      }}
    >
      <div className="tile-thumb media-thumb">
        <img
          src={item.thumbnail_url ?? `/api/videos/${item.id}/thumbnail`}
          alt={displayTitle}
          loading="lazy"
        />
        {isWatched ? <span className="watched-badge-overlay">Watched</span> : null}
        {isNew ? <span className="fresh-badge-overlay">New</span> : null}
        {previewSource && !previewError ? (
          <video
            ref={videoRef}
            src={previewSource}
            muted={previewMuted}
            playsInline
            loop
            preload={hovered ? "metadata" : "none"}
            className={hovered ? "preview-video visible" : "preview-video"}
            onLoadedData={() => {
              if (hovered) {
                void videoRef.current?.play().catch(() => undefined);
              }
            }}
            onError={() => setPreviewError(true)}
          />
        ) : null}
        <span className="duration-badge">
          {formatDuration(item.duration_seconds)}
        </span>
        <button
          className="kebab-button"
          aria-label="Video actions"
          onClick={(event) => {
            event.stopPropagation();
            const rect = event.currentTarget.getBoundingClientRect();
            setMenuAnchor(rect);
            setMenuOpen((current) => !current);
          }}
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
        {hovered && previewSource && !previewError ? (
          <button
            className="preview-audio-button"
            aria-label={previewMuted ? "Unmute preview" : "Mute preview"}
            onClick={(event) => {
              event.stopPropagation();
              setPreviewMuted((current) => !current);
            }}
          >
            <svg
              viewBox="0 0 24 24"
              className="icon-button-svg"
              aria-hidden="true"
            >
              {previewMuted ? (
                <>
                  <path
                    d="M5 9h4l4-4v14l-4-4H5z"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinejoin="round"
                  />
                  <path
                    d="m17 9 4 6M21 9l-4 6"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </>
              ) : (
                <>
                  <path
                    d="M5 9h4l4-4v14l-4-4H5z"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinejoin="round"
                  />
                  <path
                    d="M16 9.5a4.5 4.5 0 0 1 0 5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                  <path
                    d="M18.7 7a8 8 0 0 1 0 10"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </>
              )}
            </svg>
          </button>
        ) : null}
        {menuOpen && menuPlacement
          ? createPortal(
              <>
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
                        setMenuOpen(false);
                        setMenuAnchor(null);
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
                            setMenuOpen(false);
                            setMenuAnchor(null);
                            setPlaylistsOpen(false);
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
                                  onClick={() =>
                                    void addToPlaylist(playlist.id)
                                  }
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
                  <button
                    className="menu-item"
                    onClick={() => void addToQueue()}
                  >
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
                  {canManageVideo ? (
                    <>
                      <button
                        className="menu-item"
                        onClick={() => void handleSync()}
                        disabled={syncing}
                      >
                        {syncing ? "Syncing..." : "Sync"}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => void handleSync(true)}
                        disabled={syncing}
                      >
                        {syncing ? "Syncing..." : "Force sync"}
                      </button>
                      <button
                        className="menu-item"
                        onClick={() => {
                          setMenuOpen(false);
                          setMenuAnchor(null);
                          setEditOpen(true);
                        }}
                      >
                        Edit metadata
                      </button>
                    </>
                  ) : null}
                  {item.series_id ? (
                    <button
                      className="menu-item"
                      onClick={() => {
                        setMenuOpen(false);
                        setMenuAnchor(null);
                        navigate(`/series/${item.series_id}`);
                      }}
                    >
                      Go to series
                    </button>
                  ) : null}
                </div>
              </>,
              document.body,
            )
          : null}
        {progressPercent > 0 ? (
          <div className="progress-track">
            <div
              className="progress-fill"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
        ) : null}
      </div>
      <div className="tile-body">
        <div className="tile-meta-row">
          <button
            className="channel-avatar avatar-link"
            onClick={navigateToChannel}
            aria-label={item.channel ? `Open ${item.channel}` : "Open channel"}
          >
            <AvatarImage
              src={item.channel_avatar_url}
              alt={displayChannel}
              seed={item.channel_slug ?? displayChannel ?? `channel-${item.id}`}
              fallbackText={displayChannel ?? "??"}
            />
          </button>
          <div className="tile-copy">
            <strong>{displayTitle}</strong>
            <button className="channel-link-button" onClick={navigateToChannel}>
              {displayChannel}
            </button>
            <small>{statsLine || displaySeries || "Offline library"}</small>
          </div>
        </div>
      </div>
      {editOpen && canManageVideo ? (
        <MetadataEditorModal
          videoId={item.id}
          initialTitle={displayTitle}
          initialDescription={null}
          onClose={() => setEditOpen(false)}
          onSaved={async () => {
            await onRefresh?.();
          }}
        />
      ) : null}
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
    </article>
  );
}

export function VideoRail({
  title,
  titleHref,
  titleCount,
  items,
  onRefresh,
  layout = "shelf",
  emptyMessage,
  getNavigationState,
  beforeNavigate,
  canManageVideo = false,
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [canScrollBack, setCanScrollBack] = useState(false);
  const [canScrollForward, setCanScrollForward] = useState(false);

  useEffect(() => {
    if (layout !== "shelf") return;
    const node = scrollRef.current;
    if (!node) return;

    function updateScrollState() {
      const current = scrollRef.current;
      if (!current) return;
      setCanScrollBack(current.scrollLeft > 12);
      setCanScrollForward(
        current.scrollLeft + current.clientWidth < current.scrollWidth - 12,
      );
    }

    updateScrollState();
    node.addEventListener("scroll", updateScrollState, { passive: true });
    window.addEventListener("resize", updateScrollState);
    return () => {
      node.removeEventListener("scroll", updateScrollState);
      window.removeEventListener("resize", updateScrollState);
    };
  }, [items, layout]);

  function shiftShelf(direction: "back" | "forward") {
    const node = scrollRef.current;
    if (!node) return;
    const amount = Math.max(280, Math.floor(node.clientWidth * 0.82));
    node.scrollBy({
      left: direction === "forward" ? amount : -amount,
      behavior: "smooth",
    });
  }

  return (
    <section className="rail-section">
      {title ? (
        <div className="section-heading">
          <h2>
            {titleHref ? <Link className="section-heading-link" to={titleHref}>{title}</Link> : title}
          </h2>
          {titleCount != null ? <span className="section-count">{titleCount}</span> : null}
        </div>
      ) : null}
      {items.length ? (
        layout === "grid" ? (
          <div className="video-grid-layout">
            {items.map((item) => (
                <VideoCard
                  key={item.id}
                  item={item}
                  onRefresh={onRefresh}
                  compact={false}
                  navigationState={getNavigationState?.(item)}
                  beforeNavigate={beforeNavigate}
                  canManageVideo={canManageVideo}
                />
              ))}
          </div>
        ) : (
          <div className="rail-shell">
            {canScrollBack ? (
              <button
                className="rail-arrow rail-arrow-left"
                onClick={() => shiftShelf("back")}
                aria-label={`Scroll ${title ?? "videos"} left`}
              >
                <svg
                  viewBox="0 0 24 24"
                  className="icon-button-svg"
                  aria-hidden="true"
                >
                  <path
                    d="m14.5 5-7 7 7 7"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            ) : null}
            <div className="rail-scroll" ref={scrollRef}>
              {items.map((item) => (
                <VideoCard
                  key={item.id}
                  item={item}
                  onRefresh={onRefresh}
                  compact
                  navigationState={getNavigationState?.(item)}
                  beforeNavigate={beforeNavigate}
                  canManageVideo={canManageVideo}
                />
              ))}
            </div>
            {canScrollForward ? (
              <button
                className="rail-arrow rail-arrow-right"
                onClick={() => shiftShelf("forward")}
                aria-label={`Scroll ${title ?? "videos"} right`}
              >
                <svg
                  viewBox="0 0 24 24"
                  className="icon-button-svg"
                  aria-hidden="true"
                >
                  <path
                    d="m9.5 5 7 7-7 7"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            ) : null}
          </div>
        )
      ) : (
        <EmptyState
          message={emptyMessage ?? `Nothing in ${(title ?? "videos").toLowerCase()} yet.`}
        />
      )}
    </section>
  );
}
