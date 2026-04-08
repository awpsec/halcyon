from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services import app_update


def test_compare_versions_handles_build_suffixes() -> None:
    assert app_update.compare_versions("1.1.26-48.003", "1.1.26-48.004") < 0
    assert app_update.compare_versions("1.2.0", "1.1.99-999") > 0
    assert app_update.compare_versions("1.1.26-48.003", "1.1.26-48.003") == 0


def test_build_update_status_reports_available_update(tmp_path: Path, monkeypatch) -> None:
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / ".git").mkdir()
    manifest_path = install_root / "halcyon-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48.003",
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
        "get_settings",
        lambda: SimpleNamespace(
            repository_url="https://github.com/awpsec/halcyon",
            update_manifest_url="https://example.invalid/halcyon-release.json",
            config_dir=tmp_path / "config",
        ),
    )
    monkeypatch.setattr(
        app_update,
        "fetch_remote_release_manifest",
        lambda force=False: {
            "version": "1.1.26-48.004",
            "repository_url": "https://github.com/awpsec/halcyon",
            "update_command": "halcyon update",
        },
    )

    status = app_update.build_update_status(force=True)

    assert status["current_version"] == "1.1.26-48.003"
    assert status["latest_version"] == "1.1.26-48.004"
    assert status["update_available"] is True
    assert status["update_command"] == app_update.MANUAL_GIT_UPDATE_COMMAND
    assert status["error"] is None


def test_build_update_status_handles_unreachable_update_server(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "halcyon-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48.003",
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
        "get_settings",
        lambda: SimpleNamespace(
            repository_url="https://github.com/awpsec/halcyon",
            update_manifest_url="https://example.invalid/halcyon-release.json",
            config_dir=tmp_path / "config",
        ),
    )

    def fail_fetch(force: bool = False):
        raise OSError("offline")

    monkeypatch.setattr(app_update, "fetch_remote_release_manifest", fail_fetch)

    status = app_update.build_update_status(force=True)

    assert status["current_version"] == "1.1.26-48.003"
    assert status["latest_version"] == "1.1.26-48.003"
    assert status["update_available"] is False
    assert status["error"] == "Unable to reach the update server right now."


def test_build_update_status_prefers_bootstrapped_cli_command(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "halcyon-release.json"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / app_update.CLI_BOOTSTRAP_MARKER_NAME).write_text("{}", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48.003",
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
        "get_settings",
        lambda: SimpleNamespace(
            repository_url="https://github.com/awpsec/halcyon",
            update_manifest_url="https://example.invalid/halcyon-release.json",
            config_dir=config_dir,
        ),
    )
    monkeypatch.setattr(
        app_update,
        "fetch_remote_release_manifest",
        lambda force=False: {
            "version": "1.1.26-48.004",
            "repository_url": "https://github.com/awpsec/halcyon",
            "update_command": "halcyon update",
        },
    )

    status = app_update.build_update_status(force=True)

    assert status["update_command"] == "halcyon update"


def test_build_update_status_reports_release_package_fallback_without_git_clone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "halcyon-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "halcyon",
                "version": "1.1.26-48.003",
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
        "get_settings",
        lambda: SimpleNamespace(
            repository_url="https://github.com/awpsec/halcyon",
            update_manifest_url="https://example.invalid/halcyon-release.json",
            config_dir=tmp_path / "config",
        ),
    )
    monkeypatch.setattr(
        app_update,
        "fetch_remote_release_manifest",
        lambda force=False: {
            "version": "1.1.26-48.004",
            "repository_url": "https://github.com/awpsec/halcyon",
            "update_command": "halcyon update",
        },
    )

    status = app_update.build_update_status(force=True)

    assert status["update_command"].startswith("Download the newest halcyon release")
