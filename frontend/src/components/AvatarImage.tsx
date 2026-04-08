import { useMemo, useState } from "react";

const AVATAR_THEMES = [
  ["#13324c", "#7ec8ff", "#cfe9ff"],
  ["#2b1839", "#f08cd8", "#ffe6fb"],
  ["#203b24", "#88e0a2", "#e5fff0"],
  ["#44221c", "#ff9b7d", "#ffe8e0"],
  ["#243048", "#9fb6ff", "#edf2ff"],
  ["#3d2d16", "#f0c36a", "#fff4d7"],
  ["#112f32", "#78d7dc", "#ddfeff"],
  ["#391f27", "#f2a3a9", "#ffe9ec"],
  ["#1f2639", "#8ca0d7", "#e7eeff"],
  ["#2d2421", "#d8b8a5", "#fff0e5"],
];

function hashSeed(seed: string) {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function buildAvatarDataUri(seed: string, fallbackText: string) {
  const hash = hashSeed(seed || fallbackText || "halcyon");
  const [base, accent, text] = AVATAR_THEMES[hash % AVATAR_THEMES.length];
  const shape = hash % 5;
  const initials = fallbackText.slice(0, 2).toUpperCase();

  const ornament =
    shape === 0
      ? `<circle cx="18" cy="18" r="8" fill="${accent}" opacity="0.82" /><circle cx="46" cy="46" r="12" fill="${accent}" opacity="0.38" />`
      : shape === 1
        ? `<path d="M10 14h44v12H10z" fill="${accent}" opacity="0.72" /><path d="M18 38h28v10H18z" fill="${accent}" opacity="0.32" />`
        : shape === 2
          ? `<path d="M12 46 32 10l20 36Z" fill="${accent}" opacity="0.68" /><circle cx="48" cy="18" r="7" fill="${accent}" opacity="0.32" />`
          : shape === 3
            ? `<circle cx="18" cy="18" r="10" fill="${accent}" opacity="0.72" /><circle cx="46" cy="18" r="6" fill="${accent}" opacity="0.5" /><circle cx="32" cy="46" r="11" fill="${accent}" opacity="0.34" />`
            : `<path d="M12 16c8 0 12-6 20-6s12 6 20 6v32H12Z" fill="${accent}" opacity="0.62" /><path d="M12 42c8 0 12-6 20-6s12 6 20 6" fill="none" stroke="${accent}" stroke-width="3" stroke-linecap="round" opacity="0.55" />`;

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="${initials}"><rect width="64" height="64" rx="32" fill="${base}" />${ornament}<text x="32" y="38" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="21" font-weight="700" fill="${text}">${initials}</text></svg>`;
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

export function AvatarImage({
  src,
  seed,
  alt,
  fallbackText,
  className,
}: {
  src?: string | null;
  seed?: string;
  alt: string;
  fallbackText: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const fallbackSrc = useMemo(() => buildAvatarDataUri(seed ?? fallbackText, fallbackText), [seed, fallbackText]);
  const resolvedSrc = src && !failed ? src : fallbackSrc;

  return <img className={className} src={resolvedSrc} alt={alt} onError={() => setFailed(true)} />;
}
