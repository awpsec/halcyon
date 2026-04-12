import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type LiveStream, type Preferences, type Profile, type QueueResponse, type VideoSummary } from "../api/client";
import { AvatarImage } from "../components/AvatarImage";
import { EmptyState } from "../components/EmptyState";
import { HomePageSkeleton } from "../components/PageSkeletons";
import { VideoRail } from "../components/VideoRail";
import { VideoCard } from "../components/VideoRail";
import { formatCount, formatRelativeDate, normalizeImportedText } from "../lib/format";
import { clearPlaybackContext } from "../lib/playbackContext";
import { pushToast } from "../lib/notifications";
import { useAsyncData } from "../hooks/useAsyncData";

const PAGE_SIZE = 24;
const PAGED_TOPICS = new Set(["recent", "random", "explore"]);
const HOME_FEED_POLL_MS = 10000;
const HOME_FEED_FAST_POLL_MS = 3500;
const HOME_LIVE_POLL_MS = 60_000;

type PagedTopic = "recent" | "random" | "explore";

function gridMetricsForDensity(density: Preferences["density"]) {
  if (density === "compact") return { minWidth: 276, gap: 14 };
  if (density === "relaxed") return { minWidth: 392, gap: 26 };
  return { minWidth: 338, gap: 20 };
}

