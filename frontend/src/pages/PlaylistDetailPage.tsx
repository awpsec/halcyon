import { useParams } from "react-router-dom";
import { api, type Profile, type VideoSummary } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { CollectionPageSkeleton } from "../components/PageSkeletons";
import { VideoRail } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";
import { normalizeImportedText } from "../lib/format";
import { writePlaybackContext } from "../lib/playbackContext";

function toCard(video: VideoSummary) {
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
    reason: "playlist",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

export function PlaylistDetailPage({ profile }: { profile: Profile | null }) {
  const params = useParams();
  const playlistId = Number(params.playlistId);
  const canManageVideo = Boolean(profile?.is_admin);
  const { data, loading, error } = useAsyncData(
    () => api.playlistDetail(playlistId),
    [playlistId],
  );
  const queueState = useAsyncData(() => api.queue(), [playlistId]);

  if (loading) return <CollectionPageSkeleton titleWidth="w-26" />;
  if (error || !data)
    return <div className="panel error">{error ?? "Playlist not found"}</div>;

  const videos = data.videos as VideoSummary[];
  const orderedIds = videos.map((video) => video.id);
  const displayPlaylistName =
    normalizeImportedText(data.playlist.name) ?? data.playlist.name;

  return (
    <div className="page-stack">
      <section className="collection-hero">
        <div className="collection-hero-copy">
          <div className="collection-eyebrow">Playlist</div>
          <h1>{displayPlaylistName}</h1>
          <p>{data.playlist.item_count} items</p>
        </div>
      </section>
      {videos.length ? (
        <VideoRail
          title="Playlist videos"
          items={videos.map(toCard)}
          layout="grid"
          beforeNavigate={(item) => {
            writePlaybackContext({
              kind: "playlist",
              id: playlistId,
              title: displayPlaylistName,
              videoIds: orderedIds,
              activeVideoId: item.id,
              savedQueueIds: (queueState.data?.items ?? []).map(
                (entry: any) => entry.video.id,
              ),
              queueApplied: false,
            });
          }}
          canManageVideo={canManageVideo}
        />
      ) : (
        <EmptyState message="This playlist has no videos yet." />
      )}
    </div>
  );
}
