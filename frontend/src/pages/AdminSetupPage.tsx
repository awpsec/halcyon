import { useMemo, useState } from "react";
import { api, type Profile } from "../api/client";

type Props = {
  profile: Profile;
  onComplete: (profile: Profile) => void;
};

function formatError(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    return parsed.detail?.trim() || fallback;
  } catch {
    return error.message || fallback;
  }
}

export function AdminSetupPage({ profile, onComplete }: Props) {
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [confirmedPhraseSaved, setConfirmedPhraseSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  const recoveryPhrase = useMemo(
    () => profile.admin_setup_recovery_phrase ?? [],
    [profile.admin_setup_recovery_phrase],
  );
  const passwordTooShort = password.length > 0 && password.length < 8;
  const passwordsMismatch = confirmPassword.length > 0 && confirmPassword !== password;
  const setupReady =
    confirmedPhraseSaved &&
    password.length >= 8 &&
    confirmPassword === password;

  function handleDownloadRecoveryPhrase() {
    if (!recoveryPhrase.length) {
      return;
    }
    const body = [
      "Halcyon admin recovery phrase",
      "",
      `username: ${profile.name}`,
      `recovery phrase: ${recoveryPhrase.join(" ")}`,
      "",
      "Store this file somewhere safe. Anyone with this phrase can recover the admin account.",
    ].join("\n");
    const blob = new Blob([body], { type: "text/plain;charset=utf-8" });
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `halcyon-admin-recovery-${profile.name}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(objectUrl);
  }

  async function handleCopyRecoveryPhrase() {
    if (!recoveryPhrase.length) {
      return;
    }
    const phrase = recoveryPhrase.join(" ");
    try {
      await navigator.clipboard.writeText(phrase);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  async function handleSubmit() {
    if (!confirmedPhraseSaved) {
      setError("Confirm that you saved the recovery phrase before finishing setup.");
      return;
    }
    if (password.length < 8) {
      setError("Admin password must be at least 8 characters.");
      return;
    }
    if (confirmPassword !== password) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const next = await api.completeAdminSetup({ password });
      onComplete(next);
    } catch (nextError) {
      setError(formatError(nextError, "Unable to finish admin setup"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="hero-page">
      <div className="hero-card auth-card admin-setup-card">
        <div className="admin-setup-header">
          <span className="collection-eyebrow">Admin setup</span>
          <h1>Finish securing halcyon</h1>
          <p className="muted-copy">
            You are signed in as <strong>@{profile.name}</strong>. Save this recovery phrase now, then choose a permanent admin password.
          </p>
        </div>

        <div className="admin-setup-phrase-card">
          <div className="admin-setup-phrase-header">
            <div className="admin-setup-phrase-copy">
              <strong>Recovery phrase</strong>
              <small>This only appears during first-time admin setup.</small>
            </div>
            <div className="admin-setup-phrase-actions">
              <button
                type="button"
                className="admin-setup-download-button"
                onClick={handleCopyRecoveryPhrase}
              >
                {copyState === "copied" ? "Copied" : "Copy phrase"}
              </button>
              <button
                type="button"
                className="admin-setup-download-button"
                onClick={handleDownloadRecoveryPhrase}
              >
                Download phrase
              </button>
            </div>
          </div>
          <div className="admin-setup-phrase-grid">
            {recoveryPhrase.map((word, index) => (
              <span key={`${index + 1}-${word}`} className="admin-setup-word-chip">
                <small>{index + 1}</small>
                <strong>{word}</strong>
              </span>
            ))}
          </div>
          <label className="admin-setup-confirm">
            <input
              type="checkbox"
              checked={confirmedPhraseSaved}
              onChange={(event) => setConfirmedPhraseSaved(event.target.checked)}
            />
            <span>I saved this recovery phrase somewhere safe.</span>
          </label>
          {copyState === "failed" ? (
            <div className="error-inline">Clipboard copy failed. Download the phrase instead.</div>
          ) : null}
        </div>

        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="New admin password"
          />
          <input
            type="password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            placeholder="Confirm new admin password"
          />
          <div className="admin-setup-status">
            <div className={confirmedPhraseSaved ? "admin-setup-status-ok" : ""}>
              {confirmedPhraseSaved ? "Saved" : "Required"}: confirm the recovery phrase is stored safely.
            </div>
            <div className={!passwordTooShort && password.length >= 8 ? "admin-setup-status-ok" : ""}>
              {password.length >= 8 ? "Ready" : "Required"}: use an admin password with at least 8 characters.
            </div>
            <div
              className={
                confirmPassword.length > 0 && !passwordsMismatch && confirmPassword === password
                  ? "admin-setup-status-ok"
                  : ""
              }
            >
              {confirmPassword.length > 0 && !passwordsMismatch && confirmPassword === password ? "Ready" : "Required"}:
              confirm password must match exactly.
            </div>
          </div>
          <button
            type="submit"
            className="action-button"
            disabled={busy || !setupReady}
          >
            {busy ? "Securing..." : "Finish admin setup"}
          </button>
          {passwordTooShort ? (
            <div className="error-inline">Password must be at least 8 characters.</div>
          ) : null}
          {passwordsMismatch ? (
            <div className="error-inline">Passwords do not match.</div>
          ) : null}
          {error ? <div className="error-inline">{error}</div> : null}
        </form>
      </div>
    </div>
  );
}
