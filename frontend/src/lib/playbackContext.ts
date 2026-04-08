export type PlaybackContext = {
  kind: "series" | "playlist";
  id: number;
  title: string;
  videoIds: number[];
  activeVideoId?: number;
  savedQueueIds?: number[];
  queueApplied?: boolean;
};

const KEY = "halcyon.playback-context";
const LEGACY_KEY = "waytube.playback-context";

export function readPlaybackContext(): PlaybackContext | null {
  const raw = sessionStorage.getItem(KEY) ?? sessionStorage.getItem(LEGACY_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as PlaybackContext;
  } catch {
    return null;
  }
}

export function writePlaybackContext(context: PlaybackContext) {
  sessionStorage.setItem(KEY, JSON.stringify(context));
}

export function clearPlaybackContext() {
  sessionStorage.removeItem(KEY);
}
