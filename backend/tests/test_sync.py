import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.background as background_service
import app.services.sync as sync_service
from app.core.config import Settings
from app.models.base import Base
from app.models.entities import Channel, LibraryRoot, LiveMonitoredChannel, RetentionItem, SelectedFolder, Series, SyncJob, SyncSettings, UserProfile, Video, VideoFile, WatchProgress, YouTubeChannelSnapshot, YouTubeCommentReplySnapshot, YouTubeCommentSnapshot, YouTubeLiveStreamSnapshot, YouTubeMatch, YouTubeVideoSnapshot
from app.services.media import fingerprint_file
from app.services.scanner import scan_selected_folders
from app.services.sync import apply_sync_item, auto_organize_channel_files, choose_playlist_series_title, fetch_channel_about_details, refresh_live_streams, sync_scope, sync_video
from app.services.utils import slugify


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_fetch_channel_about_details_parses_counts_without_api(monkeypatch):
    html = """
    <html>
      <head><meta property="og:image" content="https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj" /></head>
      <body>
        <script>
          var ytInitialData = {
            "contents": {
              "aboutChannelViewModel": {
                "title": {"content": "Asmongold TV"},
                "description": {"content": "Channel description"},
                "subscriberCountText": "4.49M subscribers",
                "viewCountText": "5,135,286,525 views",
                "videoCountText": "6,945 videos",
                "joinedDateText": {"content": "Joined Dec 9, 2019"},
                "canonicalChannelUrl": "http://www.youtube.com/@AsmonTV",
                "links": []
              }
            }
          };
        </script>
      </body>
    </html>
    """

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html)

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await fetch_channel_about_details(client, "channel-asmongold", 3, include_art=False)

    result = asyncio.run(run())

    assert result is not None
    assert result["subscriber_count"] == 4_490_000
    assert result["view_count"] == 5_135_286_525
    assert result["video_count"] == 6_945


def test_slugify_collapses_apostrophes_without_extra_dash() -> None:
    assert slugify("Moore's Law is Dead") == "moores-law-is-dead"
    assert slugify("Moore’s Law is Dead") == "moores-law-is-dead"


def test_normalize_youtube_api_quota_resets_stale_day(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            youtube_api_quota_day="2026-04-10",
            youtube_api_quota_used_units=7300,
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)

        changed = sync_service.normalize_youtube_api_quota(settings_row, now=datetime.fromisoformat("2026-04-11T12:00:00+00:00"))

        assert changed is True
        assert settings_row.youtube_api_quota_day == sync_service.current_youtube_quota_day(datetime.fromisoformat("2026-04-11T12:00:00+00:00"))
        assert settings_row.youtube_api_quota_used_units == 0


def test_build_youtube_api_quota_summary_clamps_remaining_values(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            youtube_api_quota_day="2026-04-11",
            youtube_api_quota_used_units=12_500,
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)

        summary = sync_service.build_youtube_api_quota_summary(settings_row)

        assert summary["youtube_api_quota_daily_limit"] == 10_000
        assert summary["youtube_api_quota_used_units"] == 10_000
        assert summary["youtube_api_quota_remaining_units"] == 0
        assert summary["youtube_api_quota_remaining_percent"] == 0
        assert summary["youtube_api_quota_estimated"] is True


