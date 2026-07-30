"""Team plane gate: default-on + kill switch."""

from __future__ import annotations

from omg_cli.team.plane import DISABLE_ENV, EXPERIMENTAL_ENV, experimental_enabled


def test_team_plane_default_on() -> None:
    assert experimental_enabled({}) is True
    assert experimental_enabled({EXPERIMENTAL_ENV: ""}) is True


def test_team_plane_kill_switch() -> None:
    assert experimental_enabled({DISABLE_ENV: "1"}) is False
    assert experimental_enabled({DISABLE_ENV: "true"}) is False


def test_team_plane_legacy_explicit_off() -> None:
    assert experimental_enabled({EXPERIMENTAL_ENV: "0"}) is False
    assert experimental_enabled({EXPERIMENTAL_ENV: "false"}) is False


def test_team_plane_legacy_explicit_on() -> None:
    assert experimental_enabled({EXPERIMENTAL_ENV: "1"}) is True
