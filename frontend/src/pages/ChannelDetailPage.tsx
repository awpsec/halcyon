import { createPortal } from "react-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Profile } from "../api/client";
import { CollectionCard } from "../components/CollectionCard";
import { EmptyState } from "../components/EmptyState";
import { LinkifiedText } from "../components/LinkifiedText";
import { ChannelPageSkeleton } from "../components/PageSkeletons";
import { VideoCard } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";
import { formatCount, formatRelativeDate } from "../lib/format";
import { pushToast } from "../lib/notifications";

const tabs = ["Home", "Videos", "Series", "About"] as const;
const RECENT_UPLOAD_WINDOW_MS = 5 * 24 * 60 * 60 * 1000;

type VideoSort = "upload" | "views";

function videoTimestamp(video: { published_at?: string | null; created_at?: string | null }) {
  const value = video.published_at ?? video.created_at ?? null;
  return value ? new Date(value).getTime() : 0;
}

function sortVideosByUpload(videos: any[]) {
  return [...videos].sort((left, right) => {
    const delta = videoTimestamp(right) - videoTimestamp(left);
    if (delta !== 0) return delta;
    return right.id - left.id;
  });
}

function sortVideosByViews(videos: any[]) {
  return [...videos].sort((left, right) => {
    const delta = (right.youtube_view_count ?? 0) - (left.youtube_view_count ?? 0);
    if (delta !== 0) return delta;
    const uploadDelta = videoTimestamp(right) - videoTimestamp(left);
    if (uploadDelta !== 0) return uploadDelta;
    return right.id - left.id;
  });
}

function gridMetricsForCurrentDensity() {
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    if (root.classList.contains("density-compact")) return { minWidth: 276, gap: 14 };
    if (root.classList.contains("density-relaxed")) return { minWidth: 392, gap: 26 };
  }
  return { minWidth: 338, gap: 20 };
}

function channelMilestoneBadge(subscriberCount?: number | null) {
  if (!subscriberCount) return null;
  if (subscriberCount >= 10_000_000) {
    return {
      src: "/assets/badges/ruby_bdg.png",
      label: "10 million subscribers",
    };
  }
  if (subscriberCount >= 1_000_000) {
    return {
      src: "/assets/badges/diamond_bdg.png",
      label: "1 million subscribers",
    };
  }
  if (subscriberCount >= 500_000) {
    return {
      src: "/assets/badges/gold_bdg.png",
      label: "500 thousand subscribers",
    };
  }
  if (subscriberCount >= 100_000) {
    return {
      src: "/assets/badges/silver_bdg.png",
      label: "100 thousand subscribers",
    };
  }
  return null;
}

