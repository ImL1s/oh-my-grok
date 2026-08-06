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


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_post_spawn_reader_start_failure_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceptions after Popen (e.g. Thread.start) must still killpg."""
    import threading

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "hang_tree.py"
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
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

    real_start = threading.Thread.start
    calls = {"n": 0}

    def start_then_boom(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_start(self, *args, **kwargs)
        raise RuntimeError("reader start failed")

    monkeypatch.setattr(threading.Thread, "start", start_then_boom)
    with pytest.raises(RuntimeError, match="reader start failed"):
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
        pytest.fail("grandchild still alive after post-spawn setup failure")


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_post_spawn_buffer_setup_failure_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BaseException while allocating post-Popen buffers must still killpg."""
    from omg_cli.providers import process as process_mod

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "hang_tree.py"
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
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

    real_bytearray = process_mod._bytearray
    calls = {"n": 0}

    def boom_bytearray(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MemoryError("simulated OOM during buffer alloc")
        return real_bytearray(*args, **kwargs)

    monkeypatch.setattr(process_mod, "_bytearray", boom_bytearray)
    with pytest.raises(MemoryError, match="simulated OOM"):
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
        pytest.fail("grandchild still alive after post-spawn buffer setup failure")


def test_join_readers_hung_path_kills_tree_before_pipe_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung reader cleanup must _kill_tree before stop/pipe.close/join."""
    from omg_cli.providers import process as process_mod

    order: list[str] = []

    class FakePipe:
        def close(self) -> None:
            order.append("close")

    class FakeProc:
        def __init__(self) -> None:
            self.pid = 4242
            self.stdout = FakePipe()
            self.stderr = FakePipe()

    class FakeThread:
        def __init__(self, alive: bool) -> None:
            self._alive = alive

        def is_alive(self) -> bool:
            return self._alive

        def join(self, timeout=None) -> None:  # noqa: ARG002
            order.append("join")
            # Stay alive across the first join wave so hung path is taken;
            # clear only after killpg (second wave / post-kill join).
            if "kill" in order:
                self._alive = False

    proc = FakeProc()
    stop = threading.Event()

    def fake_kill(p) -> None:  # noqa: ARG001
        order.append("kill")

    monkeypatch.setattr(process_mod, "_kill_tree", fake_kill)
    process_mod._join_readers(
        [FakeThread(True), FakeThread(False)],
        stop=stop,
        stdout_early=[False],
        stderr_early=[False],
        proc=proc,  # type: ignore[arg-type]
    )
    assert "kill" in order
    assert order.index("kill") < order.index("close")
    assert stop.is_set()


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_cancel_during_join_readers_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_event set during reader join (after pre-join check) must still killpg."""
    from omg_cli.providers import process as process_mod

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "exit_leave_gc.py"
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
)
time.sleep(0.2)
""",
        encoding="utf-8",
    )

    cancel = threading.Event()
    real_join_readers = process_mod._join_readers
    kills_after_join: list[str] = []
    real_kill = process_mod._kill_tree
    join_done = {"v": False}

    def tracking_kill(proc):
        if join_done["v"]:
            kills_after_join.append("kill")
        return real_kill(proc)

    def join_sets_cancel(*args, **kwargs):
        # Simulate SIGINT arriving after the single pre-join cancel check.
        cancel.set()
        result = real_join_readers(*args, **kwargs)
        join_done["v"] = True
        return result

    monkeypatch.setattr(process_mod, "_kill_tree", tracking_kill)
    monkeypatch.setattr(process_mod, "_join_readers", join_sets_cancel)

    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        cancel_event=cancel,
    )
    assert result.cancelled is True
    assert kills_after_join, "expected killpg after late cancel during join"

    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail("grandchild still alive after late-cancel-during-join killpg")


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_cancel_during_wait_after_child_exit_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_event set after poll() exit must still killpg descendants."""
    import subprocess

    from omg_cli.providers import process as process_mod

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "exit_leave_gc.py"
    script.write_text(
        f"""\
import subprocess, sys, time
from pathlib import Path
subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
)
# Direct child exits quickly; grandchild remains in the session/process group.
time.sleep(0.2)
""",
        encoding="utf-8",
    )

    cancel = threading.Event()
    real_wait = subprocess.Popen.wait

    def wait_sets_cancel(self, *args, **kwargs):
        cancel.set()
        return real_wait(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", wait_sets_cancel)
    monkeypatch.setattr(process_mod.subprocess.Popen, "wait", wait_sets_cancel)

    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        cancel_event=cancel,
    )
    assert result.cancelled is True

    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail("grandchild still alive after post-exit cancel killpg")


def test_success_path_drains_stdout_past_chunk_without_silent_truncation(
    tmp_path: Path,
) -> None:
    """Natural exit must drain to EOF; no truncation flag when under max_bytes."""
    script = tmp_path / "spew_eof.py"
    payload = ("A" * 12_000) + "END_MARKER"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        max_output_bytes=64_000,
    )
    assert result.returncode == 0
    assert result.stdout_truncated is False
    assert result.overflow is False
    assert result.stdout.endswith("END_MARKER")
    assert len(result.stdout) == len(payload)


def test_drain_pipe_stop_before_eof_sets_truncation_flag() -> None:
    """Forced reader stop before EOF must set early-stop truncation (not silent)."""
    import os

    from omg_cli.providers.process import _drain_pipe

    r_fd, w_fd = os.pipe()
    os.write(w_fd, b"partial-output-still-open")
    stop = threading.Event()
    stop.set()
    sink = bytearray()
    overflow = [False]
    early = [False]
    pipe = os.fdopen(r_fd, "rb", buffering=0)
    try:
        _drain_pipe(
            pipe,
            max_bytes=10_000,
            sink=sink,
            overflow_flag=overflow,
            stop=stop,
            early_stop_flag=early,
        )
    finally:
        try:
            os.close(w_fd)
        except OSError:
            pass
    assert early[0] is True
    assert overflow[0] is False


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
