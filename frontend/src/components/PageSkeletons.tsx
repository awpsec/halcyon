function SkeletonCard({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`skeleton-card ${compact ? "compact" : ""}`}>
      <div className="skeleton-thumb skeleton-shimmer" />
      <div className="skeleton-card-body">
        <div className="skeleton-avatar skeleton-shimmer" />
        <div className="skeleton-line-stack">
          <div className="skeleton-line skeleton-shimmer w-80" />
          <div className="skeleton-line skeleton-shimmer w-45" />
          <div className="skeleton-line skeleton-shimmer w-60" />
        </div>
      </div>
    </div>
  );
}

export function HomePageSkeleton() {
  return (
    <div className="page-stack skeleton-page">
      <div className="skeleton-chip-row">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="skeleton-chip skeleton-shimmer" />
        ))}
      </div>
      <div className="skeleton-section">
        <div className="skeleton-line skeleton-shimmer w-28" />
        <div className="video-grid-layout">
          {Array.from({ length: 8 }).map((_, index) => (
            <SkeletonCard key={index} />
          ))}
        </div>
      </div>
      <div className="skeleton-section">
        <div className="skeleton-line skeleton-shimmer w-22" />
        <div className="rail-scroll skeleton-rail">
          {Array.from({ length: 5 }).map((_, index) => (
            <SkeletonCard key={index} compact />
          ))}
        </div>
      </div>
    </div>
  );
}

export function ProfilePageSkeleton() {
  return (
    <div className="page-stack skeleton-page">
      <section className="profile-skeleton-header">
        <div className="skeleton-avatar large skeleton-shimmer" />
        <div className="skeleton-line-stack">
          <div className="skeleton-line skeleton-shimmer w-30" />
          <div className="skeleton-line skeleton-shimmer w-18" />
          <div className="skeleton-line skeleton-shimmer w-42" />
        </div>
      </section>
      <div className="skeleton-section">
        <div className="skeleton-line skeleton-shimmer w-24" />
        <div className="profile-avatar-grid">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="skeleton-avatar subscription skeleton-shimmer" />
          ))}
        </div>
      </div>
      <div className="skeleton-section">
        <div className="skeleton-line skeleton-shimmer w-26" />
        <div className="rail-scroll skeleton-rail">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonCard key={index} compact />
          ))}
        </div>
      </div>
    </div>
  );
}

export function ChannelPageSkeleton() {
  return (
    <div className="page-stack skeleton-page">
      <div className="channel-skeleton-banner skeleton-shimmer" />
      <div className="channel-skeleton-header">
        <div className="skeleton-avatar xl skeleton-shimmer" />
        <div className="skeleton-line-stack">
          <div className="skeleton-line skeleton-shimmer w-34" />
          <div className="skeleton-line skeleton-shimmer w-26" />
          <div className="skeleton-line skeleton-shimmer w-60" />
          <div className="skeleton-line skeleton-shimmer w-48" />
        </div>
      </div>
      <div className="video-grid-layout">
        {Array.from({ length: 8 }).map((_, index) => (
          <SkeletonCard key={index} />
        ))}
      </div>
    </div>
  );
}

export function CollectionPageSkeleton({ titleWidth = "w-28" }: { titleWidth?: string }) {
  return (
    <div className="page-stack skeleton-page">
      <div className="skeleton-line skeleton-shimmer w-14" />
      <div className={`skeleton-line skeleton-shimmer ${titleWidth}`} />
      <div className="video-grid-layout">
        {Array.from({ length: 8 }).map((_, index) => (
          <SkeletonCard key={index} />
        ))}
      </div>
    </div>
  );
}

export function SettingsPageSkeleton() {
  return (
    <div className="page-stack skeleton-page">
      <div className="skeleton-chip-row">
        {Array.from({ length: 3 }).map((_, index) => (
          <div key={index} className="skeleton-chip skeleton-shimmer" />
        ))}
      </div>
      <div className="skeleton-settings-section">
        <div className="skeleton-line skeleton-shimmer w-22" />
        {Array.from({ length: 4 }).map((_, index) => (
          <div key={index} className="skeleton-settings-row">
            <div className="skeleton-line-stack">
              <div className="skeleton-line skeleton-shimmer w-26" />
              <div className="skeleton-line skeleton-shimmer w-42" />
            </div>
            <div className="skeleton-switch skeleton-shimmer" />
          </div>
        ))}
      </div>
      <div className="skeleton-settings-section">
        <div className="skeleton-line skeleton-shimmer w-18" />
        {Array.from({ length: 3 }).map((_, index) => (
          <div key={index} className="skeleton-settings-row">
            <div className="skeleton-line skeleton-shimmer w-60" />
          </div>
        ))}
      </div>
    </div>
  );
}
