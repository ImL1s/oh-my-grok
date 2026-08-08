"""Provider probe process-group / overflow / cancel contracts (#67-A)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from omg_cli.providers.process import (
    ProbeProcessError,
    run_probe_process,
    run_provider_process,
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


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_post_spawn_early_window_oom_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OOM in the earliest post-Popen setup (before buffer alloc) must still killpg."""
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

    def boom_early() -> None:
        raise MemoryError("simulated OOM in earliest post-Popen window")

    monkeypatch.setattr(process_mod, "_post_popen_begin", boom_early)
    with pytest.raises(MemoryError, match="earliest post-Popen"):
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
        pytest.fail("grandchild still alive after earliest post-Popen OOM")


def test_popen_wrapper_shares_kill_on_baseexception_try_with_post_spawn() -> None:
    """The bound Popen wrapper must live in the BaseException cleanup region.

    A separate OSError-only try around the wrapper leaves an async-exception
    window between successful spawn and the kill-on-BaseException region.
    """
    import ast
    import inspect
    import textwrap

    from omg_cli.providers import process as process_mod

    src = textwrap.dedent(inspect.getsource(process_mod.run_provider_process))
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_provider_process"
    )

    def _calls_name(node: ast.AST, name: str) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == name:
                    return True
                if isinstance(func, ast.Name) and func.id == name:
                    return True
        return False

    def _is_kill_on_baseexception(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return _calls_name(handler, "_kill_tree")
        if isinstance(handler.type, ast.Name) and handler.type.id == "BaseException":
            return _calls_name(handler, "_kill_tree")
        return False

    found_unified = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if not any(_is_kill_on_baseexception(h) for h in node.handlers):
            continue
        if _calls_name(node, "_popen_bound"):
            found_unified = True
            break
    assert found_unified, (
        "_popen_bound must sit inside the BaseException+_kill_tree try "
        "(not in a preceding OSError-only try)"
    )


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_popen_bound_kills_tree_if_exception_before_box_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception between Popen return and proc_box write must still reap."""
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

    def raise_before_box() -> None:
        raise KeyboardInterrupt("between Popen and proc_box")

    monkeypatch.setattr(process_mod, "_after_popen_before_box", raise_before_box)
    try:
        with pytest.raises(KeyboardInterrupt, match="between Popen and proc_box"):
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
            pytest.fail("grandchild still alive after pre-box Popen exception")
    finally:
        pass


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_popen_post_spawn_exception_kills_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Popen wrapper raising after spawn must not orphan the process group."""
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
    spawned = []

    def spawn_then_raise(proc_box, popen_kwargs):
        proc = process_mod.subprocess.Popen(**popen_kwargs)
        spawned.append(proc)
        proc_box[0] = proc
        raise MemoryError("simulated post-spawn Popen failure")

    monkeypatch.setattr(process_mod, "_popen_bound", spawn_then_raise)
    try:
        with pytest.raises(MemoryError, match="post-spawn Popen"):
            run_probe_process(
                [sys.executable, str(script)],
                env={"PATH": os.environ.get("PATH", "")},
                timeout_s=5.0,
            )

        assert len(spawned) == 1
        deadline = time.monotonic() + 2.0
        last = marker.read_text(encoding="utf-8") if marker.exists() else ""
        while time.monotonic() < deadline:
            time.sleep(0.2)
            now = marker.read_text(encoding="utf-8") if marker.exists() else ""
            if now == last:
                break
            last = now
        else:
            pytest.fail("grandchild still alive after post-spawn Popen failure")
    finally:
        if spawned:
            process_mod._kill_tree(spawned[0])


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
def test_cancel_during_result_construction_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_event set while building ProbeProcessResult must still killpg."""
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
    real_result = process_mod.ProbeProcessResult
    kills: list[int] = []
    real_kill = process_mod._kill_tree

    def tracking_kill(proc) -> None:
        kills.append(getattr(proc, "pid", -1) or -1)
        real_kill(proc)

    def result_sets_cancel(*args, **kwargs):
        # Simulate SIGINT during decode / result construction (after the
        # final pre-return cancel check in older layouts).
        cancel.set()
        return real_result(*args, **kwargs)

    monkeypatch.setattr(process_mod, "ProbeProcessResult", result_sets_cancel)
    monkeypatch.setattr(process_mod, "_kill_tree", tracking_kill)

    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        cancel_event=cancel,
    )
    assert result.cancelled is True
    assert kills, "expected killpg after cancel during result construction"

    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail("grandchild still alive after cancel-during-result-construction")


def test_antigravity_cancel_after_probe_kills_before_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_probe_argv must killpg before KeyboardInterrupt when cancel is set."""
    from omg_cli.providers import antigravity as agy_mod
    from omg_cli.providers.process import ProbeProcessResult

    killed: list[int] = []

    def fake_run(argv, **kwargs):  # noqa: ARG001
        ev = kwargs.get("cancel_event")
        assert ev is not None
        # Child already exited; cancel flips only after run_probe_process returns.
        ev.set()
        return ProbeProcessResult(
            argv=tuple(argv),
            returncode=0,
            stdout="1.1.10\n",
            stderr="",
            pid=424242,
        )

    def fake_killpg(pid, sig):  # noqa: ARG001
        killed.append(pid)

    monkeypatch.setattr(agy_mod, "run_probe_process", fake_run)
    monkeypatch.setattr(agy_mod.os, "killpg", fake_killpg)
    with pytest.raises(KeyboardInterrupt):
        agy_mod._run_probe_argv(["/tmp/fake-agy", "--version"])
    assert killed == [424242], "must killpg before raising KeyboardInterrupt"


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


def test_join_readers_blocking_pipe_close_does_not_hang_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escaped helper holding a pipe must not block cancel forever on close()."""
    from omg_cli.providers import process as process_mod

    order: list[str] = []
    close_entered = threading.Event()

    class BlockingPipe:
        def close(self) -> None:
            order.append("close")
            close_entered.set()
            # Simulate buffered-I/O lock held by a hung reader / escaped helper.
            time.sleep(60.0)

    class FakeProc:
        def __init__(self) -> None:
            self.pid = 4242
            self.stdout = BlockingPipe()
            self.stderr = BlockingPipe()

    class FakeThread:
        def __init__(self) -> None:
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def join(self, timeout=None) -> None:  # noqa: ARG002
            order.append("join")
            if "kill" in order:
                self._alive = False

    def fake_kill(p) -> None:  # noqa: ARG001
        order.append("kill")

    monkeypatch.setattr(process_mod, "_kill_tree", fake_kill)
    # Keep the bounded-close timeout short so the test stays hermetic/fast.
    monkeypatch.setattr(process_mod, "_PIPE_CLOSE_TIMEOUT_S", 0.15)

    t0 = time.monotonic()
    process_mod._join_readers(
        [FakeThread(), FakeThread()],
        stop=threading.Event(),
        stdout_early=[False],
        stderr_early=[False],
        proc=FakeProc(),  # type: ignore[arg-type]
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"_join_readers blocked too long ({elapsed:.2f}s)"
    assert "kill" in order
    assert order.index("kill") < order.index("close")
    assert close_entered.wait(timeout=0.5)


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
    read_error = [False]
    pipe = os.fdopen(r_fd, "rb", buffering=0)
    try:
        _drain_pipe(
            pipe,
            max_bytes=10_000,
            sink=sink,
            overflow_flag=overflow,
            stop=stop,
            early_stop_flag=early,
            read_error_flag=read_error,
        )
    finally:
        try:
            os.close(w_fd)
        except OSError:
            pass
    assert early[0] is True
    assert overflow[0] is False
    assert read_error[0] is False


@pytest.mark.parametrize("error_type", (OSError, MemoryError))
def test_drain_pipe_read_error_marks_partial_output_unusable(
    error_type: type[BaseException],
) -> None:
    """Any reader failure after partial output is neither EOF nor evidence."""
    from omg_cli.providers.process import _drain_pipe

    class ErrorPipe:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, size: int) -> bytes:  # noqa: ARG002
            self.reads += 1
            if self.reads == 1:
                return b"Usage of agy:\n"
            raise error_type("simulated pipe failure")

        def close(self) -> None:
            return None

    sink = bytearray()
    overflow = [False]
    early = [False]
    read_error = [False]
    _drain_pipe(
        ErrorPipe(),
        max_bytes=10_000,
        sink=sink,
        overflow_flag=overflow,
        stop=threading.Event(),
        early_stop_flag=early,
        read_error_flag=read_error,
    )
    assert bytes(sink) == b"Usage of agy:\n"
    assert overflow[0] is False
    assert early[0] is True
    assert read_error[0] is True


def test_bounded_pipe_close_reraises_interrupt_from_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup owner must receive join interrupts and kill the process tree."""
    from omg_cli.providers import process as process_mod

    class Pipe:
        def close(self) -> None:
            return None

    def interrupt_join(self, timeout=None):  # noqa: ARG001
        raise KeyboardInterrupt()

    monkeypatch.setattr(threading.Thread, "join", interrupt_join)
    with pytest.raises(KeyboardInterrupt):
        process_mod._close_pipe_bounded(Pipe(), timeout_s=0.1)


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


@pytest.mark.skipif(os.name != "posix", reason="process-group killpg is POSIX-only")
@pytest.mark.parametrize("late_flag", ("overflow", "read_error"))
def test_late_reader_failure_after_child_exit_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_flag: str
) -> None:
    """A reader flag published after child exit must still kill descendants."""
    from omg_cli.providers import process as process_mod

    marker = tmp_path / "grandchild.alive"
    script = tmp_path / "exit_leave_detached_stdio_gc.py"
    script.write_text(
        f"""\
import subprocess, sys
from pathlib import Path
marker = Path({str(marker)!r})
subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print("direct child exits before reader publishes")
""",
        encoding="utf-8",
    )

    real_drain = process_mod._drain_pipe

    def delayed_failure(*args, **kwargs) -> None:
        real_drain(*args, **kwargs)
        # The direct child has exited and closed its pipes.  Publish the reader
        # status only while cleanup is joining readers.
        time.sleep(0.05)
        if late_flag == "overflow":
            kwargs["overflow_flag"][0] = True
        else:
            kwargs["read_error_flag"][0] = True
            kwargs["early_stop_flag"][0] = True

    monkeypatch.setattr(process_mod, "_drain_pipe", delayed_failure)
    result = run_probe_process(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
    )
    assert result.overflow is (late_flag == "overflow")
    assert result.stdout_truncated is True
    assert result.stdout_read_error is (late_flag == "read_error")

    deadline = time.monotonic() + 2.0
    last = marker.read_text(encoding="utf-8") if marker.exists() else ""
    while time.monotonic() < deadline:
        time.sleep(0.2)
        now = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if now == last:
            break
        last = now
    else:
        pytest.fail(f"grandchild still alive after late {late_flag} killpg")


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


