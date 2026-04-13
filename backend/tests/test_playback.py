from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.entities import Channel, Video, VideoFile
from app.services import playback as playback_service


def make_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def create_video(db, tmp_path: Path, *, filename: str) -> Video:
    channel = Channel(name="Channel", slug=f"channel-{filename.replace('.', '-')}")
    db.add(channel)
    db.flush()

    video_path = tmp_path / "library" / "channel" / filename
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video-data")

    video = Video(
        title="Example video",
        slug=f"example-{filename.replace('.', '-')}",
        channel_id=channel.id,
        created_at=datetime.utcnow(),
        duration_seconds=3600,
        is_available=True,
    )
    db.add(video)
    db.flush()
    db.add(
        VideoFile(
            video_id=video.id,
            absolute_path=str(video_path),
            relative_path=f"channel/{filename}",
            file_size=video_path.stat().st_size,
            fingerprint=(filename.replace(".", "") + "a" * 64)[:64],
        )
    )
    db.commit()
    db.refresh(video)
    return video


def test_resolve_playback_keeps_desktop_webm_direct_play(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        video = create_video(db, tmp_path, filename="example.webm")

        monkeypatch.setattr(
            playback_service,
            "probe_media",
            lambda _path: {"codec_summary": "vp9", "audio_codec": "opus"},
        )

        playback = playback_service.resolve_playback(video)

        assert playback["direct_play"] is True
        assert playback["requires_transcode"] is False
        assert playback["stream_url"] == f"/api/videos/{video.id}/stream"


def test_resolve_playback_routes_mobile_webm_to_compatible_mp4(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        video = create_video(db, tmp_path, filename="example.webm")

        monkeypatch.setattr(
            playback_service,
            "probe_media",
            lambda _path: {"codec_summary": "vp9", "audio_codec": "opus"},
        )

        playback = playback_service.resolve_playback(video, client_profile="mobile")

        assert playback["direct_play"] is False
        assert playback["requires_transcode"] is True
        assert playback["transcode_profile"] == "transcode-mp4-mobile"
        assert playback["stream_url"] == f"/api/videos/{video.id}/compatible?client_profile=mobile"


def test_resolve_playback_routes_android_webm_to_android_compatible_mp4(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        video = create_video(db, tmp_path, filename="example.webm")

        monkeypatch.setattr(
            playback_service,
            "probe_media",
            lambda _path: {"codec_summary": "vp9", "audio_codec": "opus"},
        )

        playback = playback_service.resolve_playback(video, client_profile="android")

        assert playback["direct_play"] is False
        assert playback["requires_transcode"] is True
        assert playback["transcode_profile"] == "transcode-mp4-android"
        assert playback["stream_url"] == f"/api/videos/{video.id}/compatible?client_profile=android"


def test_resolve_playback_routes_mobile_h264_mkv_to_compatible_mp4(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        video = create_video(db, tmp_path, filename="example.mkv")

        monkeypatch.setattr(
            playback_service,
            "probe_media",
            lambda _path: {"codec_summary": "h264", "audio_codec": "aac"},
        )

        playback = playback_service.resolve_playback(video, client_profile="mobile")

        assert playback["direct_play"] is False
        assert playback["requires_transcode"] is True
        assert playback["transcode_profile"] == "remux-mp4-copy"
        assert playback["stream_url"] == f"/api/videos/{video.id}/compatible?client_profile=mobile"


def test_normalize_playback_client_profile_defaults_invalid_values():
    assert playback_service.normalize_playback_client_profile(None) == "default"
    assert playback_service.normalize_playback_client_profile("") == "default"
    assert playback_service.normalize_playback_client_profile("ANDROID") == "android"
    assert playback_service.normalize_playback_client_profile("weird") == "default"


def test_playback_client_profile_detects_mobile_headers():
    assert playback_service.playback_client_profile(
        {
            "user-agent": (
                "Mozilla/5.0 (Linux; Android 14; Light Phone) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 "
                "Mobile Safari/537.36"
            )
        }
    ) == "android"
    assert playback_service.playback_client_profile(
        {
            "user-agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )
        }
    ) == "mobile"
    assert playback_service.playback_client_profile({"sec-ch-ua-mobile": "?1"}) == "mobile"
    assert playback_service.playback_client_profile(
        {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 "
                "Safari/537.36"
            )
        }
    ) == "default"
