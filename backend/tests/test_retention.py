from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.timezone import server_timezone_name
from app.models.base import Base
from app.models.entities import Channel, RetentionItem, RetentionRun, Video, VideoFile
from app.services.retention import (
    delete_pending_retention_items,
    get_or_create_retention_settings,
    record_retention_failure,
    retention_auto_run_due,
    revert_last_retention_run,
    run_retention_cycle,
)
from app.services.subtitles import generated_subtitle_path


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def create_video_with_file(db: Session, tmp_path: Path) -> tuple[Video, VideoFile, Path]:
    source_path = tmp_path / "library" / "channel" / "recent-video.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"video-data")

    channel = Channel(name="Channel", slug="channel")
    db.add(channel)
    db.flush()

    video = Video(
        title="Recent video",
        slug="recent-video",
        channel_id=channel.id,
        created_at=datetime.utcnow(),
        is_available=True,
    )
    db.add(video)
    db.flush()

    video_file = VideoFile(
        video_id=video.id,
        absolute_path=str(source_path),
        relative_path="channel/recent-video.mp4",
        file_size=source_path.stat().st_size,
        fingerprint="f" * 64,
    )
    db.add(video_file)
    db.commit()
    db.refresh(video)
    db.refresh(video_file)
    return video, video_file, source_path


def test_manual_retention_run_does_not_remark_reverted_history(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        settings_row = get_or_create_retention_settings(db)
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.add(
            RetentionItem(
                video_id=video.id,
                video_file_id=video_file.id,
                original_absolute_path=str(source_path),
                staged_absolute_path=str(tmp_path / "retention-staging" / "historic" / "recent-video.mp4"),
                original_relative_path=video_file.relative_path,
                original_video_created_at=video.created_at,
                file_size_bytes=video_file.file_size,
                file_fingerprint=video_file.fingerprint,
                delete_after_at=datetime.utcnow(),
                status="reverted",
                run_token="historic",
            )
        )
        db.commit()

        result = run_retention_cycle(db, trigger="manual")

        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        reverted_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))

        assert result["marked"] == 0
        assert result["message"] == "Marked 0, deleted 0, reverted 0"
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)
        assert source_path.exists()
        assert reverted_item is not None
        assert reverted_item.status == "reverted"


def test_manual_retention_run_ignores_orphaned_reverted_history(tmp_path: Path):
    with make_session(tmp_path) as db:
        settings_row = get_or_create_retention_settings(db)
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.add(
            RetentionItem(
                video_id=999,
                video_file_id=999,
                original_absolute_path=str(tmp_path / "missing-original.mp4"),
                staged_absolute_path=str(tmp_path / "retention-staging" / "historic" / "missing-staged.mp4"),
                original_relative_path="missing-original.mp4",
                file_size_bytes=123,
                file_fingerprint="e" * 64,
                delete_after_at=datetime.utcnow(),
                status="reverted",
                run_token="historic",
            )
        )
        db.commit()

        result = run_retention_cycle(db, trigger="manual")
        reverted_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == 999))

        assert result["marked"] == 0
        assert result["deleted"] == 0
        assert result["reverted"] == 0
        assert result["message"] == "Marked 0, deleted 0, reverted 0"
        assert reverted_item is not None
        assert reverted_item.status == "reverted"
        assert reverted_item.last_error is None


