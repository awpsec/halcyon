import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  api,
  type JobStatusItem,
  type LiveOverview,
  type Preferences,
  type Profile,
  type SearchResults,
} from "../api/client";
import { formatCount, normalizeImportedText } from "../lib/format";
import {
  clearPlaybackContext,
  readPlaybackContext,
} from "../lib/playbackContext";

type Props = {
  profile: Profile | null;
  preferences: Preferences;
  onToggleTheme: () => void;
  onOpenMenu: (rect: DOMRect) => void;
};

type NavItem = {
  to: string;
  label: ReactNode;
  end?: boolean;
};

export type AppShellOutletContext = {
  liveOverview: LiveOverview | null;
};

const baseNavItems: NavItem[] = [
  { to: "/", label: "Home", end: true },
  { to: "/channels", label: "Subscriptions", end: true },
  { to: "/playlists", label: "Playlists" },
];
const LIVE_OVERVIEW_POLL_MS = 60_000;
const JOB_STATUS_POLL_MS = 10_000;
const MOBILE_SHELL_MAX_WIDTH = 980;

function matchesMobileUserAgent() {
  if (typeof navigator === "undefined") return false;
  const userAgentData = (
    navigator as Navigator & { userAgentData?: { mobile?: boolean } }
  ).userAgentData;
  if (typeof userAgentData?.mobile === "boolean") {
    return userAgentData.mobile;
  }
  return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
    navigator.userAgent,
  );
}

