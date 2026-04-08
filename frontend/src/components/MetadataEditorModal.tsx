import { useState } from "react";
import { api } from "../api/client";
import { pushToast } from "../lib/notifications";
import { Modal } from "./Modal";

export function MetadataEditorModal({
  videoId,
  initialTitle,
  initialDescription,
  onClose,
  onSaved,
}: {
  videoId: number;
  initialTitle: string;
  initialDescription?: string | null;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [title, setTitle] = useState(initialTitle);
  const [description, setDescription] = useState(initialDescription ?? "");
  const [saving, setSaving] = useState(false);

  async function handleSave() {
    setSaving(true);
    try {
      await api.updateMetadataOverride({
        target_type: "video",
        target_id: videoId,
        payload: {
          title: title.trim(),
          description: description.trim() || null,
        },
      });
      pushToast("success", "Metadata saved");
      await onSaved();
      onClose();
    } catch (error) {
      pushToast("error", "Metadata save failed", error instanceof Error ? error.message : "Unknown error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Edit metadata" onClose={onClose}>
      <div className="modal-form">
        <label className="settings-field">
          <span>Title</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label className="settings-field">
          <span>Description</span>
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={6} />
        </label>
        <div className="row-actions">
          <button className="ghost-button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="action-button" onClick={() => void handleSave()} disabled={saving || !title.trim()}>
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