def test_choose_playlist_series_title_prefers_exact_membership_and_non_generic_playlist(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        existing_series = Series(name="Arma 2 DayZ Mod", slug="arma-2-dayz-mod")
        db.add_all([channel, existing_series])
        db.flush()

        video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge",
            channel_id=channel.id,
            series_id=existing_series.id,
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        title, position = choose_playlist_series_title(
            video,
            "bridge12345a",
            [
                {
                    "id": "uploads",
                    "title": "Uploads",
                    "positions": {"bridge12345a": 47},
                },
                {
                    "id": "dayz",
                    "title": "DayZ Mod (FRANKIEonPC)",
                    "positions": {"bridge12345a": 47},
                },
            ],
        )

        assert title == "DayZ Mod (FRANKIEonPC)"
        assert position == 47


def test_sync_video_uses_recent_channel_uploads_when_title_search_misses(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        matched_path = tmp_path / "library" / "asmongold" / "known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="Known upload",
            slug="known-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=901,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="asmongold/known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="a" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="known123",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "asmongold" / "stale-title.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Old launch title before rename",
            slug="old-launch-title-before-rename",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="asmongold/stale-title.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="b" * 64,
            )
        )
        db.commit()

        async def fake_fetch_search_candidates(*args, **kwargs):
            return []

        async def fake_fetch_recent_channel_upload_candidates(*args, **kwargs):
            return [
                {
                    "id": "renamed123",
                    "snippet": {
                        "title": "Asmongold reacts to a completely different title",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                        "publishedAt": "2026-04-06T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 900,
                    "_waytube_source": "youtube-api-channel-recent",
                }
            ]

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("sync_video should not hit channel lookup when local channel ids already exist")

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates", fake_fetch_recent_channel_upload_candidates)
        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "renamed123"
        assert result.youtube_channel_id == "channel-asmongold"
        assert result.confidence is not None and result.confidence >= 0.58
        assert "duration-tight" in (result.reasons or [])


def test_sync_video_uses_neighbor_title_channel_hints_for_orphans_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        matched_path = tmp_path / "library" / "known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="Britain is cooked",
            slug="britain-is-cooked",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=903,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="c" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="knownbrit1",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "orphan.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Britain Navy is a joke now",
            slug="britain-navy-is-a-joke-now",
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="orphan.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="d" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            captured_queries.extend(queries)
            return [
                {
                    "id": "britnavy123",
                    "snippet": {
                        "title": "This is absolutely embarrassing..",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                        "publishedAt": "2026-04-06T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 900,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-asmongold"
        assert any("Asmongold TV" in query for query in captured_queries)


def test_sync_video_uses_series_neighbor_channel_hints_for_new_episode_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        known_channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        series = Series(name="DayZ Mod (FRANKIEonPC)", slug="dayz-mod-frankieonpc")
        db.add_all([known_channel, unknown_channel, series])
        db.flush()

        matched_path = tmp_path / "library" / "series-known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge-ep-48",
            channel_id=known_channel.id,
            series_id=series.id,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="series-known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="bridge48",
                youtube_channel_id="channel-frankie",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "series-new.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="DEM DAYZ HACKZ! - Arma 2: DayZ Mod - Ep. 6.5",
            slug="dem-dayz-hackz-ep-6-5",
            channel_id=unknown_channel.id,
            series_id=series.id,
            created_at=datetime.utcnow(),
            duration_seconds=1795,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="series-new.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="f" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            captured_queries.extend(queries)
            return [
                {
                    "id": "hackz65",
                    "snippet": {
                        "title": "DEM DAYZ HACKZ! - Arma 2: DayZ Mod - Ep. 6.5",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1795,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-frankie"
        assert any("FRANKIEonPCin1080p" in query for query in captured_queries)


def test_sync_video_marks_known_channel_mismatch_as_review(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "frankie.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"target")
        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="frankie.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="f" * 64,
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "bizim123",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "Bizim Kanal",
                        "channelId": "channel-bizim",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "review"
        assert result.youtube_channel_id == "channel-bizim"
        assert "channel-mismatch" in (result.reasons or [])


def test_sync_video_disables_api_after_fatal_search_and_uses_fallback_match(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "frankie-fallback.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"target")
        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30-fallback",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="frankie-fallback.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="1" * 64,
            )
        )
        db.commit()

        async def fake_fetch_search_candidates(*args, **kwargs):
            raise sync_service.YouTubeSyncError("YouTube search failed: quotaExceeded", fatal=True)

        async def fake_fetch_channel_candidates(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "frankie123",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            assert kwargs["api_key"] is None
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fake_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "frankie123"
        assert result.youtube_channel_id == "channel-frankie"


def test_refresh_live_streams_reuses_existing_live_channel_without_playlist_lookup(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="PGL upload",
            slug="pgl-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-pgl",
                youtube_video_id="vod123",
                status="matched",
            )
        )
        db.add(
            YouTubeLiveStreamSnapshot(
                youtube_video_id="live123",
                youtube_channel_id="channel-pgl",
                channel_id=channel.id,
                title="Current stream",
                is_live=True,
                last_seen_at=datetime.utcnow(),
                fetched_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def fail_playlist_lookup(*args, **kwargs):
            raise AssertionError("playlist lookup should be skipped for an already-live channel")

        async def fake_fetch_live_video_details(*args, **kwargs):
            return [
                {
                    "id": "live123",
                    "snippet": {
                        "title": "Current stream",
                        "channelTitle": "PGL",
                        "channelId": "channel-pgl",
                        "liveBroadcastContent": "live",
                        "thumbnails": {},
                    },
                    "liveStreamingDetails": {
                        "actualStartTime": "2026-04-15T12:00:00Z",
                        "concurrentViewers": "4812",
                    },
                    "statistics": {},
                }
            ]

        monkeypatch.setattr(sync_service, "fetch_recent_upload_playlist_video_ids", fail_playlist_lookup)
        monkeypatch.setattr(sync_service, "fetch_live_video_details", fake_fetch_live_video_details)

        async def run():
            return await refresh_live_streams(db, api_key="test-api-key", requests_per_second=3)

        rows = asyncio.run(run())

        assert len(rows) == 1
        assert rows[0].youtube_video_id == "live123"
        assert rows[0].is_live is True
        assert rows[0].concurrent_viewers == 4812


def test_refresh_live_streams_uses_web_fallback_without_api_key(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="LVNDMARK", slug="lvndmark")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="LVNDMARK upload",
            slug="lvndmark-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-lvndmark",
                youtube_video_id="vod456",
                status="matched",
            )
        )
        db.commit()

        async def fake_fetch_live_stream_candidates_web(*args, **kwargs):
            return (
                True,
                [
                    {
                        "id": "live-web-1",
                        "snippet": {
                            "title": "Checking out Bellum",
                            "channelTitle": "LVNDMARK",
                            "channelId": "channel-lvndmark",
                            "thumbnails": {},
                        },
                        "statistics": {},
                        "_waytube_live_web": True,
                        "_waytube_local_channel_id": channel.id,
                        "_waytube_checked_youtube_channel_id": "channel-lvndmark",
                    }
                ],
            )

        monkeypatch.setattr(sync_service, "fetch_live_stream_candidates_web", fake_fetch_live_stream_candidates_web)

        async def run():
            return await refresh_live_streams(db, api_key=None, requests_per_second=3)

        rows = asyncio.run(run())

        assert len(rows) == 1
        assert rows[0].youtube_video_id == "live-web-1"
        assert rows[0].youtube_channel_id == "channel-lvndmark"
        assert rows[0].channel_id == channel.id
        assert rows[0].is_live is True


def test_apply_sync_item_review_keeps_existing_channel_assignment(tmp_path: Path, monkeypatch):
    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        item = {
            "id": "bizim123",
            "snippet": {
                "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                "channelTitle": "Bizim Kanal",
                "channelId": "channel-bizim",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Wrong channel match",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "90",
            },
            "_waytube_duration_seconds": 1443,
            "_waytube_source": "watch-page",
        }

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.86,
                    reasons=["exact-title", "duration-tight", "channel-mismatch"],
                    status="review",
                )

        result = asyncio.run(run())
        refreshed_video = db.get(Video, video.id)
        created_channel = db.scalar(select(Channel).where(Channel.slug == "bizim-kanal"))

        assert result.status == "review"
        assert refreshed_video is not None
        assert refreshed_video.channel_id == channel.id
        assert created_channel is None


def test_force_sync_refreshes_existing_match_by_id_before_researching(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "matched.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="matched.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="knownmatch1",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=0.94,
                reasons=["channel", "duration-tight"],
            )
        )
        db.commit()

        search_called = False

        async def fake_fetch_video_details_by_id(*args, **kwargs):
            return {
                "id": "knownmatch1",
                "snippet": {
                    "title": "The state of British Navy is embarrassing",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-06T12:00:00Z",
                    "description": "Updated metadata",
                    "thumbnails": {},
                },
                "statistics": {},
                "_waytube_duration_seconds": 900,
                "_waytube_source": "youtube-api",
            }

        async def fake_fetch_search_candidates(*args, **kwargs):
            nonlocal search_called
            search_called = True
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_fetch_video_details_by_id)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    force=True,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "knownmatch1"
        assert "refresh-by-id" in (result.reasons or [])
        assert "force-refresh" in (result.reasons or [])
        assert search_called is False


def test_background_auto_sync_clears_stale_running_jobs_before_library_sync(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'background.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with session_factory() as db:
        db.add(
            SyncSettings(
                automatic_detection_enabled=False,
                automatic_sync_enabled=True,
                scan_interval_seconds=900,
            )
        )
        db.add(
            SyncJob(
                scope="library",
                status="running",
                created_at=datetime.utcnow() - timedelta(minutes=20),
                updated_at=datetime.utcnow() - timedelta(minutes=20),
                started_at=datetime.utcnow() - timedelta(minutes=20),
                details={},
            )
        )
        db.commit()

    monkeypatch.setattr(background_service, "SessionLocal", session_factory)
    called: list[str] = []

    async def fake_sync_scope(db, scope, target_id, api_key, **kwargs):
        called.append(scope)
        return SyncJob(scope=scope, status="completed", details={})

    monkeypatch.setattr(background_service, "sync_scope", fake_sync_scope)

    asyncio.run(background_service.background_auto_sync_once(Settings(background_tasks_enabled=False)))

    with session_factory() as db:
        stale_job = db.scalar(select(SyncJob).where(SyncJob.scope == "library"))
        assert stale_job is not None
        assert stale_job.status == "failed"
        assert stale_job.details.get("stale") is True

    assert called == ["library"]


def test_apply_sync_item_auto_organizes_root_file_without_losing_match(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_folder / "British Navy is a joke now.mp4"
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(unknown_channel)
        db.flush()

        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now",
            channel_id=unknown_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="British Navy is a joke now.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="f" * 64,
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        item = {
            "id": "britnavy123",
            "snippet": {
                "title": "The state of British Navy is embarrassing",
                "channelTitle": "Asmongold TV",
                "channelId": "channel-asmongold",
                "publishedAt": "2026-04-06T12:00:00Z",
                "description": "Updated metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.91,
                    reasons=["channel", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        organized_path = library_folder / "asmongold-tv" / source_path.name
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        refreshed_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == "channel-asmongold")
        )
        video_snapshot = db.scalar(
            select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "britnavy123")
        )

        assert refreshed_file is not None
        assert organized_path.exists()
        assert not source_path.exists()
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/asmongold-tv/British Navy is a joke now.mp4"
        assert refreshed_match is not None
        assert refreshed_match.status == "matched"
        assert refreshed_match.youtube_video_id == "britnavy123"
        assert channel_snapshot is not None
        assert video_snapshot is not None

        scan_selected_folders(db, [library_root])

        rescanned_videos = db.scalars(select(Video).order_by(Video.id.asc())).all()
        rescanned_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        rescanned_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert len(rescanned_videos) == 1
        assert rescanned_match is not None
        assert rescanned_match.youtube_video_id == "britnavy123"
        assert rescanned_file is not None
        assert rescanned_file.absolute_path == str(organized_path)
        assert rescanned_videos[0].channel is not None
        assert rescanned_videos[0].channel.slug == "asmongold-tv"


def test_apply_sync_item_assigns_series_from_playlist_membership(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "library" / "battle.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    async def fake_channel_details(*args, **kwargs):
        return {
            "snippet": {
                "title": "FRANKIEonPCin1080p",
                "description": "Channel description",
                "thumbnails": {},
            },
            "statistics": {
                "subscriberCount": "3400000",
                "videoCount": "214",
                "viewCount": "501000000",
            },
            "brandingSettings": {"image": {}},
        }

    async def fake_playlist_memberships(*args, **kwargs):
        return [
            {
                "id": "uploads",
                "title": "Uploads",
                "positions": {"bridge12345a": 47},
            },
            {
                "id": "dayz",
                "title": "DayZ Mod (FRANKIEonPC)",
                "positions": {"bridge12345a": 47},
            },
        ]

    async def fake_top_comments(*args, **kwargs):
        return []

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fake_channel_details)
    monkeypatch.setattr(sync_service, "fetch_channel_playlist_memberships", fake_playlist_memberships)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fake_top_comments)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="battle.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="p" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "bridge12345a",
            "snippet": {
                "title": "BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep.48",
                "channelTitle": "FRANKIEonPCin1080p",
                "channelId": "channel-frankie",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 1200,
            "_waytube_source": "youtube-api",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    playlist_cache={},
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        refreshed_video = db.get(Video, video.id)
        refreshed_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        assigned_series = db.get(Series, refreshed_video.series_id) if refreshed_video and refreshed_video.series_id else None

        assert refreshed_video is not None
        assert refreshed_match is not None
        assert assigned_series is not None
        assert assigned_series.name == "DayZ Mod (FRANKIEonPC)"
        assert refreshed_video.episode_number == 48
        assert "playlist-membership" in (refreshed_match.reasons or [])


def test_sync_video_reorganizes_existing_matched_file_into_selected_library_folder(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    wrong_path = library_root / "retro-game-corps" / "Could Android Replace the Steam Deck.mp4"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Retro Game Corps", slug="retro-game-corps", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Could Android Replace the Steam Deck?!",
            slug="could-android-replace-the-steam-deck",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(wrong_path),
            relative_path="retro-game-corps/Could Android Replace the Steam Deck.mp4",
            file_size=wrong_path.stat().st_size,
            fingerprint="9" * 64,
        )
        db.add(video_file)
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="Q5v10lisr_o",
                youtube_channel_id="channel-rgc",
                status="matched",
                confidence=0.95,
                reasons=["exact-title", "channel"],
                last_synced_at=datetime.utcnow(),
            )
        )
        db.commit()
        db.refresh(video)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        organized_path = library_folder / "retro-game-corps" / "Could Android Replace the Steam Deck.mp4"
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert result.status == "matched"
        assert organized_path.exists()
        assert not wrong_path.exists()
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/retro-game-corps/Could Android Replace the Steam Deck.mp4"


def test_auto_organize_channel_files_skips_live_retention_items(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)
    source_path = library_root / "Incoming clip.mp4"
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Incoming clip",
            slug="incoming-clip",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="Incoming clip.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="1" * 64,
        )
        db.add(video_file)
        db.flush()
        db.add(
            RetentionItem(
                video_id=video.id,
                video_file_id=video_file.id,
                original_absolute_path=str(source_path),
                staged_absolute_path=str(library_root / ".halcyon-retention" / "token" / "Incoming clip.mp4"),
                original_relative_path=video_file.relative_path,
                file_size_bytes=source_path.stat().st_size,
                file_fingerprint=video_file.fingerprint,
                delete_after_at=datetime.utcnow() + timedelta(hours=1),
                status="error",
                last_error="Source file missing on disk",
            )
        )
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)


def test_auto_organize_channel_files_skips_transient_download_artifacts(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_folder / "Could Android Replace the Steam Deck fragment.f401.mp4"
    source_path.write_bytes(b"fragment")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Retro Game Corps", slug="retro-game-corps", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Could Android Replace the Steam Deck",
            slug="could-android-replace-the-steam-deck",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="youtube/Could Android Replace the Steam Deck fragment.f401.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="2" * 64,
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)
        assert source_path.exists()


def test_auto_organize_channel_files_relinks_existing_canonical_duplicate(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_root / "asmongold-tv" / "Why I got banned on Twitch.mp4"
    target_path = library_folder / "asmongold-tv" / "Why I got banned on Twitch.mp4"
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"same-video")
    target_path.write_bytes(b"same-video")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Asmongold TV", slug="asmongold-tv", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Why I got banned on Twitch",
            slug="why-i-got-banned-on-twitch",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="asmongold-tv/Why I got banned on Twitch.mp4",
            file_size=source_path.stat().st_size,
            fingerprint=fingerprint_file(source_path),
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(target_path)
        assert refreshed_file.relative_path == "youtube/asmongold-tv/Why I got banned on Twitch.mp4"
        assert target_path.exists()
        assert not source_path.exists()


def test_sync_scope_orphans_reorganizes_existing_matched_file(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    wrong_path = library_root / "the-phawx" / "Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        settings_row = SyncSettings(automatic_detection_enabled=True, automatic_sync_enabled=False)
        db.add(settings_row)
        channel = Channel(name="The Phawx", slug="the-phawx", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme",
            slug="asus-zenbook-a16-review-snapdragon-x2-elite-extreme",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1500,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(wrong_path),
            relative_path="the-phawx/Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4",
            file_size=wrong_path.stat().st_size,
            fingerprint="3" * 64,
        )
        db.add(video_file)
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="-KeYWbfhixo",
                youtube_channel_id="channel-the-phawx",
                status="matched",
                confidence=0.99,
                reasons=["known-channel"],
                last_synced_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def run() -> SyncJob:
            return await sync_scope(db, scope="orphans", target_id=None, api_key=None)

        job = asyncio.run(run())
        organized_path = library_folder / "the-phawx" / "Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert job.status == "completed"
        assert organized_path.exists()
        assert not wrong_path.exists()
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/the-phawx/Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"


def test_apply_sync_item_merges_duplicate_youtube_video_records(tmp_path: Path, monkeypatch):
    target_path = tmp_path / "library" / "target.mp4"
    duplicate_path = tmp_path / "library" / "duplicate.mp4"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"same-video")
    duplicate_path.write_bytes(b"same-video")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        user = UserProfile(name="tester", display_name="Tester", accent_color="#fff")
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add_all([user, channel])
        db.flush()

        target_video = Video(
            title="DayZ Part 1",
            slug="dayz-part-1",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        duplicate_video = Video(
            title="DayZ Part 1 duplicate",
            slug="dayz-part-1-duplicate",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add_all([target_video, duplicate_video])
        db.flush()

        db.add_all(
            [
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="target.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="a" * 64,
                ),
                VideoFile(
                    video_id=duplicate_video.id,
                    absolute_path=str(duplicate_path),
                    relative_path="duplicate.mp4",
                    file_size=duplicate_path.stat().st_size,
                    fingerprint="b" * 64,
                ),
                YouTubeMatch(
                    video_id=duplicate_video.id,
                    youtube_video_id="dup123",
                    youtube_channel_id="channel-psi",
                    status="matched",
                    confidence=0.91,
                    reasons=["title"],
                ),
                WatchProgress(
                    user_id=user.id,
                    video_id=duplicate_video.id,
                    position_seconds=321,
                    completed=False,
                ),
            ]
        )
        db.commit()
        db.refresh(target_video)

        item = {
            "id": "dup123",
            "snippet": {
                "title": "ARMA 2 DayZ Overpoch Mod - Series 2 - Part 1 - The Curse!",
                "channelTitle": "PsiSyndicate",
                "channelId": "channel-psi",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    target_video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        remaining_videos = db.scalars(select(Video).order_by(Video.id.asc())).all()
        merged_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.youtube_video_id == "dup123"))
        merged_progress = db.scalar(
            select(WatchProgress).where(
                WatchProgress.user_id == user.id,
                WatchProgress.video_id == target_video.id,
            )
        )

        assert len(remaining_videos) == 1
        assert merged_match is not None
        assert merged_match.video_id == target_video.id
        assert merged_progress is not None
        assert merged_progress.position_seconds == 321
        assert target_path.exists()
        assert not duplicate_path.exists()


def test_apply_sync_item_clears_stale_duplicate_match_before_assigning(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        target_video = Video(
            title="Target video",
            slug="target-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(target_video)
        db.flush()

        db.add(
            YouTubeMatch(
                video_id=target_video.id + 999,
                youtube_video_id="dup-stale-123",
                youtube_channel_id="channel-psi",
                status="matched",
                confidence=0.5,
                reasons=["stale"],
            )
        )
        db.commit()
        db.refresh(target_video)

        item = {
            "id": "dup-stale-123",
            "snippet": {
                "title": "Target video",
                "channelTitle": "PsiSyndicate",
                "channelId": "channel-psi",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    target_video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        surviving_matches = db.scalars(
            select(YouTubeMatch).where(YouTubeMatch.youtube_video_id == "dup-stale-123").order_by(YouTubeMatch.id.asc())
        ).all()

        assert len(surviving_matches) == 1
        assert surviving_matches[0].video_id == target_video.id


def test_apply_sync_item_fetches_replies_beyond_inline_batch(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    async def fake_fetch_top_comments(*args, **kwargs):
        return [
            {
                "snippet": {
                    "totalReplyCount": 7,
                    "topLevelComment": {
                        "id": "top-comment-1",
                        "snippet": {
                            "authorDisplayName": "Top Commenter",
                            "textDisplay": "Top level comment",
                            "likeCount": 9,
                            "publishedAt": "2026-04-14T12:00:00Z",
                        },
                    },
                },
                "replies": {
                    "comments": [
                        {
                            "id": f"reply-{index}",
                            "snippet": {
                                "authorDisplayName": f"Reply {index}",
                                "textDisplay": f"Reply body {index}",
                                "likeCount": index,
                                "publishedAt": "2026-04-14T12:00:00Z",
                            },
                        }
                        for index in range(1, 6)
                    ]
                },
            }
        ]

    async def fake_fetch_comment_replies(*args, **kwargs):
        return [
            {
                "id": f"reply-{index}",
                "snippet": {
                    "authorDisplayName": f"Reply {index}",
                    "textDisplay": f"Reply body {index}",
                    "likeCount": index,
                    "publishedAt": "2026-04-14T12:00:00Z",
                },
            }
            for index in range(1, 8)
        ]

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fake_fetch_top_comments)
    monkeypatch.setattr(sync_service, "fetch_comment_replies", fake_fetch_comment_replies)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="Reply test video",
            slug="reply-test-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(tmp_path / "reply-test.mp4"),
                relative_path="reply-test.mp4",
                file_size=123,
                fingerprint="c" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "reply-test-yt",
            "snippet": {
                "title": "Reply test video",
                "channelTitle": "Reply Channel",
                "channelId": "reply-channel-id",
                "publishedAt": "2026-04-14T12:00:00Z",
                "description": "Reply test",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    max_replies_per_comment=7,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    confidence=0.93,
                    reasons=["title"],
                    status="matched",
                )

        asyncio.run(run())

        stored_comment = db.scalar(select(YouTubeCommentSnapshot).where(YouTubeCommentSnapshot.youtube_video_id == "reply-test-yt"))
        stored_replies = db.scalars(
            select(YouTubeCommentReplySnapshot)
            .where(YouTubeCommentReplySnapshot.youtube_video_id == "reply-test-yt")
            .order_by(YouTubeCommentReplySnapshot.position.asc(), YouTubeCommentReplySnapshot.id.asc())
        ).all()

        assert stored_comment is not None
        assert stored_comment.reply_count == 7
        assert len(stored_replies) == 7
        assert [reply.youtube_reply_id for reply in stored_replies] == [f"reply-{index}" for index in range(1, 8)]
