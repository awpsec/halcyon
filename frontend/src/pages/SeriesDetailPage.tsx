import { useParams } from "react-router-dom";
import { api, type Profile, type VideoSummary } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { CollectionPageSkeleton } from "../components/PageSkeletons";
import { VideoRail } from "../components/VideoRail";
import { useAsyncData } from "../hooks/useAsyncData";
import { formatRelativeDate, normalizeImportedText } from "../lib/format";
import { writePlaybackContext } from "../lib/playbackContext";
import { pushToast } from "../lib/notifications";

function toCard(video: VideoSummary) {
  return {
    id: video.id,
    title: video.title,
    channel: video.channel_name,
    channel_avatar_url: video.channel_avatar_url,
    series: video.series_name,
    channel_id: video.channel_id,
    series_id: video.series_id,
    duration_seconds: video.duration_seconds,
    thumbnail_url: video.thumbnail_url,
    watched: video.watched,
    progress_seconds: video.progress_seconds,
    reason: "series",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

export function SeriesDetailPage({ profile }: { profile: Profile | null }) {
  const params = useParams();
  const seriesId = Number(params.seriesId);
  const canManageVideo = Boolean(profile?.is_admin);
  const { data, loading, error, setData } = useAsyncData(
    () => api.seriesDetail(seriesId),
    [seriesId],
  );
  const queueState = useAsyncData(() => api.queue(), [seriesId]);

  if (loading) return <CollectionPageSkeleton titleWidth="w-28" />;
  if (error || !data)
    return <div className="panel error">{error ?? "Series not found"}</div>;

  const videos = data.videos as VideoSummary[];
  const orderedIds = videos.map((video) => video.id);
  const firstVideo = videos[0];
  const displaySeriesName =
    normalizeImportedText(data.series.name) ?? data.series.name;

  return (
    <div className="page-stack">
      <section className="collection-hero">
        <div className="collection-hero-copy">
          <div className="collection-eyebrow">Series</div>
          <h1>{displaySeriesName}</h1>
          <p>
            {data.series.video_count} videos
            {firstVideo?.published_at
              ? ` • started ${formatRelativeDate(firstVideo.published_at)}`
              : ""}
          </p>
        </div>
        {canManageVideo ? (
          <button
            className="ghost-button"
            onClick={async () => {
              pushToast("info", "Series sync started", displaySeriesName);
              try {
                const result: any = await api.syncSeries(seriesId);
                if (result?.status === "partial" || result?.status === "failed") {
                  pushToast(
                    "error",
                    "Series sync completed with issues",
                    result?.details?.warning ??
                      result?.details?.error ??
                      displaySeriesName,
                  );
                } else {
                  pushToast("success", "Series sync finished", displaySeriesName);
                }
                setData(await api.seriesDetail(seriesId));
              } catch (nextError) {
                pushToast(
                  "error",
                  "Series sync failed",
                  nextError instanceof Error
                    ? nextError.message
                    : "Unknown sync error",
                );
              }
            }}
          >
            Refresh snapshot
          </button>
        ) : null}
      </section>

      {videos.length ? (
        <VideoRail
          title="Episodes"
          items={videos.map(toCard)}
          layout="grid"
          beforeNavigate={(item) => {
            writePlaybackContext({
              kind: "series",
              id: seriesId,
              title: displaySeriesName,
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
        <EmptyState message="This series has no indexed videos yet." />
      )}
    </div>
  );
}
