import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { type Preferences, api } from "../api/client";
import { AvatarImage } from "../components/AvatarImage";
import { LinkifiedText } from "../components/LinkifiedText";
import { useAsyncData } from "../hooks/useAsyncData";
import {
  formatCount,
  formatRelativeDate,
  normalizeImportedText,
} from "../lib/format";

const PLAYER_MODE_STORAGE_KEY = "halcyon.playerMode";

function liveTimestampLabel(value: string | null) {
  if (!value) return null;
  return formatRelativeDate(value) ?? null;
}

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

async function requestFullscreen(element: HTMLElement | null) {
  if (!element) return;
  if (document.fullscreenElement === element) return;
  await element.requestFullscreen();
}

async function exitFullscreen() {
  if (!document.fullscreenElement) return;
  await document.exitFullscreen();
}

type LiveWatchPageProps = {
  preferences: Preferences;
};

export function LiveWatchPage({ preferences }: LiveWatchPageProps) {
  const { youtubeVideoId } = useParams();
  const playerShellRef = useRef<HTMLDivElement | null>(null);
  const [descriptionExpanded, setDescriptionExpanded] = useState(false);
  const [displayMode, setDisplayMode] = useState<"default" | "theater">(() =>
    resolvePlayerModePreference(preferences.defaultPlayerMode),
  );
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [chatHeight, setChatHeight] = useState<number | null>(null);
  const { data, loading, error } = useAsyncData(
    () => (youtubeVideoId ? api.liveStream(youtubeVideoId) : Promise.reject(new Error("Missing live stream id"))),
    [youtubeVideoId],
  );

  useEffect(() => {
    setDisplayMode(resolvePlayerModePreference(preferences.defaultPlayerMode));
  }, [preferences.defaultPlayerMode, youtubeVideoId]);

  useEffect(() => {
    try {
      localStorage.setItem(PLAYER_MODE_STORAGE_KEY, displayMode);
    } catch {
      // Ignore storage failures for local preference-only state.
    }
  }, [displayMode]);

  useEffect(() => {
    function handleFullscreenChange() {
      setIsFullscreen(document.fullscreenElement === playerShellRef.current);
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  useEffect(() => {
    setDescriptionExpanded(false);
  }, [youtubeVideoId]);

  useEffect(() => {
    const node = playerShellRef.current;
    if (!node) return;

    const measureHeight = () => {
      const nextHeight = Math.round(node.getBoundingClientRect().height);
      if (nextHeight > 0) {
        setChatHeight(nextHeight);
      }
    };

    measureHeight();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measureHeight());
    observer.observe(node);
    return () => observer.disconnect();
  }, [displayMode, youtubeVideoId]);

  if (loading && !data) {
    return (
      <div className="page-stack watch-page live-watch-page">
        <section className="watch-layout watch-layout-loading">
          <div className="watch-stage-row">
            <div className="watch-main-column">
              <div className="watch-player-slot">
                <div className="watch-skeleton-player">
                  <span className="loading-dots" aria-label="Loading live stream">
                    <span>.</span>
                    <span>.</span>
                    <span>.</span>
                  </span>
                </div>
              </div>
            </div>
            <aside className="watch-sidebar">
              <div className="watch-sidebar-section">
                <div className="watch-skeleton-line" />
                <div className="watch-skeleton-line short" />
                <div className="watch-skeleton-line tall" />
              </div>
            </aside>
          </div>
        </section>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="page-stack watch-page live-watch-page">
        <section className="watch-layout">
          <div className="watch-stage-row">
            <div className="watch-main-column">
              <section className="panel">
                <p className="muted-copy">{error ?? "Unable to load live stream."}</p>
              </section>
            </div>
          </div>
        </section>
      </div>
    );
  }

  const displayTitle = normalizeImportedText(data.title) ?? data.title;
  const displayChannel =
    normalizeImportedText(data.channel_name) ?? data.channel_name ?? "Unknown channel";
  const rawDescription = data.description?.trim() ?? "";
  const description = normalizeImportedText(rawDescription) ?? rawDescription;
  const startedAt = liveTimestampLabel(data.actual_start_at);
  const scheduledAt = liveTimestampLabel(data.scheduled_start_at);
  const lastSeenAt = liveTimestampLabel(data.last_seen_at);
  const viewCountLabel =
    data.concurrent_viewers != null
      ? `${formatCount(data.concurrent_viewers)} watching now`
      : "Viewer count unavailable";
  const canExpandDescription =
    description.length > 280 || description.split(/\r?\n/).length > 4;
  const streamStateLabel = startedAt
    ? `Started ${startedAt}`
    : scheduledAt
      ? `Scheduled ${scheduledAt}`
      : "Start time unavailable";
  const checkedLabel = lastSeenAt ? `Checked ${lastSeenAt}` : null;
  const embedDomain =
    typeof window !== "undefined" ? window.location.hostname : "";
  const chatEnabled = data.chat_enabled ?? true;
  const chatUrl =
    data.is_live && embedDomain && chatEnabled
      ? `https://www.youtube.com/live_chat?v=${encodeURIComponent(
          data.youtube_video_id,
        )}&embed_domain=${encodeURIComponent(embedDomain)}&dark_theme=${
          preferences.theme === "dark" ? "1" : "0"
        }`
      : null;
  const chatPanelHeight = chatHeight ? `${chatHeight}px` : "min(68vh, 720px)";
  const hasEmbeddedChat = Boolean(chatUrl);
  const playerControls = (
    <div className="player-topbar">
      <div className="player-topbar-right">
        <button
          className={`icon-button floating-control floating-control-fades-when-active ${displayMode === "theater" ? "active-chip" : ""}`}
          onClick={() =>
            setDisplayMode((current) =>
              current === "default" ? "theater" : "default",
            )
          }
          aria-label="Toggle theater mode"
          type="button"
        >
          <svg
            viewBox="0 0 24 24"
            className="icon-button-svg"
            aria-hidden="true"
          >
            <path
              d="M4 6.75A2.75 2.75 0 0 1 6.75 4h10.5A2.75 2.75 0 0 1 20 6.75v10.5A2.75 2.75 0 0 1 17.25 20H6.75A2.75 2.75 0 0 1 4 17.25zm1.5 0v10.5c0 .69.56 1.25 1.25 1.25h2.5V5.5h-2.5c-.69 0-1.25.56-1.25 1.25m5.25-1.25v13h6.5c.69 0 1.25-.56 1.25-1.25V6.75c0-.69-.56-1.25-1.25-1.25z"
              fill="currentColor"
            />
          </svg>
        </button>
        <button
          className={`icon-button floating-control ${isFullscreen ? "active-chip" : ""}`}
          onClick={() =>
            void (isFullscreen
              ? exitFullscreen()
              : requestFullscreen(playerShellRef.current))
          }
          aria-label="Toggle fullscreen"
          type="button"
        >
          <svg
            viewBox="0 0 24 24"
            className="icon-button-svg"
            aria-hidden="true"
          >
            <path
              d="M5.75 4h4.5v1.5h-3v3h-1.5zm8 0h4.5v4.5h-1.5v-3h-3zm3 8h1.5v4.5h-4.5V15h3zm-9.5 3h3v1.5h-4.5V12h1.5z"
              fill="currentColor"
            />
          </svg>
        </button>
      </div>
    </div>
  );

  return (
    <div
      className={`page-stack watch-page live-watch-page ${displayMode === "theater" ? "watch-page-theater" : ""}`}
    >
      <section className="watch-layout">
        <div className="watch-stage-row">
          <div className="watch-main-column">
            <div className="watch-player-slot">
              <div
                ref={playerShellRef}
                className="video-frame advanced-player watch-player-frame live-watch-player-frame"
              >
                <div className="live-embed-shell">
                  {data.embed_blocked_reason ? (
                    <div
                      className="live-chat-empty"
                      style={{ height: "100%", minHeight: "20rem", padding: "1.5rem" }}
                    >
                      <p>{data.embed_blocked_reason}</p>
                      <p className="muted-copy">
                        Upload a YouTube cookies file in Settings, or re-export it after confirming your age on youtube.com, to let halcyon try authenticated live playback for blocked streams.
                      </p>
                      <a
                        className="ghost-button"
                        href={data.watch_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open on YouTube
                      </a>
                    </div>
                  ) : (
                    <>
                      <iframe
                        className="live-player-embed"
                        src={data.embed_url}
                        title={displayTitle}
                        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                        allowFullScreen
                      />
                      {playerControls}
                    </>
                  )}
                </div>
              </div>
            </div>

            <div className="watch-meta-slot">
              <section className="watch-meta live-watch-meta-panel">
                <h1 className="watch-title">{displayTitle}</h1>

                <div className="watch-action-row">
                  <div className="watch-channel-row">
                    <div className="watch-channel-block">
                      {data.channel_slug ? (
                        <Link
                          className="watch-channel-avatar watch-channel-avatar-button live-watch-channel-anchor"
                          to={`/channels/${data.channel_slug}`}
                        >
                          <AvatarImage
                            src={data.channel_avatar_url}
                            alt={displayChannel}
                            seed={displayChannel}
                            fallbackText={displayChannel}
                          />
                        </Link>
                      ) : (
                        <span className="watch-channel-avatar">
                          <AvatarImage
                            src={data.channel_avatar_url}
                            alt={displayChannel}
                            seed={displayChannel}
                            fallbackText={displayChannel}
                          />
                        </span>
                      )}

                      <span className="watch-channel-copy">
                        {data.channel_slug ? (
                          <Link
                            className="watch-channel-name-button live-watch-channel-anchor"
                            to={`/channels/${data.channel_slug}`}
                          >
                            <strong>{displayChannel}</strong>
                          </Link>
                        ) : (
                          <strong>{displayChannel}</strong>
                        )}
                        <small>{viewCountLabel}</small>
                      </span>
                    </div>

                    <a
                      className="ghost-button"
                      href={data.watch_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open on YouTube
                    </a>
                  </div>
                </div>

                <div
                  className={`watch-description live-watch-description-panel ${descriptionExpanded ? "expanded" : ""} ${canExpandDescription ? "is-expandable" : ""}`}
                  onClick={() => {
                    if (!descriptionExpanded && canExpandDescription) {
                      setDescriptionExpanded(true);
                    }
                  }}
                >
                  <div className="watch-description-head">
                    <div className="watch-description-meta">
                      <strong>About this stream</strong>
                      <span>{streamStateLabel}</span>
                      {checkedLabel ? <span>{checkedLabel}</span> : null}
                    </div>
                  </div>
                  <LinkifiedText
                    text={description || "No description available."}
                    className="watch-description-copy linkified-text"
                  />
                  {canExpandDescription ? (
                    <div className="watch-description-footer">
                      {!descriptionExpanded ? (
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
                      ) : (
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
                      )}
                    </div>
                  ) : null}
                </div>
              </section>
            </div>
          </div>

          <aside className="watch-sidebar">
            {data.is_live ? (
              <section className="watch-sidebar-section live-chat-section">
                <div
                  className={`live-chat-shell ${hasEmbeddedChat ? "has-chat" : "is-disabled"}`}
                  style={{ height: chatPanelHeight }}
                >
                  {hasEmbeddedChat ? (
                    <iframe
                      className="live-chat-frame"
                      src={chatUrl ?? undefined}
                      title={`${displayTitle} live chat`}
                      loading="lazy"
                    />
                  ) : (
                    <div className="live-chat-empty">
                      <p>Chat is disabled.</p>
                    </div>
                  )}
                </div>
              </section>
            ) : null}

            <section className="watch-sidebar-section">
              <div className="section-heading">
                <h2>Stream details</h2>
              </div>
              <div className="live-stream-summary-card">
                {data.thumbnail_url ? (
                  <img
                    className="live-stream-summary-thumb"
                    src={data.thumbnail_url}
                    alt={displayTitle}
                  />
                ) : null}
                <div className="live-stream-summary-copy">
                  <strong>{displayTitle}</strong>
                  <span>{displayChannel}</span>
                  <small>{streamStateLabel}</small>
                  {checkedLabel ? <small>{checkedLabel}</small> : null}
                </div>
              </div>
            </section>
          </aside>
        </div>
      </section>
    </div>
  );
}