function toCard(video: VideoSummary | any, reasonOverride?: string) {
  return {
    id: video.id,
    watch_ref: video.watch_ref,
    title: video.title,
    channel: video.channel_name ?? video.channel,
    channel_slug: video.channel_slug,
    channel_avatar_url: video.channel_avatar_url,
    series: video.series_name ?? video.series,
    channel_id: video.channel_id,
    series_id: video.series_id,
    duration_seconds: video.duration_seconds,
    thumbnail_url: video.thumbnail_url,
    watched: video.watched,
    progress_seconds: video.progress_seconds,
    created_at: video.created_at,
    reason: reasonOverride ?? video.reason ?? "library",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

function itemNeedsSync(card: any) {
  const channelName = String(card?.channel_name ?? card?.channel ?? "").trim().toLowerCase();
  const hasThumbnail = Boolean(card?.thumbnail_url);
  const hasAvatar = Boolean(card?.channel_avatar_url);
  return channelName === "unknown channel" || !hasThumbnail || !hasAvatar;
}

function liveTimestampLabel(value: string | null) {
  if (!value) return null;
  return formatRelativeDate(value) ?? null;
}

function HomeLiveShelf({ items }: { items: LiveStream[] }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [canScrollBack, setCanScrollBack] = useState(false);
  const [canScrollForward, setCanScrollForward] = useState(false);

  useEffect(() => {
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
  }, [items]);

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
      <div className="section-heading">
        <h2>
          <Link className="section-heading-link" to="/live">
            Live
          </Link>
        </h2>
        <span className="section-count">
          {items.length} {items.length === 1 ? "stream" : "streams"}
        </span>
      </div>
      <div className="rail-shell">
        {canScrollBack ? (
          <button
            className="rail-arrow rail-arrow-left"
            onClick={() => shiftShelf("back")}
            aria-label="Scroll live streams left"
          >
            <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
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
          {items.map((item) => {
            const displayTitle = normalizeImportedText(item.title) ?? item.title;
            const displayChannel =
              normalizeImportedText(item.channel_name) ?? item.channel_name ?? "Unknown channel";
            const startedAt = liveTimestampLabel(item.actual_start_at);
            const scheduledAt = liveTimestampLabel(item.scheduled_start_at);
            const statsLine = [
              item.concurrent_viewers != null
                ? `${formatCount(item.concurrent_viewers)} watching`
                : null,
              startedAt ? `Started ${startedAt}` : scheduledAt ? `Scheduled ${scheduledAt}` : null,
            ]
              .filter(Boolean)
              .join(" • ");

            return (
              <article key={item.youtube_video_id} className="video-tile compact-tile live-home-card">
                <Link className="live-home-card-link" to={`/live/${item.youtube_video_id}`}>
                  <div className="tile-thumb media-thumb">
                    {item.thumbnail_url ? (
                      <img src={item.thumbnail_url} alt={displayTitle} loading="lazy" />
                    ) : (
                      <div className="live-home-thumb-fallback">LIVE</div>
                    )}
                    <span className="live-pill live-home-pill">LIVE</span>
                  </div>
                </Link>
                <div className="tile-body">
                  <div className="tile-meta-row">
                    {item.channel_slug ? (
                      <Link
                        className="channel-avatar avatar-link"
                        to={`/channels/${item.channel_slug}`}
                        aria-label={`Open ${displayChannel}`}
                      >
                        <AvatarImage
                          src={item.channel_avatar_url}
                          alt={displayChannel}
                          seed={item.channel_slug ?? displayChannel}
                          fallbackText={displayChannel}
                        />
                      </Link>
                    ) : (
                      <span className="channel-avatar">
                        <AvatarImage
                          src={item.channel_avatar_url}
                          alt={displayChannel}
                          seed={displayChannel}
                          fallbackText={displayChannel}
                        />
                      </span>
                    )}
                    <div className="tile-copy">
                      <Link className="live-home-title-link" to={`/live/${item.youtube_video_id}`}>
                        <strong>{displayTitle}</strong>
                      </Link>
                      {item.channel_slug ? (
                        <Link className="live-home-channel-link" to={`/channels/${item.channel_slug}`}>
                          {displayChannel}
                        </Link>
                      ) : (
                        <span className="live-home-channel-link">{displayChannel}</span>
                      )}
                      <small className="live-home-meta">{statsLine || "Live now"}</small>
                    </div>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
        {canScrollForward ? (
          <button
            className="rail-arrow rail-arrow-right"
            onClick={() => shiftShelf("forward")}
            aria-label="Scroll live streams right"
          >
            <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
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
    </section>
  );
}

export function HomePage({ preferences, profile }: { preferences: Preferences; profile: Profile | null }) {
  const canManageVideo = Boolean(profile?.is_admin);
  const { data, loading, error, setData } = useAsyncData(() => api.home(), []);
  const liveState = useAsyncData(() => api.liveOverview(), []);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTopic = searchParams.get("topic") ?? "all";
  const [activeTopic, setActiveTopic] = useState(requestedTopic);
  const [pagedItems, setPagedItems] = useState<Record<PagedTopic, VideoSummary[]>>({
    recent: [],
    random: [],
    explore: [],
  });
  const [pagedHasMore, setPagedHasMore] = useState<Record<PagedTopic, boolean>>({
    recent: true,
    random: true,
    explore: true,
  });
  const [pagedLoading, setPagedLoading] = useState<Record<PagedTopic, boolean>>({
    recent: false,
    random: false,
    explore: false,
  });
  const [recentSectionWidth, setRecentSectionWidth] = useState(0);
  const [queueItems, setQueueItems] = useState<QueueResponse["items"]>([]);
  const [queueLoading, setQueueLoading] = useState(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const recentGridRef = useRef<HTMLDivElement | null>(null);
  const hasMoreRef = useRef<Record<PagedTopic, boolean>>({
    recent: true,
    random: true,
    explore: true,
  });
  const loadingMoreRef = useRef<Record<PagedTopic, boolean>>({
    recent: false,
    random: false,
    explore: false,
  });
  const countRef = useRef<Record<PagedTopic, number>>({
    recent: 0,
    random: 0,
    explore: 0,
  });
  const feedSections = useMemo(() => (data ?? []).filter((section) => section.key !== "subscriptions"), [data]);
  const feedSectionsWithoutQueue = useMemo(() => feedSections.filter((section) => section.key !== "queue"), [feedSections]);
  const homeLiveItems = useMemo(
    () => (liveState.data?.enabled ? liveState.data.items : []).slice(0, 18),
    [liveState.data],
  );
  const hasPendingFeedItems = useMemo(
    () => feedSections.some((section) => section.items.some((item) => itemNeedsSync(item))),
    [feedSections],
  );

  useEffect(() => {
    let cancelled = false;

    async function refreshLiveOverview() {
      try {
        const next = await api.liveOverview();
        if (!cancelled) {
          liveState.setData(next);
        }
      } catch {
        // Keep the current live shelf in place on refresh failures.
      }
    }

    void refreshLiveOverview();
    const interval = window.setInterval(() => {
      void refreshLiveOverview();
    }, HOME_LIVE_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [liveState.setData]);

  async function refreshHomeFeed() {
    try {
      const next = await api.home();
      setData(next);
    } catch {
      // Keep the current feed in place on background refresh failures.
    }
  }

  async function refreshPagedTopic(topic: PagedTopic) {
    try {
      if (topic === "recent") {
        const next = await api.videos({ offset: 0, limit: Math.max(PAGE_SIZE, countRef.current[topic] || PAGE_SIZE) });
        setPagedItems((current) => ({ ...current, [topic]: next }));
        const nextHasMore = next.length >= PAGE_SIZE;
        setPagedHasMore((current) => ({ ...current, [topic]: nextHasMore }));
        hasMoreRef.current = { ...hasMoreRef.current, [topic]: nextHasMore };
        countRef.current = { ...countRef.current, [topic]: next.length };
        return;
      }
      const next =
        topic === "random"
          ? await api.suggested(0, Math.max(PAGE_SIZE, countRef.current[topic] || PAGE_SIZE))
          : await api.explore(0, Math.max(PAGE_SIZE, countRef.current[topic] || PAGE_SIZE));
      setPagedItems((current) => ({ ...current, [topic]: next.items }));
      setPagedHasMore((current) => ({ ...current, [topic]: next.has_more }));
      hasMoreRef.current = { ...hasMoreRef.current, [topic]: next.has_more };
      countRef.current = { ...countRef.current, [topic]: next.items.length };
    } catch {
      // Keep the current paged section in place on background refresh failures.
    }
  }

  async function refreshVisibleHomeContent() {
    await refreshHomeFeed();
    if (activeTopic === "all" || activeTopic === "explore") {
      await refreshPagedTopic("explore");
    } else if (PAGED_TOPICS.has(activeTopic)) {
      await refreshPagedTopic(activeTopic as PagedTopic);
    } else if (activeTopic === "queue") {
      await refreshQueue();
    }
  }

  async function refreshQueue() {
    setQueueLoading(true);
    try {
      const nextQueue = await api.queue();
      setQueueItems(nextQueue.items);
    } catch (nextError) {
      pushToast(
        "error",
        "Unable to load queue",
        nextError instanceof Error ? nextError.message : "Unknown queue error",
      );
    } finally {
      setQueueLoading(false);
    }
  }

  async function clearQueue() {
    await api.bulkQueue([], true);
    clearPlaybackContext();
    setQueueItems([]);
    setData((current) =>
      (current ?? []).map((section) =>
        section.key === "queue"
          ? { ...section, items: [] }
          : section
      )
    );
    pushToast("success", "Queue cleared");
  }

  async function removeQueueItem(itemId: number, title: string) {
    const removedVideoId = queueItems.find((item) => item.id === itemId)?.video.id;
    try {
      await api.deleteQueueItem(itemId);
      setQueueItems((current) => current.filter((item) => item.id !== itemId));
      setData((current) =>
        (current ?? []).map((section) =>
          section.key === "queue"
            ? {
                ...section,
                items: removedVideoId ? section.items.filter((item) => item.id !== removedVideoId) : section.items,
              }
            : section,
        ),
      );
      pushToast("success", "Removed from queue", title);
    } catch (nextError) {
      pushToast(
        "error",
        "Unable to remove from queue",
        nextError instanceof Error ? nextError.message : "Unknown queue error",
      );
    }
  }

  async function loadMore(topic: PagedTopic, reset = false) {
    if (loadingMoreRef.current[topic]) return;
    if (!reset && !hasMoreRef.current[topic]) return;

    loadingMoreRef.current = { ...loadingMoreRef.current, [topic]: true };
    setPagedLoading((current) => ({ ...current, [topic]: true }));
    try {
      if (topic === "recent") {
        const next = await api.videos({ offset: reset ? 0 : countRef.current[topic], limit: PAGE_SIZE });
        setPagedItems((current) => ({ ...current, [topic]: reset ? next : [...current[topic], ...next] }));
        const nextHasMore = next.length >= PAGE_SIZE;
        setPagedHasMore((current) => ({ ...current, [topic]: nextHasMore }));
        hasMoreRef.current = { ...hasMoreRef.current, [topic]: nextHasMore };
        countRef.current = {
          ...countRef.current,
          [topic]: (reset ? 0 : countRef.current[topic]) + next.length,
        };
      } else {
        const next =
          topic === "random"
            ? await api.suggested(reset ? 0 : countRef.current[topic], PAGE_SIZE)
            : await api.explore(reset ? 0 : countRef.current[topic], PAGE_SIZE);
        setPagedItems((current) => ({
          ...current,
          [topic]: reset ? next.items : [...current[topic], ...next.items],
        }));
        setPagedHasMore((current) => ({ ...current, [topic]: next.has_more }));
        hasMoreRef.current = { ...hasMoreRef.current, [topic]: next.has_more };
        countRef.current = {
          ...countRef.current,
          [topic]: (reset ? 0 : countRef.current[topic]) + next.items.length,
        };
      }
    } catch (nextError) {
      pushToast(
        "error",
        topic === "recent" ? "Recently added failed" : topic === "random" ? "Suggested failed" : "Explore failed",
        nextError instanceof Error ? nextError.message : "Unable to load more videos",
      );
    } finally {
      loadingMoreRef.current = { ...loadingMoreRef.current, [topic]: false };
      setPagedLoading((current) => ({ ...current, [topic]: false }));
    }
  }

  useEffect(() => {
    void loadMore("explore", true);
  }, []);

  useEffect(() => {
    if (!PAGED_TOPICS.has(activeTopic)) return;
    const topic = activeTopic as PagedTopic;
    if (!pagedItems[topic].length && !pagedLoading[topic]) {
      void loadMore(topic, true);
    }
  }, [activeTopic, pagedItems, pagedLoading]);

  useEffect(() => {
    if (activeTopic === "queue") {
      void refreshQueue();
    }
  }, [activeTopic]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = () => {
      if (cancelled) return;
      const delay = hasPendingFeedItems ? HOME_FEED_FAST_POLL_MS : HOME_FEED_POLL_MS;
      timer = setTimeout(async () => {
        if (cancelled || document.visibilityState !== "visible") {
          schedule();
          return;
        }
        await refreshVisibleHomeContent();
        schedule();
      }, delay);
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      void refreshVisibleHomeContent();
    };

    const handleFocus = () => {
      void refreshVisibleHomeContent();
    };

    schedule();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("focus", handleFocus);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("focus", handleFocus);
    };
  }, [activeTopic, hasPendingFeedItems, queueItems.length]);

  useEffect(() => {
    function onScroll() {
      const visibleTopics: PagedTopic[] =
        activeTopic === "all"
          ? ["explore"]
          : PAGED_TOPICS.has(activeTopic)
            ? [activeTopic as PagedTopic]
            : [];
      if (!visibleTopics.length) return;
      const scrollBottom = window.innerHeight + window.scrollY;
      const threshold = document.documentElement.scrollHeight - 720;
      if (scrollBottom >= threshold) {
        for (const topic of visibleTopics) {
          if (!hasMoreRef.current[topic] || loadingMoreRef.current[topic]) continue;
          void loadMore(topic);
          break;
        }
      }
    }

    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [activeTopic]);

  useEffect(() => {
    const visibleTopics: PagedTopic[] =
      activeTopic === "all"
        ? ["explore"]
        : PAGED_TOPICS.has(activeTopic)
          ? [activeTopic as PagedTopic]
          : [];
    for (const topic of visibleTopics) {
      if (pagedHasMore[topic] && !pagedLoading[topic] && document.documentElement.scrollHeight <= window.innerHeight + 160) {
        void loadMore(topic);
        break;
      }
    }
  }, [activeTopic, pagedHasMore, pagedItems, pagedLoading]);

  const recentSection = useMemo(() => feedSections.find((section) => section.key === "recent"), [feedSections]);
  const stackedSections = useMemo(() => feedSections.filter((section) => section.key !== "recent"), [feedSections]);
  const validTopics = useMemo(() => new Set(["all", "explore", ...feedSections.map((section) => section.key)]), [feedSections]);
  const selectedStaticSection = useMemo(
    () =>
      activeTopic !== "all" && activeTopic !== "queue" && !PAGED_TOPICS.has(activeTopic)
        ? feedSections.find((section) => section.key === activeTopic) ?? null
        : null,
    [activeTopic, feedSections],
  );
  const recentHomepageItems = useMemo(() => {
    if (!recentSection) return [];
    const metrics = gridMetricsForDensity(preferences.density);
    const effectiveWidth = recentSectionWidth || window.innerWidth - 48;
    const columns = Math.max(1, Math.floor((effectiveWidth + metrics.gap) / (metrics.minWidth + metrics.gap)));
    return recentSection.items.slice(0, columns * 4);
  }, [preferences.density, recentSection, recentSectionWidth]);

  useEffect(() => {
    if (validTopics.has(requestedTopic)) {
      setActiveTopic(requestedTopic);
    } else {
      setActiveTopic("all");
    }
  }, [requestedTopic, validTopics]);

  function selectTopic(topic: string) {
    setActiveTopic(topic);
    if (topic === "all") {
      setSearchParams({}, { replace: true });
    } else {
      setSearchParams({ topic }, { replace: true });
    }
  }
  useEffect(() => {
    const node = recentGridRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setRecentSectionWidth(entry.contentRect.width);
    });
    observer.observe(node);
    setRecentSectionWidth(node.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, [recentGridRef, activeTopic, preferences.density]);

  if (loading && !feedSections.length) return <HomePageSkeleton />;
  if (error && !feedSections.length) return <div className="panel error">{error}</div>;

  return (
    <div className="page-stack home-page">
      <div className="topic-strip">
        <button className={`topic-chip ${activeTopic === "all" ? "active" : ""}`} onClick={() => selectTopic("all")}>All</button>
        {feedSectionsWithoutQueue.map((section) => (
          <button
            key={section.key}
            className={`topic-chip ${activeTopic === section.key ? "active" : ""}`}
            onClick={() => selectTopic(section.key)}
          >
            {section.title}
          </button>
        ))}
        <button
          className={`topic-chip ${activeTopic === "explore" ? "active" : ""}`}
          onClick={() => selectTopic("explore")}
        >
          Explore
        </button>
      </div>

      {activeTopic === "all" && recentSection ? (
        <section id={`section-${recentSection.key}`} className="home-block" ref={recentGridRef}>
          <div className="section-heading">
            <h2>{recentSection.title}</h2>
          </div>
          <div className="video-grid-layout">
            {recentHomepageItems.map((video) => (
              <VideoCard key={`recent-home-${video.id}`} item={toCard(video)} canManageVideo={canManageVideo} />
            ))}
          </div>
        </section>
      ) : null}

      {activeTopic === "all" && homeLiveItems.length ? (
        <div id="section-live-home" className="home-block">
          <HomeLiveShelf items={homeLiveItems} />
        </div>
      ) : null}

      {activeTopic === "all" && stackedSections.map((section) => (
        <div key={section.key} id={`section-${section.key}`} className="home-block">
          {section.key === "queue" ? (
            <section className="rail-section">
              <div className="section-heading">
                <h2>
                  <a className="section-heading-link" href="/?topic=queue" onClick={(event) => { event.preventDefault(); selectTopic("queue"); }}>
                    {section.title}
                  </a>
                </h2>
                {section.items.length ? <button className="ghost-button" onClick={() => void clearQueue()}>Clear queue</button> : null}
              </div>
              {section.items.length ? <VideoRail title="" items={section.items} canManageVideo={canManageVideo} /> : <EmptyState message="Your queue is empty." />}
            </section>
          ) : (
            <VideoRail title={section.title} items={section.items} canManageVideo={canManageVideo} />
          )}
        </div>
      ))}

      {selectedStaticSection ? (
        <div id={`section-${selectedStaticSection.key}`} className="home-block">
          {selectedStaticSection.key === "queue" ? (
            <section className="rail-section">
              <div className="section-heading">
                <h2>{selectedStaticSection.title}</h2>
                {selectedStaticSection.items.length ? <button className="ghost-button" onClick={() => void clearQueue()}>Clear queue</button> : null}
              </div>
              {selectedStaticSection.items.length ? (
                <VideoRail title="" items={selectedStaticSection.items} canManageVideo={canManageVideo} />
              ) : (
                <EmptyState message="Your queue is empty." />
              )}
            </section>
          ) : (
            <section className={`rail-section ${selectedStaticSection.key === "continue" || selectedStaticSection.key === "longform" ? "home-grid-section" : ""}`}>
              <div className="section-heading">
                <h2>{selectedStaticSection.title}</h2>
              </div>
              {selectedStaticSection.items.length ? (
                selectedStaticSection.key === "continue" || selectedStaticSection.key === "longform" ? (
                  <div className="video-grid-layout">
                    {selectedStaticSection.items.map((video) => (
                      <VideoCard
                        key={`${selectedStaticSection.key}-${video.id}`}
                        item={toCard(video)}
                        canManageVideo={canManageVideo}
                      />
                    ))}
                  </div>
                ) : (
                  <VideoRail title="" items={selectedStaticSection.items} canManageVideo={canManageVideo} />
                )
              ) : selectedStaticSection.key === "continue" ? (
                <EmptyState message="There's nothing to continue." />
              ) : selectedStaticSection.key === "longform" ? (
                <EmptyState message="No long-form videos yet." />
              ) : (
                <EmptyState message={`No ${selectedStaticSection.title.toLowerCase()} yet.`} />
              )}
            </section>
          )}
        </div>
      ) : null}

      {activeTopic === "queue" ? (
        <section id="section-queue-manage" className="home-block">
          <div className="section-heading">
            <h2>Queue</h2>
            {queueItems.length ? <button className="ghost-button" onClick={() => void clearQueue()}>Clear queue</button> : null}
          </div>
          {queueItems.length ? (
            <div className="queue-management-grid">
              {queueItems.map((item) => (
                <div key={`queue-item-${item.id}`} className="queue-management-card">
                  <VideoCard item={toCard(item.video)} canManageVideo={canManageVideo} />
                  <div className="queue-item-actions">
                    <button
                      className="ghost-button"
                      onClick={() => void removeQueueItem(item.id, item.video.title)}
                    >
                      Remove from queue
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : queueLoading ? (
            <div className="explore-sentinel">
              <span className="loading-dots" aria-label="Loading queue"><span>.</span><span>.</span><span>.</span></span>
            </div>
          ) : (
            <EmptyState message="Your queue is empty." />
          )}
        </section>
      ) : null}

      {activeTopic === "all" || activeTopic === "explore" ? (
      <section id="section-explore" className="home-block">
        <div className="section-heading">
          <h2>Explore</h2>
        </div>
        {pagedItems.explore.length ? (
          <>
            <div className="video-grid-layout">
              {pagedItems.explore.map((video) => (
                <VideoCard key={`explore-${video.id}`} item={toCard(video)} canManageVideo={canManageVideo} />
              ))}
            </div>
            <div ref={sentinelRef} className="explore-sentinel">
              {pagedLoading.explore ? <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span> : null}
            </div>
          </>
        ) : pagedLoading.explore ? (
          <div className="explore-sentinel">
            <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span>
          </div>
        ) : (
          <EmptyState message="No videos indexed yet." />
        )}
      </section>
      ) : null}

      {activeTopic === "recent" ? (
        <section id="section-recent-grid" className="home-block">
          <div className="section-heading">
            <h2>Recently Added</h2>
          </div>
          {pagedItems.recent.length ? (
            <>
              <div className="video-grid-layout">
                {pagedItems.recent.map((video) => (
                  <VideoCard
                    key={`recent-${video.id}`}
                    item={toCard(video, "recently-added")}
                    canManageVideo={canManageVideo}
                  />
                ))}
              </div>
              <div className="explore-sentinel">
                {pagedLoading.recent ? <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span> : null}
              </div>
            </>
          ) : pagedLoading.recent ? (
            <div className="explore-sentinel">
              <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span>
            </div>
          ) : (
            <EmptyState message="No videos indexed yet." />
          )}
        </section>
      ) : null}

      {activeTopic === "random" ? (
        <section id="section-suggested-grid" className="home-block">
          <div className="section-heading">
            <h2>Suggested</h2>
          </div>
          {pagedItems.random.length ? (
            <>
              <div className="video-grid-layout">
                {pagedItems.random.map((video) => (
                  <VideoCard key={`suggested-${video.id}`} item={toCard(video)} canManageVideo={canManageVideo} />
                ))}
              </div>
              <div className="explore-sentinel">
                {pagedLoading.random ? <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span> : null}
              </div>
            </>
          ) : pagedLoading.random ? (
            <div className="explore-sentinel">
              <span className="loading-dots" aria-label="Loading more"><span>.</span><span>.</span><span>.</span></span>
            </div>
          ) : (
            <EmptyState message="No suggested videos available yet." />
          )}
        </section>
      ) : null}
    </div>
  );
}
