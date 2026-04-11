from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.init_db import init_db, seed_defaults
from app.db.session import SessionLocal
from app.services.background import current_scan_interval_seconds, run_background_cycle_once

logger = get_logger()


async def worker_loop() -> None:
    settings = get_settings()
    init_db()

    with SessionLocal() as db:
        seed_defaults(db, [str(path) for path in settings.mounted_roots])
    logger.info(
        "Background worker started mounted_roots=%s scan_interval_seconds=%s",
        [str(path) for path in settings.mounted_roots],
        current_scan_interval_seconds(settings),
    )

    while True:
        await run_background_cycle_once(settings)
        await asyncio.sleep(current_scan_interval_seconds(settings))


def main() -> None:
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
