import { useParams } from "react-router-dom";
import { api, type Profile } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { CollectionPageSkeleton } from "../components/PageSkeletons";
import { VideoRail } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";

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
    watched: video.watched,
    progress_seconds: video.progress_seconds,
    reason: "saved",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

export function SavedVideosPage({ profile }: { profile: Profile | null }) {
  const params = useParams();
  const username = params.username ?? "";
  const canManageVideo = Boolean(profile?.is_admin);
  const { data, loading, error } = useAsyncData(() => api.profileSaved(username), [username]);

  if (loading) return <CollectionPageSkeleton titleWidth="w-28" />;
  if (error || !data) return <div className="panel error">{error ?? "Unable to load saved videos"}</div>;

  return (
    <div className="page-stack">
      <section className="collection-hero">
        <div className="collection-hero-copy">
          <div className="collection-eyebrow">Saved</div>
          <h1>{data.profile.display_name}&rsquo;s saved videos</h1>
          <p>{data.items.length} saved videos</p>
        </div>
      </section>
      {data.items.length ? (
        <VideoRail title="Saved videos" items={data.items.map(toCard)} layout="grid" canManageVideo={canManageVideo} />
      ) : (
        <EmptyState message="No saved videos yet." />
      )}
    </div>
  );
}
