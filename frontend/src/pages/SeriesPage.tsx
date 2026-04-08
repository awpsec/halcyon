import { api, type SeriesSummary } from "../api/client";
import { CollectionCard } from "../components/CollectionCard";
import { EmptyState } from "../components/EmptyState";
import { CollectionPageSkeleton } from "../components/PageSkeletons";
import { useAsyncData } from "../hooks/useAsyncData";
import { pushToast } from "../lib/notifications";

export function SeriesPage() {
  const { data, loading, error, setData } = useAsyncData(() => api.series(), []);

  async function toggleSeriesSaved(seriesId: number, saved: boolean) {
    const series = (data ?? []).find((item) => item.id === seriesId);
    try {
      await api.setSeriesSaved(seriesId, saved);
      pushToast("success", saved ? "Saved series videos" : "Removed series videos from saved", series?.name ?? "Series");
      setData(await api.series());
    } catch (nextError) {
      pushToast("error", saved ? "Unable to save series videos" : "Unable to unsave series videos", nextError instanceof Error ? nextError.message : "Unknown save error");
    }
  }

  if (loading) return <CollectionPageSkeleton titleWidth="w-30" />;
  if (error) return <div className="panel error">{error}</div>;

  return (
    <div className="page-stack">
      <section className="collection-hero">
        <div className="collection-hero-copy">
          <div className="collection-eyebrow">Series</div>
          <h1>Ordered runs and playlists inferred from folders</h1>
        </div>
      </section>
      {(data ?? []).length ? (
        <section className="video-grid-layout">
          {(data ?? []).map((series: SeriesSummary) => (
            <CollectionCard
              key={series.id}
              title={series.name}
              subtitle={series.description ?? "Folder-inferred series"}
              badge="Series"
              thumbnailUrl={series.preview_thumbnails?.[0] ?? null}
              stackedThumbnails={series.preview_thumbnails ?? []}
              to={`/series/${series.id}`}
              meta={`${series.video_count} videos`}
              menuItems={[
                {
                  label: series.all_videos_saved ? "Remove from saved" : "Add to saved",
                  onSelect: () => toggleSeriesSaved(series.id, !series.all_videos_saved),
                },
                {
                  label: "Sync",
                  onSelect: async () => {
                    await api.syncSeries(series.id);
                    pushToast("success", "Series sync started", series.name);
                  },
                },
              ]}
            />
          ))}
        </section>
      ) : (
        <EmptyState message="No series found yet." />
      )}
    </div>
  );
}
