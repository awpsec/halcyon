from __future__ import annotations

from datetime import UTC, datetime


def server_now() -> datetime:
    return datetime.now().astimezone()


def server_timezone_name() -> str:
    current = server_now()
    timezone_info = current.tzinfo
    key = getattr(timezone_info, "key", None) or getattr(timezone_info, "zone", None)
    if isinstance(key, str) and key.strip():
        return key
    label = current.tzname()
    if isinstance(label, str) and label.strip():
        return label
    return "Server local time"


def localize_utc_to_server(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone()
