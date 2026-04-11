from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import set_watch_state
from app.models.base import Base
from app.models.entities import Channel, Series, UserProfile, Video, VideoFile, WatchHistory, WatchProgress, YouTubeMatch, YouTubeVideoSnapshot
from app.schemas.common import WatchStateIn
from app.services.feed import build_home_feed


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def create_user_and_video(db: Session, tmp_path: Path) -> tuple[UserProfile, Video]:
    user = UserProfile(name="tester", display_name="Tester", accent_color="#fff")
    db.add(user)

    channel = Channel(name="Channel", slug="channel")
    db.add(channel)
    db.flush()

    video_path = tmp_path / "library" / "channel" / "example.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video-data")

    video = Video(
        title="Example video",
        slug="example-video",
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
            relative_path="channel/example.mp4",
            file_size=video_path.stat().st_size,
            fingerprint="a" * 64,
        )
    )
    db.commit()
    return user, video


def test_unwatched_resets_progress_and_removes_continue_watching(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, video = create_user_and_video(db, tmp_path)

        db.add(
            WatchProgress(
                user_id=user.id,
                video_id=video.id,
                position_seconds=420,
                completed=False,
            )
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        continue_section = next((section for section in sections if section.key == "continue"), None)
        assert continue_section is not None
        assert [item.id for item in continue_section.items] == [video.id]

        set_watch_state(video.id, WatchStateIn(state="unwatched"), db=db, current_user=user)

        refreshed_progress = db.scalar(
            select(WatchProgress).where(
                WatchProgress.user_id == user.id,
                WatchProgress.video_id == video.id,
            )
        )
        history_rows = db.scalars(
            select(WatchHistory).where(
                WatchHistory.user_id == user.id,
                WatchHistory.video_id == video.id,
            )
        ).all()
        refreshed_sections = build_home_feed(db, user.id)
        refreshed_continue = next((section for section in refreshed_sections if section.key == "continue"), None)

        assert refreshed_progress is None
        assert history_rows == []
        assert refreshed_continue is None


def test_recently_added_excludes_watched_videos(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, watched_video = create_user_and_video(db, tmp_path)

        second_path = tmp_path / "library" / "channel" / "fresh.mp4"
        second_path.write_bytes(b"fresh-video-data")
        fresh_video = Video(
            title="Fresh video",
            slug="fresh-video",
            channel_id=watched_video.channel_id,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(fresh_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=fresh_video.id,
                absolute_path=str(second_path),
                relative_path="channel/fresh.mp4",
                file_size=second_path.stat().st_size,
                fingerprint="b" * 64,
            )
        )
        db.add(
            WatchProgress(
                user_id=user.id,
                video_id=watched_video.id,
                position_seconds=0,
                completed=True,
            )
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        recent_section = next((section for section in sections if section.key == "recent"), None)

        assert recent_section is not None
        assert [item.id for item in recent_section.items] == [fresh_video.id]


def test_recently_added_prefers_uploaded_date_over_added_date(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, older_upload = create_user_and_video(db, tmp_path)
        older_upload.created_at = datetime(2026, 4, 11, 12, 0, 0)

        newer_path = tmp_path / "library" / "channel" / "newer-upload.mp4"
        newer_path.write_bytes(b"newer-upload-data")
        newer_upload = Video(
            title="Newer upload",
            slug="newer-upload",
            channel_id=older_upload.channel_id,
            created_at=datetime(2026, 4, 10, 12, 0, 0),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(newer_upload)
        db.flush()
        db.add(
            VideoFile(
                video_id=newer_upload.id,
                absolute_path=str(newer_path),
                relative_path="channel/newer-upload.mp4",
                file_size=newer_path.stat().st_size,
                fingerprint="n" * 64,
            )
        )
        db.add_all(
            [
                YouTubeMatch(
                    video_id=older_upload.id,
                    youtube_video_id="yt-older-upload",
                    status="matched",
                ),
                YouTubeMatch(
                    video_id=newer_upload.id,
                    youtube_video_id="yt-newer-upload",
                    status="matched",
                ),
                YouTubeVideoSnapshot(
                    youtube_video_id="yt-older-upload",
                    title=older_upload.title,
                    published_at=datetime(2026, 4, 1, 12, 0, 0),
                    published_at_source="youtube-api",
                ),
                YouTubeVideoSnapshot(
                    youtube_video_id="yt-newer-upload",
                    title=newer_upload.title,
                    published_at=datetime(2026, 4, 5, 12, 0, 0),
                    published_at_source="youtube-api",
                ),
            ]
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        recent_section = next((section for section in sections if section.key == "recent"), None)

        assert recent_section is not None
        assert [item.id for item in recent_section.items[:2]] == [newer_upload.id, older_upload.id]


def test_longform_excludes_watched_videos(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, watched_video = create_user_and_video(db, tmp_path)
        watched_video.duration_seconds = 5400

        second_path = tmp_path / "library" / "channel" / "long-fresh.mp4"
        second_path.write_bytes(b"long-fresh-video-data")
        fresh_long_video = Video(
            title="Fresh long video",
            slug="fresh-long-video",
            channel_id=watched_video.channel_id,
            created_at=datetime.utcnow(),
            duration_seconds=7200,
            is_available=True,
        )
        db.add(fresh_long_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=fresh_long_video.id,
                absolute_path=str(second_path),
                relative_path="channel/long-fresh.mp4",
                file_size=second_path.stat().st_size,
                fingerprint="c" * 64,
            )
        )
        db.add(
            WatchProgress(
                user_id=user.id,
                video_id=watched_video.id,
                position_seconds=0,
                completed=True,
            )
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        longform_section = next((section for section in sections if section.key == "longform"), None)

        assert longform_section is not None
        assert [item.id for item in longform_section.items] == [fresh_long_video.id]


def test_home_suggested_prioritizes_next_series_episode_without_home_next_up_section(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, first_video = create_user_and_video(db, tmp_path)
        series = Series(name="Test Series", slug="test-series")
        db.add(series)
        db.flush()

        first_video.series_id = series.id
        first_video.episode_number = 1
        first_video.title = "Episode 1"

        second_path = tmp_path / "library" / "channel" / "episode-2.mp4"
        second_path.write_bytes(b"episode-two-data")
        second_video = Video(
            title="Episode 2",
            slug="episode-2",
            channel_id=first_video.channel_id,
            series_id=series.id,
            episode_number=2,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(second_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=second_video.id,
                absolute_path=str(second_path),
                relative_path="channel/episode-2.mp4",
                file_size=second_path.stat().st_size,
                fingerprint="b" * 64,
            )
        )

        third_path = tmp_path / "library" / "channel" / "other.mp4"
        third_path.write_bytes(b"other-data")
        other_video = Video(
            title="Other video",
            slug="other-video",
            channel_id=first_video.channel_id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(other_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=other_video.id,
                absolute_path=str(third_path),
                relative_path="channel/other.mp4",
                file_size=third_path.stat().st_size,
                fingerprint="c" * 64,
            )
        )

        db.add(
            WatchProgress(
                user_id=user.id,
                video_id=first_video.id,
                position_seconds=0,
                completed=True,
            )
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        suggested_section = next((section for section in sections if section.key == "random"), None)
        next_up_section = next((section for section in sections if section.key == "series"), None)

        assert next_up_section is None
        assert suggested_section is not None
        assert [item.id for item in suggested_section.items[:2]] == [second_video.id, other_video.id]
        assert suggested_section.items[0].reason == "next-up"


def test_home_suggested_next_series_episode_uses_earliest_unwatched_episode(tmp_path: Path):
    with make_session(tmp_path) as db:
        user, first_video = create_user_and_video(db, tmp_path)
        series = Series(name="Ordered Series", slug="ordered-series")
        db.add(series)
        db.flush()

        first_video.series_id = series.id
        first_video.episode_number = 1
        first_video.title = "Episode 1"

        second_path = tmp_path / "library" / "channel" / "episode-2.mp4"
        second_path.write_bytes(b"episode-two-data")
        second_video = Video(
            title="Episode 2",
            slug="ordered-series-2",
            channel_id=first_video.channel_id,
            series_id=series.id,
            episode_number=2,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(second_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=second_video.id,
                absolute_path=str(second_path),
                relative_path="channel/episode-2.mp4",
                file_size=second_path.stat().st_size,
                fingerprint="d" * 64,
            )
        )

        third_path = tmp_path / "library" / "channel" / "episode-3.mp4"
        third_path.write_bytes(b"episode-three-data")
        third_video = Video(
            title="Episode 3",
            slug="ordered-series-3",
            channel_id=first_video.channel_id,
            series_id=series.id,
            episode_number=3,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            is_available=True,
        )
        db.add(third_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=third_video.id,
                absolute_path=str(third_path),
                relative_path="channel/episode-3.mp4",
                file_size=third_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )

        db.add_all(
            [
                WatchProgress(
                    user_id=user.id,
                    video_id=first_video.id,
                    position_seconds=0,
                    completed=True,
                ),
                WatchProgress(
                    user_id=user.id,
                    video_id=third_video.id,
                    position_seconds=0,
                    completed=True,
                ),
            ]
        )
        db.commit()

        sections = build_home_feed(db, user.id)
        suggested_section = next((section for section in sections if section.key == "random"), None)

        assert suggested_section is not None
        assert suggested_section.items[0].id == second_video.id
        assert suggested_section.items[0].reason == "next-up"
