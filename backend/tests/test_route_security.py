from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import add_playlist_item, create_playlist, router, update_progress
from app.core.config import get_settings
from app.db.session import get_db
from app.models.base import Base
from app.models.entities import Channel, Playlist, SessionToken, UserProfile, Video, VideoFile, WatchHistory, WatchProgress
from app.schemas.common import PlaylistCreateIn, ProgressIn, QueueItemIn
from app.services.auth import hash_session_token


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def create_user(db: Session, name: str) -> UserProfile:
    user = UserProfile(name=name, display_name=name.title(), accent_color="#fff")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_video(db: Session, tmp_path: Path) -> Video:
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
    db.refresh(video)
    return video


def make_client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_update_progress_ignores_payload_user_id(tmp_path: Path):
    with make_session(tmp_path) as db:
        owner = create_user(db, "owner")
        other = create_user(db, "other")
        video = create_video(db, tmp_path)

        update_progress(
            video.id,
            ProgressIn(user_id=other.id, position_seconds=90, completed=False),
            db=db,
            current_user=owner,
        )

        owner_progress = db.scalar(select(WatchProgress).where(WatchProgress.user_id == owner.id, WatchProgress.video_id == video.id))
        other_progress = db.scalar(select(WatchProgress).where(WatchProgress.user_id == other.id, WatchProgress.video_id == video.id))
        owner_history = db.scalars(select(WatchHistory).where(WatchHistory.user_id == owner.id, WatchHistory.video_id == video.id)).all()
        other_history = db.scalars(select(WatchHistory).where(WatchHistory.user_id == other.id, WatchHistory.video_id == video.id)).all()

        assert owner_progress is not None
        assert owner_progress.position_seconds == 90
        assert other_progress is None
        assert len(owner_history) == 1
        assert other_history == []


def test_create_playlist_binds_to_current_user(tmp_path: Path):
    with make_session(tmp_path) as db:
        owner = create_user(db, "owner")
        other = create_user(db, "other")

        playlist = create_playlist(
            PlaylistCreateIn(user_id=other.id, name="Favorites", description="desc"),
            db=db,
            current_user=owner,
        )
        stored = db.scalar(select(Playlist).where(Playlist.name == "Favorites"))

        assert stored is not None
        assert stored.user_id == owner.id
        assert playlist.id == stored.id


def test_add_playlist_item_rejects_non_owner(tmp_path: Path):
    with make_session(tmp_path) as db:
        owner = create_user(db, "owner")
        other = create_user(db, "other")
        video = create_video(db, tmp_path)
        playlist = Playlist(user_id=owner.id, name="Favorites")
        db.add(playlist)
        db.commit()
        db.refresh(playlist)

        try:
            add_playlist_item(
                playlist.id,
                QueueItemIn(video_id=video.id),
                db=db,
                current_user=other,
            )
            assert False, "Expected playlist ownership protection"
        except HTTPException as error:
            assert error.status_code == 404


def test_video_stream_requires_authentication(tmp_path: Path):
    with make_session(tmp_path) as db:
        user = create_user(db, "owner")
        video = create_video(db, tmp_path)
        client = make_client(db)

        unauthenticated = client.get(f"/api/videos/{video.id}/stream")

        db.add(SessionToken(token=hash_session_token("session-token"), user_id=user.id))
        db.commit()
        client.cookies.set(get_settings().session_cookie_name, "session-token")

        authenticated = client.get(f"/api/videos/{video.id}/stream")

        assert unauthenticated.status_code == 401
        assert authenticated.status_code == 200
        assert authenticated.content == b"video-data"