def test_manual_retention_run_can_restage_reverted_item(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        first_result = run_retention_cycle(db, trigger="manual", force=True)
        assert first_result["marked"] == 1

        first_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert first_item is not None
        first_staged_path = Path(first_item.staged_absolute_path)
        assert first_item.status == "staged"
        assert first_staged_path.exists()
        assert not source_path.exists()

        revert_result = revert_last_retention_run(db)
        assert revert_result["reverted"] == 1

        reverted_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert reverted_item is not None
        assert reverted_item.status == "reverted"
        assert source_path.exists()

        second_result = run_retention_cycle(db, trigger="manual", force=True)
        assert second_result["marked"] == 1

        restaged_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert restaged_item is not None
        assert restaged_item.id == first_item.id
        assert restaged_item.status == "staged"
        assert restaged_item.run_token == second_result["run_token"]
        assert Path(restaged_item.staged_absolute_path).exists()
        assert not source_path.exists()


def test_manual_retention_run_reports_missing_source_files(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        source_path.unlink()

        result = run_retention_cycle(db, trigger="manual", force=True)

        assert result["marked"] == 0
        assert result["message"] == "Marked 0, deleted 0, reverted 0, skipped 1 missing source file"
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        refreshed_video = db.scalar(select(Video).where(Video.id == video.id))
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)
        assert refreshed_video is not None
        assert refreshed_video.is_available is False

        retention_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert retention_item is None


def test_record_retention_failure_persists_history(tmp_path: Path):
    with make_session(tmp_path) as db:
        result = record_retention_failure(db, trigger="manual", message="UNIQUE constraint failed")
        settings_row = get_or_create_retention_settings(db)
        run = db.scalar(select(RetentionRun).order_by(RetentionRun.id.desc()))

        assert result["status"] == "failed"
        assert settings_row.last_run_status == "failed"
        assert settings_row.last_run_message == "UNIQUE constraint failed"
        assert run is not None
        assert run.status == "failed"
        assert run.message == "UNIQUE constraint failed"


def test_delete_pending_retention_items_removes_staged_files(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        first_result = run_retention_cycle(db, trigger="manual", force=True)
        assert first_result["marked"] == 1

        staged_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert staged_item is not None
        staged_path = Path(staged_item.staged_absolute_path)
        assert staged_path.exists()

        delete_result = delete_pending_retention_items(db)

        assert delete_result["deleted"] == 1
        assert not source_path.exists()
        assert not staged_path.exists()
        assert db.get(VideoFile, video_file.id) is None

        deleted_item = db.scalar(select(RetentionItem).where(RetentionItem.id == staged_item.id))
        assert deleted_item is not None
        assert deleted_item.status == "deleted"
        assert deleted_item.video_id is None
        assert deleted_item.video_file_id is None


def test_delete_pending_retention_items_removes_generated_subtitle_sidecars(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        subtitle_path = generated_subtitle_path(source_path)
        subtitle_path.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n",
            encoding="utf-8",
        )

        run_retention_cycle(db, trigger="manual", force=True)
        assert subtitle_path.exists()

        delete_result = delete_pending_retention_items(db)

        assert delete_result["deleted"] == 1
        assert not subtitle_path.exists()


def test_retention_runs_persist_file_lists_for_mark_delete_and_revert(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, _ = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        run_retention_cycle(db, trigger="manual", force=True)
        mark_run = db.scalar(select(RetentionRun).order_by(RetentionRun.id.desc()))

        assert mark_run is not None
        assert mark_run.details == {"marked_files": ["channel/recent-video.mp4"]}

        revert_last_retention_run(db)
        revert_run = db.scalar(select(RetentionRun).order_by(RetentionRun.id.desc()))

        assert revert_run is not None
        assert revert_run.details == {"reverted_files": ["channel/recent-video.mp4"]}

        run_retention_cycle(db, trigger="manual", force=True)
        delete_pending_retention_items(db)
        delete_run = db.scalar(select(RetentionRun).order_by(RetentionRun.id.desc()))

        assert delete_run is not None
        assert delete_run.details == {"deleted_files": ["channel/recent-video.mp4"]}
        assert db.get(VideoFile, video_file.id) is None


def test_delete_pending_retention_items_does_not_delete_metadata_when_staged_file_is_missing(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        run_retention_cycle(db, trigger="manual", force=True)
        staged_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert staged_item is not None
        staged_path = Path(staged_item.staged_absolute_path)
        staged_path.unlink()

        delete_result = delete_pending_retention_items(db)

        assert delete_result["deleted"] == 0
        assert "encountered 1 retention issue" in delete_result["message"]
        assert db.get(VideoFile, video_file.id) is not None
        assert not source_path.exists()

        errored_item = db.scalar(select(RetentionItem).where(RetentionItem.id == staged_item.id))
        assert errored_item is not None
        assert errored_item.status == "error"
        assert errored_item.last_error == "Staged file missing"


def test_retention_auto_run_due_uses_last_auto_run_at_not_last_manual_run(tmp_path: Path):
    with make_session(tmp_path) as db:
        settings_row = get_or_create_retention_settings(db)
        now = datetime(2026, 4, 6, 12, 0, 0)

        settings_row.auto_schedule_kind = "interval"
        settings_row.auto_interval_minutes = 30
        settings_row.last_run_at = now - timedelta(minutes=5)
        settings_row.last_auto_run_at = now - timedelta(minutes=45)

        assert retention_auto_run_due(settings_row, now=now) is True


def test_retention_settings_lock_timezone_to_server(tmp_path: Path):
    with make_session(tmp_path) as db:
        settings_row = get_or_create_retention_settings(db)
        expected_timezone = server_timezone_name()

        assert settings_row.auto_timezone == expected_timezone

        settings_row.auto_timezone = "Pacific/Auckland"
        db.commit()

        refreshed = get_or_create_retention_settings(db)

        assert refreshed.auto_timezone == "Pacific/Auckland"


def test_retention_auto_run_due_uses_saved_timezone(tmp_path: Path):
    with make_session(tmp_path) as db:
        settings_row = get_or_create_retention_settings(db)
        settings_row.auto_schedule_kind = "daily"
        settings_row.auto_time_hour = 9
        settings_row.auto_time_minute = 0
        settings_row.auto_timezone = "America/Los_Angeles"
        settings_row.last_auto_run_at = datetime(2026, 4, 6, 15, 30, 0)

        assert retention_auto_run_due(settings_row, now=datetime(2026, 4, 6, 16, 5, 0)) is True

        settings_row.last_auto_run_at = datetime(2026, 4, 6, 16, 1, 0)

        assert retention_auto_run_due(settings_row, now=datetime(2026, 4, 6, 16, 5, 0)) is False


def test_auto_retention_deletes_due_staged_files_even_when_retention_is_disabled(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = False
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        first_result = run_retention_cycle(db, trigger="manual", force=True)
        assert first_result["marked"] == 1

        staged_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert staged_item is not None
        staged_item.delete_after_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()

        auto_result = run_retention_cycle(db, trigger="auto")

        assert auto_result["status"] == "completed"
        assert auto_result["deleted"] == 1
        assert db.get(VideoFile, video_file.id) is None
        assert not source_path.exists()


def test_auto_retention_not_due_still_cleans_due_deletions_without_advancing_schedule(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, _ = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.auto_schedule_kind = "weekly"
        settings_row.auto_weekday = datetime.utcnow().weekday()
        settings_row.auto_time_hour = 23
        settings_row.auto_time_minute = 59
        settings_row.last_auto_run_at = datetime.utcnow()
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        run_retention_cycle(db, trigger="manual", force=True)
        staged_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert staged_item is not None
        staged_item.delete_after_at = datetime.utcnow() - timedelta(minutes=1)
        previous_last_auto = settings_row.last_auto_run_at
        db.commit()

        auto_result = run_retention_cycle(db, trigger="auto")
        db.refresh(settings_row)

        assert auto_result["status"] == "completed"
        assert auto_result["deleted"] == 1
        assert settings_row.last_auto_run_at == previous_last_auto


def test_auto_retention_noop_skip_does_not_update_history_or_last_run(tmp_path: Path):
    with make_session(tmp_path) as db:
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = False
        settings_row.last_run_status = "completed"
        settings_row.last_run_message = "Manual run"
        settings_row.last_run_trigger = "manual"
        db.add(
            RetentionRun(
                trigger="auto",
                status="skipped",
                message="Retention disabled",
                marked_count=0,
                deleted_count=0,
                reverted_count=0,
            )
        )
        db.commit()

        auto_result = run_retention_cycle(db, trigger="auto")
        db.refresh(settings_row)
        runs = db.scalars(select(RetentionRun).order_by(RetentionRun.id.asc())).all()

        assert auto_result["status"] == "skipped"
        assert auto_result["message"] == "Retention disabled"
        assert settings_row.last_run_status == "completed"
        assert settings_row.last_run_message == "Manual run"
        assert settings_row.last_run_trigger == "manual"
        assert runs == []


def test_retention_restores_staged_file_if_commit_fails(tmp_path: Path):
    with make_session(tmp_path) as db:
        video, video_file, source_path = create_video_with_file(db, tmp_path)
        video.created_at = datetime.utcnow().replace(year=2024)
        settings_row = get_or_create_retention_settings(db)
        settings_row.enabled = True
        settings_row.retention_days = 30
        settings_row.staging_folder_path = str(tmp_path / "retention-staging")
        db.commit()

        original_commit = db.commit

        def failing_commit():
            raise RuntimeError("forced commit failure")

        db.commit = failing_commit  # type: ignore[method-assign]
        try:
            try:
                run_retention_cycle(db, trigger="manual", force=True)
            except RuntimeError as error:
                assert str(error) == "forced commit failure"
        finally:
            db.commit = original_commit  # type: ignore[method-assign]

        assert source_path.exists()
        assert not any(path.is_file() for path in (tmp_path / "retention-staging").rglob("*"))
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        retained_item = db.scalar(select(RetentionItem).where(RetentionItem.video_file_id == video_file.id))
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)
        assert retained_item is None