export function ChannelDetailPage({ profile }: { profile: Profile | null }) {
  const params = useParams();
  const channelRef = params.channelRef ?? "";
  const canManageChannel = Boolean(profile?.is_admin);
  const { data, loading, error, setData } = useAsyncData(() => api.channel(channelRef), [channelRef]);
  const [activeTab, setActiveTab] = useState<(typeof tabs)[number]>("Home");
  const [syncing, setSyncing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [videoSort, setVideoSort] = useState<VideoSort>("upload");
  const [homeVideosWidth, setHomeVideosWidth] = useState(0);
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const [sortMenuAnchor, setSortMenuAnchor] = useState<DOMRect | null>(null);
  const [descriptionOverflowing, setDescriptionOverflowing] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const sortMenuRef = useRef<HTMLDivElement | null>(null);
  const homeVideosRef = useRef<HTMLElement | null>(null);
  const descriptionPreviewRef = useRef<HTMLDivElement | null>(null);

  const menuStyle = useMemo(() => {
    if (!menuAnchor || typeof window === "undefined") return null;
    const maxLeft = window.innerWidth - 220;
    return {
      top: menuAnchor.bottom + 8,
      left: Math.min(menuAnchor.right - 196, maxLeft),
    };
  }, [menuAnchor]);

  const sortMenuStyle = useMemo(() => {
    if (!sortMenuAnchor || typeof window === "undefined") return null;
    const width = 188;
    return {
      top: `${Math.min(sortMenuAnchor.bottom + 8, window.innerHeight - 144)}px`,
      left: `${Math.max(12, Math.min(sortMenuAnchor.right - width, window.innerWidth - width - 12))}px`,
    };
  }, [sortMenuAnchor]);

  useEffect(() => {
    if (!menuOpen) return;
    function handlePointer(event: MouseEvent) {
      if (menuRef.current?.contains(event.target as Node)) return;
      setMenuOpen(false);
      setMenuAnchor(null);
    }
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        setMenuAnchor(null);
      }
    }
    window.addEventListener("mousedown", handlePointer);
    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("mousedown", handlePointer);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!sortMenuOpen) return;
    function handlePointer(event: MouseEvent) {
      if (sortMenuRef.current?.contains(event.target as Node)) return;
      setSortMenuOpen(false);
      setSortMenuAnchor(null);
    }
    function handleEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setSortMenuOpen(false);
      setSortMenuAnchor(null);
    }
    window.addEventListener("mousedown", handlePointer);
    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("mousedown", handlePointer);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [sortMenuOpen]);

  function toCard(video: any) {
    return {
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
      watched: video.watched,
      created_at: video.created_at,
      published_at: video.published_at,
      youtube_view_count: video.youtube_view_count,
      youtube_like_count: video.youtube_like_count,
      youtube_comment_count: video.youtube_comment_count,
    };
  }

  const seriesGroups = useMemo(() => {
    const groups = new Map<string, any[]>();
    for (const video of data?.videos ?? []) {
      if (!video.series_name || !video.series_id) continue;
      const items = groups.get(video.series_name) ?? [];
      items.push(video);
      groups.set(video.series_name, items);
    }
    return Array.from(groups.entries()).map(([name, videos]) => ({
      id: videos[0]?.series_id,
      name,
      videos,
      thumbnail_url: videos[0]?.thumbnail_url,
      preview_thumbnails: videos
        .slice(0, 3)
        .map((video) => video.thumbnail_url ?? `/api/videos/${video.id}/thumbnail`)
        .filter(Boolean),
    }));
  }, [data]);

  const orderedVideos = data?.videos ?? [];
  const uploadOrderedVideos = useMemo(() => sortVideosByUpload(orderedVideos), [orderedVideos]);
  const allVideosSorted = useMemo(
    () => (videoSort === "views" ? sortVideosByViews(orderedVideos) : uploadOrderedVideos),
    [orderedVideos, uploadOrderedVideos, videoSort],
  );
  const recentUploads = useMemo(() => {
    const threshold = Date.now() - RECENT_UPLOAD_WINDOW_MS;
    return uploadOrderedVideos.filter((video) => {
      const timestamp = videoTimestamp(video);
      return timestamp > 0 && timestamp >= threshold;
    });
  }, [uploadOrderedVideos]);
  const homeVideos = useMemo(() => {
    const recentIds = new Set(recentUploads.map((video) => video.id));
    return uploadOrderedVideos.filter((video) => !recentIds.has(video.id));
  }, [recentUploads, uploadOrderedVideos]);
  const homeVideoCards = useMemo(() => {
    const metrics = gridMetricsForCurrentDensity();
    const effectiveWidth =
      homeVideosWidth || (typeof window !== "undefined" ? window.innerWidth - 48 : 1240);
    const columns = Math.max(1, Math.floor((effectiveWidth + metrics.gap) / (metrics.minWidth + metrics.gap)));
    return homeVideos.slice(0, columns * 3);
  }, [homeVideos, homeVideosWidth]);

  useEffect(() => {
    const node = homeVideosRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setHomeVideosWidth(entry.contentRect.width);
    });
    observer.observe(node);
    setHomeVideosWidth(node.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, [activeTab]);

  useEffect(() => {
    setSortMenuOpen(false);
    setSortMenuAnchor(null);
  }, [activeTab]);

  useEffect(() => {
    const node = descriptionPreviewRef.current;
    if (!node) {
      setDescriptionOverflowing(false);
      return;
    }

    const measureOverflow = () => {
      setDescriptionOverflowing(node.scrollHeight - node.clientHeight > 2);
    };

    measureOverflow();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measureOverflow());
    observer.observe(node);
    return () => observer.disconnect();
  }, [data?.channel.description]);

  if (loading) return <ChannelPageSkeleton />;
  if (error || !data) return <div className="panel error">{error ?? "Channel not found"}</div>;
  const countLine = [
    data.channel.subscriber_count ? `${formatCount(data.channel.subscriber_count)} subscribers` : null,
    `${data.channel.youtube_video_count ?? data.channel.video_count} videos`,
    data.channel.view_count ? `${formatCount(data.channel.view_count)} views` : null,
  ]
    .filter(Boolean)
    .join(" • ");
  const joinedLabel = data.channel.joined_at
    ? new Date(data.channel.joined_at).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : null;
  const milestoneBadge = channelMilestoneBadge(data.channel.subscriber_count);
  const sortLabel = videoSort === "views" ? "Views" : "Upload date";
  async function refreshChannel() {
    setData(await api.channel(channelRef));
  }

  async function handleChannelSync(force = false) {
    setSyncing(true);
    pushToast("info", force ? "Forced channel resync started" : "Channel sync started", data.channel.name);
    try {
      const result: any = await api.syncChannel(data.channel.id, { force });
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast(
          "error",
          force ? "Forced channel resync completed with issues" : "Channel sync completed with issues",
          result?.details?.warning ?? result?.details?.error ?? data.channel.name
        );
      } else {
        pushToast("success", force ? "Forced channel resync finished" : "Channel sync finished", data.channel.name);
      }
      await refreshChannel();
    } catch (error) {
      pushToast("error", force ? "Forced channel resync failed" : "Channel sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setSyncing(false);
      setMenuOpen(false);
      setMenuAnchor(null);
    }
  }

  async function handleHighResBannerPull() {
    setSyncing(true);
    pushToast("info", "High resolution banner sync started", data.channel.name);
    try {
      const result: any = await api.syncChannel(data.channel.id, { high_res_banner: true });
      if (result?.status === "partial" || result?.status === "failed") {
        pushToast("error", "High resolution banner sync completed with issues", result?.details?.warning ?? result?.details?.error ?? data.channel.name);
      } else {
        pushToast("success", "High resolution banner synced", data.channel.name);
      }
      await refreshChannel();
    } catch (error) {
      pushToast("error", "High resolution banner sync failed", error instanceof Error ? error.message : "Unknown sync error");
    } finally {
      setSyncing(false);
      setMenuOpen(false);
      setMenuAnchor(null);
    }
  }

  async function toggleSeriesSaved(seriesId: number, saved: boolean) {
    const series = seriesGroups.find((item) => item.id === seriesId);
    try {
      await api.setSeriesSaved(seriesId, saved);
      pushToast("success", saved ? "Saved series videos" : "Removed series videos from saved", series?.name ?? "Series");
      await refreshChannel();
    } catch (error) {
      pushToast("error", saved ? "Unable to save series videos" : "Unable to unsave series videos", error instanceof Error ? error.message : "Unknown save error");
    }
  }

  return (
    <div className="page-stack channel-page">
      <section className="channel-hero">
        <div className="channel-banner">
          {data.channel.banner_url ? <img src={data.channel.banner_url} alt={data.channel.name} /> : <div className="channel-banner-fallback" />}
          <button
            className="icon-button channel-banner-menu"
            type="button"
            aria-label="Channel actions"
            onClick={(event) => {
              const rect = (event.currentTarget as HTMLButtonElement).getBoundingClientRect();
              setMenuAnchor(rect);
              setMenuOpen((current) => !current);
            }}
          >
            <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
              <circle cx="12" cy="5" r="2.15" fill="currentColor" />
              <circle cx="12" cy="12" r="2.15" fill="currentColor" />
              <circle cx="12" cy="19" r="2.15" fill="currentColor" />
            </svg>
          </button>
        </div>
        <div className="channel-hero-copy">
          <div className="channel-avatar-large">
            {data.channel.avatar_url ? <img src={data.channel.avatar_url} alt={data.channel.name} /> : <span>{data.channel.name.slice(0, 2).toUpperCase()}</span>}
          </div>
          <div className="channel-hero-text">
            <h1 className="channel-title-row">
              <span>{data.channel.name}</span>
              {milestoneBadge ? <img className="channel-inline-badge" src={milestoneBadge.src} alt={milestoneBadge.label} /> : null}
            </h1>
            <p>@{data.channel.slug} • {countLine}</p>
            {data.channel.description ? (
              <div className="channel-description-preview">
                <div ref={descriptionPreviewRef} className="channel-description-clamp">
                  <LinkifiedText text={data.channel.description} className="channel-description-inline linkified-text" />
                </div>
                {descriptionOverflowing ? (
                  <button
                    className="channel-description-fade-button"
                    type="button"
                    onClick={() => setActiveTab("About")}
                    aria-label="Open About to read the full channel description"
                  >
                    <span>More</span>
                  </button>
                ) : null}
              </div>
            ) : null}
            <div className="channel-actions">
              <button className={`ghost-button channel-subscribe-button ${data.channel.subscribed ? "is-subscribed" : ""}`} onClick={async () => { await api.toggleSubscription(data.channel.slug); await refreshChannel(); }}>
                {data.channel.subscribed ? "Subscribed ✓" : "Subscribe"}
              </button>
              {data.channel.youtube_fetched_at ? <span className="channel-updated-text">Updated {formatRelativeDate(data.channel.youtube_fetched_at)}</span> : null}
            </div>
          </div>
        </div>
      </section>
      {menuOpen && menuStyle
        ? createPortal(
            <div ref={menuRef} className="card-menu channel-card-menu" style={menuStyle} onClick={(event) => event.stopPropagation()}>
              <button
                className="menu-item"
                onClick={async () => {
                  setMenuOpen(false);
                  setMenuAnchor(null);
                  await api.toggleSubscription(data.channel.slug);
                  await refreshChannel();
                }}
              >
                {data.channel.subscribed ? "Unsubscribe" : "Subscribe"}
              </button>
              {canManageChannel ? (
                <>
                  <button className="menu-item" onClick={() => void handleChannelSync()} disabled={syncing}>
                    {syncing ? "Syncing..." : "Sync channel"}
                  </button>
                  <button className="menu-item" onClick={() => void handleChannelSync(true)} disabled={syncing}>
                    {syncing ? "Syncing..." : "Force sync channel"}
                  </button>
                  <button className="menu-item" onClick={() => void handleHighResBannerPull()} disabled={syncing}>
                    {syncing ? "Syncing..." : "Pull high resolution banner"}
                  </button>
                </>
              ) : null}
            </div>,
            document.body
          )
        : null}

      <div className="channel-tab-strip">
        {tabs.map((tab) => (
          <button key={tab} className={`channel-tab ${activeTab === tab ? "active" : ""}`} onClick={() => setActiveTab(tab)}>
            {tab}
          </button>
        ))}
      </div>

      {activeTab === "Home" ? (
        <>
          {recentUploads.length ? (
            <section className="home-block">
              <div className="section-heading">
                <h2>Recent Uploads</h2>
              </div>
              <div className="video-grid-layout featured-grid">
                {recentUploads.map((video: any) => (
                  <VideoCard key={video.id} item={toCard(video)} compact={false} canManageVideo={canManageChannel} />
                ))}
              </div>
            </section>
          ) : null}
          {homeVideoCards.length ? (
            <section className="home-block" ref={homeVideosRef}>
              <div className="section-heading">
                <h2>Videos</h2>
              </div>
              <div className="video-grid-layout">
                {homeVideoCards.map((video: any) => (
                  <VideoCard key={video.id} item={toCard(video)} compact={false} canManageVideo={canManageChannel} />
                ))}
              </div>
            </section>
          ) : null}
          {seriesGroups.length ? (
            <section className="home-block">
              <div className="section-heading">
                <h2>Series</h2>
              </div>
              <div className="video-grid-layout">
              {seriesGroups.slice(0, 6).map((group) => (
                <CollectionCard
                  key={group.name}
                  title={group.name}
                  subtitle={data.channel.name}
                  badge="Series"
                  thumbnailUrl={group.thumbnail_url}
                  stackedThumbnails={group.preview_thumbnails}
                  to={`/series/${group.id}`}
                  meta={`${group.videos.length} videos`}
                  menuItems={group.id ? [
                    {
                      label: group.videos.every((video: any) => video.user_saved) ? "Remove from saved" : "Add to saved",
                      onSelect: () => toggleSeriesSaved(group.id, !group.videos.every((video: any) => video.user_saved)),
                    },
                  ] : undefined}
                />
              ))}
              </div>
            </section>
          ) : (
            <EmptyState message="This channel has no detected series yet." />
          )}
        </>
      ) : null}

      {activeTab === "Videos" ? (
        <section className="home-block">
          <div className="section-heading channel-videos-heading">
            <h2>All Videos</h2>
            <button
              className="ghost-button channel-sort-button"
              type="button"
              aria-haspopup="menu"
              aria-expanded={sortMenuOpen}
              onClick={(event) => {
                const rect = (event.currentTarget as HTMLButtonElement).getBoundingClientRect();
                setSortMenuAnchor(rect);
                setSortMenuOpen((current) => !current);
              }}
            >
              <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
                <path
                  d="M4 7h16M7 12h10M10 17h4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
              <span>Sort by</span>
              <strong>{sortLabel}</strong>
            </button>
          </div>
          <div className="video-grid-layout">
            {allVideosSorted.map((video: any) => (
              <VideoCard key={video.id} item={toCard(video)} compact={false} canManageVideo={canManageChannel} />
            ))}
          </div>
        </section>
      ) : null}

      {activeTab === "Series" ? (
        seriesGroups.length ? (
          <section className="video-grid-layout">
            {seriesGroups.map((group) => (
              <CollectionCard
                key={group.name}
                title={group.name}
                subtitle={data.channel.name}
                badge="Series"
                thumbnailUrl={group.thumbnail_url}
                stackedThumbnails={group.preview_thumbnails}
                to={`/series/${group.id}`}
                meta={`${group.videos.length} videos`}
                menuItems={group.id ? [
                  {
                    label: group.videos.every((video: any) => video.user_saved) ? "Remove from saved" : "Add to saved",
                    onSelect: () => toggleSeriesSaved(group.id, !group.videos.every((video: any) => video.user_saved)),
                  },
                ] : undefined}
              />
            ))}
          </section>
        ) : (
          <EmptyState message="This channel has no grouped series yet." />
        )
      ) : null}

      {activeTab === "About" ? (
        <section className="channel-about">
          <div className="panel">
            <div className="section-heading"><h2>Description</h2></div>
            <LinkifiedText text={data.channel.description ?? "No synced description yet."} className="about-copy linkified-text" />
          </div>
          <div className="panel">
            <div className="section-heading"><h2>Stats</h2></div>
            <div className="stats-grid">
              <div><strong>{formatCount(data.channel.subscriber_count) ?? "n/a"}</strong><span>Subscribers</span></div>
              <div><strong>{formatCount(data.channel.view_count) ?? "n/a"}</strong><span>Views</span></div>
              <div><strong>{data.channel.youtube_video_count ?? data.channel.video_count}</strong><span>Videos</span></div>
              <div><strong>{formatRelativeDate(data.channel.youtube_fetched_at) ?? "offline"}</strong><span>Snapshot age</span></div>
              <div><strong>{joinedLabel ?? "n/a"}</strong><span>Joined</span></div>
            </div>
            {data.channel.links?.length ? (
              <div className="channel-link-list">
                {data.channel.links.map((link: { title: string; url: string }) => (
                  <a key={`${link.title}-${link.url}`} className="channel-link-chip" href={link.url} target="_blank" rel="noreferrer">
                    {link.title}
                  </a>
                ))}
              </div>
            ) : null}
            {data.channel.canonical_url ? (
              <div className="channel-link-list">
                <a className="channel-link-chip" href={data.channel.canonical_url} target="_blank" rel="noreferrer">
                  View on YouTube
                </a>
              </div>
            ) : null}
          </div>
        </section>
      ) : null}
      {sortMenuOpen && sortMenuStyle
        ? createPortal(
            <div ref={sortMenuRef} className="card-menu channel-sort-menu" style={sortMenuStyle}>
              <button
                className={`menu-item ${videoSort === "upload" ? "is-selected" : ""}`}
                onClick={() => {
                  setVideoSort("upload");
                  setSortMenuOpen(false);
                  setSortMenuAnchor(null);
                }}
              >
                Upload date
              </button>
              <button
                className={`menu-item ${videoSort === "views" ? "is-selected" : ""}`}
                onClick={() => {
                  setVideoSort("views");
                  setSortMenuOpen(false);
                  setSortMenuAnchor(null);
                }}
              >
                Views
              </button>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
