from __future__ import annotations

import json
from pathlib import Path

from app.services import app_update


def test_compare_versions_handles_build_suffixes() -> None:
    assert app_update.compare_versions("1.1.26-48", "1.1.26-49") < 0
    assert app_update.compare_versions("1.2.0", "1.1.99-999") > 0
    assert app_update.compare_versions("1.1.26-48", "1.1.26-48") == 0


def test_build_update_status_reports_available_update(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "halcyon-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48",
                "repository_url": "https://github.com/awpsec/halcyon",
                "manifest_url": "https://example.invalid/halcyon-release.json",
                "update_command": "halcyon update",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_update, "RELEASE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        app_update,
        "fetch_remote_release_manifest",
        lambda force=False: {
            "version": "1.1.27-1",
            "repository_url": "https://github.com/awpsec/halcyon",
            "update_command": "halcyon update",
        },
    )

    status = app_update.build_update_status(force=True)

    assert status["current_version"] == "1.1.26-48"
    assert status["latest_version"] == "1.1.27-1"
    assert status["update_available"] is True
    assert status["update_command"] == "halcyon update"
    assert status["error"] is None


def test_build_update_status_handles_unreachable_update_server(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "halcyon-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48",
                "repository_url": "https://github.com/awpsec/halcyon",
                "manifest_url": "https://example.invalid/halcyon-release.json",
                "update_command": "halcyon update",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_update, "RELEASE_MANIFEST_PATH", manifest_path)

    def fail_fetch(force: bool = False):
        raise OSError("offline")

    monkeypatch.setattr(app_update, "fetch_remote_release_manifest", fail_fetch)

    status = app_update.build_update_status(force=True)

    assert status["current_version"] == "1.1.26-48"
    assert status["latest_version"] == "1.1.26-48"
    assert status["update_available"] is False
    assert status["error"] == "Unable to reach the update server right now."
