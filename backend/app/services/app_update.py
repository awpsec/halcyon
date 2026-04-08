from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.core.config import get_settings


RELEASE_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "halcyon-release.json"
UPDATE_CACHE_TTL_SECONDS = 600
UPDATE_COMMAND = "halcyon update"

_CACHE_LOCK = Lock()
_CACHE_VALUE: dict | None = None
_CACHE_AT = 0.0


def _normalize_parts(version: str) -> list[int]:
    parts: list[int] = []
    for chunk in version.replace("-", ".").split("."):
        if not chunk:
            continue
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits or "0"))
    return parts or [0]


def compare_versions(left: str, right: str) -> int:
    left_parts = _normalize_parts(left)
    right_parts = _normalize_parts(right)
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def read_local_release_manifest() -> dict:
    if not RELEASE_MANIFEST_PATH.exists():
        return {
            "name": "halcyon",
            "version": "0.0.0",
            "repository_url": get_settings().repository_url,
            "manifest_url": get_settings().update_manifest_url,
            "update_command": UPDATE_COMMAND,
        }
    return json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))


def current_release_version() -> str:
    return str(read_local_release_manifest().get("version") or "0.0.0")


def _checked_at_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_remote_release_manifest(force: bool = False) -> dict:
    global _CACHE_AT, _CACHE_VALUE
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE_VALUE is not None and now - _CACHE_AT < UPDATE_CACHE_TTL_SECONDS:
            return dict(_CACHE_VALUE)
    settings = get_settings()
    request = Request(
        settings.update_manifest_url,
        headers={"User-Agent": f"{settings.app_name}/{current_release_version()}"},
    )
    with urlopen(request, timeout=4) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    with _CACHE_LOCK:
        _CACHE_VALUE = dict(payload)
        _CACHE_AT = now
    return payload


def build_update_status(force: bool = False) -> dict:
    local_manifest = read_local_release_manifest()
    current_version = str(local_manifest.get("version") or "0.0.0")
    repository_url = str(local_manifest.get("repository_url") or get_settings().repository_url)
    update_command = str(local_manifest.get("update_command") or UPDATE_COMMAND)
    status = {
        "current_version": current_version,
        "latest_version": current_version,
        "update_available": False,
        "repository_url": repository_url,
        "update_command": update_command,
        "checked_at": _checked_at_iso(),
        "error": None,
    }
    try:
        remote_manifest = fetch_remote_release_manifest(force=force)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
        status["error"] = "Unable to reach the update server right now."
        return status

    latest_version = str(remote_manifest.get("version") or current_version)
    status["latest_version"] = latest_version
    status["repository_url"] = str(remote_manifest.get("repository_url") or repository_url)
    status["update_command"] = str(remote_manifest.get("update_command") or update_command)
    status["update_available"] = compare_versions(current_version, latest_version) < 0
    return status
