from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.db.init_db as init_db_module
import app.services.scanner as scanner_service
from app.api import routes as routes_module
from app.api.routes import _scan_library_storage_bytes, _selected_storage_roots, get_sync_settings, library_storage, list_roots, list_selected_folders
from app.db.init_db import seed_defaults
from app.models.base import Base
from app.models.entities import Channel, LibraryRoot, RetentionItem, RetentionSettings, SelectedFolder, Series, UserProfile, Video, VideoFile
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


def test_library_storage_route_uses_selected_roots_without_crashing(tmp_path: Path):
    library_root = tmp_path / "library"
    selected_root = library_root / "youtube"
    selected_root.mkdir(parents=True)
    (selected_root / "video.mp4").write_bytes(b"x" * 13)

    with make_session(tmp_path) as db:
        seed_defaults(db, [str(library_root)])
        root = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(library_root)))
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube"))
        db.commit()

        data = library_storage(db=db, current_user=object())

        assert data["library_bytes"] == 13
        assert data["root_count"] == 1
        assert data["total_bytes"] >= data["available_bytes"] >= 0


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
