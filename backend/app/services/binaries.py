from __future__ import annotations

import shutil
from pathlib import Path

from app.core.config import get_settings


def resolve_binary(name: str) -> str:
    direct = shutil.which(name)
    if direct:
        return direct

    settings = get_settings()
    if settings.ffmpeg_bin_dir:
        for candidate in (settings.ffmpeg_bin_dir / f"{name}.exe", settings.ffmpeg_bin_dir / name):
            if candidate.exists():
                return str(candidate)

    search_roots = [Path.cwd(), *Path.cwd().parents[:3]]
    for root in search_roots:
        for candidate in (
            root / ".tools" / "ffmpeg" / "bin" / f"{name}.exe",
            root / ".tools" / "ffmpeg" / f"{name}.exe",
            root / ".tools" / "ffmpeg" / "bin" / name,
            root / ".tools" / "ffmpeg" / name,
        ):
            if candidate.exists():
                return str(candidate)

    raise FileNotFoundError(f"{name} binary not found")
