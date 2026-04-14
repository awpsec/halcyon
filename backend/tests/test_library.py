import asyncio
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.db.init_db as init_db_module
import app.services.scanner as scanner_service
import app.services.subtitles as subtitle_service
import app.services.sync as sync_service
from app.api import routes as routes_module
from app.api.routes import _indexed_library_storage_bytes, _scan_library_storage_bytes, _selected_storage_roots, get_sync_settings, library_storage, list_roots, list_selected_folders, list_videos, manually_match_review_item, update_sync_settings
from app.db.init_db import seed_defaults
from app.models.base import Base
from app.models.entities import Channel, LibraryRoot, RetentionItem, RetentionSettings, SelectedFolder, Series, SyncSettings, UserProfile, Video, VideoFile, YouTubeMatch, YouTubeVideoSnapshot
from app.schemas.common import SyncReviewManualIn, SyncSettingsIn
from app.services.media import find_caption_tracks
from app.services.scanner import scan_selected_folders


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_seed_defaults_creates_profiles_and_roots(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])

        profiles = db.scalars(select(UserProfile).order_by(UserProfile.id)).all()
        roots = db.scalars(select(LibraryRoot)).all()
        admin = next(profile for profile in profiles if profile.name == "admin")

        assert [profile.name for profile in profiles] == ["admin", "guest"]
        assert admin.is_admin is True
        assert admin.requires_admin_setup is True
        assert admin.recovery_phrase_hash is not None
        assert admin.recovery_phrase_pending is not None
        assert len(admin.recovery_phrase_pending.split()) == 6
        assert len(roots) == 1
        assert roots[0].path == str(tmp_path / "library")


def test_library_routes_expose_implicit_root_selection_when_none_exist(tmp_path: Path):
    with make_session(tmp_path) as db:
        seed_defaults(db, [str(tmp_path / "library")])

        roots = list_roots(db=db, current_user=object())
        selected = list_selected_folders(db=db, current_user=object())

        assert len(roots) == 1
        assert roots[0].selected_count == 1
        assert len(selected) == 1
        assert selected[0].root_id == roots[0].id
        assert selected[0].relative_path == ""
        assert selected[0].id < 0


def test_library_routes_do_not_expose_implicit_root_selection_for_custom_mount(tmp_path: Path):
    custom_root = tmp_path / "users" / "zedd"
    custom_root.mkdir(parents=True)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(custom_root)])

        roots = list_roots(db=db, current_user=object())
        selected = list_selected_folders(db=db, current_user=object())

        assert len(roots) == 1
        assert roots[0].path == str(custom_root)
        assert roots[0].selected_count == 0
        assert selected == []


def test_seed_defaults_uses_configured_scan_interval_for_initial_sync_settings(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        configured = type("SettingsStub", (), {"scan_interval_seconds": 900, "config_dir": tmp_path})()
        monkeypatch.setattr(init_db_module, "settings", configured)

        seed_defaults(db, [str(tmp_path / "library")])

        sync_settings = db.scalar(select(init_db_module.SyncSettings))

        assert sync_settings is not None
        assert sync_settings.scan_interval_seconds == 900


def test_get_sync_settings_uses_configured_scan_interval_when_row_is_missing(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)

        configured = type("SettingsStub", (), {"scan_interval_seconds": 900, "youtube_api_key": None})()
        monkeypatch.setattr(routes_module, "settings", configured)

        sync_settings = get_sync_settings(db=db, current_user=admin)

        assert sync_settings.scan_interval_seconds == 900


def test_get_sync_settings_does_not_echo_stored_api_key(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)
        db.add(
            SyncSettings(
                automatic_detection_enabled=True,
                automatic_sync_enabled=False,
                scan_interval_seconds=30,
                allow_fallback_art=False,
                prefer_high_res_banners=False,
                comment_limit=100,
                requests_per_second=3,
                youtube_api_key="top-secret-key",
            )
        )
        db.commit()

        configured = type("SettingsStub", (), {"scan_interval_seconds": 900, "youtube_api_key": None})()
        monkeypatch.setattr(routes_module, "settings", configured)

        sync_settings = get_sync_settings(db=db, current_user=admin)

        assert sync_settings.youtube_api_key_configured is True
        assert "youtube_api_key" not in sync_settings.model_dump()


def test_update_sync_settings_preserves_existing_api_key_when_blank(tmp_path: Path):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)
        settings_row = SyncSettings(
            automatic_detection_enabled=True,
            automatic_sync_enabled=False,
            scan_interval_seconds=30,
            allow_fallback_art=False,
            prefer_high_res_banners=False,
            comment_limit=100,
            requests_per_second=3,
            youtube_api_key="top-secret-key",
        )
        db.add(settings_row)
        db.commit()

        asyncio.run(
            update_sync_settings(
                SyncSettingsIn(
                    automatic_detection_enabled=True,
                    automatic_sync_enabled=True,
                    scan_interval_seconds=45,
                    allow_fallback_art=False,
                    prefer_high_res_banners=False,
                    comment_limit=100,
                    requests_per_second=3,
                    youtube_api_key=None,
                ),
                db=db,
                current_user=admin,
            )
        )

        db.refresh(settings_row)
        assert settings_row.youtube_api_key == "top-secret-key"


