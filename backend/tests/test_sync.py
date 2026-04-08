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
from app.models.entities import Channel, LibraryRoot, RetentionItem, SelectedFolder, SyncJob, SyncSettings, Video, VideoFile, YouTubeChannelSnapshot, YouTubeMatch, YouTubeVideoSnapshot
from app.services.media import fingerprint_file
from app.services.scanner import scan_selected_folders
from app.services.sync import apply_sync_item, auto_organize_channel_files, fetch_channel_about_details, sync_scope, sync_video


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

    async def fake_sync_scope(db, scope, target_id, api_key):
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
