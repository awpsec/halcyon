import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { api, type Preferences, type Profile, type SessionResponse } from "./api/client";
import { AppShell } from "./components/AppShell";
import { ToastHost } from "./components/ToastHost";
import { AdminSetupPage } from "./pages/AdminSetupPage";
import { AuthPage } from "./pages/AuthPage";
import { ChannelsPage } from "./pages/ChannelsPage";
import { ChannelDetailPage } from "./pages/ChannelDetailPage";
import { HomePage } from "./pages/HomePage";
import { LibraryPage } from "./pages/LibraryPage";
import { PlaylistsPage } from "./pages/PlaylistsPage";
import { PlaylistDetailPage } from "./pages/PlaylistDetailPage";
import { ProfilePage } from "./pages/ProfilePage";
import { SeriesPage } from "./pages/SeriesPage";
import { SeriesDetailPage } from "./pages/SeriesDetailPage";
import { SearchPage } from "./pages/SearchPage";
import { SavedVideosPage } from "./pages/SavedVideosPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SyncReviewPage } from "./pages/SyncReviewPage";
import { VideoPage } from "./pages/VideoPage";

type StoredSession = {
  username: string;
  display_name: string;
  session_token: string;
  avatar_url?: string | null;
};

const SESSIONS_KEY = "halcyon.sessions";
const LEGACY_SESSIONS_KEY = "waytube.sessions";
const PREFERENCES_KEY = "halcyon.preferences";
const LEGACY_PREFERENCES_KEY = "waytube.preferences";

function readJsonStorage<T>(primaryKey: string, legacyKey?: string): T | null {
  const raw =
    localStorage.getItem(primaryKey) ??
    (legacyKey ? localStorage.getItem(legacyKey) : null);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function rememberSession(session: SessionResponse) {
  const current = readJsonStorage<StoredSession[]>(
    SESSIONS_KEY,
    LEGACY_SESSIONS_KEY,
  ) ?? [];
  const filtered = current.filter((item) => item.username !== session.user.name);
  filtered.unshift({
    username: session.user.name,
    display_name: session.user.display_name,
    session_token: session.session_token,
    avatar_url: session.user.avatar_url
  });
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(filtered.slice(0, 8)));
  return filtered.slice(0, 8);
}

function discardSession(sessionToken: string) {
  const current = readJsonStorage<StoredSession[]>(
    SESSIONS_KEY,
    LEGACY_SESSIONS_KEY,
  ) ?? [];
  const filtered = current.filter((item) => item.session_token !== sessionToken);
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(filtered));
  return filtered;
}

function discardSessionsForUsername(username: string) {
  const current = readJsonStorage<StoredSession[]>(
    SESSIONS_KEY,
    LEGACY_SESSIONS_KEY,
  ) ?? [];
  const filtered = current.filter((item) => item.username !== username);
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(filtered));
  return filtered;
}

function readStoredSessions() {
  return (
    readJsonStorage<StoredSession[]>(SESSIONS_KEY, LEGACY_SESSIONS_KEY) ?? []
  );
}

function updateStoredSessionProfile(profile: Profile) {
  const current = readStoredSessions();
  const next = current.map((item) =>
    item.username === profile.name
      ? { ...item, display_name: profile.display_name, avatar_url: profile.avatar_url }
      : item
  );
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(next));
  return next;
}

