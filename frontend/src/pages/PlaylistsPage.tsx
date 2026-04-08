import { useState } from "react";
import { api, type Profile } from "../api/client";
import { CollectionCard } from "../components/CollectionCard";
import { EmptyState } from "../components/EmptyState";
import { CollectionPageSkeleton } from "../components/PageSkeletons";
import { useAsyncData } from "../hooks/useAsyncData";
import { pushToast } from "../lib/notifications";

export function PlaylistsPage({ profile }: { profile: Profile | null }) {
  const { data, loading, error, setData } = useAsyncData(() => api.playlists(), []);
  const [name, setName] = useState("");

  async function togglePlaylistSaved(playlistId: number, saved: boolean) {
    const playlist = (data ?? []).find((item: any) => item.id === playlistId);
    try {
      await api.setPlaylistSaved(playlistId, saved);
      pushToast(
        "success",
        saved ? "Saved playlist videos" : "Removed playlist videos from saved",
        playlist?.name ?? "Playlist",
        saved && profile ? { href: `/profile/${profile.name}/saved` } : undefined,
      );
      setData(await api.playlists());
    } catch (error) {
      pushToast("error", saved ? "Unable to save playlist videos" : "Unable to unsave playlist videos", error instanceof Error ? error.message : "Unknown save error");
    }
  }

  if (loading) return <CollectionPageSkeleton titleWidth="w-24" />;
  if (error) return <div className="panel error">{error}</div>;

  return (
    <div className="page-stack">
      <section className="collection-hero">
        <div className="collection-hero-copy">
          <div className="collection-eyebrow">Playlists</div>
          <h1>Your playlists</h1>
        </div>
        <div className="inline-form">
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="New playlist name" />
          <button
            className="ghost-button"
            disabled={!name || !profile}
            onClick={async () => {
              if (!profile) return;
              await api.createPlaylist({ user_id: profile.id, name });
              setName("");
              setData(await api.playlists());
            }}
          >
            Create
          </button>
        </div>
      </section>
      {(data ?? []).length ? (
        <section className="video-grid-layout">
          {(data ?? []).map((playlist: any) => (
            <CollectionCard
              key={playlist.id}
              title={playlist.name}
              subtitle={playlist.description}
              badge="Playlist"
              thumbnailUrl={playlist.preview_thumbnails?.[0] ?? null}
              stackedThumbnails={playlist.preview_thumbnails ?? []}
              to={`/playlists/${playlist.id}`}
              meta={`${playlist.item_count} items`}
              menuItems={[
                {
                  label: playlist.all_videos_saved ? "Remove from saved" : "Add to saved",
                  onSelect: () => togglePlaylistSaved(playlist.id, !playlist.all_videos_saved),
                },
              ]}
            />
          ))}
        </section>
      ) : (
        <EmptyState message="You have no playlists yet." />
      )}
    </div>
  );
}