function ThemeIcon({ theme }: { theme: Preferences["theme"] }) {
  return theme === "dark" ? (
    <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
      <path
        d="M12 3a8.9 8.9 0 1 0 8.9 8.9A7.4 7.4 0 0 1 12 3Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
      <circle
        cx="12"
        cy="12"
        r="4.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" className="icon-button-svg search-icon-svg" aria-hidden="true">
      <circle
        cx="10.5"
        cy="10.5"
        r="5.9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="m14.7 14.7 4.8 4.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
      <path
        d="M4 7h16M4 12h16M4 17h16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function AppShell({
  profile,
  preferences,
  onToggleTheme,
  onOpenMenu,
}: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults>({
    videos: [],
    channels: [],
  });
  const [liveOverview, setLiveOverview] = useState<LiveOverview | null>(null);
  const [activeJobs, setActiveJobs] = useState<JobStatusItem[]>([]);
  const [jobsOpen, setJobsOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [isMobileShell, setIsMobileShell] = useState(false);
  const searchRef = useRef<HTMLDivElement | null>(null);
  const jobsRef = useRef<HTMLDivElement | null>(null);
  const isWatchPage = location.pathname.startsWith("/video/");
  const headerLogoSrc =
    preferences.theme === "dark"
      ? "/assets/branding/halcyon_ed_bw.png"
      : "/assets/branding/halcyon_ed_color.png";
  const navItems = useMemo(
    () => {
      const items = [
        baseNavItems[0],
        {
          to: profile ? `/profile/${profile.name}` : "/profile",
          label: "Profile",
        },
      ];
      if (liveOverview?.enabled ?? true) {
        items.push({
          to: "/live",
          label: "Live",
        });
      }
      items.push(...baseNavItems.slice(1));
      return items;
    },
    [liveOverview?.enabled, profile],
  );

  useEffect(() => {
    function updateMobileShell() {
      if (typeof window === "undefined") {
        setIsMobileShell(false);
        return;
      }
      const hasCompactWidth = window.innerWidth <= MOBILE_SHELL_MAX_WIDTH;
      setIsMobileShell(matchesMobileUserAgent() || hasCompactWidth);
    }

    updateMobileShell();
    window.addEventListener("resize", updateMobileShell);
    return () => window.removeEventListener("resize", updateMobileShell);
  }, []);

  useEffect(() => {
    const context = readPlaybackContext();
    if (!context || !context.queueApplied) return;
    if (location.pathname.startsWith("/video/")) return;
    void api
      .bulkQueue(context.savedQueueIds ?? [], true)
      .catch(() => undefined)
      .finally(() => clearPlaybackContext());
  }, [location.pathname]);

  useEffect(() => {
    if (!searchOpen || query.trim().length < 2) {
      setResults({ videos: [], channels: [] });
      return;
    }
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      void api.search(query).then((next) => {
        if (!cancelled) {
          setResults(next);
        }
      });
    }, 150);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [query, searchOpen]);

  useEffect(() => {
    let cancelled = false;

    async function loadLiveOverview() {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        return;
      }
      try {
        const next = await api.liveOverview();
        if (!cancelled) {
          setLiveOverview(next);
        }
      } catch {}
    }

    void loadLiveOverview();
    const interval = window.setInterval(() => {
      void loadLiveOverview();
    }, LIVE_OVERVIEW_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadJobs() {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        return;
      }
      try {
        const next = await api.jobsStatus();
        if (!cancelled) {
          setActiveJobs(next.items);
        }
      } catch {
        if (!cancelled) {
          setActiveJobs([]);
        }
      }
    }

    void loadJobs();
    const interval = window.setInterval(() => {
      void loadJobs();
    }, JOB_STATUS_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    setMobileNavOpen(false);
    setSearchOpen(false);
    setJobsOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (searchRef.current && !searchRef.current.contains(target)) {
        setSearchOpen(false);
        setQuery("");
      }
      if (jobsRef.current && !jobsRef.current.contains(target)) {
        setJobsOpen(false);
      }
    }

    function handleKeydown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setSearchOpen(false);
        setQuery("");
        setJobsOpen(false);
        setMobileNavOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeydown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeydown);
    };
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.body.classList.toggle(
      "mobile-nav-open",
      isMobileShell && mobileNavOpen,
    );
    return () => document.body.classList.remove("mobile-nav-open");
  }, [isMobileShell, mobileNavOpen]);

  useEffect(() => {
    if (!isMobileShell) {
      setMobileNavOpen(false);
    }
  }, [isMobileShell]);

  const jobProgress = useMemo(() => {
    const numeric = activeJobs
      .map((job) => job.percent)
      .filter((value): value is number => typeof value === "number");
    if (!numeric.length) return null;
    return Math.max(
      6,
      Math.round(
        numeric.reduce((sum, value) => sum + value, 0) / numeric.length,
      ),
    );
  }, [activeJobs]);

  const activeJobLabel = useMemo(() => {
    if (!activeJobs.length) return null;
    const first = activeJobs[0];
    const percent =
      typeof first.percent === "number" ? `${first.percent}%` : "working";
    return `${first.scope} ${percent}`;
  }, [activeJobs]);

  function openSearchPage(nextQuery: string) {
    const trimmed = nextQuery.trim();
    if (trimmed.length < 2) return;
    setSearchOpen(false);
    setQuery(trimmed);
    navigate(`/search?q=${encodeURIComponent(trimmed)}`);
  }

  function describeJob(job: JobStatusItem) {
    const percent =
      typeof job.percent === "number" ? `${job.percent}%` : "active";
    const details = job.details ?? {};
    if (job.scope === "transcode") {
      return `Transcoding ${details.title ? `· ${String(details.title)}` : ""}`;
    }
    if (job.scope === "library") {
      return `Indexing library · ${percent}`;
    }
    if (job.scope === "video") {
      return `Syncing video snapshot · ${percent}`;
    }
    if (job.scope === "channel") {
      return `Syncing channel snapshot · ${percent}`;
    }
    if (job.scope === "series") {
      return `Syncing series snapshot · ${percent}`;
    }
    if (details.warning) {
      return `${job.scope} · ${percent} · ${String(details.warning)}`;
    }
    return `${job.scope} · ${percent}`;
  }

  function jobSecondary(job: JobStatusItem) {
    const details = job.details ?? {};
    if (
      typeof details.processed === "number" &&
      typeof details.total === "number"
    ) {
      return `${details.processed}/${details.total}`;
    }
    if (details.warning) {
      return String(details.warning);
    }
    if (details.title) {
      return String(details.title);
    }
    if (details.output_path) {
      return `${details.profile ?? "hls-default"} · ${String(details.output_path).split(/[\\/]/).slice(-2).join("/")}`;
    }
    return activeJobLabel ?? "Working";
  }

  const searchControl = (
    <div className="header-search-control" ref={searchRef}>
      {isWatchPage && !isMobileShell ? (
        <div className="search-shell watch-search-shell">
          <input
            type="search"
            value={query}
            onFocus={() => setSearchOpen(true)}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                openSearchPage(query);
              }
            }}
            placeholder="Search videos and channels"
          />
        </div>
      ) : null}
      <button
        className={`icon-button search-toggle ${isWatchPage && !isMobileShell ? "watch-search-toggle" : ""} ${searchOpen ? "active-chip" : ""}`}
        onClick={() => {
          setSearchOpen((current) => !current);
          setJobsOpen(false);
          setMobileNavOpen(false);
        }}
        aria-label="Search library"
      >
        <SearchIcon />
      </button>
      {searchOpen ? (
        <div className={`header-search-popover search-popover-enter ${isMobileShell ? "mobile-search-popover" : ""}`}>
          <div className="search-shell header-search-shell">
            <input
              autoFocus
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  openSearchPage(query);
                }
              }}
              placeholder="Search videos and channels"
            />
          </div>
          {query.trim().length < 2 ? (
            <div className="search-results-empty">
              Type at least 2 characters.
            </div>
          ) : (
            <div className="search-results-panel">
              {results.channels.length ? (
                <div className="search-group">
                  <div className="menu-section-label">Channels</div>
                  {results.channels.slice(0, 4).map((channel) => (
                    <button
                      key={`channel-${channel.id}`}
                      className="search-result-row"
                      onClick={() => {
                        setSearchOpen(false);
                        setQuery("");
                        navigate(`/channels/${channel.slug}`);
                      }}
                    >
                      <span className="menu-account-avatar">
                        {channel.avatar_url ? (
                          <img
                            src={channel.avatar_url}
                            alt={channel.name}
                          />
                        ) : (
                          channel.name.slice(0, 2).toUpperCase()
                        )}
                      </span>
                      <span className="menu-account-copy">
                        <strong>
                          {normalizeImportedText(channel.name) ??
                            channel.name}
                        </strong>
                        <small>{`${formatCount(channel.video_count)} videos`}</small>
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
              {results.videos.length ? (
                <div className="search-group">
                  <div className="menu-section-label">Videos</div>
                  {results.videos.slice(0, 8).map((video) => (
                    <button
                      key={`video-${video.id}`}
                      className="search-result-row"
                      onClick={() => {
                        setSearchOpen(false);
                        setQuery("");
                        navigate(`/video/${video.watch_ref ?? video.id}`);
                      }}
                    >
                      <span className="search-result-thumb">
                        <img
                          src={
                            video.thumbnail_url ??
                            `/api/videos/${video.id}/thumbnail`
                          }
                          alt={
                            normalizeImportedText(video.title) ??
                            video.title
                          }
                        />
                      </span>
                      <span className="menu-account-copy">
                        <strong>
                          {normalizeImportedText(video.title) ??
                            video.title}
                        </strong>
                        <small>
                          {normalizeImportedText(video.channel_name) ??
                            video.channel_name ??
                            "Unknown channel"}
                        </small>
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
              {!results.channels.length && !results.videos.length ? (
                <div className="search-results-empty">
                  No matches for "{query}".
                </div>
              ) : null}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );

  return (
    <div className={`app-shell density-${preferences.density} ${isMobileShell ? "mobile-shell" : ""}`}>
      {isMobileShell && mobileNavOpen ? (
        <>
          <button
            className="mobile-nav-backdrop"
            type="button"
            aria-label="Close navigation menu"
            onClick={() => setMobileNavOpen(false)}
          />
          <aside className="mobile-nav-drawer" aria-label="Navigation menu">
            <div className="mobile-nav-header">
              <div className="mobile-nav-user">
                <strong>{profile?.display_name ?? "Select user"}</strong>
                <small>@{profile?.name ?? "guest"}</small>
              </div>
              <button
                className="icon-button mobile-nav-close"
                type="button"
                aria-label="Close navigation menu"
                onClick={() => setMobileNavOpen(false)}
              >
                <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
                  <path
                    d="m6 6 12 12M18 6 6 18"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
            <nav className="mobile-nav-links">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  className="mobile-nav-link"
                  to={item.to}
                  end={item.end}
                  onClick={() => setMobileNavOpen(false)}
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
            <div className="mobile-nav-actions">
              <button
                className="ghost-button mobile-drawer-button"
                type="button"
                onClick={() => {
                  setMobileNavOpen(false);
                  onToggleTheme();
                }}
              >
                <ThemeIcon theme={preferences.theme} />
                <span>{preferences.theme === "dark" ? "Light mode" : "Dark mode"}</span>
              </button>
              {activeJobs.length ? (
                <div className="mobile-job-panel">
                  <div className="menu-section-label">Server activity</div>
                  {activeJobs.map((job) => (
                    <div className="job-popover-row" key={`${job.scope}-${job.id}`}>
                      <strong>{describeJob(job)}</strong>
                      <small>{jobSecondary(job)}</small>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          </aside>
        </>
      ) : null}
      <header className={`top-shell ${isMobileShell ? "mobile-top-shell" : ""}`}>
        {isMobileShell ? (
          <div className="mobile-header-start">
            <button
              className="icon-button mobile-menu-toggle"
              type="button"
              aria-label="Open navigation menu"
              aria-expanded={mobileNavOpen}
              onClick={() => {
                setMobileNavOpen(true);
                setSearchOpen(false);
                setJobsOpen(false);
              }}
            >
              <MenuIcon />
            </button>
          </div>
        ) : (
          <Link
            className="brand compact-brand home-logo-link"
            to="/"
            onClick={() => {
              setSearchOpen(false);
            }}
          >
            <img
              className="brand-image"
              src={headerLogoSrc}
              alt="halcyon"
            />
          </Link>
        )}
        {isMobileShell ? (
          <Link
            className="brand compact-brand home-logo-link mobile-brand"
            to="/"
            onClick={() => {
              setSearchOpen(false);
              setMobileNavOpen(false);
            }}
          >
            <img
              className="brand-image"
              src={headerLogoSrc}
              alt="halcyon"
            />
          </Link>
        ) : null}
        <nav
          className={`top-nav ${isWatchPage ? "watch-top-nav" : ""} ${isMobileShell ? "mobile-top-nav" : ""}`}
        >
          {!isWatchPage && !isMobileShell
            ? navItems.map((item) => (
                <NavLink key={item.to} className="nav-link" to={item.to} end={item.end}>
                  {item.label}
                </NavLink>
              ))
            : null}
          {!isMobileShell ? searchControl : null}
        </nav>
        <div className={`header-actions ${isMobileShell ? "mobile-header-actions" : ""}`}>
          {isMobileShell ? searchControl : null}
          {activeJobs.length ? (
            <div className="header-job-status" ref={jobsRef}>
              <button
                className={`job-indicator ${jobProgress == null ? "is-busy" : ""}`}
                style={{
                  ["--job-progress" as string]: `${jobProgress ?? 24}%`,
                }}
                onClick={() => setJobsOpen((current) => !current)}
                onMouseEnter={() => {
                  if (!isMobileShell) setJobsOpen(true);
                }}
                onMouseLeave={() => {
                  if (!isMobileShell) setJobsOpen(false);
                }}
                aria-label={activeJobLabel ?? "Active jobs"}
              >
                <span className="job-ring" />
                <span className="job-indicator-copy">
                  <strong>{activeJobs.length}</strong>
                </span>
              </button>
              {jobsOpen ? (
                <div
                  className="job-popover"
                  onMouseEnter={() => {
                    if (!isMobileShell) setJobsOpen(true);
                  }}
                  onMouseLeave={() => {
                    if (!isMobileShell) setJobsOpen(false);
                  }}
                >
                  <div className="menu-section-label">Server activity</div>
                  {activeJobs.map((job) => (
                    <div
                      className="job-popover-row"
                      key={`${job.scope}-${job.id}`}
                    >
                      <strong>{describeJob(job)}</strong>
                      <small>{jobSecondary(job)}</small>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          {!isMobileShell ? (
            <button
              className="icon-button theme-toggle"
              onClick={onToggleTheme}
              aria-label="Toggle theme"
            >
              <ThemeIcon theme={preferences.theme} />
            </button>
          ) : null}
          <div className="profile-chip">
            <button
              className={`user-menu-trigger ${isMobileShell ? "mobile-user-menu-trigger" : ""}`}
              onClick={(event) =>
                onOpenMenu(event.currentTarget.getBoundingClientRect())
              }
            >
              <span className="header-avatar">
                {profile?.avatar_url ? (
                  <img src={profile.avatar_url} alt={profile.display_name} />
                ) : (
                  (profile?.display_name ?? "?").slice(0, 2).toUpperCase()
                )}
              </span>
              <span className="profile-chip-copy">
                <strong>{profile?.display_name ?? "Select user"}</strong>
                <small>@{profile?.name ?? "guest"}</small>
              </span>
              <svg
                viewBox="0 0 20 20"
                className="icon-button-svg"
                aria-hidden="true"
              >
                <path
                  d="m5 7 5 5 5-5"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                />
              </svg>
            </button>
          </div>
        </div>
      </header>
      <main className="content">
        <Outlet context={{ liveOverview } satisfies AppShellOutletContext} />
      </main>
    </div>
  );
}