def test_update_sync_settings_can_clear_api_key(tmp_path: Path):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)
        settings_row = SyncSettings(
            automatic_detection_enabled=True,
            automatic_sync_enabled=False,
            scan_interval_seconds=30,
            allow_fallback_art=False,
            prefer_high_res_banners=False,
            comment_limit=100,
            requests_per_second=3,
            youtube_api_key="top-secret-key",
        )
        db.add(settings_row)
        db.commit()

        asyncio.run(
            update_sync_settings(
                SyncSettingsIn(
                    automatic_detection_enabled=True,
                    automatic_sync_enabled=False,
                    scan_interval_seconds=30,
                    allow_fallback_art=False,
                    prefer_high_res_banners=False,
                    comment_limit=100,
                    requests_per_second=3,
                    clear_youtube_api_key=True,
                ),
                db=db,
                current_user=admin,
            )
        )

        db.refresh(settings_row)
        assert settings_row.youtube_api_key is None


def test_update_sync_settings_persists_subtitle_generation_flag(tmp_path: Path):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)
        settings_row = SyncSettings(
            automatic_detection_enabled=True,
            automatic_sync_enabled=False,
            subtitle_generation_enabled=False,
            scan_interval_seconds=30,
            allow_fallback_art=False,
            prefer_high_res_banners=False,
            comment_limit=100,
            requests_per_second=3,
        )
        db.add(settings_row)
        db.commit()

        result = asyncio.run(
            update_sync_settings(
                SyncSettingsIn(
                    automatic_detection_enabled=True,
                    automatic_sync_enabled=False,
                    subtitle_generation_enabled=True,
                    scan_interval_seconds=30,
                    allow_fallback_art=False,
                    prefer_high_res_banners=False,
                    live_tab_enabled=True,
                    comment_limit=100,
                    requests_per_second=3,
                ),
                db=db,
                current_user=admin,
            )
        )

        db.refresh(settings_row)
        assert settings_row.subtitle_generation_enabled is True
        assert result.subtitle_generation_enabled is True


def test_find_caption_tracks_labels_generated_ai_captions(tmp_path: Path):
    video_path = tmp_path / "captions-demo.mp4"
    video_path.write_bytes(b"video")
    generated_caption = tmp_path / "captions-demo.halcyon.vtt"
    generated_caption.write_text("WEBVTT\n\n", encoding="utf-8")

    tracks = find_caption_tracks(video_path)

    assert len(tracks) == 1
    assert tracks[0]["label"] == "AI Captions"


def test_subtitle_backfill_job_generates_vtt_for_existing_video(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="CoolCreator", slug="coolcreator")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "CoolCreator" / "Episode 1.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"video")

        video = Video(
            title="Episode 1",
            slug="episode-1",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="CoolCreator/Episode 1.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="f" * 64,
            )
        )
        db.commit()

        async def fake_request(source_path: Path, output_path: Path, *, force: bool = False, app_settings=None):
            del source_path, force, app_settings
            output_path.write_text("WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8")
            return {"ok": True, "output_path": str(output_path)}

        monkeypatch.setattr(subtitle_service, "request_subtitle_generation", fake_request)

        job = subtitle_service.create_subtitle_backfill_job(db)
        result = asyncio.run(
            subtitle_service.process_subtitle_backfill_job(
                db,
                job,
                app_settings=routes_module.settings.model_copy(
                    update={
                        "subtitle_service_url": "http://whisper:9000",
                        "subtitle_manual_batch_size": 5,
                    }
                ),
            )
        )

        generated_path = subtitle_service.generated_subtitle_path(video_path)

        assert result.status == "completed"
        assert generated_path.exists()
        assert any(track["label"] == "AI Captions" for track in find_caption_tracks(video_path))


