import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { FeedSection, VideoSummary } from "../api/client";
import { normalizeImportedText } from "../lib/format";

function formatDuration(value: number) {
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = value % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function SectionGrid({ section }: { section: FeedSection }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h2>{section.title}</h2>
          <p>{section.items.length} picks</p>
        </div>
      </div>
      <div className="card-grid">
        {section.items.map((item) => (
          <Link
            className="video-card"
            to={`/video/${item.watch_ref ?? item.id}`}
            key={item.id}
          >
            <div className="card-thumb">
              <span>{item.reason.split("-").join(" ")}</span>
            </div>
            <div className="card-body">
              <strong>{normalizeImportedText(item.title) ?? item.title}</strong>
              <span>
                {normalizeImportedText(item.channel) ??
                  item.channel ??
                  "Unknown channel"}
              </span>
              <span>
                {normalizeImportedText(item.series) ??
                  item.series ??
                  "Single upload"}
              </span>
              <div className="meta-row">
                <small>{formatDuration(item.duration_seconds)}</small>
                <small>
                  {item.youtube_view_count
                    ? `${item.youtube_view_count.toLocaleString()} views`
                    : "offline"}
                </small>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

export function VideoList({
  videos,
  action,
}: {
  videos: VideoSummary[];
  action?: (video: VideoSummary) => ReactNode;
}) {
  return (
    <div className="list-panel">
      {videos.map((video) => (
        <div className="list-row" key={video.id}>
          <div>
            <Link to={`/video/${video.watch_ref ?? video.id}`}>
              {normalizeImportedText(video.title) ?? video.title}
            </Link>
            <p>
              {normalizeImportedText(video.channel_name) ??
                video.channel_name ??
                "Unknown channel"}
              {video.series_name
                ? ` • ${normalizeImportedText(video.series_name) ?? video.series_name}`
                : ""}
            </p>
          </div>
          <div className="row-actions">
            <span>{formatDuration(video.duration_seconds)}</span>
            {action?.(video)}
          </div>
        </div>
      ))}
    </div>
  );
}
