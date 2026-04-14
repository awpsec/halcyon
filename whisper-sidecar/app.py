from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from faster_whisper import WhisperModel
from pydantic import BaseModel

app = FastAPI(title="halcyon-whisper", version="1.0.0")

_model_lock = Lock()
_model: WhisperModel | None = None


class TranscriptionRequest(BaseModel):
    source_path: str
    output_path: str
    force: bool = False
    language: str | None = None


def _timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{milliseconds:03}"


def _render_vtt(segments: list[tuple[float, float, str]]) -> str:
    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(segments, start=1):
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            continue
        lines.extend(
            [
                str(index),
                f"{_timestamp(start)} --> {_timestamp(end)}",
                cleaned,
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _whisper_model() -> WhisperModel:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            _model = WhisperModel(
                os.getenv("WHISPER_MODEL", "base"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
                download_root=os.getenv("WHISPER_MODEL_DIR", "/models"),
            )
    return _model


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model": os.getenv("WHISPER_MODEL", "base"),
        "device": os.getenv("WHISPER_DEVICE", "cpu"),
    }


@app.post("/transcriptions")
def transcribe(payload: TranscriptionRequest) -> dict:
    source_path = Path(payload.source_path)
    output_path = Path(payload.output_path)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=404, detail="Source media file was not found")
    if output_path.exists() and output_path.stat().st_size > 0 and not payload.force:
        return {
            "ok": True,
            "cached": True,
            "output_path": str(output_path),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    beam_size = max(1, int(os.getenv("WHISPER_BEAM_SIZE", "1")))
    model = _whisper_model()
    try:
        segment_iter, info = model.transcribe(
            str(source_path),
            language=payload.language,
            beam_size=beam_size,
            best_of=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segments = [(segment.start, segment.end, segment.text) for segment in segment_iter]
    except Exception as exc:  # pragma: no cover - runtime dependency behavior
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc

    output_path.write_text(_render_vtt(segments), encoding="utf-8")
    return {
        "ok": True,
        "cached": False,
        "output_path": str(output_path),
        "language": getattr(info, "language", None),
        "duration_seconds": getattr(info, "duration", None),
        "segments": len(segments),
    }
