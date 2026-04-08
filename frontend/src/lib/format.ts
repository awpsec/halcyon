export function formatDuration(value: number) {
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function formatCount(value?: number | null) {
  if (value == null) return null;
  if (value >= 1_000_000_000)
    return `${(value / 1_000_000_000).toFixed(value >= 10_000_000_000 ? 0 : 1)}B`;
  if (value >= 1_000_000)
    return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000)
    return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}K`;
  return String(value);
}

export function formatRelativeDate(value?: string | null) {
  if (!value) return null;
  const date = parseApiDate(value);
  if (Number.isNaN(date.getTime())) return null;

  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const thresholds = [
    { unit: "year", seconds: 31_536_000 },
    { unit: "month", seconds: 2_592_000 },
    { unit: "week", seconds: 604_800 },
    { unit: "day", seconds: 86_400 },
    { unit: "hour", seconds: 3_600 },
    { unit: "minute", seconds: 60 },
  ] as const;

  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

  for (const threshold of thresholds) {
    if (Math.abs(diffSeconds) >= threshold.seconds) {
      return formatter.format(
        Math.round(diffSeconds / threshold.seconds),
        threshold.unit,
      );
    }
  }

  return "just now";
}

export function parseApiDate(value?: string | null) {
  if (!value) return new Date(NaN);
  const normalizedValue =
    /[zZ]$|[+-]\d{2}:\d{2}$/.test(value) || value.includes("GMT")
      ? value
      : `${value}Z`;
  return new Date(normalizedValue);
}

export function formatAbsoluteDateTime(value?: string | null) {
  const date = parseApiDate(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    month: "numeric",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

export function normalizeImportedText(value?: string | null) {
  if (!value) return value ?? null;

  return value
    .replace(/[｜│]/g, "|")
    .replace(/[？﹖]/g, "?")
    .replace(/\bf399\b/gi, "?")
    .replace(/\bf401\b/gi, "!")
    .replace(/\?\s+!/g, "?!")
    .replace(/!\s+\?/g, "!?")
    .replace(/([A-Za-z0-9)])_(?=\s)/g, "$1:")
    .replace(/\s*([,;])\s*/g, "$1 ")
    .replace(/\s*([!?]+)\s*/g, "$1 ")
    .replace(/\s+/g, " ")
    .trim();
}
