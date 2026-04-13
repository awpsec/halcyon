from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import TranscodeJob, Video
from app.services.binaries import resolve_binary
from app.services.media import probe_media


DIRECT_PLAY_VIDEO_CODECS = {"h264", "av1", "vp9"}
DIRECT_PLAY_AUDIO_CODECS = {"aac", "opus", "vorbis", "mp3"}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".webm", ".m4v"}
WEBM_VIDEO_CODECS = {"vp9", "av1"}
WEBM_AUDIO_CODECS = {"opus", "vorbis"}
MOBILE_USER_AGENT_PATTERN = re.compile(
    r"Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini",
    re.IGNORECASE,
)
logger = get_logger()


def _subprocess_popen_kwargs() -> dict:
    if os.name != "nt":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": creationflags,
        "startupinfo": startupinfo,
    }


def _normalize_codec(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().lower()
    normalized = normalized.split("(", 1)[0].strip()
    normalized = normalized.split(",", 1)[0].strip()
    normalized = normalized.split(" ", 1)[0].strip()
    return normalized


def playback_client_profile(headers: Mapping[str, str] | None = None) -> str:
    if not headers:
        return "default"
    sec_ch_mobile = (headers.get("sec-ch-ua-mobile") or "").strip()
    if sec_ch_mobile == "?1":
        return "mobile"
    user_agent = headers.get("user-agent") or ""
    if MOBILE_USER_AGENT_PATTERN.search(user_agent):
        return "mobile"
    return "default"


def _can_direct_play(
    *,
    suffix: str,
    video_codec: str,
    audio_codec: str,
    client_profile: str,
) -> bool:
    if client_profile == "mobile":
        return (
            suffix in {".mp4", ".m4v"}
            and video_codec == "h264"
            and (not audio_codec or audio_codec in {"aac", "mp3"})
        )
    return (
        suffix in DIRECT_PLAY_EXTENSIONS
        and (not video_codec or video_codec in DIRECT_PLAY_VIDEO_CODECS)
        and (not audio_codec or audio_codec in DIRECT_PLAY_AUDIO_CODECS)
    )


def resolve_playback(video: Video, client_profile: str = "default") -> dict:
    primary_file = video.files[0] if video.files else None
    if not primary_file:
        return {
            "direct_play": False,
            "requires_transcode": False,
            "stream_url": None,
            "source_path": None,
            "transcode_profile": None,
            "source_missing": True,
        }

    source_path = Path(primary_file.absolute_path)
    if not source_path.exists():
        return {
            "direct_play": False,
            "requires_transcode": False,
            "stream_url": None,
            "source_path": primary_file.absolute_path,
            "transcode_profile": None,
            "source_missing": True,
        }

    media_info = probe_media(source_path)
    suffix = source_path.suffix.lower()
    video_codec = _normalize_codec(media_info.get("codec_summary") or primary_file.codec_summary)
    audio_codec = _normalize_codec(media_info.get("audio_codec"))
    direct_play = _can_direct_play(
        suffix=suffix,
        video_codec=video_codec,
        audio_codec=audio_codec,
        client_profile=client_profile,
    )

    processing_profile = None
    stream_url = f"/api/videos/{video.id}/stream"
    if not direct_play:
        if client_profile == "mobile":
            if video_codec == "h264":
                processing_profile = (
                    "remux-mp4-copy"
                    if audio_codec in {"", "aac", "mp3"}
                    else "remux-mp4-aac"
                )
                stream_url = f"/api/videos/{video.id}/compatible"
            else:
                processing_profile = "hls-default"
                stream_url = f"/api/videos/{video.id}/hls/index.m3u8"
        elif video_codec in WEBM_VIDEO_CODECS and audio_codec in WEBM_AUDIO_CODECS:
            processing_profile = "remux-webm"
            stream_url = f"/api/videos/{video.id}/compatible"
        elif video_codec == "h264":
            processing_profile = "remux-mp4-copy" if audio_codec in {"aac", "mp3"} else "remux-mp4-aac"
            stream_url = f"/api/videos/{video.id}/compatible"
        else:
            processing_profile = "hls-default"
            stream_url = f"/api/videos/{video.id}/hls/index.m3u8"
    return {
        "direct_play": direct_play,
        "requires_transcode": not direct_play,
        "stream_url": stream_url,
        "source_path": primary_file.absolute_path,
        "transcode_profile": processing_profile,
        "source_missing": False,
    }


def transcode_output_path(cache_dir: Path, fingerprint: str, profile: str = "hls-default") -> Path:
    return cache_dir / "transcodes" / fingerprint / profile / "index.m3u8"


def compatible_output_path(cache_dir: Path, fingerprint: str, profile: str) -> Path:
    extension = ".webm" if profile == "remux-webm" else ".mp4"
    return cache_dir / "processed" / fingerprint / profile / f"stream{extension}"


def completion_marker_path(output_path: Path) -> Path:
    return output_path.with_suffix(f"{output_path.suffix}.complete")


def is_process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except BaseException:
        return False
    return True


def _ensure_job(db: Session, video: Video, fingerprint: str, profile: str) -> TranscodeJob:
    job = db.scalar(select(TranscodeJob).where(TranscodeJob.fingerprint == fingerprint, TranscodeJob.profile == profile))
    if not job:
        job = TranscodeJob(video_id=video.id, fingerprint=fingerprint, profile=profile)
        db.add(job)
        db.commit()
        db.refresh(job)
    return job


def ensure_hls_transcode(db: Session, video: Video, cache_dir: Path, profile: str = "hls-default") -> Path:
    primary_file = video.files[0] if video.files else None
    if not primary_file:
        raise RuntimeError("Video has no source file")

    output_path = transcode_output_path(cache_dir, primary_file.fingerprint, profile)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path = completion_marker_path(output_path)

    job = _ensure_job(db, video, primary_file.fingerprint, profile)
    if output_path.exists() and marker_path.exists():
        if job and job.status == "running":
            job.status = "completed"
            job.pid = None
            db.commit()
        return output_path

    if job.status == "running" and is_process_running(job.pid):
        return output_path

    if output_path.parent.exists():
        shutil.rmtree(output_path.parent, ignore_errors=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    job.status = "running"
    job.output_path = str(output_path)
    job.pid = None
    db.commit()
    logger.info("Transcode started video_id=%s profile=%s output=%s", video.id, profile, output_path)

    cmd = [
        resolve_binary("ffmpeg"),
        "-y",
        "-i",
        primary_file.absolute_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-f",
        "hls",
        "-hls_time",
        "6",
        "-start_number",
        "0",
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_flags",
        "independent_segments+append_list",
        str(output_path),
    ]

    try:
        log_path = output_path.parent / "ffmpeg.log"
        with log_path.open("a", encoding="utf-8", errors="ignore") as handle:
            process = subprocess.Popen(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                **_subprocess_popen_kwargs(),
            )
        job.pid = process.pid
        db.commit()
    except Exception as exc:
        job.status = "failed"
        job.pid = None
        db.commit()
        logger.exception("Transcode failed video_id=%s profile=%s", video.id, profile)
        raise RuntimeError("ffmpeg transcoding failed") from exc

    return output_path


def ensure_compatible_stream(db: Session, video: Video, cache_dir: Path, profile: str) -> Path:
    primary_file = video.files[0] if video.files else None
    if not primary_file:
        raise RuntimeError("Video has no source file")

    output_path = compatible_output_path(cache_dir, primary_file.fingerprint, profile)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path = completion_marker_path(output_path)
    job = _ensure_job(db, video, primary_file.fingerprint, profile)
    if output_path.exists() and output_path.stat().st_size > 0 and marker_path.exists():
        job.status = "completed"
        job.output_path = str(output_path)
        job.pid = None
        db.commit()
        return output_path

    if job.status == "running" and is_process_running(job.pid):
        if wait_for_output_ready(output_path, marker_path, timeout_seconds=90):
            return output_path
        raise RuntimeError("Compatible stream is still being prepared")

    output_path.unlink(missing_ok=True)
    marker_path.unlink(missing_ok=True)

    job.status = "running"
    job.output_path = str(output_path)
    job.pid = None
    db.commit()

    if profile == "remux-webm":
        cmd = [
            resolve_binary("ffmpeg"),
            "-y",
            "-i",
            primary_file.absolute_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            str(output_path),
        ]
    elif profile == "remux-mp4-copy":
        cmd = [
            resolve_binary("ffmpeg"),
            "-y",
            "-i",
            primary_file.absolute_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    else:
        cmd = [
            resolve_binary("ffmpeg"),
            "-y",
            "-i",
            primary_file.absolute_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    try:
        logger.info("Compatible stream start video_id=%s profile=%s output=%s", video.id, profile, output_path)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_subprocess_popen_kwargs(),
        )
        job.pid = process.pid
        db.commit()
        stdout, stderr = process.communicate(timeout=900)
        if process.returncode != 0:
            raise RuntimeError(stderr or stdout or "ffmpeg exited with a non-zero status")
    except Exception as exc:
        job.status = "failed"
        job.pid = None
        db.commit()
        output_path.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        logger.exception("Compatible stream failed video_id=%s profile=%s", video.id, profile)
        raise RuntimeError("ffmpeg processing failed") from exc

    marker_path.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
    job.status = "completed"
    job.pid = None
    job.output_path = str(output_path)
    db.commit()
    logger.info("Compatible stream completed video_id=%s profile=%s output=%s", video.id, profile, output_path)
    return output_path


def stop_transcode_job(db: Session, job: TranscodeJob) -> bool:
    stopped = False
    if job.pid:
        try:
            os.kill(job.pid, signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False
    if job.output_path:
        output_dir = Path(job.output_path).parent
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        completion_marker_path(Path(job.output_path)).unlink(missing_ok=True)
    job.status = "failed"
    job.pid = None
    db.commit()
    return stopped


def reconcile_transcode_job(db: Session, job: TranscodeJob) -> TranscodeJob:
    output_exists = bool(job.output_path and Path(job.output_path).exists())
    marker_exists = bool(job.output_path and completion_marker_path(Path(job.output_path)).exists())
    pid_running = is_process_running(job.pid)
    if job.status == "running":
        if output_exists and marker_exists and not pid_running:
            job.status = "completed"
            job.pid = None
            db.commit()
        elif not pid_running:
            job.status = "failed"
            job.pid = None
            if job.output_path:
                Path(job.output_path).unlink(missing_ok=True)
                completion_marker_path(Path(job.output_path)).unlink(missing_ok=True)
            db.commit()
    return job


def transcode_is_throttled(job: TranscodeJob, now: datetime | None = None) -> bool:
    if job.status != "running":
        return False
    current_time = now or datetime.utcnow()
    age_seconds = max(0.0, (current_time - job.created_at).total_seconds())
    if age_seconds < 30:
        return False
    if not job.output_path:
        return True
    output_path = Path(job.output_path)
    target_path = output_path.parent if output_path.suffix == ".m3u8" else output_path
    if not target_path.exists():
        return True
    try:
        last_change = max(path.stat().st_mtime for path in ([target_path] if target_path.is_file() else target_path.glob("*")))
    except ValueError:
        return True
    except OSError:
        return True
    return (time.time() - last_change) > 25


def wait_for_transcode_playlist(output_path: Path, timeout_seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if output_path.exists():
            try:
                contents = output_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                contents = ""
            if "#EXTINF" in contents:
                return True
        time.sleep(0.25)
    if not output_path.exists():
        return False
    try:
        return "#EXTINF" in output_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def wait_for_output_ready(output_path: Path, marker_path: Path, timeout_seconds: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if output_path.exists() and output_path.stat().st_size > 0 and marker_path.exists():
            return True
        time.sleep(0.35)
    return output_path.exists() and output_path.stat().st_size > 0 and marker_path.exists()
