import { useEffect, useMemo, useState } from "react";
import { api, type AuthBootstrapStatus } from "../api/client";

type StoredSession = {
  username: string;
  display_name: string;
  session_token: string;
  avatar_url?: string | null;
};

type Props = {
  storedSessions: StoredSession[];
  onLogin: (username: string, password: string) => Promise<void>;
  onRegister: (username: string, password: string, displayName: string, pin: string) => Promise<void>;
  onResetPassword: (username: string, pin: string, password: string) => Promise<void>;
  onRecover: (recoveryPhrase: string, password: string) => Promise<void>;
  onSwitch: (sessionToken: string) => Promise<void>;
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

export function AuthPage({ storedSessions, onLogin, onRegister, onResetPassword, onRecover, onSwitch }: Props) {
  const [mode, setMode] = useState<"login" | "create">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [pin, setPin] = useState("");
  const [recoveryPhrase, setRecoveryPhrase] = useState("");
  const [recoveryPassword, setRecoveryPassword] = useState("");
  const [recoveryConfirmPassword, setRecoveryConfirmPassword] = useState("");
  const [resetUsername, setResetUsername] = useState("");
  const [resetPin, setResetPin] = useState("");
  const [resetPassword, setResetPassword] = useState("");
  const [resetConfirmPassword, setResetConfirmPassword] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [recoveryError, setRecoveryError] = useState("");
  const [resetBusy, setResetBusy] = useState(false);
  const [resetError, setResetError] = useState("");
  const [bootstrap, setBootstrap] = useState<AuthBootstrapStatus | null>(null);
  const title = useMemo(() => {
    if (mode === "login") return "Sign in";
    return "Create account";
  }, [mode]);
  const allowRegistration = bootstrap?.allow_registration ?? true;

  useEffect(() => {
    let cancelled = false;
    void api.bootstrapStatus()
      .then((next) => {
        if (cancelled) return;
        setBootstrap(next);
        if (!next.allow_registration) {
          setMode((current) => (current === "create" ? "login" : current));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBootstrap(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubmit() {
    setError("");
    setBusy(true);
    try {
      if (mode === "login") {
        await onLogin(username, password);
      } else {
        await onRegister(username, password, displayName || username, pin);
      }
    } catch (err) {
      setError(formatError(err, "Authentication failed"));
    } finally {
      setBusy(false);
    }
  }

  async function handleRecover() {
    setRecoveryError("");
    setRecoveryBusy(true);
    try {
      await onRecover(recoveryPhrase, recoveryPassword);
    } catch (err) {
      setRecoveryError(formatError(err, "Recovery failed"));
    } finally {
      setRecoveryBusy(false);
    }
  }

  async function handlePasswordReset() {
    setResetError("");
    setResetBusy(true);
    try {
      await onResetPassword(resetUsername, resetPin, resetPassword);
    } catch (err) {
      setResetError(formatError(err, "Password reset failed"));
    } finally {
      setResetBusy(false);
    }
  }

  return (
    <div className="hero-page">
      <div className="hero-card auth-card">
        <div className="auth-switch">
          <button className={`ghost-button ${mode === "login" ? "active-chip" : ""}`} onClick={() => setMode("login")}>
            Sign in
          </button>
          {allowRegistration ? (
            <button className={`ghost-button ${mode === "create" ? "active-chip" : ""}`} onClick={() => setMode("create")}>
              Create account
            </button>
          ) : null}
        </div>

        <h1>{title}</h1>
        {bootstrap?.admin_setup_required ? (
          <div className="auth-note">
            <strong>Admin setup is still pending.</strong>
            <small>
              Sign in as <code>{bootstrap.admin_username}</code> with the temporary password from the server logs, then finish the one-time admin setup flow.
            </small>
          </div>
        ) : null}

        <div className="auth-form">
          <input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Username" />
          {mode === "create" ? (
            <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Display name" />
          ) : null}
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                void handleSubmit();
              }
            }}
            placeholder="Password"
          />
          {mode === "create" ? (
            <>
              <input
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                placeholder="Confirm password"
              />
              <div className="auth-pin-field">
                <input
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  value={pin}
                  onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  placeholder="6-digit account PIN"
                />
                <button
                  type="button"
                  className="ghost-button info-tip auth-pin-tooltip"
                  data-tooltip="This PIN is permanent, cannot be changed later, and must be a 6-digit number you can remember."
                  aria-label="PIN help"
                >
                  ?
                </button>
              </div>
            </>
          ) : null}
          <button
            className="action-button"
            disabled={
              busy ||
              !password ||
              !username ||
              (mode === "create" && !displayName) ||
              (mode === "create" && password.length < 8) ||
              (mode === "create" && pin.length !== 6) ||
              (mode === "create" && confirmPassword !== password)
            }
            onClick={() => void handleSubmit()}
          >
            {busy ? "Working..." : title}
          </button>
          {mode === "create" && confirmPassword !== password && confirmPassword ? (
            <div className="error-inline">Passwords do not match.</div>
          ) : null}
          {mode === "create" && password && password.length < 8 ? (
            <div className="error-inline">Password must be at least 8 characters.</div>
          ) : null}
          {mode === "create" && pin && pin.length !== 6 ? (
            <div className="error-inline">PIN must be exactly 6 numeric digits.</div>
          ) : null}
          {error ? <div className="error-inline">{error}</div> : null}
        </div>

        <div className="auth-help">
          <button
            className={`ghost-button auth-help-toggle ${helpOpen ? "active-chip" : ""}`}
            type="button"
            onClick={() => setHelpOpen((current) => !current)}
          >
            <span>Need help?</span>
            <svg viewBox="0 0 16 16" className={`retention-history-caret ${helpOpen ? "is-open" : ""}`} aria-hidden="true">
              <path
                d="M4.25 6.25 8 10l3.75-3.75"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          {helpOpen ? (
            <div className="auth-help-panel">
              <div className="auth-help-copy">
                <strong>Reset account password</strong>
                <small>Use your permanent 6-digit account PIN to set a fresh password for your account.</small>
              </div>
              <div className="auth-form">
                <input
                  value={resetUsername}
                  onChange={(event) => setResetUsername(event.target.value)}
                  placeholder="Username"
                />
                <input
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  value={resetPin}
                  onChange={(event) => setResetPin(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  placeholder="6-digit account PIN"
                />
                <input
                  type="password"
                  value={resetPassword}
                  onChange={(event) => setResetPassword(event.target.value)}
                  placeholder="New password"
                />
                <input
                  type="password"
                  value={resetConfirmPassword}
                  onChange={(event) => setResetConfirmPassword(event.target.value)}
                  placeholder="Confirm new password"
                />
                <button
                  className="action-button"
                  disabled={
                    resetBusy ||
                    !resetUsername.trim() ||
                    resetPin.length !== 6 ||
                    resetPassword.length < 8 ||
                    resetConfirmPassword !== resetPassword
                  }
                  onClick={() => void handlePasswordReset()}
                >
                  {resetBusy ? "Working..." : "Reset password"}
                </button>
                {resetConfirmPassword && resetConfirmPassword !== resetPassword ? (
                  <div className="error-inline">Passwords do not match.</div>
                ) : null}
                {resetError ? <div className="error-inline">{resetError}</div> : null}
              </div>
              <div className="auth-help-copy">
                <strong>Recover admin account</strong>
                <small>
                  Use the saved six-word recovery phrase to set a fresh admin password for <code>{bootstrap?.admin_username ?? "admin"}</code>.
                </small>
              </div>
              <div className="auth-form">
                <input value={bootstrap?.admin_username ?? "admin"} disabled placeholder="Admin username" />
                <input
                  value={recoveryPhrase}
                  onChange={(event) => setRecoveryPhrase(event.target.value)}
                  placeholder="Recovery phrase"
                />
                <input
                  type="password"
                  value={recoveryPassword}
                  onChange={(event) => setRecoveryPassword(event.target.value)}
                  placeholder="New admin password"
                />
                <input
                  type="password"
                  value={recoveryConfirmPassword}
                  onChange={(event) => setRecoveryConfirmPassword(event.target.value)}
                  placeholder="Confirm new admin password"
                />
                <button
                  className="action-button"
                  disabled={
                    recoveryBusy ||
                    !recoveryPhrase.trim() ||
                    recoveryPassword.length < 8 ||
                    recoveryConfirmPassword !== recoveryPassword
                  }
                  onClick={() => void handleRecover()}
                >
                  {recoveryBusy ? "Working..." : "Recover admin account"}
                </button>
                {recoveryConfirmPassword && recoveryConfirmPassword !== recoveryPassword ? (
                  <div className="error-inline">Passwords do not match.</div>
                ) : null}
                {recoveryError ? <div className="error-inline">{recoveryError}</div> : null}
              </div>
            </div>
          ) : null}
        </div>

        {storedSessions.length ? (
          <div className="remembered-sessions">
            <h2>Existing accounts</h2>
            <div className="remembered-session-list">
              {storedSessions.map((session) => (
                <button
                  key={session.username}
                  className="remembered-session-row"
                  onClick={async () => {
                    setError("");
                    setBusy(true);
                    try {
                      await onSwitch(session.session_token);
                    } catch (err) {
                      setError(formatError(err, "Switch user failed"));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <span className="menu-account-avatar">
                    {session.avatar_url ? <img src={session.avatar_url} alt={session.display_name} /> : session.display_name.slice(0, 2).toUpperCase()}
                  </span>
                  <span className="menu-account-copy">
                    <strong>{session.display_name}</strong>
                    <small>@{session.username}</small>
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
