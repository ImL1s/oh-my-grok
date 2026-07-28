"""Issue #19: --safe / --yolo ordering-independent and mutually exclusive."""
from __future__ import annotations

import pytest

from omg_cli.main import apply_safe_yolo_flags, build_parser, main


def _parse(argv: list[str]):
    parser = build_parser()
    args = parser.parse_args(argv)
    return apply_safe_yolo_flags(parser, args)


def test_yolo_before_subcommand_not_clobbered():
    args = _parse(["--yolo", "doctor"])
    assert args.yolo is True
    assert args.safe is False


def test_yolo_after_subcommand():
    args = _parse(["doctor", "--yolo"])
    assert args.yolo is True
    assert args.safe is False


def test_safe_before_subcommand_not_clobbered():
    args = _parse(["--safe", "doctor"])
    assert args.safe is True
    assert args.yolo is False


def test_safe_after_subcommand():
    args = _parse(["doctor", "--safe"])
    assert args.safe is True
    assert args.yolo is False


def test_neither_flag_defaults_false():
    args = _parse(["doctor"])
    assert args.safe is False
    assert args.yolo is False


def test_yolo_on_ulw_before_and_after_goal_position():
    """Mode subcommands: flag before subcommand and after both stick."""
    before = _parse(["--yolo", "ulw", "ship it", "--dry-run"])
    assert before.yolo is True
    assert before.safe is False
    assert before.command == "ulw"

    after = _parse(["ulw", "--yolo", "ship it", "--dry-run"])
    assert after.yolo is True
    assert after.safe is False


def test_safe_on_ulw_before_and_after():
    before = _parse(["--safe", "ulw", "goal", "--dry-run"])
    assert before.safe is True
    after = _parse(["ulw", "--safe", "goal", "--dry-run"])
    assert after.safe is True


def test_nested_team_flag_positions():
    """Nested parents=[common] (team → status) must not clobber outer flags."""
    before = _parse(["--yolo", "team", "status"])
    assert before.yolo is True
    assert before.safe is False

    mid = _parse(["team", "--yolo", "status"])
    assert mid.yolo is True

    after = _parse(["team", "status", "--yolo"])
    assert after.yolo is True

    safe_before = _parse(["--safe", "team", "status"])
    assert safe_before.safe is True
    assert safe_before.yolo is False


def test_safe_and_yolo_together_on_subcommand_is_usage_error():
    parser = build_parser()
    args = parser.parse_args(["ulw", "--safe", "--yolo", "x"])
    with pytest.raises(SystemExit) as ei:
        apply_safe_yolo_flags(parser, args)
    assert ei.value.code == 2


def test_safe_and_yolo_split_across_root_and_subcommand():
    parser = build_parser()
    args = parser.parse_args(["--safe", "ulw", "--yolo", "x"])
    with pytest.raises(SystemExit) as ei:
        apply_safe_yolo_flags(parser, args)
    assert ei.value.code == 2


def test_safe_and_yolo_both_on_root():
    parser = build_parser()
    args = parser.parse_args(["--safe", "--yolo", "doctor"])
    with pytest.raises(SystemExit) as ei:
        apply_safe_yolo_flags(parser, args)
    assert ei.value.code == 2


def test_main_contradiction_exit_2_and_message(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["ulw", "--safe", "--yolo", "nope"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "--safe" in err and "--yolo" in err
    assert "mutually exclusive" in err.lower()


def test_main_yolo_before_reaches_handler(monkeypatch):
    seen: dict[str, bool] = {}

    def fake_doctor(args):
        seen["yolo"] = bool(args.yolo)
        seen["safe"] = bool(args.safe)
        return 0

    monkeypatch.setattr("omg_cli.main.cmd_doctor", fake_doctor)
    # Re-bind func on a fresh parse path: main() uses set_defaults(func=cmd_doctor)
    # at build time, so patch the function object the parser already closed over.
    from omg_cli import main as main_mod

    parser = main_mod.build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "doctor" in choices:
            choices["doctor"].set_defaults(func=fake_doctor)

    monkeypatch.setattr(main_mod, "build_parser", lambda: parser)
    assert main_mod.main(["--yolo", "doctor"]) == 0
    assert seen == {"yolo": True, "safe": False}


def test_main_yolo_after_reaches_handler(monkeypatch):
    seen: dict[str, bool] = {}

    def fake_doctor(args):
        seen["yolo"] = bool(args.yolo)
        seen["safe"] = bool(args.safe)
        return 0

    from omg_cli import main as main_mod

    parser = main_mod.build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "doctor" in choices:
            choices["doctor"].set_defaults(func=fake_doctor)

    monkeypatch.setattr(main_mod, "build_parser", lambda: parser)
    assert main_mod.main(["doctor", "--yolo"]) == 0
    assert seen == {"yolo": True, "safe": False}


def test_main_safe_before_reaches_handler(monkeypatch):
    seen: dict[str, bool] = {}

    def fake_doctor(args):
        seen["yolo"] = bool(args.yolo)
        seen["safe"] = bool(args.safe)
        return 0

    from omg_cli import main as main_mod

    parser = main_mod.build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "doctor" in choices:
            choices["doctor"].set_defaults(func=fake_doctor)

    monkeypatch.setattr(main_mod, "build_parser", lambda: parser)
    assert main_mod.main(["--safe", "doctor"]) == 0
    assert seen == {"yolo": False, "safe": True}
