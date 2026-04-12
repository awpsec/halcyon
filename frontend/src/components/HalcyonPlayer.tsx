import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import Hls from "hls.js";
import Plyr from "plyr";
import "plyr/dist/plyr.css";

type CaptionTrack = {
  id: number;
  label: string;
  url: string;
};

type ChapterMarker = {
  startSeconds: number;
  label: string;
};

export function HalcyonPlayer({
  source,
  autoplay,
  captions,
  chapters = [],
  mousewheelVolumeControl = true,
  aspectRatio = "16 / 9",
  mode = "default",
  onDimensionsChange,
  onReady,
  onPause,
  onEnded,
  onLoadingChange,
  onFatalError,
}: {
  source: string;
  autoplay: boolean;
  captions: CaptionTrack[];
  chapters?: ChapterMarker[];
  mousewheelVolumeControl?: boolean;
  aspectRatio?: string;
  mode?: "default" | "theater";
  onDimensionsChange?: (aspectRatio: string) => void;
  onReady?: (video: HTMLVideoElement, player: Plyr) => void;
  onPause?: (video: HTMLVideoElement) => void;
  onEnded?: (video: HTMLVideoElement) => void;
  onLoadingChange?: (loading: boolean) => void;
  onFatalError?: (message: string) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const playerRef = useRef<Plyr | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const [durationSeconds, setDurationSeconds] = useState<number | null>(null);
  const [chapterHost, setChapterHost] = useState<HTMLElement | null>(null);
  const [currentTimeSeconds, setCurrentTimeSeconds] = useState(0);
  const onReadyRef = useRef(onReady);
  const onPauseRef = useRef(onPause);
  const onEndedRef = useRef(onEnded);
  const onLoadingRef = useRef(onLoadingChange);
  const onFatalErrorRef = useRef(onFatalError);

  useEffect(() => {
    onReadyRef.current = onReady;
    onPauseRef.current = onPause;
    onEndedRef.current = onEnded;
    onLoadingRef.current = onLoadingChange;
    onFatalErrorRef.current = onFatalError;
  }, [onEnded, onFatalError, onLoadingChange, onPause, onReady]);

  useEffect(() => {
    const node = videoRef.current;
    if (!node) return;
    setDurationSeconds(null);
    setChapterHost(null);
    const plyrRatio = aspectRatio.replace("/", ":").replace(/\s+/g, "");

    if (source.endsWith(".m3u8")) {
      if (Hls.isSupported()) {
        const hls = new Hls({
          maxBufferLength: 180,
          backBufferLength: 180,
          maxMaxBufferLength: 180,
          lowLatencyMode: false,
          liveDurationInfinity: false,
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (!data.fatal) return;
          onLoadingRef.current?.(false);
          onFatalErrorRef.current?.("This stream was terminated. Refresh to resume.");
        });
        hls.loadSource(source);
        hls.attachMedia(node);
        hlsRef.current = hls;
      } else {
        node.src = source;
      }
    } else {
      node.src = source;
    }

    const player = new Plyr(node, {
      autoplay,
      seekTime: 5,
      ratio: plyrRatio,
      captions: { active: captions.length > 0, language: "auto", update: true },
      controls: [
        "play-large",
        "restart",
        "rewind",
        "play",
        "fast-forward",
        "progress",
        "current-time",
        "duration",
        "mute",
        "volume",
        "captions",
        "settings",
        "pip",
        "fullscreen",
      ],
      fullscreen: {
        enabled: true,
        iosNative: true,
      },
      settings: ["captions", "speed"],
    });
    playerRef.current = player;
    onReadyRef.current?.(node, player);

    const pauseHandler = () => onPauseRef.current?.(node);
    const endedHandler = () => onEndedRef.current?.(node);
    const loadStartHandler = () => onLoadingRef.current?.(true);
    const waitingHandler = () => onLoadingRef.current?.(true);
    const canPlayHandler = () => onLoadingRef.current?.(false);
    const playingHandler = () => onLoadingRef.current?.(false);
    const metadataHandler = () => {
      if (node.videoWidth > 0 && node.videoHeight > 0) {
        onDimensionsChange?.(`${node.videoWidth} / ${node.videoHeight}`);
      }
      if (Number.isFinite(node.duration) && node.duration > 0) {
        setDurationSeconds(node.duration);
      }
      setCurrentTimeSeconds(node.currentTime || 0);
    };
    const timeUpdateHandler = () => {
      setCurrentTimeSeconds(node.currentTime || 0);
    };
    const errorHandler = () => {
      onLoadingRef.current?.(false);
      onFatalErrorRef.current?.("This stream was terminated. Refresh to resume.");
    };
    node.addEventListener("pause", pauseHandler);
    node.addEventListener("ended", endedHandler);
    node.addEventListener("loadstart", loadStartHandler);
    node.addEventListener("waiting", waitingHandler);
    node.addEventListener("canplay", canPlayHandler);
    node.addEventListener("playing", playingHandler);
    node.addEventListener("loadedmetadata", metadataHandler);
    node.addEventListener("timeupdate", timeUpdateHandler);
    node.addEventListener("seeked", timeUpdateHandler);
    node.addEventListener("error", errorHandler);

    return () => {
      node.removeEventListener("pause", pauseHandler);
      node.removeEventListener("ended", endedHandler);
      node.removeEventListener("loadstart", loadStartHandler);
      node.removeEventListener("waiting", waitingHandler);
      node.removeEventListener("canplay", canPlayHandler);
      node.removeEventListener("playing", playingHandler);
      node.removeEventListener("loadedmetadata", metadataHandler);
      node.removeEventListener("timeupdate", timeUpdateHandler);
      node.removeEventListener("seeked", timeUpdateHandler);
      node.removeEventListener("error", errorHandler);
      player.destroy();
      playerRef.current = null;
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
      node.removeAttribute("src");
      node.load();
    };
  }, [aspectRatio, autoplay, captions.length, source]);

  const shellStyle = useMemo(
    () =>
      ({
        aspectRatio,
        width: "100%",
        maxWidth: "100%",
        marginInline: "0",
      }) as CSSProperties,
    [aspectRatio],
  );

  const chapterMarkers = useMemo(() => {
    if (!durationSeconds || durationSeconds <= 0 || chapters.length < 2) {
      return [];
    }
    return chapters
      .filter(
        (chapter) =>
          chapter.startSeconds > 0 && chapter.startSeconds < durationSeconds,
      )
      .map((chapter) => ({
        ...chapter,
        leftPercent: (chapter.startSeconds / durationSeconds) * 100,
      }));
  }, [chapters, durationSeconds]);

  const currentChapterLabel = useMemo(() => {
    if (!chapters.length) return null;
    const sorted = [...chapters].sort((a, b) => a.startSeconds - b.startSeconds);
    let active: ChapterMarker | null = null;
    for (const chapter of sorted) {
      if (chapter.startSeconds <= currentTimeSeconds + 0.05) {
        active = chapter;
      } else {
        break;
      }
    }
    return active?.label ?? null;
  }, [chapters, currentTimeSeconds]);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return undefined;

    function updateChapterHost() {
      const root = shellRef.current;
      if (!root) {
        setChapterHost(null);
        return;
      }
      const nextHost = root.querySelector(".plyr__progress");
      setChapterHost(nextHost instanceof HTMLElement ? nextHost : null);
    }

    updateChapterHost();
    const mutationObserver = new MutationObserver(() => updateChapterHost());
    mutationObserver.observe(shell, { childList: true, subtree: true });
    const resizeObserver =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => updateChapterHost())
        : null;
    resizeObserver?.observe(shell);
    window.addEventListener("resize", updateChapterHost);
    return () => {
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
      window.removeEventListener("resize", updateChapterHost);
    };
  }, [source]);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell || !mousewheelVolumeControl) return;

    function handleWheel(event: WheelEvent) {
      const node = videoRef.current;
      if (!node) return;
      event.preventDefault();
      const direction = event.deltaY < 0 ? 1 : -1;
      const nextVolume = Math.max(0, Math.min(1, node.volume + direction * 0.05));
      node.muted = false;
      node.volume = Number(nextVolume.toFixed(2));
    }

    shell.addEventListener("wheel", handleWheel, { passive: false });
    return () => shell.removeEventListener("wheel", handleWheel);
  }, [mousewheelVolumeControl]);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;

    function handleKeyDown(event: KeyboardEvent) {
      const node = videoRef.current;
      if (!node) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        node.currentTime = Math.max(0, node.currentTime - 5);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        const duration = Number.isFinite(node.duration) ? node.duration : Number.MAX_SAFE_INTEGER;
        node.currentTime = Math.min(duration, node.currentTime + 5);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        node.muted = false;
        node.volume = Number(Math.min(1, node.volume + 0.05).toFixed(2));
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        node.muted = false;
        node.volume = Number(Math.max(0, node.volume - 0.05).toFixed(2));
      }
    }

    shell.addEventListener("keydown", handleKeyDown);
    return () => shell.removeEventListener("keydown", handleKeyDown);
  }, []);

  return (
    <div
      ref={shellRef}
      className={`halcyon-player-shell ${mode === "theater" ? "is-theater" : "is-default"}`}
      style={shellStyle}
      tabIndex={0}
    >
      <video ref={videoRef} playsInline crossOrigin="anonymous" preload="auto">
        {captions.map((track, index) => (
          <track key={track.id} src={track.url} label={track.label} kind="subtitles" default={index === 0} />
        ))}
      </video>
      {currentChapterLabel ? (
        <div className="halcyon-player-current-chapter" aria-live="polite">
          <span>{currentChapterLabel}</span>
        </div>
      ) : null}
      {chapterMarkers.length && chapterHost
        ? createPortal(
            <div className="halcyon-player-chapters" aria-label="Video chapters">
              {chapterMarkers.map((chapter) => (
                <button
                  key={`${chapter.startSeconds}-${chapter.label}`}
                  type="button"
                  className="halcyon-player-chapter-marker"
                  style={{ left: `${chapter.leftPercent}%` }}
                  aria-label={`Jump to ${chapter.label}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    const node = videoRef.current;
                    if (!node) return;
                    node.currentTime = chapter.startSeconds;
                  }}
                >
                  <span className="halcyon-player-chapter-tooltip">{chapter.label}</span>
                </button>
              ))}
            </div>,
            chapterHost,
          )
        : null}
    </div>
  );
}
