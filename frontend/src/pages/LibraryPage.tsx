import { api, type Profile } from "../api/client";
import { VideoList } from "../components/SectionGrid";
import { useAsyncData } from "../hooks/useAsyncData";

export function LibraryPage({ profile }: { profile: Profile | null }) {
  const videosState = useAsyncData(() => api.videos(), []);

  return (
    <div className="page-stack">
      <section className="panel split-panel">
        <h1>Library</h1>
        <button
          className="action-button"
          onClick={async () => {
            await api.scan();
            videosState.setData(await api.videos());
          }}
        >
          Scan library
        </button>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Indexed videos</h2>
          <p>{videosState.data?.length ?? 0} found</p>
        </div>
        <VideoList
          videos={videosState.data ?? []}
          action={(video) => (
            <button className="ghost-button" onClick={() => void api.addQueueItem(video.id)}>
              Queue
            </button>
          )}
        />
      </section>
    </div>
  );
}