def test_subtitle_backfill_job_marks_failed_when_sidecar_batch_fails(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="CoolCreator", slug="coolcreator")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "CoolCreator" / "Episode 2.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"video")

        video = Video(
            title="Episode 2",
            slug="episode-2",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="CoolCreator/Episode 2.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )
        db.commit()

        async def fake_request(source_path: Path, output_path: Path, *, force: bool = False, app_settings=None):
            del source_path, output_path, force, app_settings
            raise RuntimeError("whisper unavailable")

        monkeypatch.setattr(subtitle_service, "request_subtitle_generation", fake_request)

        job = subtitle_service.create_subtitle_backfill_job(db)
        result = asyncio.run(
            subtitle_service.process_subtitle_backfill_job(
                db,
                job,
                app_settings=routes_module.settings.model_copy(
                    update={
                        "subtitle_service_url": "http://whisper:9000",
                        "subtitle_manual_batch_size": 5,
                    }
                ),
            )
        )

        assert result.status == "failed"
        assert result.details["failed"] == 1


def test_manual_sync_review_match_accepts_youtube_url(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add_all([admin, channel])
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        match = YouTubeMatch(
            video_id=video.id,
            youtube_video_id="oldmatch1234",
            youtube_channel_id="old-channel",
            status="review",
            confidence=0.61,
            reasons=["channel-mismatch"],
        )
        db.add(match)
        db.commit()

        async def fake_fetch_watch_page_candidate(*args, **kwargs):
            return {
                "id": "abc123def45",
                "snippet": {
                    "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                    "channelTitle": "FRANKIEonPCin1080p",
                    "channelId": "channel-frankie",
                    "publishedAt": "2026-04-12T12:00:00Z",
                    "description": "Manual review pick",
                    "thumbnails": {},
                },
                "statistics": {},
                "_waytube_duration_seconds": 1443,
                "_waytube_source": "watch-page",
            }

        async def fake_apply_sync_item(db: Session, video: Video, item: dict, **kwargs):
            review_match = db.get(YouTubeMatch, match.id)
            review_match.youtube_video_id = item["id"]
            review_match.youtube_channel_id = item["snippet"]["channelId"]
            review_match.status = kwargs["status"]
            review_match.confidence = kwargs["confidence"]
            review_match.reasons = kwargs["reasons"]
            db.commit()
            return review_match

        monkeypatch.setattr(routes_module, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
        monkeypatch.setattr(routes_module, "apply_sync_item", fake_apply_sync_item)

        result = asyncio.run(
            manually_match_review_item(
                match.id,
                SyncReviewManualIn(youtube_ref="https://youtu.be/abc123def45"),
                db=db,
                current_user=admin,
            )
        )

        refreshed_match = db.get(YouTubeMatch, match.id)

        assert result == {"ok": True}
        assert refreshed_match is not None
        assert refreshed_match.youtube_video_id == "abc123def45"
        assert refreshed_match.status == "matched"
        assert "manual-review" in (refreshed_match.reasons or [])


def test_send_video_to_review_researches_and_queues_candidate(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        match = YouTubeMatch(
            video_id=video.id,
            youtube_video_id="wrongmatch01",
            youtube_channel_id="wrong-channel",
            status="matched",
            confidence=0.87,
            reasons=["old-match"],
        )
        db.add(match)
        db.commit()

        async def fake_fetch_channel_candidates(*args, **kwargs):
            return []

        async def fake_fetch_search_candidates(*args, **kwargs):
            return [
                {
                    "id": "abc123def45",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "youtube-api",
                }
            ]

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        def fake_score_match(_video: Video, _item: dict, *, channel_hints=None):
            del channel_hints
            return 0.74, ["exact-title", "duration-tight"]

        async def fake_apply_sync_item(db: Session, video: Video, item: dict, **kwargs):
            review_match = db.get(YouTubeMatch, match.id)
            review_match.youtube_video_id = item["id"]
            review_match.youtube_channel_id = item["snippet"]["channelId"]
            review_match.status = kwargs["status"]
            review_match.confidence = kwargs["confidence"]
            review_match.reasons = kwargs["reasons"]
            db.commit()
            return review_match

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fake_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "score_match", fake_score_match)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        result = asyncio.run(
            sync_service.send_video_to_review(
                db,
                video,
                "test-key",
                100,
                3,
                object(),
            )
        )

        refreshed_match = db.get(YouTubeMatch, match.id)

        assert result.id == match.id
        assert refreshed_match is not None
        assert refreshed_match.youtube_video_id == "abc123def45"
        assert refreshed_match.status == "review"
        assert refreshed_match.confidence == 0.74
        assert "admin-review" in (refreshed_match.reasons or [])
        assert "duration-tight" in (refreshed_match.reasons or [])


def test_send_video_to_review_broadens_search_when_channel_hints_are_polluted(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        polluted_neighbor = Video(
            title="Another Frankie upload",
            slug="another-frankie-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1200,
            is_available=True,
        )
        db.add(polluted_neighbor)
        db.flush()

        match = YouTubeMatch(
            video_id=video.id,
            youtube_video_id="wrongmatch01",
            youtube_channel_id="wrong-channel",
            status="matched",
            confidence=0.87,
            reasons=["old-match"],
        )
        polluted_match = YouTubeMatch(
            video_id=polluted_neighbor.id,
            youtube_video_id="wrongmatch02",
            youtube_channel_id="wrong-channel",
            status="matched",
            confidence=0.91,
            reasons=["old-match"],
        )
        db.add_all([match, polluted_match])
        db.commit()

        async def fake_fetch_channel_candidates(*args, **kwargs):
            return []

        async def fake_fetch_search_candidates(_client, _api_key, _queries, _requests_per_second, channel_ids=None, status_callback=None):
            del status_callback
            if channel_ids:
                return [
                    {
                        "id": "wrongscope01",
                        "snippet": {
                            "title": "LADY BANDITS! Turkish kids remix",
                            "channelTitle": "Bizim Kanal",
                            "channelId": "wrong-channel",
                        },
                        "statistics": {},
                        "_waytube_duration_seconds": 1443,
                        "_waytube_source": "youtube-api",
                    }
                ]
            return [
                {
                    "id": "abc123def45",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "youtube-api",
                }
            ]

        async def fake_fetch_recent_channel_upload_candidates(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        def fake_score_match(_video: Video, item: dict, *, channel_hints=None):
            del channel_hints
            if item["id"] == "abc123def45":
                return 0.78, ["exact-title", "duration-tight"]
            return 0.34, ["channel-mismatch"]

        async def fake_apply_sync_item(db: Session, video: Video, item: dict, **kwargs):
            review_match = db.get(YouTubeMatch, match.id)
            review_match.youtube_video_id = item["id"]
            review_match.youtube_channel_id = item["snippet"]["channelId"]
            review_match.status = kwargs["status"]
            review_match.confidence = kwargs["confidence"]
            review_match.reasons = kwargs["reasons"]
            db.commit()
            return review_match

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fake_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates", fake_fetch_recent_channel_upload_candidates)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "score_match", fake_score_match)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        result = asyncio.run(
            sync_service.send_video_to_review(
                db,
                video,
                "test-key",
                100,
                3,
                object(),
            )
        )

        refreshed_match = db.get(YouTubeMatch, match.id)

        assert result.id == match.id
        assert refreshed_match is not None
        assert refreshed_match.youtube_video_id == "abc123def45"
        assert refreshed_match.youtube_channel_id == "channel-frankie"
        assert refreshed_match.status == "review"
        assert refreshed_match.confidence == 0.78
        assert "admin-review" in (refreshed_match.reasons or [])


def test_send_video_to_review_keeps_stubborn_item_in_review_queue(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        match = YouTubeMatch(
            video_id=video.id,
            youtube_video_id="wrongmatch01",
            youtube_channel_id="wrong-channel",
            status="matched",
            confidence=0.87,
            reasons=["old-match"],
        )
        db.add(match)
        db.commit()

        async def fake_fetch_channel_candidates(*args, **kwargs):
            return []

        async def fake_fetch_search_candidates(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fake_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)

        result = asyncio.run(
            sync_service.send_video_to_review(
                db,
                video,
                "test-key",
                100,
                3,
                object(),
            )
        )

        refreshed_match = db.get(YouTubeMatch, match.id)

        assert result.id == match.id
        assert refreshed_match is not None
        assert refreshed_match.youtube_video_id is None
        assert refreshed_match.youtube_channel_id is None
        assert refreshed_match.status == "review"
        assert refreshed_match.confidence == 0.0
        assert "admin-review" in (refreshed_match.reasons or [])
        assert "no-candidate-found" in (refreshed_match.reasons or [])


def test_list_videos_prefers_uploaded_date_over_added_date(tmp_path: Path):
    with make_session(tmp_path) as db:
        admin = UserProfile(name="admin", display_name="Admin", accent_color="#fff", is_admin=True)
        db.add(admin)

        channel = Channel(name="Channel", slug="channel")
        db.add(channel)
        db.flush()

        older_path = tmp_path / "library" / "channel" / "older.mp4"
        older_path.parent.mkdir(parents=True, exist_ok=True)
        older_path.write_bytes(b"older")
        older_video = Video(
            title="Older upload",
            slug="older-upload",
            channel_id=channel.id,
            created_at=datetime(2026, 4, 11, 12, 0, 0),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(older_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=older_video.id,
                absolute_path=str(older_path),
                relative_path="channel/older.mp4",
                file_size=older_path.stat().st_size,
                fingerprint="o" * 64,
            )
        )

        newer_path = tmp_path / "library" / "channel" / "newer.mp4"
        newer_path.write_bytes(b"newer")
        newer_video = Video(
            title="Newer upload",
            slug="newer-upload",
            channel_id=channel.id,
            created_at=datetime(2026, 4, 10, 12, 0, 0),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(newer_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=newer_video.id,
                absolute_path=str(newer_path),
                relative_path="channel/newer.mp4",
                file_size=newer_path.stat().st_size,
                fingerprint="n" * 64,
            )
        )

        db.add_all(
            [
                YouTubeMatch(
                    video_id=older_video.id,
                    youtube_video_id="yt-older",
                    status="matched",
                ),
                YouTubeMatch(
                    video_id=newer_video.id,
                    youtube_video_id="yt-newer",
                    status="matched",
                ),
                YouTubeVideoSnapshot(
                    youtube_video_id="yt-older",
                    title=older_video.title,
                    published_at=datetime(2026, 4, 1, 12, 0, 0),
                    published_at_source="youtube-api",
                ),
                YouTubeVideoSnapshot(
                    youtube_video_id="yt-newer",
                    title=newer_video.title,
                    published_at=datetime(2026, 4, 11, 1, 0, 0),
                    published_at_source="youtube-api",
                ),
            ]
        )
        db.commit()

        videos = list_videos(offset=0, limit=None, db=db, current_user=admin)

        assert [video.id for video in videos[:2]] == [newer_video.id, older_video.id]


def test_seed_defaults_preserves_existing_avatar(tmp_path: Path):
    with make_session(tmp_path) as db:
        db.add(
            UserProfile(
                name="custom-user",
                display_name="Custom User",
                accent_color="#7ea6d6",
                avatar_url="/custom/avatar.png",
            )
        )
        db.commit()

        seed_defaults(db, [str(tmp_path / "library")], include_demo_users=True)

        custom_user = db.scalar(select(UserProfile).where(UserProfile.name == "custom-user"))

        assert custom_user is not None
        assert custom_user.avatar_url == "/custom/avatar.png"


def test_scan_selected_folders_infers_channel_and_series(tmp_path: Path):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator" / "Retro Tech Series"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "2024-11-09 Episode 03 - Handheld teardown.mp4"
    video_path.write_bytes(b"fake-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="CoolCreator/Retro Tech Series"))
        db.commit()

        job = scan_selected_folders(db, [library_root])

        video = db.scalar(select(Video))
        channel = db.scalar(select(Channel))
        series = db.scalar(select(Series))
        video_file = db.scalar(select(VideoFile))

        assert job.details["discovered"] == 1
        assert video is not None
        assert video.title.startswith("2024-11-09 Episode 03")
        assert video.episode_number == 3
        assert channel.name == "CoolCreator"
        assert series.name == "Retro Tech Series"
        assert video_file.relative_path == "CoolCreator/Retro Tech Series/2024-11-09 Episode 03 - Handheld teardown.mp4"


def test_scan_selected_folders_preserves_literal_ellipses_in_title(tmp_path: Path):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "Wait... what happened here.mp4"
    video_path.write_bytes(b"fake-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        job = scan_selected_folders(db, [library_root])
        video = db.scalar(select(Video))

        assert job.details["discovered"] == 1
        assert video is not None
        assert video.title == "Wait... what happened here"


def test_scan_selected_folders_preserves_decimal_episode_title(tmp_path: Path):
    library_root = tmp_path / "library"
    target_dir = library_root / "DayZ Mod (FRANKIEonPC)"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "DEM DAYZ HACKZ! - Arma 2 DayZ Mod - Ep. 6.5.mp4"
    video_path.write_bytes(b"fake-video-data")
    stale_timestamp = datetime(2024, 1, 1).timestamp()
    os.utime(video_path, (stale_timestamp, stale_timestamp))

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        scan_selected_folders(db, [library_root])
        video = db.scalar(select(Video))

        assert video is not None
        assert video.title == "DEM DAYZ HACKZ! - Arma 2 DayZ Mod - Ep. 6.5"


def test_scan_selected_folders_treats_one_folder_playlist_dump_as_series(tmp_path: Path):
    library_root = tmp_path / "library"
    playlist_dir = library_root / "ARMA 2 DayZ Overpoch Mod Series 2"
    playlist_dir.mkdir(parents=True)
    video_path = playlist_dir / "ARMA 2 DayZ Overpoch Mod - Series 2 - Part 1 - The Curse!.mp4"
    video_path.write_bytes(b"fake-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        scan_selected_folders(db, [library_root])

        video = db.scalar(select(Video))

        assert video is not None
        assert video.channel_id is not None
        assert video.series_id is not None
        assert db.get(Channel, video.channel_id).name == "Unknown Channel"
        assert db.get(Series, video.series_id).name == "ARMA 2 DayZ Overpoch Mod - Series 2"
        assert video.episode_number == 1


def test_scan_selected_single_folder_playlist_dump_as_series(tmp_path: Path):
    library_root = tmp_path / "library"
    playlist_dir = library_root / "DayZ Mod (FRANKIEonPC)"
    playlist_dir.mkdir(parents=True)
    video_path = playlist_dir / "BATTLE OF THE BRIDGE! - Arma 2 DayZ Mod - Ep. 48.mp4"
    video_path.write_bytes(b"fake-video-data")
    stale_timestamp = datetime(2024, 1, 1).timestamp()
    os.utime(video_path, (stale_timestamp, stale_timestamp))

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="DayZ Mod (FRANKIEonPC)"))
        db.commit()

        scan_selected_folders(db, [library_root])

        video = db.scalar(select(Video))

        assert video is not None
        assert video.series_id is not None
        assert db.get(Series, video.series_id).name == "Arma 2 DayZ Mod"
        assert video.channel_id is not None
        assert db.get(Channel, video.channel_id).name == "Unknown Channel"
        assert video.title == "BATTLE OF THE BRIDGE! - Arma 2 DayZ Mod - Ep. 48"


def test_scan_selected_folders_preserves_channel_folder_and_infers_series_from_title(tmp_path: Path):
    library_root = tmp_path / "library"
    channel_dir = library_root / "frankieonpcin1080p"
    channel_dir.mkdir(parents=True)
    video_path = channel_dir / "BATTLE OF THE BRIDGE! - Arma 2 DayZ Mod - Ep. 48.mp4"
    video_path.write_bytes(b"fake-video-data")
    stale_timestamp = datetime(2024, 1, 1).timestamp()
    os.utime(video_path, (stale_timestamp, stale_timestamp))

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        scan_selected_folders(db, [library_root])

        video = db.scalar(select(Video))

        assert video is not None
        assert video.channel_id is not None
        assert db.get(Channel, video.channel_id).name == "frankieonpcin1080p"
        assert video.series_id is not None
        assert db.get(Series, video.series_id).name == "Arma 2 DayZ Mod"


def test_scan_selected_folders_infers_series_from_root_level_title(tmp_path: Path):
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)
    video_path = library_root / "ARMA 2： DayZ Overpoch Mod — Series 2 — Part 1 — The Curse!.mp4"
    video_path.write_bytes(b"fake-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        scan_selected_folders(db, [library_root])

        video = db.scalar(select(Video))

        assert video is not None
        assert video.series_id is not None
        assert db.get(Series, video.series_id).name == "ARMA 2: DayZ Overpoch Mod - Series 2"
        assert video.episode_number == 1


def test_scan_selected_folders_removes_duplicate_fingerprint_copy(tmp_path: Path):
    library_root = tmp_path / "library"
    first_dir = library_root / "Alpha"
    second_dir = library_root / "Beta"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    first_path = first_dir / "Duplicate Video.mp4"
    second_path = second_dir / "Duplicate Video copy.mp4"
    first_path.write_bytes(b"same-video-data")
    second_path.write_bytes(b"same-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        scan_selected_folders(db, [library_root])

        videos = db.scalars(select(Video).order_by(Video.id.asc())).all()
        files = db.scalars(select(VideoFile).order_by(VideoFile.id.asc())).all()

        assert len(videos) == 1
        assert len(files) == 1
        assert first_path.exists() != second_path.exists()


def test_scan_selected_folders_skips_ytdlp_fragment_files(tmp_path: Path):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator"
    target_dir.mkdir(parents=True)
    (target_dir / "Could Android Replace the Steam Deck fragment.f401.mp4").write_bytes(b"fragment")
    (target_dir / "Could Android Replace the Steam Deck.mp4").write_bytes(b"final")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        job = scan_selected_folders(db, [library_root])
        videos = db.scalars(select(Video).order_by(Video.id.asc())).all()

        assert job.details["discovered"] == 1
        assert [video.title for video in videos] == ["Could Android Replace the Steam Deck"]


def test_scan_selected_folders_normalizes_fullwidth_bar_in_title(tmp_path: Path):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "Verdant Complex ｜ 2-Hour Brutalist Ambience.mp4"
    video_path.write_bytes(b"fake-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        job = scan_selected_folders(db, [library_root])
        video = db.scalar(select(Video))

        assert job.details["discovered"] == 1
        assert video is not None
        assert video.title == "Verdant Complex | 2-Hour Brutalist Ambience"


def test_scan_selected_folders_generates_preview_for_new_video(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "Preview target.mp4"
    video_path.write_bytes(b"fake-video-data")
    generated_calls: list[tuple[str, str]] = []

    def fake_probe_media(_path: Path) -> dict:
        stat = video_path.stat()
        return {
            "duration_seconds": 120,
            "codec_summary": "h264",
            "audio_codec": "aac",
            "container": "mp4",
            "bitrate_kbps": 1600,
            "fps": 24,
            "resolution": "1920x1080",
            "modified_at": datetime.fromtimestamp(stat.st_mtime),
            "file_size": stat.st_size,
        }

    def fake_generate_thumbnail(_path: Path, _cache_dir: Path, fingerprint: str) -> str:
        thumb_path = tmp_path / "cache" / "thumbnails" / f"{fingerprint}.jpg"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(b"thumb")
        return str(thumb_path)

    def fake_generate_preview(_path: Path, _cache_dir: Path, fingerprint: str, clip_seconds: int = 30) -> str:
        preview_path = tmp_path / "cache" / "previews" / f"{fingerprint}.mp4"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(b"preview")
        generated_calls.append((fingerprint, str(preview_path)))
        return str(preview_path)

    monkeypatch.setattr(scanner_service, "probe_media", fake_probe_media)
    monkeypatch.setattr(scanner_service, "generate_thumbnail", fake_generate_thumbnail)
    monkeypatch.setattr(scanner_service, "generate_preview_clip", fake_generate_preview)
    monkeypatch.setattr(scanner_service.settings, "cache_dir", tmp_path / "cache")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        job = scan_selected_folders(db, [library_root])
        video = db.scalar(select(Video))

        assert job.details["discovered"] == 1
        assert video is not None
        assert len(generated_calls) == 1


def test_scan_selected_folders_backfills_missing_preview_for_unchanged_video(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    target_dir = library_root / "CoolCreator"
    target_dir.mkdir(parents=True)
    video_path = target_dir / "Preview backfill.mp4"
    video_path.write_bytes(b"fake-video-data")
    cache_dir = tmp_path / "cache"
    generated_calls: list[str] = []

    def fake_probe_media(_path: Path) -> dict:
        stat = video_path.stat()
        return {
            "duration_seconds": 120,
            "codec_summary": "h264",
            "audio_codec": "aac",
            "container": "mp4",
            "bitrate_kbps": 1600,
            "fps": 24,
            "resolution": "1920x1080",
            "modified_at": datetime.fromtimestamp(stat.st_mtime),
            "file_size": stat.st_size,
        }

    def fake_generate_thumbnail(_path: Path, _cache_dir: Path, fingerprint: str) -> str:
        thumb_path = cache_dir / "thumbnails" / f"{fingerprint}.jpg"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(b"thumb")
        return str(thumb_path)

    def fake_generate_preview(_path: Path, _cache_dir: Path, fingerprint: str, clip_seconds: int = 30) -> str:
        preview_path = cache_dir / "previews" / f"{fingerprint}.mp4"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(b"preview")
        generated_calls.append(fingerprint)
        return str(preview_path)

    monkeypatch.setattr(scanner_service, "probe_media", fake_probe_media)
    monkeypatch.setattr(scanner_service, "generate_thumbnail", fake_generate_thumbnail)
    monkeypatch.setattr(scanner_service, "generate_preview_clip", fake_generate_preview)
    monkeypatch.setattr(scanner_service.settings, "cache_dir", cache_dir)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        first_job = scan_selected_folders(db, [library_root])
        assert first_job.details["discovered"] == 1
        assert len(generated_calls) == 1

        preview_dir = cache_dir / "previews"
        for preview_file in preview_dir.glob("*.mp4"):
            preview_file.unlink()

        second_job = scan_selected_folders(db, [library_root])
        assert second_job.details["discovered"] == 1
        assert len(generated_calls) == 2


def test_scan_selected_folders_skips_retention_staging_directory(tmp_path: Path):
    library_root = tmp_path / "library"
    regular_dir = library_root / "CoolCreator"
    retention_dir = library_root / ".halcyon-retention" / "token"
    regular_dir.mkdir(parents=True)
    retention_dir.mkdir(parents=True)
    (regular_dir / "real-video.mp4").write_bytes(b"real-video-data")
    (retention_dir / "staged-video.mp4").write_bytes(b"staged-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        job = scan_selected_folders(db, [library_root])
        videos = db.scalars(select(Video).order_by(Video.id.asc())).all()

        assert job.details["discovered"] == 1
        assert [video.title for video in videos] == ["real-video"]


def test_library_storage_counts_nested_selected_folder_bytes(tmp_path: Path):
    library_root = tmp_path / "library"
    selected_root = library_root / "youtube"
    nested_dir = selected_root / "creator" / "series"
    nested_dir.mkdir(parents=True)
    (nested_dir / "episode-one.mp4").write_bytes(b"a" * 11)
    (nested_dir / "episode-two.webm").write_bytes(b"b" * 17)
    (selected_root / ".halcyon-retention").mkdir(parents=True)
    (selected_root / ".halcyon-retention" / "staged.mp4").write_bytes(b"c" * 99)
    (selected_root / "fragment.f401.mp4").write_bytes(b"d" * 101)
    (library_root / "backups").mkdir(parents=True)
    (library_root / "backups" / "ignored.mp4").write_bytes(b"e" * 123)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube"))
        db.commit()

        content_roots = _selected_storage_roots(db)
        total_bytes = _scan_library_storage_bytes(content_roots)

        assert content_roots == [selected_root]
        assert total_bytes == 28


def test_library_storage_without_selection_uses_only_default_library_root(tmp_path: Path):
    custom_root = tmp_path / "users" / "zedd"
    custom_root.mkdir(parents=True)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(custom_root)])

        content_roots = _selected_storage_roots(db)

        assert content_roots == []


def test_library_storage_route_uses_selected_roots_without_crashing(tmp_path: Path):
    library_root = tmp_path / "library"
    selected_root = library_root / "youtube"
    selected_root.mkdir(parents=True)
    video_path = selected_root / "video.mp4"
    video_path.write_bytes(b"x" * 13)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube"))
        db.flush()

        channel = Channel(name="Indexed", slug="indexed-storage")
        db.add(channel)
        db.flush()
        video = Video(
            title="Indexed storage video",
            slug="indexed-storage-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="youtube/video.mp4",
                file_size=13,
                fingerprint="d" * 64,
            )
        )
        db.commit()

        data = library_storage(db=db, current_user=object())

        assert data["library_bytes"] == 13
        assert data["root_count"] == 1
        assert data["total_bytes"] >= data["available_bytes"] >= 0


def test_indexed_library_storage_uses_db_files_instead_of_walking_root(tmp_path: Path):
    library_root = tmp_path / "library"
    selected_root = library_root / "youtube"
    selected_root.mkdir(parents=True)
    indexed_video_path = selected_root / "video.mp4"
    indexed_video_path.write_bytes(b"x" * 13)
    (library_root / "backups").mkdir(parents=True)
    (library_root / "backups" / "ignored.mp4").write_bytes(b"y" * 999)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube"))
        db.flush()

        channel = Channel(name="Indexed", slug="indexed")
        db.add(channel)
        db.flush()
        video = Video(
            title="Indexed video",
            slug="indexed-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(indexed_video_path),
                relative_path="youtube/video.mp4",
                file_size=13,
                fingerprint="c" * 64,
            )
        )
        db.commit()

        content_roots = _selected_storage_roots(db)

        assert _scan_library_storage_bytes(content_roots) == 13
        assert _indexed_library_storage_bytes(db, content_roots) == 13


def test_scan_selected_folders_without_selection_skips_custom_mount_root(tmp_path: Path):
    custom_root = tmp_path / "users" / "zedd"
    custom_root.mkdir(parents=True)
    (custom_root / "youtube").mkdir()
    (custom_root / "youtube" / "video.mp4").write_bytes(b"video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(custom_root)])

        job = scan_selected_folders(db, [custom_root])

        assert job.details["discovered"] == 0
        assert db.scalar(select(func.count(Video.id))) == 0


def test_scan_selected_folders_preserves_retention_error_rows(tmp_path: Path):
    library_root = tmp_path / "library"
    regular_dir = library_root / "CoolCreator"
    retention_dir = library_root / ".halcyon-retention" / "token"
    regular_dir.mkdir(parents=True)
    retention_dir.mkdir(parents=True)
    (regular_dir / "real-video.mp4").write_bytes(b"real-video-data")
    staged_path = retention_dir / "retained-video.mp4"
    staged_path.write_bytes(b"retained-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        channel = Channel(name="CoolCreator", slug="coolcreator")
        db.add(channel)
        db.flush()
        video = Video(
            title="retained-video",
            slug="retained-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=False,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(staged_path),
            relative_path=".halcyon-retention/token/retained-video.mp4",
            file_size=staged_path.stat().st_size,
            fingerprint="a" * 64,
        )
        db.add(video_file)
        db.flush()
        db.add(RetentionSettings(staging_folder_path=str(library_root / ".halcyon-retention")))
        db.add(
            RetentionItem(
                video_id=video.id,
                video_file_id=video_file.id,
                original_absolute_path=str(library_root / "CoolCreator" / "retained-video.mp4"),
                staged_absolute_path=str(staged_path),
                original_relative_path="CoolCreator/retained-video.mp4",
                file_size_bytes=staged_path.stat().st_size,
                file_fingerprint=video_file.fingerprint,
                delete_after_at=datetime.utcnow(),
                status="error",
                last_error="Original path already exists",
            )
        )
        db.commit()

        job = scan_selected_folders(db, [library_root])
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        refreshed_video = db.scalar(select(Video).where(Video.id == video.id))

        assert job.details["discovered"] == 1
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(staged_path)
        assert refreshed_video is not None
        assert refreshed_video.title == "retained-video"


def test_scan_selected_folders_removes_missing_files_despite_reverted_history(tmp_path: Path):
    library_root = tmp_path / "library"
    regular_dir = library_root / "CoolCreator"
    regular_dir.mkdir(parents=True)
    live_path = regular_dir / "real-video.mp4"
    live_path.write_bytes(b"real-video-data")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        channel = Channel(name="CoolCreator", slug="coolcreator")
        db.add(channel)
        db.flush()
        stale_video = Video(
            title="stale-video",
            slug="stale-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(stale_video)
        db.flush()
        stale_file = VideoFile(
            video_id=stale_video.id,
            absolute_path=str(regular_dir / "stale-video.mp4"),
            relative_path="CoolCreator/stale-video.mp4",
            file_size=123,
            fingerprint="b" * 64,
        )
        db.add(stale_file)
        db.flush()
        db.add(
            RetentionItem(
                video_id=stale_video.id,
                video_file_id=stale_file.id,
                original_absolute_path=str(regular_dir / "stale-video.mp4"),
                staged_absolute_path=str(library_root / ".halcyon-retention" / "historic" / "stale-video.mp4"),
                original_relative_path="CoolCreator/stale-video.mp4",
                file_size_bytes=123,
                file_fingerprint=stale_file.fingerprint,
                delete_after_at=datetime.utcnow(),
                status="reverted",
            )
        )
        db.commit()

        job = scan_selected_folders(db, [library_root])
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == stale_file.id))
        refreshed_video = db.scalar(select(Video).where(Video.id == stale_video.id))

        assert job.details["removed"] == 2
        assert refreshed_file is None
        assert refreshed_video is None


def test_scan_selected_folders_marks_job_failed_after_rollback(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)
    (library_root / "broken.mp4").write_bytes(b"broken-video")

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])

        def raise_failure(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(scanner_service, "upsert_video_for_path", raise_failure)

        try:
            scan_selected_folders(db, [library_root])
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("scan_selected_folders should have failed")

        latest_job = db.scalar(select(scanner_service.ScanJob).order_by(scanner_service.ScanJob.id.desc()))
        assert latest_job is not None
        assert latest_job.status == "failed"
        assert latest_job.details["error"] == "boom"
