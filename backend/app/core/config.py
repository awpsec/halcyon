from functools import lru_cache
from pathlib import Path
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


LEGACY_ENV_ALIASES = {
    "WAYTUBE_CONFIG_DIR": "HALCYON_CONFIG_DIR",
    "WAYTUBE_CACHE_DIR": "HALCYON_CACHE_DIR",
    "WAYTUBE_MOUNTED_ROOTS": "HALCYON_MOUNTED_ROOTS",
    "WAYTUBE_DATABASE_URL": "HALCYON_DATABASE_URL",
    "WAYTUBE_YOUTUBE_API_KEY": "HALCYON_YOUTUBE_API_KEY",
    "WAYTUBE_FFMPEG_BIN_DIR": "HALCYON_FFMPEG_BIN_DIR",
    "WAYTUBE_SESSION_COOKIE_NAME": "HALCYON_SESSION_COOKIE_NAME",
    "WAYTUBE_SCAN_INTERVAL_SECONDS": "HALCYON_SCAN_INTERVAL_SECONDS",
    "WAYTUBE_BACKGROUND_TASKS_ENABLED": "HALCYON_BACKGROUND_TASKS_ENABLED",
    "WAYTUBE_TRANSCODE_CACHE_LIMIT_MB": "HALCYON_TRANSCODE_CACHE_LIMIT_MB",
    "WAYTUBE_ALLOW_ORIGIN": "HALCYON_ALLOW_ORIGIN",
    "WAYTUBE_POSTGRES_HOST": "HALCYON_POSTGRES_HOST",
}


def _promote_legacy_environment() -> None:
    for legacy_name, current_name in LEGACY_ENV_ALIASES.items():
        legacy_value = os.getenv(legacy_name)
        if legacy_value is not None and os.getenv(current_name) is None:
            os.environ[current_name] = legacy_value


class Settings(BaseSettings):
    app_name: str = "halcyon"
    api_prefix: str = "/api"
    config_dir: Path = Field(default=Path("/config"))
    cache_dir: Path = Field(default=Path("/cache"))
    mounted_roots: list[Path] = Field(default_factory=lambda: [Path("/library")])
    database_url: str | None = None
    youtube_api_key: str | None = None
    ffmpeg_bin_dir: Path | None = None
    session_cookie_name: str = "halcyon_session"
    scan_interval_seconds: int = 30
    background_tasks_enabled: bool = True
    transcode_cache_limit_mb: int = 20480
    allow_origin: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="HALCYON_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        default_host = "postgres" if Path("/.dockerenv").exists() else os.getenv("HALCYON_POSTGRES_HOST", "127.0.0.1")
        return f"postgresql+psycopg://halcyon:halcyon@{default_host}:5432/halcyon"


@lru_cache
def get_settings() -> Settings:
    _promote_legacy_environment()
    settings = Settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    for root in settings.mounted_roots:
        root.mkdir(parents=True, exist_ok=True)
    return settings
