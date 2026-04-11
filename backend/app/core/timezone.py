from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import get_settings


def normalize_timezone_name(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return None
    return candidate


def _configured_timezone_name() -> str | None:
    settings = get_settings()
    return normalize_timezone_name(settings.server_timezone) or normalize_timezone_name(os.getenv("TZ"))


def timezone_for_name(value: str | None):
    normalized = normalize_timezone_name(value)
    if normalized:
        return ZoneInfo(normalized)
    return None


def server_timezone():
    configured = timezone_for_name(_configured_timezone_name())
    if configured is not None:
        return configured
    current = datetime.now().astimezone()
    if current.tzinfo is not None:
        return current.tzinfo
    return UTC


def coerce_datetime_to_timezone(value: datetime, timezone_name: str | None) -> datetime:
    timezone = timezone_for_name(timezone_name) or server_timezone()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def server_now() -> datetime:
    return datetime.now(UTC).astimezone(server_timezone())


def server_timezone_name() -> str:
    configured = _configured_timezone_name()
    if configured:
        return configured
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
    return normalized.astimezone(server_timezone())


def localize_utc_to_timezone(value: datetime | None, timezone_name: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(timezone_for_name(timezone_name) or server_timezone())
