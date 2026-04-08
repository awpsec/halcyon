import { api } from "../api/client";
import { useAsyncData } from "../hooks/useAsyncData";

export function SyncReviewPage() {
  const { data, loading, error, setData } = useAsyncData(() => api.syncReview(), []);

  if (loading) return <div className="panel">Loading review queue…</div>;
  if (error) return <div className="panel error">{error}</div>;

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Sync Review</p>
          <h1>Uncertain YouTube matches</h1>
        </div>
      </div>
      <div className="list-panel">
        {(data?.items ?? []).map((item: any) => (
          <div className="list-row" key={item.id}>
            <div>
              <strong>{item.video_title}</strong>
              <p>
                {item.youtube_video_id} • confidence {Math.round(item.confidence * 100)}%
              </p>
            </div>
            <div className="row-actions">
              <button
                className="ghost-button"
                onClick={async () => {
                  await api.approveMatch(item.id);
                  setData(await api.syncReview());
                }}
              >
                Approve
              </button>
              <button
                className="ghost-button"
                onClick={async () => {
                  await api.unlinkMatch(item.id);
                  setData(await api.syncReview());
                }}
              >
                Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
