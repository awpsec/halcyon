import { useMemo } from "react";
import { Link } from "react-router-dom";
import { api, type FeedCard, type Profile, type VideoSummary } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ProfilePageSkeleton } from "../components/PageSkeletons";
import { VideoCard, VideoRail } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";
import { pushToast } from "../lib/notifications";

function toCard(video: VideoSummary): FeedCard {
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
    watched: video.watched,
    progress_seconds: video.progress_seconds,
    reason: "subscription",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

export function ChannelsPage({ profile }: { profile: Profile | null }) {
  const canManageVideo = Boolean(profile?.is_admin);
  const channelsState = useAsyncData(() => api.channels(), []);
  const feedState = useAsyncData(() => api.home(), []);
  const exploreState = useAsyncData(() => api.explore(0, 60), []);

  const subscribedChannelIds = useMemo(
    () => new Set((channelsState.data ?? []).filter((channel: any) => channel.subscribed).map((channel: any) => channel.id)),
    [channelsState.data],
  );
  const subscribedChannels = useMemo(() => (channelsState.data ?? []).filter((channel: any) => channel.subscribed), [channelsState.data]);
  const hasNewSubscriptions = useMemo(
    () => subscribedChannels.some((channel: any) => channel.has_new_video),
    [subscribedChannels],
  );

  const subscribedSections = useMemo(
    () =>
      (feedState.data ?? [])
        .filter((section) => ["recent", "subscriptions", "random", "series"].includes(section.key))
        .map((section) => ({
          ...section,
          items: section.items.filter((item) => item.channel_id && subscribedChannelIds.has(item.channel_id)),
        }))
        .filter((section) => section.items.length),
    [feedState.data, subscribedChannelIds],
  );

  const exploreItems = useMemo(
    () =>
      (exploreState.data?.items ?? [])
        .filter((item) => item.channel_id && subscribedChannelIds.has(item.channel_id))
        .map(toCard),
    [exploreState.data, subscribedChannelIds],
  );

  if (channelsState.loading || feedState.loading || exploreState.loading) return <ProfilePageSkeleton />;
  if (channelsState.error || feedState.error || exploreState.error) return <div className="panel error">{channelsState.error ?? feedState.error ?? exploreState.error}</div>;

  if (!subscribedChannelIds.size) {
    return <EmptyState message="You have no subscribed channels, subscribe to see them here." />;
  }

  return (
    <div className="page-stack">
      <section className="rail-section">
        <div className="section-heading">
          <h2>Subscriptions</h2>
          {hasNewSubscriptions ? (
            <button
              className="ghost-button"
              type="button"
              onClick={async () => {
                await api.clearSubscriptionMarkers();
                channelsState.setData(await api.channels());
                pushToast("success", "Marked subscription updates as seen");
              }}
            >
              Mark all seen
            </button>
          ) : null}
        </div>
        <div className="profile-avatar-grid subscriptions-avatar-row">
          {subscribedChannels.map((channel: any) => (
            <Link className="profile-avatar-link" to={`/channels/${channel.slug}`} key={channel.id} aria-label={channel.name}>
              <span className="channel-avatar-large subscription-avatar">
                {channel.avatar_url ? <img src={channel.avatar_url} alt={channel.name} /> : <span>{channel.name.slice(0, 2).toUpperCase()}</span>}
              </span>
              {channel.has_new_video ? <span className="subscription-new-dot" aria-hidden="true" /> : null}
              <span className="profile-avatar-tooltip">{channel.name}</span>
            </Link>
          ))}
        </div>
      </section>

      {subscribedSections.map((section) => (
        <VideoRail
          key={section.key}
          title={section.title === "Subscribed Channels" ? "Recently Added" : section.title}
          items={section.items}
          canManageVideo={canManageVideo}
        />
      ))}

      <section className="home-block">
        <div className="section-heading">
          <h2>Explore</h2>
        </div>
        {exploreItems.length ? (
          <div className="video-grid-layout">
            {exploreItems.map((item) => (
              <VideoCard key={`subscription-explore-${item.id}`} item={item} canManageVideo={canManageVideo} />
            ))}
          </div>
        ) : (
          <EmptyState message="No more videos from your subscriptions yet." />
        )}
      </section>
    </div>
  );
}
