import { Link, useParams } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { CollectionCard } from "../components/CollectionCard";
import { VideoRail } from "../components/VideoRail";
import { api } from "../api/client";
import { useAsyncData } from "../hooks/useAsyncData";
import { ProfilePageSkeleton } from "../components/PageSkeletons";
import { pushToast } from "../lib/notifications";

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
    reason: "profile",
    published_at: video.published_at,
    youtube_view_count: video.youtube_view_count,
    youtube_like_count: video.youtube_like_count,
    youtube_comment_count: video.youtube_comment_count,
  };
}

export function ProfilePage({ currentProfile }: { currentProfile: { name: string; is_admin?: boolean } | null }) {
  const params = useParams();
  const username = params.username ?? currentProfile?.name ?? "";
  const canManageVideo = Boolean(currentProfile?.is_admin);
  const { data, loading, error, setData } = useAsyncData(() => api.profileSummary(username), [username]);

  if (loading) return <ProfilePageSkeleton />;
  if (error || !data) return <div className="panel error">{error ?? "Unable to load profile"}</div>;
  const profileData = data;

  async function togglePlaylistSaved(playlistId: number, saved: boolean) {
    const playlist = profileData.playlists.find((item: any) => item.id === playlistId);
    try {
      await api.setPlaylistSaved(playlistId, saved);
      pushToast(
        "success",
        saved ? "Saved playlist videos" : "Removed playlist videos from saved",
        playlist?.name ?? "Playlist",
        saved ? { href: `/profile/${profileData.profile.name}/saved` } : undefined,
      );
      setData(await api.profileSummary(username));
    } catch (nextError) {
      pushToast("error", saved ? "Unable to save playlist videos" : "Unable to unsave playlist videos", nextError instanceof Error ? nextError.message : "Unknown save error");
    }
  }

  return (
    <div className="page-stack profile-page">
      <section className="panel profile-banner">
        <div className="profile-identity">
          <div className="channel-avatar-large">
            {profileData.profile.avatar_url ? <img src={profileData.profile.avatar_url} alt={profileData.profile.display_name} /> : <span>{profileData.profile.display_name.slice(0, 2).toUpperCase()}</span>}
          </div>
          <div className="profile-copy">
            <h1>{profileData.profile.display_name}</h1>
            <span>@{profileData.profile.name}</span>
            <small>
              {profileData.subscriptions.length} subscriptions • {profileData.playlists.length} playlists • {profileData.recently_watched.length} history items
            </small>
          </div>
        </div>
      </section>

      <section className="rail-section">
        <div className="section-heading">
          <h2>Playlists</h2>
        </div>
        {profileData.playlists.length ? (
          <div className="video-grid-layout playlist-grid">
            {profileData.playlists.map((playlist: any) => (
              <CollectionCard
                key={playlist.id}
                title={playlist.name}
                subtitle={playlist.description}
                badge="Playlist"
                to={`/playlists/${playlist.id}`}
                meta={`${playlist.item_count} items`}
                stackedThumbnails={playlist.preview_thumbnails ?? []}
                thumbnailUrl={playlist.preview_thumbnails?.[0] ?? null}
                menuItems={[
                  {
                    label: playlist.all_videos_saved ? "Remove from saved" : "Add to saved",
                    onSelect: () => togglePlaylistSaved(playlist.id, !playlist.all_videos_saved),
                  },
                ]}
              />
            ))}
          </div>
        ) : (
          <EmptyState message="You have no playlists yet." />
        )}
      </section>

      <section className="rail-section">
        <div className="section-heading">
          <h2>Subscriptions</h2>
        </div>
        {profileData.subscriptions.length ? (
          <div className="profile-avatar-grid">
            {profileData.subscriptions.map((channel: any) => (
              <Link className="profile-avatar-link" to={`/channels/${channel.slug}`} key={channel.id} aria-label={channel.name}>
                <span className="channel-avatar-large subscription-avatar">
                  {channel.avatar_url ? <img src={channel.avatar_url} alt={channel.name} /> : <span>{channel.name.slice(0, 2).toUpperCase()}</span>}
                </span>
                {channel.has_new_video ? <span className="subscription-new-dot" aria-hidden="true" /> : null}
                <span className="profile-avatar-tooltip">{channel.name}</span>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyState message="You have no subscribed channels, subscribe to see them here." />
        )}
      </section>

      <VideoRail
        title="History"
        titleCount={profileData.recently_watched.length}
        items={profileData.recently_watched.map(toCard)}
        emptyMessage="You have not watched anything yet."
        layout="shelf"
        canManageVideo={canManageVideo}
      />

      <VideoRail
        title="Likes"
        titleCount={profileData.liked_videos.length}
        items={profileData.liked_videos.map(toCard)}
        emptyMessage="You have no liked videos yet."
        layout="shelf"
        canManageVideo={canManageVideo}
      />

      <section className="rail-section">
        <div className="section-heading">
          <h2>
            <Link className="section-heading-link" to={`/profile/${profileData.profile.name}/saved`}>
              Saved
            </Link>
            <button
              className="info-tip profile-inline-tip"
              type="button"
              data-tooltip="Saved videos and saved playlists are exempt from retention."
              aria-label="Saved retention info"
            >
              i
            </button>
          </h2>
          <span className="section-count">{profileData.saved_videos.length}</span>
        </div>
        <VideoRail
          items={profileData.saved_videos.map(toCard)}
          emptyMessage="You have no saved videos yet."
          layout="shelf"
          canManageVideo={canManageVideo}
        />
      </section>
    </div>
  );
}
