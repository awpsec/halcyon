from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata
from datetime import datetime
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"}


def normalize_text(value: str) -> str:
    lowered = unicodedata.normalize("NFKC", value).lower().replace("&", " and ")
    lowered = (
        lowered.replace("—", " ")
        .replace("–", " ")
        .replace("―", " ")
        .replace(":", " ")
        .replace("：", " ")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
    )
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_generic_channel_name(value: str | None) -> bool:
    normalized = normalize_text(value or "")
    return normalized in {"", "unknown channel", "offline library"}


def resolve_display_name(local_name: str | None, synced_name: str | None) -> str | None:
    if not synced_name:
        return local_name
    if not local_name or is_generic_channel_name(local_name):
        return synced_name
    local_norm = normalize_text(local_name)
    synced_norm = normalize_text(synced_name)
    if local_norm == synced_norm:
        return synced_name
    shorter = min(len(local_norm), len(synced_norm))
    longer = max(len(local_norm), len(synced_norm))
    if shorter and shorter / longer >= 0.8 and (local_norm in synced_norm or synced_norm in local_norm):
        return synced_name
    prefix_len = 0
    for left, right in zip(local_norm, synced_norm):
        if left != right:
            break
        prefix_len += 1
    if shorter >= 5 and prefix_len >= max(4, int(shorter * 0.7)):
        return synced_name
    local_tokens = set(tokenize_text(local_name))
    synced_tokens = set(tokenize_text(synced_name))
    if local_tokens and synced_tokens and len(local_tokens & synced_tokens) >= max(1, min(len(local_tokens), len(synced_tokens)) - 1):
        return synced_name
    if local_norm and synced_norm and local_norm[:3] == synced_norm[:3]:
        if SequenceMatcher(None, local_norm, synced_norm).ratio() >= 0.62:
            return synced_name
    return local_name


def tokenize_text(value: str) -> list[str]:
    return [token for token in normalize_text(value).split(" ") if token]


def canonicalize_search_text(value: str) -> str:
    working = unicodedata.normalize("NFKC", value)
    replacements = {
        "—": "-",
        "–": "-",
        "―": "-",
        "｜": "|",
        "│": "|",
        "：": ":",
        "’": "'",
        "“": '"',
        "”": '"',
    }
    for old, new in replacements.items():
        working = working.replace(old, new)
    return " ".join(working.split()).strip()


def clean_display_title(value: str) -> str:
    working = canonicalize_search_text(value)
    working = re.sub(r"\bf399\b", "?", working, flags=re.IGNORECASE)
    working = re.sub(r"\bf401\b", "!", working, flags=re.IGNORECASE)
    working = re.sub(r"\?\s+!", "?!", working)
    working = re.sub(r"!\s+\?", "!?", working)
    working = re.sub(r"\s*([:：])\s*", r"\1 ", working)
    working = re.sub(r"\s*([,;])\s*", r"\1 ", working)
    working = re.sub(r"\s*([!?]+)\s*", lambda match: f"{match.group(1)} ", working)
    working = re.sub(r"\s+", " ", working).strip()
    return working


def tokens_match_query(haystack: str, query: str) -> bool:
    normalized_haystack = normalize_text(haystack)
    haystack_tokens = tokenize_text(normalized_haystack)
    query_tokens = tokenize_text(query)
    if not query_tokens:
        return True
    for token in query_tokens:
        if token not in normalized_haystack and not any(candidate.startswith(token) for candidate in haystack_tokens):
            return False
    return True


def slugify(value: str) -> str:
    # Apostrophes should collapse words together, not create extra dashes.
    collapsed = re.sub(r"[\'’`]+", "", value.strip().lower())
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", collapsed).strip("-")
    return normalized or "item"


def parse_episode_number(name: str) -> int | None:
    patterns = [
        r"(?:episode|ep|part|pt)\s*[_\- ]*(\d+)",
        r"^(\d{1,3})[\s._-]",
        r"[\s._-](\d{1,3})$",
    ]
    lowered = name.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return int(match.group(1))
    return None


PLAYLIST_FOLDER_MARKERS = {
    "arc",
    "archive",
    "episodes",
    "playlist",
    "playlists",
    "season",
    "seasons",
    "series",
    "videos",
    "vod",
    "vods",
}


def clean_path_label(value: str) -> str:
    return clean_display_title(value.replace("_", " ").replace(".", " ").strip())


def infer_series_name(value: str) -> str | None:
    cleaned = clean_path_label(value)
    if not cleaned:
        return None

    series_match = re.search(
        r"^(?P<base>.+?)(?:\s*[-|]\s*|\s+)(?:series|season)\s*(?P<number>\d+)\b",
        canonicalize_search_text(cleaned),
        flags=re.IGNORECASE,
    )
    if series_match:
        base = clean_path_label(series_match.group("base"))
        return f"{base} - Series {int(series_match.group('number'))}" if base else None

    short_match = re.search(r"^(?P<base>.+?)\s*[-|]\s*s(?P<number>\d+)\b", cleaned, flags=re.IGNORECASE)
    if short_match:
        base = clean_path_label(short_match.group("base"))
        return f"{base} - Series {int(short_match.group('number'))}" if base else None

    return None


def split_title_parts(path: Path) -> tuple[str, str | None]:
    ellipsis_token = "HALCYONELLIPSISMARKER"
    stem = re.sub(r"\.{3,}", ellipsis_token, path.stem)
    stem = clean_display_title(stem.replace("_", " ").replace(".", " ").strip()).replace(ellipsis_token, "...")
    parts = [part for part in path.parts if part not in (path.anchor,)]
    channel = parts[-3] if len(parts) >= 3 else (parts[-2] if len(parts) >= 2 else "Unknown Channel")
    series = clean_path_label(parts[-2]) if len(parts) >= 2 else infer_series_name(stem)
    if series and series.lower() == channel.lower():
        series = None
    return stem, series


def infer_folder_hints(path: Path) -> tuple[str, str, str | None]:
    title, nested_series = split_title_parts(path)
    parts = [part for part in path.parts if part not in (path.anchor,)]
    folder_parts = list(parts[:-1])
    if not folder_parts:
        return title, "Unknown Channel", infer_series_name(title)
    if len(folder_parts) >= 2:
        return title, folder_parts[0], nested_series

    folder_name = clean_path_label(folder_parts[0])
    folder_tokens = set(tokenize_text(folder_name))
    title_tokens = set(tokenize_text(title))
    overlap = len(folder_tokens & title_tokens) / max(1, len(folder_tokens)) if folder_tokens else 0.0
    playlistish = bool(folder_tokens & PLAYLIST_FOLDER_MARKERS)
    episodic = parse_episode_number(title) is not None
    looks_like_series = playlistish or overlap >= 0.2 or (episodic and len(folder_tokens) >= 2)
    if looks_like_series:
        return title, "Unknown Channel", infer_series_name(folder_name) or folder_name
    title_series = infer_series_name(title)
    if title_series:
        return title, "Unknown Channel", title_series
    return title, folder_name, None


def infer_published_at(path: Path) -> datetime | None:
    match = re.search(r"(20\d{2})[-_ ]?(\d{2})[-_ ]?(\d{2})", path.stem)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
