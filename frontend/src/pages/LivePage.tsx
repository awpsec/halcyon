import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { useAsyncData } from "../hooks/useAsyncData";
import {
  formatCount,
  formatRelativeDate,
  normalizeImportedText,
} from "../lib/format";

const LIVE_POLL_MS = 60_000;

function liveTimestampLabel(value: string | null) {
  if (!value) return null;
  return formatRelativeDate(value) ?? null;
}

export function LivePage() {
  const { data, loading, error, setData } = useAsyncData(() => api.liveOverview(), []);

  useEffect(() => {
    let cancelled = false;

    async function refreshLive() {
      try {
        const next = await api.liveOverview();
        if (!cancelled) {
          setData(next);
        }
      } catch {
        // Keep the last successful payload visible.
      }
    }

    const interval = window.setInterval(() => {
      void refreshLive();
    }, LIVE_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [setData]);

  if (loading && !data) {
    return (
      <div className="page-stack live-page">
        <section className="panel">
          <div className="section-heading">
            <h2>Live</h2>
          </div>
          <p className="muted-copy">Loading live streams...</p>
        </section>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="page-stack live-page">
        <section className="panel">
          <div className="section-heading">
            <h2>Live</h2>
          </div>
          <p className="muted-copy">{error}</p>
        </section>
      </div>
    );
  }

  const overview = data;
  if (!overview?.enabled) {
    return (
      <div className="page-stack live-page">
        <section className="panel">
          <div className="section-heading">
            <h2>Live</h2>
          </div>
          <EmptyState message="Live tab is turned off in server settings." />
        </section>
      </div>
    );
  }

  if (!overview.api_key_configured) {
    return (
      <div className="page-stack live-page">
        <section className="panel live-empty-panel">
          <div className="section-heading">
            <h2>Live</h2>
          </div>
          <EmptyState message="Add a YouTube API key in server settings to watch live streams here." />
        </section>
      </div>
    );
  }

  return (
    <div className="page-stack live-page">
      <section className="watch-description live-page-intro">
        <div className="live-page-intro-head">
          <h1>Live</h1>
          <span className="live-page-intro-count">
            {overview.items.length} {overview.items.length === 1 ? "stream" : "streams"}
          </span>
        </div>
        <p className="muted-copy">
          Streams from channels Halcyon already knows how to match. Active
          livestreams appear here automatically when those tracked YouTube
          channels go live.
        </p>
        <small className="muted-copy">
          Last checked {liveTimestampLabel(overview.last_live_sync_at) ?? "recently"}
        </small>
      </section>

      {overview.items.length ? (
        <section className="live-grid">
          {overview.items.map((item) => {
            const displayTitle = normalizeImportedText(item.title) ?? item.title;
            const displayChannel =
              normalizeImportedText(item.channel_name) ??
              item.channel_name ??
              "Unknown channel";
            const startedAt = liveTimestampLabel(item.actual_start_at);
            const scheduledAt = liveTimestampLabel(item.scheduled_start_at);
            return (
              <article key={item.youtube_video_id} className="live-card">
                <Link className="live-card-thumb-link" to={`/live/${item.youtube_video_id}`}>
                  <div className="live-card-thumb">
                    {item.thumbnail_url ? (
                      <img src={item.thumbnail_url} alt={displayTitle} />
                    ) : (
                      <div className="live-card-thumb-fallback">LIVE</div>
                    )}
                    <span className="live-pill">LIVE</span>
                  </div>
                </Link>
                <div className="live-card-copy">
                  <Link className="live-card-title-link" to={`/live/${item.youtube_video_id}`}>
                    <strong>{displayTitle}</strong>
                  </Link>
                  <span className="live-card-channel-row">
                    {item.channel_slug ? (
                      <Link
                        className="live-card-channel-link live-card-channel-avatar-link"
                        to={`/channels/${item.channel_slug}`}
                      >
                        {item.channel_avatar_url ? (
                          <img
                            className="live-card-channel-avatar"
                            src={item.channel_avatar_url}
                            alt={displayChannel}
                          />
                        ) : (
                          <span className="live-card-channel-avatar live-card-channel-avatar-fallback">
                            {displayChannel.slice(0, 2).toUpperCase()}
                          </span>
                        )}
                      </Link>
                    ) : item.channel_avatar_url ? (
                      <img
                        className="live-card-channel-avatar"
                        src={item.channel_avatar_url}
                        alt=""
                      />
                    ) : null}
                    {item.channel_slug ? (
                      <Link className="live-card-channel-link" to={`/channels/${item.channel_slug}`}>
                        <small>{displayChannel}</small>
                      </Link>
                    ) : (
                      <small>{displayChannel}</small>
                    )}
                  </span>
                  <div className="live-card-meta">
                    {item.concurrent_viewers != null ? (
                      <span>{formatCount(item.concurrent_viewers)} watching</span>
                    ) : null}
                    {startedAt ? <span>Started {startedAt}</span> : null}
                    {!startedAt && scheduledAt ? <span>Scheduled {scheduledAt}</span> : null}
                  </div>
                </div>
              </article>
            );
          })}
        </section>
      ) : (
        <section className="panel live-empty-panel">
          <div className="section-heading">
            <h2>No active streams</h2>
          </div>
          <EmptyState message="Nothing is live right now from the channels Halcyon can match." />
        </section>
      )}
    </div>
  );
}
