import { createPortal } from "react-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { formatCount, normalizeImportedText } from "../lib/format";

export type CollectionCardMenuItem = {
  label: string;
  disabled?: boolean;
  onSelect: () => void | Promise<void>;
};

export function CollectionCard({
  title,
  subtitle,
  badge,
  thumbnailUrl,
  stackedThumbnails = [],
  to,
  meta,
  menuItems,
}: {
  title: string;
  subtitle?: string | null;
  badge: string;
  thumbnailUrl?: string | null;
  stackedThumbnails?: string[];
  to: string;
  meta?: string | null;
  menuItems?: CollectionCardMenuItem[];
}) {
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const previewStack = stackedThumbnails.filter(Boolean).slice(1, 3);
  const displayTitle = normalizeImportedText(title) ?? title;
  const displaySubtitle = normalizeImportedText(subtitle) ?? subtitle;
  const menuStyle = useMemo(() => {
    if (!menuAnchor || typeof window === "undefined") return null;
    const width = 196;
    return {
      top: `${Math.min(menuAnchor.bottom + 8, window.innerHeight - 140)}px`,
      left: `${Math.min(menuAnchor.right - width, window.innerWidth - width - 12)}px`,
    };
  }, [menuAnchor]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    function handlePointerDown(event: MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (menuRef.current?.contains(target)) return;
      if (target.closest(".kebab-button")) return;
      setMenuOpen(false);
      setMenuAnchor(null);
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        setMenuAnchor(null);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  return (
    <article
      className="video-tile collection-tile"
      onClick={() => navigate(to)}
    >
      <div className="tile-thumb media-thumb collection-thumb">
        {previewStack.length ? (
          <div className="collection-thumb-stack" aria-hidden="true">
            {previewStack.map((url, index) => (
              <span
                className={`collection-thumb-stack-item stack-${index + 1}`}
                key={`${url}-${index}`}
              >
                <img src={url} alt="" />
              </span>
            ))}
          </div>
        ) : null}
        {thumbnailUrl ? <img src={thumbnailUrl} alt={displayTitle} /> : null}
        <span className="collection-badge">{badge}</span>
        {menuItems?.length ? (
          <button
            className="kebab-button collection-kebab-button"
            aria-label={`${displayTitle} actions`}
            onClick={(event) => {
              event.stopPropagation();
              const rect = event.currentTarget.getBoundingClientRect();
              setMenuAnchor(rect);
              setMenuOpen((current) => !current);
            }}
            type="button"
          >
            <svg viewBox="0 0 24 24" className="icon-button-svg" aria-hidden="true">
              <circle cx="12" cy="5" r="2.15" fill="currentColor" />
              <circle cx="12" cy="12" r="2.15" fill="currentColor" />
              <circle cx="12" cy="19" r="2.15" fill="currentColor" />
            </svg>
          </button>
        ) : null}
      </div>
      <div className="tile-body">
        <div className="tile-copy">
          <strong>{displayTitle}</strong>
          {displaySubtitle ? <span>{displaySubtitle}</span> : null}
          {meta ? <small>{meta}</small> : null}
        </div>
      </div>
      {menuOpen && menuItems?.length && menuStyle
        ? createPortal(
            <div
              ref={menuRef}
              className="card-menu collection-card-menu"
              style={menuStyle}
              onClick={(event) => event.stopPropagation()}
            >
              {menuItems.map((item) => (
                <button
                  key={item.label}
                  className="menu-item"
                  disabled={item.disabled}
                  onClick={async () => {
                    await item.onSelect();
                    setMenuOpen(false);
                    setMenuAnchor(null);
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </article>
  );
}

export function collectionMeta(count?: number | null) {
  if (count == null) return null;
  return `${formatCount(count)} videos`;
}
