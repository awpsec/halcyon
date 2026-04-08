import { useEffect, useRef, useState, type FormEvent } from "react";
import { Modal } from "./Modal";

export function PlaylistCreateModal({
  onClose,
  onCreate,
  pending = false,
}: {
  onClose: () => void;
  onCreate: (name: string) => Promise<void> | void;
  pending?: boolean;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a playlist name.");
      return;
    }
    setError(null);
    await onCreate(trimmed);
  }

  return (
    <Modal title="New playlist" onClose={pending ? () => undefined : onClose}>
      <form className="modal-form playlist-create-modal" onSubmit={handleSubmit}>
        <label className="settings-field">
          <span>Name</span>
          <input
            ref={inputRef}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              if (error) setError(null);
            }}
            placeholder="Playlist name"
            maxLength={120}
            disabled={pending}
          />
        </label>
        {error ? <p className="modal-error-text">{error}</p> : null}
        <div className="row-actions">
          <button
            className="ghost-button"
            type="button"
            onClick={onClose}
            disabled={pending}
          >
            Cancel
          </button>
          <button className="action-button" type="submit" disabled={pending}>
            {pending ? "Creating..." : "Create playlist"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
