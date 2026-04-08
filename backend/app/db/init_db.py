from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.timezone import server_timezone_name
from app.db.session import engine
from app.models.base import Base
from app.models.entities import LibraryRoot, RetentionSettings, SelectedFolder, SessionToken, SyncSettings, UserProfile, VideoFile
from app.services.auth import generate_recovery_phrase, generate_temporary_password, hash_password, hash_recovery_phrase, hash_session_token, is_hashed_session_token

DEFAULT_USER_AVATAR = "/assets/branding/default_avi.png"
DEFAULT_ADMIN_USERNAME = "admin"

logger = get_logger()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        user_columns = {column["name"] for column in inspector.get_columns("user_profiles")}
        if "password_hash" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN password_hash VARCHAR(255)"))
        if "pin_hash" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN pin_hash VARCHAR(255)"))
        if "avatar_url" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN avatar_url VARCHAR(1024)"))
        if "last_subscription_seen_at" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN last_subscription_seen_at DATETIME"))
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN is_admin BOOLEAN DEFAULT FALSE"))
            connection.execute(text("UPDATE user_profiles SET is_admin = FALSE WHERE is_admin IS NULL"))
        if "requires_admin_setup" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN requires_admin_setup BOOLEAN DEFAULT FALSE"))
            connection.execute(text("UPDATE user_profiles SET requires_admin_setup = FALSE WHERE requires_admin_setup IS NULL"))
        if "recovery_phrase_hash" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN recovery_phrase_hash VARCHAR(255)"))
        if "recovery_phrase_pending" not in user_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN recovery_phrase_pending VARCHAR(255)"))
        sync_columns = {column["name"] for column in inspector.get_columns("sync_settings")}
        if "automatic_detection_enabled" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN automatic_detection_enabled BOOLEAN DEFAULT TRUE"))
            connection.execute(text("UPDATE sync_settings SET automatic_detection_enabled = TRUE WHERE automatic_detection_enabled IS NULL"))
        if "scan_interval_seconds" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN scan_interval_seconds INTEGER DEFAULT 30"))
            connection.execute(text("UPDATE sync_settings SET scan_interval_seconds = 30 WHERE scan_interval_seconds IS NULL"))
        if "allow_fallback_art" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN allow_fallback_art BOOLEAN DEFAULT FALSE"))
            connection.execute(text("UPDATE sync_settings SET allow_fallback_art = FALSE WHERE allow_fallback_art IS NULL"))
        if "youtube_api_key" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN youtube_api_key VARCHAR(255)"))
        if "requests_per_second" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN requests_per_second INTEGER DEFAULT 3"))
            connection.execute(text("UPDATE sync_settings SET requests_per_second = 3 WHERE requests_per_second IS NULL"))
        if "prefer_high_res_banners" not in sync_columns:
            connection.execute(text("ALTER TABLE sync_settings ADD COLUMN prefer_high_res_banners BOOLEAN DEFAULT FALSE"))
            connection.execute(text("UPDATE sync_settings SET prefer_high_res_banners = FALSE WHERE prefer_high_res_banners IS NULL"))
        youtube_snapshot_columns = {column["name"] for column in inspector.get_columns("youtube_video_snapshots")}
        if "published_at_source" not in youtube_snapshot_columns:
            connection.execute(text("ALTER TABLE youtube_video_snapshots ADD COLUMN published_at_source VARCHAR(32)"))
        if "thumbnail_url" not in youtube_snapshot_columns:
            connection.execute(text("ALTER TABLE youtube_video_snapshots ADD COLUMN thumbnail_url VARCHAR(1024)"))
        if "dislike_count" not in youtube_snapshot_columns:
            connection.execute(text("ALTER TABLE youtube_video_snapshots ADD COLUMN dislike_count INTEGER"))
        if "rating" not in youtube_snapshot_columns:
            connection.execute(text("ALTER TABLE youtube_video_snapshots ADD COLUMN rating FLOAT"))
        youtube_channel_columns = {column["name"] for column in inspector.get_columns("youtube_channel_snapshots")}
        if "canonical_url" not in youtube_channel_columns:
            connection.execute(text("ALTER TABLE youtube_channel_snapshots ADD COLUMN canonical_url VARCHAR(1024)"))
        if "joined_at" not in youtube_channel_columns:
            connection.execute(text("ALTER TABLE youtube_channel_snapshots ADD COLUMN joined_at DATETIME"))
        if "links" not in youtube_channel_columns:
            connection.execute(text("ALTER TABLE youtube_channel_snapshots ADD COLUMN links JSON"))
        transcode_columns = {column["name"] for column in inspector.get_columns("transcode_jobs")}
        if "pid" not in transcode_columns:
            connection.execute(text("ALTER TABLE transcode_jobs ADD COLUMN pid INTEGER"))
        retention_item_columns = {column["name"] for column in inspector.get_columns("retention_items")}
        if "original_video_created_at" not in retention_item_columns:
            connection.execute(text("ALTER TABLE retention_items ADD COLUMN original_video_created_at DATETIME"))
        if "file_size_bytes" not in retention_item_columns:
            connection.execute(text("ALTER TABLE retention_items ADD COLUMN file_size_bytes INTEGER DEFAULT 0"))
            connection.execute(text("UPDATE retention_items SET file_size_bytes = 0 WHERE file_size_bytes IS NULL"))
        if "file_fingerprint" not in retention_item_columns:
            connection.execute(text("ALTER TABLE retention_items ADD COLUMN file_fingerprint VARCHAR(128)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_file_fingerprint ON retention_items (file_fingerprint)"))
        retention_settings_columns = {column["name"] for column in inspector.get_columns("retention_settings")}
        if "auto_schedule_kind" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_schedule_kind VARCHAR(32) DEFAULT 'interval'"))
            connection.execute(text("UPDATE retention_settings SET auto_schedule_kind = 'interval' WHERE auto_schedule_kind IS NULL"))
        if "auto_interval_minutes" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_interval_minutes INTEGER DEFAULT 15"))
            connection.execute(text("UPDATE retention_settings SET auto_interval_minutes = 15 WHERE auto_interval_minutes IS NULL"))
        if "auto_time_hour" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_time_hour INTEGER DEFAULT 4"))
            connection.execute(text("UPDATE retention_settings SET auto_time_hour = 4 WHERE auto_time_hour IS NULL"))
        if "auto_time_minute" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_time_minute INTEGER DEFAULT 0"))
            connection.execute(text("UPDATE retention_settings SET auto_time_minute = 0 WHERE auto_time_minute IS NULL"))
        if "auto_weekday" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_weekday INTEGER DEFAULT 0"))
            connection.execute(text("UPDATE retention_settings SET auto_weekday = 0 WHERE auto_weekday IS NULL"))
        if "auto_timezone" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN auto_timezone VARCHAR(64) DEFAULT 'UTC'"))
            connection.execute(text("UPDATE retention_settings SET auto_timezone = 'UTC' WHERE auto_timezone IS NULL OR auto_timezone = ''"))
        if "last_auto_run_at" not in retention_settings_columns:
            connection.execute(text("ALTER TABLE retention_settings ADD COLUMN last_auto_run_at DATETIME"))

        retention_item_columns_by_name = {
            column["name"]: column for column in inspector.get_columns("retention_items")
        }
        if (
            retention_item_columns_by_name.get("video_id", {}).get("nullable") is False
            or retention_item_columns_by_name.get("video_file_id", {}).get("nullable") is False
        ):
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                text(
                    """
                    CREATE TABLE retention_items__new (
                        id INTEGER NOT NULL PRIMARY KEY,
                        video_id INTEGER,
                        video_file_id INTEGER,
                        original_absolute_path VARCHAR(2048) NOT NULL,
                        staged_absolute_path VARCHAR(2048) NOT NULL UNIQUE,
                        original_relative_path VARCHAR(2048),
                        original_video_created_at DATETIME,
                        file_size_bytes INTEGER DEFAULT 0,
                        file_fingerprint VARCHAR(128),
                        marked_at DATETIME NOT NULL,
                        delete_after_at DATETIME NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        run_token VARCHAR(64),
                        last_error VARCHAR(1024),
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        FOREIGN KEY(video_id) REFERENCES videos (id),
                        FOREIGN KEY(video_file_id) REFERENCES video_files (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO retention_items__new (
                        id,
                        video_id,
                        video_file_id,
                        original_absolute_path,
                        staged_absolute_path,
                        original_relative_path,
                        original_video_created_at,
                        file_size_bytes,
                        file_fingerprint,
                        marked_at,
                        delete_after_at,
                        status,
                        run_token,
                        last_error,
                        created_at,
                        updated_at
                    )
                    SELECT
                        id,
                        video_id,
                        video_file_id,
                        original_absolute_path,
                        staged_absolute_path,
                        original_relative_path,
                        original_video_created_at,
                        file_size_bytes,
                        file_fingerprint,
                        marked_at,
                        delete_after_at,
                        status,
                        run_token,
                        last_error,
                        created_at,
                        updated_at
                    FROM retention_items
                    """
                )
            )
            connection.execute(text("DROP TABLE retention_items"))
            connection.execute(text("ALTER TABLE retention_items__new RENAME TO retention_items"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_retention_items_video_file_id ON retention_items (video_file_id)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_retention_items_staged_absolute_path ON retention_items (staged_absolute_path)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_video_id ON retention_items (video_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_status ON retention_items (status)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_run_token ON retention_items (run_token)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_delete_after_at ON retention_items (delete_after_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_marked_at ON retention_items (marked_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_retention_items_file_fingerprint ON retention_items (file_fingerprint)"))
            connection.execute(text("PRAGMA foreign_keys=ON"))


def _ensure_admin_account(db: Session) -> None:
    admin = db.query(UserProfile).filter(func.lower(UserProfile.name) == DEFAULT_ADMIN_USERNAME).order_by(UserProfile.id.asc()).first()
    temporary_password: str | None = None

    if not admin:
        recovery_phrase = generate_recovery_phrase()
        temporary_password = generate_temporary_password()
        admin = UserProfile(
            name=DEFAULT_ADMIN_USERNAME,
            display_name="Admin",
            accent_color="#9dc4ff",
            password_hash=hash_password(temporary_password),
            avatar_url=DEFAULT_USER_AVATAR,
            is_admin=True,
            requires_admin_setup=True,
            recovery_phrase_hash=hash_recovery_phrase(recovery_phrase),
            recovery_phrase_pending=recovery_phrase,
        )
        db.add(admin)
        db.flush()
    else:
        admin.is_admin = True
        admin.display_name = admin.display_name or "Admin"
        admin.avatar_url = admin.avatar_url or DEFAULT_USER_AVATAR

        if not admin.recovery_phrase_hash or (admin.requires_admin_setup and not admin.recovery_phrase_pending):
            recovery_phrase = generate_recovery_phrase()
            admin.recovery_phrase_hash = hash_recovery_phrase(recovery_phrase)
            admin.recovery_phrase_pending = recovery_phrase
            admin.requires_admin_setup = True

        if admin.requires_admin_setup:
            temporary_password = generate_temporary_password()
            admin.password_hash = hash_password(temporary_password)
            for token in db.query(SessionToken).filter(SessionToken.user_id == admin.id).all():
                db.delete(token)

    if admin.requires_admin_setup and temporary_password:
        logger.warning(
            "Admin bootstrap active. Sign in with username '%s' and temporary password '%s', then finish setup in halcyon.",
            admin.name,
            temporary_password,
        )


def seed_defaults(db: Session, mounted_roots: list[str], *, include_demo_users: bool = False) -> None:
    _ensure_admin_account(db)

    defaults = [
        ("guest", "Guest", "#9aa6b5", "guest"),
    ]
    if include_demo_users:
        defaults.extend(
            [
                ("test", "Test", "#8aa5c4", "test"),
            ]
        )
    for username, display_name, accent_color, password in defaults:
        existing = db.query(UserProfile).filter(UserProfile.name == username).one_or_none()
        if not existing:
            db.add(
                UserProfile(
                    name=username,
                    display_name=display_name,
                    accent_color=accent_color,
                    password_hash=hash_password(password),
                    avatar_url=DEFAULT_USER_AVATAR,
                )
            )
        elif not existing.password_hash:
            existing.password_hash = hash_password(password)
        if existing and not existing.avatar_url:
            existing.avatar_url = DEFAULT_USER_AVATAR

    normalized_mounted_roots = {root_path.rstrip("\\/") for root_path in mounted_roots}
    existing_labels = {item for item in db.query(LibraryRoot.label).all()}

    def next_root_label(seed_index: int) -> str:
        index = seed_index
        while True:
            label = f"Library {index}"
            if (label,) not in existing_labels and label not in existing_labels:
                existing_labels.add(label)
                return label
            index += 1

    for index, root_path in enumerate(mounted_roots, start=1):
        existing = db.query(LibraryRoot).filter(LibraryRoot.path == root_path).one_or_none()
        if not existing:
            db.add(LibraryRoot(label=next_root_label(index), path=root_path, is_available=True))
        else:
            existing.is_available = True

    existing_roots = db.query(LibraryRoot).all()
    for root in existing_roots:
        normalized_root_path = root.path.rstrip("\\/")
        if normalized_root_path in normalized_mounted_roots:
            continue
        has_selected_folders = db.query(SelectedFolder.id).filter(SelectedFolder.root_id == root.id).first() is not None
        has_indexed_files = (
            db.query(VideoFile.id)
            .filter(VideoFile.absolute_path.like(f"{normalized_root_path}%"))
            .first()
            is not None
        )
        if has_selected_folders or has_indexed_files:
            root.is_available = False
            continue
        db.delete(root)

    if not db.query(SyncSettings).count():
        db.add(SyncSettings(automatic_detection_enabled=True, scan_interval_seconds=30, allow_fallback_art=False))
    if not db.query(RetentionSettings).count():
        db.add(
            RetentionSettings(
                enabled=False,
                retention_days=30,
                auto_timezone=server_timezone_name(),
            )
        )
    else:
        timezone_name = server_timezone_name()
        for retention_settings in db.query(RetentionSettings).all():
            retention_settings.auto_timezone = timezone_name

    for session_token in db.query(SessionToken).all():
        if not is_hashed_session_token(session_token.token):
            session_token.token = hash_session_token(session_token.token)

    db.commit()
