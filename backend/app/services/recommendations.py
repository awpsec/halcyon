"""Content-based + affinity ranking for watch-page suggestions.

The previous behaviour ordered suggestions by `same_channel DESC` first, which
made the "Suggested" rail little more than "more videos from this channel".
This module blends several signals so suggestions surface *other creators
covering the same topic* and things the viewer is likely to enjoy, while a
diversity pass keeps any single channel from dominating the list.

Pure function of DB state -> deterministic, so pagination stays stable.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Subscription,
    Video,
    WatchProgress,
    YouTubeVideoSnapshot,
)

# --- Signal weights (tuned so topic + variety dominate, channel is a nudge) ---
W_TAG_OVERLAP = 3.2          # per shared tag (strongest topical signal)
W_TITLE_OVERLAP = 1.8        # per shared meaningful title/description token
W_SAME_CATEGORY = 1.6        # same YouTube category
W_SAME_SERIES = 6.0          # part of the same series
W_SAME_CHANNEL = 0.7         # small "more from this creator" nudge only
W_SUBSCRIBED = 2.2           # viewer is subscribed to the candidate's channel
W_WATCHED_CHANNEL = 1.0      # viewer has watched this channel before
W_POPULARITY = 1.6           # log-scaled view count
W_RECENCY = 1.0              # newer is slightly preferred
W_JITTER = 0.4               # tiny stable shuffle for discovery / tiebreak

TAG_OVERLAP_CAP = 5
TITLE_OVERLAP_CAP = 8
CANDIDATE_POOL_LIMIT = 400   # bound per-request scoring cost
CHANNEL_REPEAT_PENALTY = 2.6  # demotes the 2nd, 3rd, ... video from a channel

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
        "with", "is", "are", "was", "were", "be", "this", "that", "these",
        "those", "it", "its", "as", "at", "by", "from", "how", "why", "what",
        "when", "where", "who", "you", "your", "my", "i", "we", "they", "he",
        "she", "do", "does", "did", "new", "vs", "ep", "part", "official",
        "video", "full", "hd", "ft", "feat", "vlog", "episode",
    }
)


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    }


def _normalize_tags(tags) -> set[str]:
    if not tags:
        return set()
    out: set[str] = set()
    for tag in tags:
        if not tag:
            continue
        out.add(str(tag).strip().lower())
    return out


def _stable_jitter(seed_id: int, candidate_id: int) -> float:
    # Deterministic pseudo-random in [0, 1) without Math.random/time.
    mixed = (seed_id * 1_000_003 + candidate_id * 2_654_435_761) & 0xFFFFFFFF
    return ((mixed ^ (mixed >> 13)) & 0xFFFF) / float(0x10000)


def _snapshot_map(db: Session, videos: list[Video]) -> dict[int, YouTubeVideoSnapshot]:
    youtube_ids: dict[str, int] = {}
    for video in videos:
        match = video.youtube_match
        if match and match.youtube_video_id:
            youtube_ids[match.youtube_video_id] = video.id
    if not youtube_ids:
        return {}
    snapshots = db.scalars(
        select(YouTubeVideoSnapshot).where(
            YouTubeVideoSnapshot.youtube_video_id.in_(list(youtube_ids.keys()))
        )
    ).all()
    return {
        youtube_ids[snap.youtube_video_id]: snap
        for snap in snapshots
        if snap.youtube_video_id in youtube_ids
    }


def _topic_profile(video: Video, snapshot: YouTubeVideoSnapshot | None):
    tokens = _tokenize(video.title) | _tokenize(video.description)
    tags = _normalize_tags(getattr(video, "tags", None))
    category = None
    if snapshot:
        tokens |= _tokenize(snapshot.title) | _tokenize(snapshot.description)
        tags |= _normalize_tags(snapshot.tags)
        category = snapshot.category_id
    return tokens, tags, category


def rank_suggestions(
    db: Session,
    seed: Video,
    candidates: list[Video],
    user_id: int,
) -> list[Video]:
    """Return `candidates` reordered as topic/affinity-aware suggestions."""
    if not candidates:
        return []

    snapshots = _snapshot_map(db, [seed, *candidates])
    seed_tokens, seed_tags, seed_category = _topic_profile(
        seed, snapshots.get(seed.id)
    )

    subscribed_channels = set(
        db.scalars(
            select(Subscription.channel_id).where(Subscription.user_id == user_id)
        ).all()
    )
    watched_channels = set(
        db.scalars(
            select(Video.channel_id)
            .join(WatchProgress, WatchProgress.video_id == Video.id)
            .where(WatchProgress.user_id == user_id, Video.channel_id.is_not(None))
            .distinct()
        ).all()
    )

    now = datetime.utcnow()

    def score(candidate: Video) -> float:
        snapshot = snapshots.get(candidate.id)
        tokens, tags, category = _topic_profile(candidate, snapshot)

        total = 0.0
        # --- Topical relevance: the heart of "same stuff, other creators" ---
        if seed_tags and tags:
            total += W_TAG_OVERLAP * min(len(seed_tags & tags), TAG_OVERLAP_CAP)
        if seed_tokens and tokens:
            total += W_TITLE_OVERLAP * min(len(seed_tokens & tokens), TITLE_OVERLAP_CAP)
        if seed_category and category and seed_category == category:
            total += W_SAME_CATEGORY

        # --- Structural relationships ---
        if seed.series_id and candidate.series_id == seed.series_id:
            total += W_SAME_SERIES
        if seed.channel_id and candidate.channel_id == seed.channel_id:
            total += W_SAME_CHANNEL

        # --- Personal affinity ---
        if candidate.channel_id in subscribed_channels:
            total += W_SUBSCRIBED
        if candidate.channel_id in watched_channels:
            total += W_WATCHED_CHANNEL

        # --- Popularity (log-scaled so a viral video can't swamp relevance) ---
        if snapshot and snapshot.view_count and snapshot.view_count > 0:
            total += W_POPULARITY * min(1.0, math.log10(snapshot.view_count) / 7.0)

        # --- Recency ---
        published = (
            (snapshot.published_at if snapshot else None)
            or candidate.published_at
            or candidate.created_at
        )
        if published:
            age_days = max(0.0, (now - published).total_seconds() / 86_400.0)
            total += W_RECENCY * math.exp(-age_days / 365.0)

        total += W_JITTER * _stable_jitter(seed.id, candidate.id)
        return total

    scored = sorted(
        candidates,
        key=lambda c: (score(c), _stable_jitter(seed.id, c.id), c.id),
        reverse=True,
    )

    # --- Diversity pass: penalize repeated channels by their rank-within-channel
    # so the list interleaves creators instead of clustering one channel. A very
    # relevant 2nd video from a channel can still survive the penalty. ---
    seen_per_channel: dict[int | None, int] = {}
    adjusted: list[tuple[float, float, int, Video]] = []
    for candidate in scored:
        repeat_index = seen_per_channel.get(candidate.channel_id, 0)
        seen_per_channel[candidate.channel_id] = repeat_index + 1
        penalty = repeat_index * CHANNEL_REPEAT_PENALTY
        adjusted.append(
            (
                score(candidate) - penalty,
                _stable_jitter(seed.id, candidate.id),
                candidate.id,
                candidate,
            )
        )

    adjusted.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in adjusted]


WATCH_HISTORY_PROFILE_LIMIT = 40


def rank_for_user(db: Session, pool: list[Video], user_id: int) -> list[Video]:
    """Rank a pool of videos for the home "Suggested" rail.

    Builds a lightweight taste profile from the viewer's recent watch history
    (topics, tags, categories, channels) plus their subscriptions, then scores
    the pool against it. Cold-start viewers (no history) fall back to a
    popularity + recency + discovery-jitter ordering. A diversity pass keeps the
    rail from clustering a single channel.
    """
    if not pool:
        return []

    recent_watches = db.scalars(
        select(Video)
        .join(WatchProgress, WatchProgress.video_id == Video.id)
        .where(WatchProgress.user_id == user_id)
        .order_by(WatchProgress.updated_at.desc())
        .limit(WATCH_HISTORY_PROFILE_LIMIT)
    ).unique().all()

    subscribed_channels = set(
        db.scalars(
            select(Subscription.channel_id).where(Subscription.user_id == user_id)
        ).all()
    )

    profile_snapshots = _snapshot_map(db, recent_watches)
    profile_tokens: set[str] = set()
    profile_tags: set[str] = set()
    profile_categories: set[str] = set()
    affinity_channels: set[int] = set()
    for watched in recent_watches:
        tokens, tags, category = _topic_profile(
            watched, profile_snapshots.get(watched.id)
        )
        profile_tokens |= tokens
        profile_tags |= tags
        if category:
            profile_categories.add(category)
        if watched.channel_id is not None:
            affinity_channels.add(watched.channel_id)

    has_profile = bool(
        profile_tokens or profile_tags or subscribed_channels or affinity_channels
    )

    pool_snapshots = _snapshot_map(db, pool)
    now = datetime.utcnow()

    def score(candidate: Video) -> float:
        snapshot = pool_snapshots.get(candidate.id)
        tokens, tags, category = _topic_profile(candidate, snapshot)

        total = 0.0
        if profile_tags and tags:
            total += W_TAG_OVERLAP * min(len(profile_tags & tags), TAG_OVERLAP_CAP)
        if profile_tokens and tokens:
            total += W_TITLE_OVERLAP * min(
                len(profile_tokens & tokens), TITLE_OVERLAP_CAP
            )
        if category and category in profile_categories:
            total += W_SAME_CATEGORY
        if candidate.channel_id in subscribed_channels:
            total += W_SUBSCRIBED
        if candidate.channel_id in affinity_channels:
            total += W_WATCHED_CHANNEL

        if snapshot and snapshot.view_count and snapshot.view_count > 0:
            total += W_POPULARITY * min(1.0, math.log10(snapshot.view_count) / 7.0)

        published = (
            (snapshot.published_at if snapshot else None)
            or candidate.published_at
            or candidate.created_at
        )
        if published:
            age_days = max(0.0, (now - published).total_seconds() / 86_400.0)
            total += W_RECENCY * math.exp(-age_days / 365.0)

        # Cold start: lean harder on discovery so the rail isn't empty/flat.
        total += (W_JITTER if has_profile else 1.4) * _stable_jitter(
            user_id, candidate.id
        )
        return total

    scored = sorted(
        pool,
        key=lambda c: (score(c), _stable_jitter(user_id, c.id), c.id),
        reverse=True,
    )

    seen_per_channel: dict[int | None, int] = {}
    adjusted: list[tuple[float, float, int, Video]] = []
    for candidate in scored:
        repeat_index = seen_per_channel.get(candidate.channel_id, 0)
        seen_per_channel[candidate.channel_id] = repeat_index + 1
        penalty = repeat_index * CHANNEL_REPEAT_PENALTY
        adjusted.append(
            (
                score(candidate) - penalty,
                _stable_jitter(user_id, candidate.id),
                candidate.id,
                candidate,
            )
        )

    adjusted.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in adjusted]
