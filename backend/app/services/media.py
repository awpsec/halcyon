from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from app.services.binaries import resolve_binary
from app.services.utils import VIDEO_EXTENSIONS

SUBTITLE_EXTENSIONS = {".vtt", ".srt"}


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def fingerprint_file(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(path).encode("utf-8"))
    digest.update(str(stat.st_size).encode("utf-8"))
    digest.update(str(int(stat.st_mtime)).encode("utf-8"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 128))
    return digest.hexdigest()


def _subprocess_run_kwargs() -> dict:
    if os.name != "nt":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": creationflags,
        "startupinfo": startupinfo,
    }


def probe_media(path: Path) -> dict:
    try:
        cmd = [
            resolve_binary("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15, **_subprocess_run_kwargs())
        payload = json.loads(result.stdout)
    except Exception:
        payload = {"format": {}, "streams": []}

    duration = 0
    codec_summary = None
    resolution = None
    audio_codec = None
    container = None
    bitrate_kbps = None
    fps = None
    format_data = payload.get("format") or {}
    streams = payload.get("streams") or []

    if format_data.get("duration"):
        try:
            duration = int(float(format_data["duration"]))
        except (TypeError, ValueError):
            duration = 0
    if format_data.get("format_name"):
        container = format_data.get("format_name")
    if format_data.get("bit_rate"):
        try:
            bitrate_kbps = round(int(format_data["bit_rate"]) / 1000)
        except (TypeError, ValueError):
            bitrate_kbps = None

    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream:
        codec_summary = video_stream.get("codec_name")
        width = video_stream.get("width")
        height = video_stream.get("height")
        rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        if width and height:
            resolution = f"{width}x{height}"
        if rate and rate != "0/0":
            try:
                numerator, denominator = rate.split("/", 1)
                fps = round(int(numerator) / int(denominator), 2)
            except Exception:
                fps = None

    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if audio_stream:
        audio_codec = audio_stream.get("codec_name")

    stat = path.stat()
    return {
        "duration_seconds": duration,
        "codec_summary": codec_summary,
        "audio_codec": audio_codec,
        "container": container,
        "bitrate_kbps": bitrate_kbps,
        "fps": fps,
        "resolution": resolution,
        "modified_at": datetime.fromtimestamp(stat.st_mtime),
        "file_size": stat.st_size,
    }


def find_caption_tracks(path: Path) -> list[dict]:
    tracks: list[dict] = []
    stem = path.stem
    for candidate in sorted(path.parent.iterdir()):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if not candidate.stem.startswith(stem):
            continue
        suffix = candidate.stem[len(stem):].strip(".-_ ")
        normalized_suffix = suffix.casefold()
        if normalized_suffix in {"halcyon", "halcyon-ai", "ai", "ai-captions"}:
            label = "AI Captions"
        else:
            label = suffix if suffix else candidate.suffix.upper().replace(".", "")
        tracks.append(
            {
                "path": str(candidate),
                "format": candidate.suffix.lower().replace(".", ""),
                "label": label or "Captions",
            }
        )
    return tracks


def srt_to_vtt(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    converted = ["WEBVTT", ""]
    for line in lines:
        converted.append(line.replace(",", ".") if "-->" in line else line)
    return "\n".join(converted)


def generate_thumbnail(path: Path, cache_dir: Path, fingerprint: str) -> str | None:
    output_path = cache_dir / "thumbnails" / f"{fingerprint}.jpg"
    if output_path.exists() and output_path.stat().st_size > 0:
        return str(output_path)
    output_path.unlink(missing_ok=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg = resolve_binary("ffmpeg")
    except FileNotFoundError:
        return None

    duration = probe_media(path).get("duration_seconds") or 0
    seek_points = []
    if duration > 10:
        seek_points.extend(sorted({max(1, int(duration * ratio)) for ratio in (0.15, 0.35, 0.55, 0.75)}))
    seek_points.extend([1, 0])

    try:
        for seek_seconds in seek_points:
            cmd = [
                ffmpeg,
                "-y",
                "-ss",
                str(seek_seconds),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-vf",
                "scale=640:-1",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60, **_subprocess_run_kwargs())
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                return str(output_path)
    except Exception:
        return None
    output_path.unlink(missing_ok=True)
    return None


def generate_preview_clip(path: Path, cache_dir: Path, fingerprint: str, clip_seconds: int = 30) -> str | None:
    output_path = cache_dir / "previews" / f"{fingerprint}.mp4"
    if output_path.exists() and output_path.stat().st_size > 0:
        return str(output_path)
    output_path.unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        ffmpeg = resolve_binary("ffmpeg")
    except FileNotFoundError:
        return None

    duration = probe_media(path).get("duration_seconds") or 0
    start_seconds = max(0, min(12, duration // 8 if duration else 0))

    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_seconds),
        "-i",
        str(path),
        "-t",
        str(max(8, min(clip_seconds, duration or clip_seconds))),
        "-vf",
        "scale=640:-2,fps=24",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180, **_subprocess_run_kwargs())
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return str(output_path)
    except Exception:
        pass
    output_path.unlink(missing_ok=True)
    return None


def download_thumbnail(url: str, cache_dir: Path, fingerprint: str, *, force_replace: bool = False) -> str | None:
    output_path = cache_dir / "thumbnails" / f"{fingerprint}.jpg"
    if not force_replace and output_path.exists() and output_path.stat().st_size > 0:
        return str(output_path)
    if force_replace:
        output_path.unlink(missing_ok=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        output_path.write_bytes(response.content)
    except Exception:
        output_path.unlink(missing_ok=True)
        return None
    return str(output_path)


def placeholder_thumbnail_data_url(title: str, subtitle: str | None = None) -> str:
    safe_title = title[:48]
    safe_subtitle = (subtitle or "").strip()[:40]
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
      <rect width="640" height="360" fill="#141a22"/>
      <rect x="18" y="18" width="604" height="324" fill="none" stroke="#7ea6d6" stroke-opacity="0.35"/>
      <path d="M0 72h640M0 180h640M0 288h640" stroke="#7ea6d6" stroke-opacity="0.08"/>
      <path d="M160 0v360M320 0v360M480 0v360" stroke="#7ea6d6" stroke-opacity="0.08"/>
      <text x="28" y="270" fill="#eef2f5" font-size="28" font-family="Segoe UI, Arial, sans-serif">{safe_title}</text>
      <text x="28" y="305" fill="#9eb3c8" font-size="18" font-family="Segoe UI, Arial, sans-serif">{safe_subtitle}</text>
    </svg>
    """.strip()
    return f"data:image/svg+xml;utf8,{quote(svg)}"


def placeholder_thumbnail_svg(title: str, subtitle: str | None = None) -> str:
    safe_title = title[:48]
    safe_subtitle = (subtitle or "").strip()[:40]
    return f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
      <rect width="640" height="360" fill="#141a22"/>
      <rect x="18" y="18" width="604" height="324" fill="none" stroke="#7ea6d6" stroke-opacity="0.35"/>
      <path d="M0 72h640M0 180h640M0 288h640" stroke="#7ea6d6" stroke-opacity="0.08"/>
      <path d="M160 0v360M320 0v360M480 0v360" stroke="#7ea6d6" stroke-opacity="0.08"/>
      <text x="28" y="270" fill="#eef2f5" font-size="28" font-family="Segoe UI, Arial, sans-serif">{safe_title}</text>
      <text x="28" y="305" fill="#9eb3c8" font-size="18" font-family="Segoe UI, Arial, sans-serif">{safe_subtitle}</text>
    </svg>
    """.strip()