function AppRoutes() {
  const navigate = useNavigate();
  const location = useLocation();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{ top: number; right: number }>({ top: 68, right: 24 });
  const [authReady, setAuthReady] = useState(false);
  const [storedSessions, setStoredSessions] = useState<StoredSession[]>(() => readStoredSessions());
  const [preferences, setPreferences] = useState<Preferences>(() => {
    const defaults: Preferences = {
      theme: "dark",
      autoplay: true,
      preferMpv: false,
      mousewheelVolumeControl: true,
      density: "comfortable",
      defaultPlayerMode: "last-used",
    };
    const stored = readJsonStorage<Partial<Preferences>>(
      PREFERENCES_KEY,
      LEGACY_PREFERENCES_KEY,
    );
    if (stored) {
      return {
        ...defaults,
        ...stored,
      };
    }
    return defaults;
  });
  const menuRef = useRef<HTMLDivElement | null>(null);
  const ownProfilePath = profile ? `/profile/${profile.name}` : "/profile";

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const rememberedSessions = readStoredSessions();
        try {
          const active = await api.me();
          if (cancelled) return;
          setProfile(active);
        } catch {
          const remembered = rememberedSessions[0];
          if (remembered?.session_token) {
            try {
              const restored = await api.switchSession(remembered.session_token);
              if (cancelled) return;
              setStoredSessions(rememberSession(restored));
              setProfile(restored.user);
            } catch {
              if (cancelled) return;
              setStoredSessions(discardSession(remembered.session_token));
              setProfile(null);
            }
          } else {
            setProfile(null);
          }
        }
      } catch {
        if (!cancelled) {
          setProfile(null);
        }
      } finally {
        if (!cancelled) {
          setAuthReady(true);
        }
      }
    }

    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = preferences.theme;
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences));
  }, [preferences]);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [location.pathname]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }

    if (menuOpen) {
      document.addEventListener("mousedown", handlePointerDown);
      return () => document.removeEventListener("mousedown", handlePointerDown);
    }

    return undefined;
  }, [menuOpen]);

  async function adoptSession(session: SessionResponse) {
    setStoredSessions(rememberSession(session));
    setProfile(session.user);
    setMenuOpen(false);
    navigate("/");
  }

  async function handleLogin(username: string, password: string) {
    const session = await api.login({ username, password });
    await adoptSession(session);
  }

  async function handleRegister(username: string, password: string, displayName: string, pin: string) {
    const session = await api.register({ username, password, pin, display_name: displayName });
    await adoptSession(session);
  }

  async function handleAdminRecovery(recoveryPhrase: string, password: string) {
    const session = await api.recoverAdminAccount({ recovery_phrase: recoveryPhrase, password });
    await adoptSession(session);
  }

  async function handlePasswordReset(username: string, pin: string, password: string) {
    const session = await api.resetPasswordByPin({ username, pin, password });
    await adoptSession(session);
  }

  async function handleSwitch(sessionToken: string) {
    const session = await api.switchSession(sessionToken);
    await adoptSession(session);
  }

  async function handleLogout() {
    await api.logout();
    if (profile) {
      setStoredSessions(discardSessionsForUsername(profile.name));
    }
    setProfile(null);
    setMenuOpen(false);
    navigate("/auth");
  }

  function handleProfileChange(nextProfile: Profile) {
    setProfile(nextProfile);
    setStoredSessions(updateStoredSessionProfile(nextProfile));
  }

  function handleAdminSetupComplete(nextProfile: Profile) {
    handleProfileChange(nextProfile);
    navigate("/");
  }

  return (
    <>
      <ToastHost />
      {menuOpen ? (
        <div className="user-menu-popover" ref={menuRef} style={{ top: menuPosition.top, right: menuPosition.right }}>
          {profile ? (
            <button
              className="menu-profile-summary menu-profile-summary-button"
              onClick={() => {
                setMenuOpen(false);
                navigate(`/profile/${profile.name}`);
              }}
            >
              <div className="menu-profile-avatar">
                {profile.avatar_url ? <img src={profile.avatar_url} alt={profile.display_name} /> : <span>{profile.display_name.slice(0, 2).toUpperCase()}</span>}
              </div>
              <div className="menu-profile-copy">
                <strong>{profile.display_name}</strong>
                <small>@{profile.name}</small>
              </div>
            </button>
          ) : null}
          <button className="menu-item" onClick={() => { setMenuOpen(false); navigate("/settings"); }}>
            Settings
          </button>
          <div className="menu-section-label">Switch account</div>
          {storedSessions.map((session) => (
            <button
              key={session.session_token}
              className="menu-item account-menu-item"
              onClick={() => void handleSwitch(session.session_token)}
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
          <button className="menu-item" onClick={() => { setMenuOpen(false); navigate("/auth"); }}>
            Add account
          </button>
          <button className="menu-item" onClick={() => void handleLogout()}>
            Log out
          </button>
        </div>
      ) : null}
      <Routes>
        <Route
          path="/auth"
          element={
            <AuthPage
              storedSessions={storedSessions}
              onLogin={handleLogin}
              onRegister={handleRegister}
              onResetPassword={handlePasswordReset}
              onRecover={handleAdminRecovery}
              onSwitch={handleSwitch}
            />
          }
        />
        <Route
          element={
            !authReady ? (
              <div className="hero-page"><div className="hero-card">Loading…</div></div>
            ) : profile?.requires_admin_setup ? (
              <AdminSetupPage profile={profile} onComplete={handleAdminSetupComplete} />
            ) : profile ? (
              <AppShell
                profile={profile}
                preferences={preferences}
                onToggleTheme={() => setPreferences((current) => ({ ...current, theme: current.theme === "dark" ? "light" : "dark" }))}
                onOpenMenu={(rect) => {
                  setMenuPosition({
                    top: Math.round(rect.bottom + 10),
                    right: Math.max(18, Math.round(window.innerWidth - rect.right)),
                  });
                  setMenuOpen((current) => !current);
                }}
              />
            ) : (
              <Navigate to="/auth" replace />
            )
          }
        >
          <Route path="/" element={<HomePage preferences={preferences} profile={profile} />} />
          <Route path="/profile" element={<Navigate to={ownProfilePath} replace />} />
          <Route path="/profile/:username" element={<ProfilePage currentProfile={profile} />} />
          <Route path="/profile/:username/saved" element={<SavedVideosPage profile={profile} />} />
          <Route path="/library" element={<LibraryPage profile={profile} />} />
          <Route path="/channels" element={<ChannelsPage profile={profile} />} />
          <Route path="/channels/:channelRef" element={<ChannelDetailPage profile={profile} />} />
          <Route path="/series" element={<SeriesPage />} />
          <Route path="/series/:seriesId" element={<SeriesDetailPage profile={profile} />} />
          <Route path="/playlists" element={<PlaylistsPage profile={profile} />} />
          <Route path="/playlists/:playlistId" element={<PlaylistDetailPage profile={profile} />} />
          <Route path="/search" element={<SearchPage profile={profile} />} />
          <Route
            path="/settings"
            element={<SettingsPage profile={profile} preferences={preferences} onPreferencesChange={setPreferences} onProfileChange={handleProfileChange} />}
          />
          <Route path="/sync-review" element={profile?.is_admin ? <SyncReviewPage /> : <Navigate to="/" replace />} />
          <Route path="/video/:videoId" element={<VideoPage profile={profile} preferences={preferences} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
