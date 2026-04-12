import { useState } from "react";
import { api, type SyncReviewItem } from "../api/client";
import { useAsyncData } from "../hooks/useAsyncData";
import { pushToast } from "../lib/notifications";

function extractApiErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    return parsed.detail?.trim() || error.message;
  } catch {
    return error.message || fallback;
  }
}

function formatConfidence(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "Unknown confidence";
  return `Confidence ${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

type Props = {
  eyebrow?: string;
  title?: string;
  note?: string;
};

export function SyncReviewPanel({
  eyebrow = "Sync Review",
  title = "Uncertain YouTube matches",
  note = "Approve good matches, reject bad ones, or paste a YouTube URL/ID to lock in the exact video yourself.",
}: Props) {
  const { data, loading, error, setData } = useAsyncData(() => api.syncReview(), []);
  const [manualInputs, setManualInputs] = useState<Record<number, string>>({});
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);

  async function refreshQueue() {
    setData(await api.syncReview());
  }

  async function runAction(key: string, action: () => Promise<unknown>, successMessage: string) {
    setPanelError(null);
    setPendingAction(key);
    try {
      await action();
      await refreshQueue();
      pushToast("success", successMessage, "Review queue updated.");
    } catch (actionError) {
      setPanelError(extractApiErrorMessage(actionError, "Unable to update the review queue."));
    } finally {
      setPendingAction(null);
    }
  }

  const items = data?.items ?? [];

  return (
    <div className="sync-review-panel">
      <div className="section-heading">
        <h2>{title}</h2>
      </div>
      <p className="settings-section-note">
        {eyebrow ? (
          <>
            <span className="eyebrow">{eyebrow}</span>
            {" "}
          </>
        ) : null}
        {note}
      </p>
      {panelError ? <p className="update-modal-warning">{panelError}</p> : null}
      {loading ? <div className="search-results-empty">Loading review queue...</div> : null}
      {error && !loading ? <div className="search-results-empty">{error}</div> : null}
      {!loading && !error ? (
        <div className="sync-review-list retention-scroll-wrap">
          {items.map((item: SyncReviewItem) => {
            const manualValue = manualInputs[item.id] ?? "";
            const approveKey = `approve:${item.id}`;
            const rejectKey = `reject:${item.id}`;
            const manualKey = `manual:${item.id}`;
            return (
              <article className="sync-review-card" key={item.id}>
                <div className="sync-review-copy">
                  <div className="sync-review-title-row">
                    <strong>{item.video_title ?? "Unknown video"}</strong>
                    <span className="sync-review-confidence">{formatConfidence(item.confidence)}</span>
                  </div>
                  <div className="sync-review-meta">
                    <span>Local channel: {item.channel_name ?? "Unknown channel"}</span>
                    {item.youtube_title ? <span>Candidate: {item.youtube_title}</span> : null}
                    {item.youtube_channel_title ? <span>YouTube channel: {item.youtube_channel_title}</span> : null}
                  </div>
                  {item.reasons?.length ? (
                    <div className="sync-review-reasons">
                      {item.reasons.map((reason) => (
                        <span className="sync-review-reason-pill" key={`${item.id}:${reason}`}>
                          {reason}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="sync-review-actions">
                  {item.youtube_watch_url ? (
                    <a
                      className="ghost-button settings-utility-button"
                      href={item.youtube_watch_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open candidate
                    </a>
                  ) : null}
                  <button
                    className="ghost-button settings-utility-button"
                    type="button"
                    disabled={pendingAction !== null}
                    onClick={() => void runAction(approveKey, () => api.approveMatch(item.id), "Match approved")}
                  >
                    {pendingAction === approveKey ? "Approving..." : "Approve"}
                  </button>
                  <button
                    className="ghost-button settings-utility-button"
                    type="button"
                    disabled={pendingAction !== null}
                    onClick={() => void runAction(rejectKey, () => api.unlinkMatch(item.id), "Match rejected")}
                  >
                    {pendingAction === rejectKey ? "Rejecting..." : "Reject"}
                  </button>
                </div>
                <div className="sync-review-manual">
                  <input
                    value={manualValue}
                    placeholder="Paste YouTube URL or 11-character video ID"
                    onChange={(event) =>
                      setManualInputs((current) => ({
                        ...current,
                        [item.id]: event.target.value,
                      }))
                    }
                  />
                  <button
                    className="action-button"
                    type="button"
                    disabled={pendingAction !== null || !manualValue.trim()}
                    onClick={() =>
                      void runAction(
                        manualKey,
                        async () => {
                          await api.manualMatch(item.id, manualValue.trim());
                          setManualInputs((current) => ({ ...current, [item.id]: "" }));
                        },
                        "Manual match saved",
                      )
                    }
                  >
                    {pendingAction === manualKey ? "Saving..." : "Use this match"}
                  </button>
                </div>
              </article>
            );
          })}
          {!items.length ? (
            <div className="search-results-empty">No review items right now.</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
