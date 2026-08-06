"""Provider probe process-group / overflow / cancel contracts (#67-A)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from omg_cli.providers.process import (
    ProbeProcessError,
    run_probe_process,
)


def test_run_probe_rejects_shell_unsafe_argv() -> None:
    with pytest.raises(ProbeProcessError):
        run_probe_process([])
    with pytest.raises(ProbeProcessError):
        run_probe_process(["ok", ""])
    with pytest.raises(ProbeProcessError):
        run_probe_process(["ok", "a\x00b"])
    with pytest.raises(ProbeProcessError):
        run_probe_process([1, "x"])  # type: ignore[list-item]


def test_run_probe_uses_list_argv_shell_false_and_posix_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from omg_cli.providers import process as process_mod

    captured: dict = {}
    real_popen = subprocess.Popen

    class TrackingPopen(real_popen):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", TrackingPopen)
    monkeypatch.setattr(process_mod.subprocess, "Popen", TrackingPopen)

    result = run_probe_process(
        [sys.executable, "-c", "print('hi')"],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
    )
    assert result.returncode == 0
    assert "hi" in result.stdout
    assert captured["kwargs"].get("shell") is False
    argv = captured["kwargs"].get("args") or captured["args"][0]
    assert isinstance(argv, list)
    if os.name == "posix":
        assert captured["kwargs"].get("start_new_session") is True


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "hang_tree.py"
    # Child stays in the probe session; grandchild shares the same process group
    # (no start_new_session) so killpg must reap the tree — not only the direct child.
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
gc = subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
)
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    t0 = time.monotonic()
    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=0.4,
    )
    elapsed = time.monotonic() - t0
    assert result.timed_out is True
    assert elapsed < 3.0
    # Grandchild must not keep rewriting the marker after killpg.
    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail("grandchild still alive after timeout killpg")


@pytest.mark.skipif(os.name != "posix", reason="cancel killpg is POSIX-only")
def test_cancel_kills_process_group(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text(
        "import time\nwhile True:\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    cancel = threading.Event()

    def arm() -> None:
        time.sleep(0.2)
        cancel.set()

    threading.Thread(target=arm, daemon=True).start()
    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        cancel_event=cancel,
    )
    assert result.cancelled is True
    assert result.timed_out is False


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_keyboard_interrupt_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BaseException unwind must killpg before joining pipe readers."""
    import subprocess

    from omg_cli.providers import process as process_mod

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "hang_tree.py"
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
)
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )

    real_poll = subprocess.Popen.poll
    armed = {"n": 0}

    def poll_then_interrupt(self, *args, **kwargs):
        armed["n"] += 1
        if armed["n"] >= 3:
            raise KeyboardInterrupt()
        return real_poll(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "poll", poll_then_interrupt)
    monkeypatch.setattr(process_mod.subprocess.Popen, "poll", poll_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_probe_process(
            [sys.executable, str(script)],
            env={"PATH": os.environ.get("PATH", "")},
            timeout_s=5.0,
        )

    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail("grandchild still alive after KeyboardInterrupt killpg")


def test_antigravity_probes_pass_cancel_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """version/help probes must wire cancel_event into run_probe_process."""
    from omg_cli.providers import antigravity as agy_mod
    from omg_cli.providers.process import ProbeProcessResult

    seen: list[object] = []
    help_text = (
        Path(__file__).resolve().parent / "fixtures" / "antigravity" / "help.txt"
    ).read_text(encoding="utf-8")

    def fake_run(argv, **kwargs):
        seen.append(kwargs.get("cancel_event"))
        text = help_text if "--help" in argv else "1.1.10\n"
        return ProbeProcessResult(
            argv=tuple(argv),
            returncode=0,
            stdout=text,
            stderr="",
        )

    monkeypatch.setattr(agy_mod, "run_probe_process", fake_run)
    monkeypatch.setattr(agy_mod, "discover_binary", lambda: "/tmp/fake-agy")
    agy_mod.probe_version("/tmp/fake-agy")
    agy_mod._probe_help_text("/tmp/fake-agy")
    assert len(seen) == 2
    assert all(isinstance(ev, threading.Event) for ev in seen)


def test_overflow_stops_process_and_flags_result(tmp_path: Path) -> None:
    script = tmp_path / "spew.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('x' * 2_000_000)\n"
        "sys.stdout.flush()\n"
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        max_output_bytes=8_192,
    )
    assert result.overflow is True
    assert result.bytes_stdout <= 8_192
    assert result.stdout_truncated is True
    assert len(result.stdout.encode("utf-8", errors="replace")) <= 8_192


def test_secret_parent_env_not_inherited_by_default() -> None:
    result = run_probe_process(
        [
            sys.executable,
            "-c",
            "import os,sys; sys.exit(0 if 'SUPER_SECRET_TOKEN' not in os.environ else 1)",
        ],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
    )
    # Parent may have the secret; child env is only what we pass.
    os.environ["SUPER_SECRET_TOKEN"] = "leak-me"
    try:
        result = run_probe_process(
            [
                sys.executable,
                "-c",
                "import os,sys; sys.exit(0 if 'SUPER_SECRET_TOKEN' not in os.environ else 1)",
            ],
            env={"PATH": os.environ.get("PATH", "")},
            timeout_s=5.0,
        )
        assert result.returncode == 0
    finally:
        os.environ.pop("SUPER_SECRET_TOKEN", None)
