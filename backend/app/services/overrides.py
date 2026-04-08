from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import MetadataOverride, Video


def apply_video_override(db: Session | None, video: Video) -> Video:
    if db is None:
        return video
    override = db.scalar(
        select(MetadataOverride).where(MetadataOverride.target_type == "video", MetadataOverride.target_id == video.id)
    )
    if not override:
        return video
    payload = override.payload or {}
    if payload.get("title"):
        video.title = payload["title"]
    if "description" in payload:
        video.description = payload["description"]
    return video
