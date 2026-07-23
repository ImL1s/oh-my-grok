"""Notification configuration is strict, disabled by default, and read-only."""
from __future__ import annotations

import json
import os

import pytest

from omg_cli.notify.config import (
    NotificationConfigError,
    disabled_notification_config,
    load_notification_config,
    parse_notification_config,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes


def test_missing_notification_config_is_disabled_without_creating_files(tmp_path):
    config = load_notification_config(tmp_path / "notifications.json")
    assert config == disabled_notification_config()
    assert list(tmp_path.iterdir()) == []


def test_parse_notification_config_is_strict_and_bounded():
    config = parse_notification_config(
        {
            "store_kind": "omg_notification_config",
            "schema_version": 1,
            "enabled": True,
            "adapters": [
                {"adapter": "terminal", "enabled": False},
                {
                    "adapter": "https",
                    "enabled": True,
                    "url_env": "OMG_NOTIFY_URL",
                    "allowed_hosts": ["hooks.acme.example.net"],
                    "timeout_ms": 1000,
                    "header_env": {"authorization": "OMG_NOTIFY_AUTH"},
                },
            ],
        }
    )
    assert config["enabled"] is True
    assert [row["adapter"] for row in config["adapters"]] == ["terminal", "https"]

    with pytest.raises(NotificationConfigError, match="unknown"):
        parse_notification_config({**config, "unexpected": True})
    with pytest.raises(NotificationConfigError, match="duplicate"):
        parse_notification_config({**config, "adapters": [config["adapters"][0]] * 2})

    unsafe = dict(config["adapters"][1])
    unsafe.pop("header_env")
    unsafe["headers"] = {"authorization": "Bearer raw-secret"}
    with pytest.raises(NotificationConfigError, match="raw headers"):
        parse_notification_config({**config, "adapters": [unsafe]})


def test_load_notification_config_refuses_symlink_and_oversize(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(disabled_notification_config()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(NotificationConfigError, match="symlink"):
        load_notification_config(link)

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + b" " * 70_000 + b"}")
    huge.chmod(0o600)
    with pytest.raises(NotificationConfigError, match="byte"):
        load_notification_config(huge)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(NotificationConfigError, match="regular"):
        load_notification_config(directory)


def test_load_requires_current_owner_exact_mode_and_canonical_json(tmp_path, monkeypatch):
    path = tmp_path / "notifications.json"
    body = canonical_json_bytes(disabled_notification_config())
    path.write_bytes(body)
    path.chmod(0o600)
    assert load_notification_config(path) == disabled_notification_config()

    path.chmod(0o640)
    with pytest.raises(NotificationConfigError, match="0600"):
        load_notification_config(path)
    path.chmod(0o600)
    path.write_text(json.dumps(disabled_notification_config(), indent=2))
    with pytest.raises(NotificationConfigError, match="canonical"):
        load_notification_config(path)

    path.write_bytes(body)
    path.chmod(0o600)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(NotificationConfigError, match="owned"):
        load_notification_config(path)


def test_persisted_https_secrets_require_environment_reference():
    for url in (
        "http://hooks.acme.example.net/omg",
        "https://127.0.0.1/omg",
        "https://hooks.acme.example.net/secret-webhook",
    ):
        with pytest.raises(NotificationConfigError, match="raw webhook"):
            parse_notification_config(
                {
                    "store_kind": "omg_notification_config",
                    "schema_version": 1,
                    "enabled": True,
                    "adapters": [
                        {
                            "adapter": "https",
                            "enabled": True,
                            "url": url,
                            "allowed_hosts": ["hooks.acme.example.net"],
                            "timeout_ms": 1000,
                            "header_env": {},
                        }
                    ],
                }
            )


def test_config_supports_argv_only_command_and_fixed_desktop():
    parsed = parse_notification_config(
        {
            "store_kind": "omg_notification_config",
            "schema_version": 1,
            "enabled": True,
            "adapters": [
                {
                    "adapter": "command",
                    "enabled": True,
                    "argv": ["/usr/bin/logger", "omg"],
                    "allowed_executables": ["/usr/bin/logger"],
                    "timeout_ms": 500,
                },
                {
                    "adapter": "desktop",
                    "enabled": False,
                    "platform": "macos",
                    "timeout_ms": 500,
                },
            ],
        }
    )
    assert [row["adapter"] for row in parsed["adapters"]] == ["command", "desktop"]
    with pytest.raises(NotificationConfigError, match="allowlisted"):
        parse_notification_config(
            {
                "store_kind": "omg_notification_config",
                "schema_version": 1,
                "enabled": True,
                "adapters": [
                    {
                        "adapter": "command",
                        "enabled": True,
                        "argv": ["/bin/echo", "hello"],
                        "allowed_executables": ["/usr/bin/logger"],
                        "timeout_ms": 500,
                    }
                ],
            }
        )
