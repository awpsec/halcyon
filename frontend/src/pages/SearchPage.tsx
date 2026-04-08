import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Profile } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { VideoCard } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";
import { formatCount, normalizeImportedText } from "../lib/format";

export function SearchPage({ profile }: { profile: Profile | null }) {
  const canManageVideo = Boolean(profile?.is_admin);
  const [searchParams] = useSearchParams();
  const query = (searchParams.get("q") ?? "").trim();
  const shouldSearch = query.length >= 2;
  const { data, loading, error } = useAsyncData(
    () =>
      shouldSearch
        ? api.search(query)
        : Promise.resolve({ videos: [], channels: [] }),
    [query, shouldSearch],
  );

  const channelResults = useMemo(() => data?.channels ?? [], [data]);
  const videoResults = useMemo(
    () =>
      (data?.videos ?? []).map((video) => ({
        id: video.id,
        watch_ref: video.watch_ref,
        title: video.title,
        channel: video.channel_name,
        channel_slug: video.channel_slug,
        channel_avatar_url: video.channel_avatar_url,
        series: video.series_name,
        channel_id: video.channel_id,
        series_id: video.series_id,
        duration_seconds: video.duration_seconds,
        thumbnail_url: video.thumbnail_url,
        progress_seconds: video.progress_seconds,
        published_at: video.published_at,
        youtube_view_count: video.youtube_view_count,
        youtube_like_count: video.youtube_like_count,
        youtube_comment_count: video.youtube_comment_count,
      })),
    [data],
  );

  if (!shouldSearch) {
    return (
      <div className="page-stack search-results-page">
        <header className="search-results-header">
          <h2>Search</h2>
        </header>
        <EmptyState message="Type at least 2 characters in the header search to search halcyon." />
      </div>
    );
  }

  return (
    <div className="page-stack search-results-page">
      <header className="search-results-header">
        <div className="search-results-copy">
          <h2>Search for "{query}"</h2>
          <small className="muted-copy">{`${channelResults.length} channels • ${videoResults.length} videos`}</small>
        </div>
      </header>

      {loading ? <div className="search-results-status">Searching…</div> : null}
      {error ? (
        <div className="search-results-status error">{error}</div>
      ) : null}

      {!loading && !error ? (
        <>
          <section className="search-section">
            <div className="section-heading compact-heading">
              <h3>Channels</h3>
            </div>
            {channelResults.length ? (
              <div className="search-channel-grid">
                {channelResults.map((channel) => (
                  <Link
                    key={channel.id}
                    to={`/channels/${channel.slug}`}
                    className="search-channel-card"
                  >
                    <span className="search-channel-avatar">
                      {channel.avatar_url ? (
                        <img src={channel.avatar_url} alt={channel.name} />
                      ) : (
                        channel.name.slice(0, 2).toUpperCase()
                      )}
                    </span>
                    <span className="search-channel-copy">
                      <strong>
                        {normalizeImportedText(channel.name) ?? channel.name}
                      </strong>
                      <small>{formatCount(channel.video_count)} videos</small>
                    </span>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState message={`No channel matches for "${query}".`} />
            )}
          </section>

          <section className="search-section">
            <div className="section-heading compact-heading">
              <h3>Videos</h3>
            </div>
            {videoResults.length ? (
              <div className="video-grid-layout">
                {videoResults.map((video) => (
                  <VideoCard key={`search-video-${video.id}`} item={video} canManageVideo={canManageVideo} />
                ))}
              </div>
            ) : (
              <EmptyState message={`No video matches for "${query}".`} />
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
