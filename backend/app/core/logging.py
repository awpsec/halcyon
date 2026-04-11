from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import get_settings


def _log_path() -> Path:
    settings = get_settings()
    return settings.config_dir / "halcyon.log"


def setup_logging() -> None:
    logger = logging.getLogger("halcyon")
    if logger.handlers:
        return

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_formatter = logging.Formatter("%(levelname)s %(message)s")
    handler = RotatingFileHandler(log_path, maxBytes=1_500_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(file_formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(stream_formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    logger.info("halcyon logging initialized")


def get_logger(name: str = "halcyon") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def read_log_lines(limit: int = 20) -> list[str]:
    log_path = _log_path()
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return lines[-limit:]