def test_on_process_started_runs_once_after_successful_popen() -> None:
    seen: list[int] = []

    def _obs(proc) -> None:
        seen.append(int(proc.pid))

    result = run_provider_process(
        [sys.executable, "-c", "print('hi')"],
        env={"PATH": os.environ.get("PATH", "")},
        timeout_s=5.0,
        on_process_started=_obs,
    )
    assert result.returncode == 0
    assert len(seen) == 1
    assert seen[0] > 0


def test_on_process_started_runs_inside_kill_on_exception_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observer exception must kill+reap the child (no orphan)."""

    alive_pids: list[int] = []

    def _obs(proc) -> None:
        alive_pids.append(int(proc.pid))
        raise RuntimeError("bind failed")

    with pytest.raises(RuntimeError, match="bind failed"):
        run_provider_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={"PATH": os.environ.get("PATH", "")},
            timeout_s=10.0,
            on_process_started=_obs,
        )
    assert alive_pids
    pid = alive_pids[0]
    # Child must be gone after observer failure cleanup.
    deadline = time.monotonic() + 2.0
    still = True
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            still = True
        except ProcessLookupError:
            still = False
            break
        except OSError:
            still = False
            break
        time.sleep(0.05)
    assert still is False


def test_on_process_started_exception_kills_and_reaps_child_group() -> None:
    seen: list[int] = []

    def _obs(proc) -> None:
        seen.append(int(proc.pid))
        raise RuntimeError("no bind")

    with pytest.raises(RuntimeError, match="no bind"):
        run_provider_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={"PATH": os.environ.get("PATH", "")},
            timeout_s=10.0,
            on_process_started=_obs,
        )
    assert seen
    pid = seen[0]
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.05)
    pytest.fail(f"child pid {pid} still alive after observer exception")


def test_on_process_started_is_not_serialized_in_run_request() -> None:
    from omg_cli.providers.models import ProviderRunRequest

    def _obs(proc) -> None:
        return None

    req = ProviderRunRequest(prompt="x", on_process_started=_obs)
    payload = req.to_dict()
    assert "on_process_started" not in payload
    assert payload.get("has_on_process_started") is True
    # JSON-serializable
    json.dumps(payload)
