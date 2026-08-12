"""Isolated real-tmux harness for Team UX regression (#104).

Uses a private ``tmux -S`` socket only — never the developer default server.
All helpers are fail-open on cleanup and bound artifact dumps on failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_DIR = REPO_ROOT / "tests" / "fixtures" / "providers"

_PANE_ID_RE = re.compile(r"^%[0-9]{1,16}$")
_WINDOW_ID_RE = re.compile(r"^@[0-9]{1,16}$")
_SESSION_ID_RE = re.compile(r"^\$[0-9]{1,16}$")

# Named failpoints actually installed by FailureInjector.install().
# Keep this list equal to wired hooks — do not advertise unwired phases.
FAILPOINTS = (
    "invocation_snapshot",
    "pre_side_effect",
    "first_worker_split",
    "later_worker_split",
    "nonce_publish",
    "pane_identity",
    "receipt",
    "team_json",
    "layout",
    "focus_restore",
)

# Stable CI upload root (subdirs per dump). Override with OMG104_ARTIFACT_DIR.
ARTIFACT_ROOT = Path(
    os.environ.get("OMG104_ARTIFACT_DIR") or "/tmp/omg104-artifacts"
)


@dataclass(frozen=True)
class PaneSnapshot:
    pane_id: str
    window_id: str
    session_id: str
    session_name: str
    pane_pid: int
    pane_dead: bool
    active: bool
    current_path: str = ""
    title: str = ""


@dataclass(frozen=True)
class TopologySnapshot:
    session_id: str
    session_name: str
    window_id: str
    panes: tuple[PaneSnapshot, ...]
    active_pane_id: str | None
    layout: str = ""
    clients: tuple[str, ...] = ()

    @property
    def pane_ids(self) -> tuple[str, ...]:
        return tuple(p.pane_id for p in self.panes)

    @property
    def window_ids(self) -> frozenset[str]:
        return frozenset(p.window_id for p in self.panes)


@dataclass
class LeaderPane:
    pane_id: str
    window_id: str
    session_id: str
    session_name: str
    pane_pid: int
    socket_path: str


@dataclass
class ForeignWindow:
    session_name: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int


@dataclass
class ForeignClient:
    """Second tmux client attached to the isolated server (control mode)."""

    session_name: str
    proc: subprocess.Popen[str] | None = None
    # Socket path for select-window fallback (avoid forward-ref to server class).
    socket_path: str | None = None

    def select_window(self, window_id: str) -> None:
        """Switch the shared session's current window (multi-client race)."""
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.write(f"select-window -t {window_id}\n")
                self.proc.stdin.flush()
                return
            except (BrokenPipeError, OSError):
                pass
        if not self.socket_path:
            raise RuntimeError("ForeignClient missing socket_path for select-window")
        proc = subprocess.run(
            ["tmux", "-S", self.socket_path, "select-window", "-t", window_id],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"foreign select-window failed: {(proc.stderr or proc.stdout or '').strip()}"
            )

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                try:
                    self.proc.stdin.write("detach-client\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


class FailpointError(RuntimeError):
    """Raised when an armed failpoint fires."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"OMG failpoint fired: {phase}")


@dataclass
class FailureInjector:
    """Deterministic failpoints via monkeypatch (no production hooks required)."""

    armed: set[str] = field(default_factory=set)
    fired: list[str] = field(default_factory=list)

    def arm(self, *phases: str) -> None:
        for phase in phases:
            if phase not in FAILPOINTS:
                raise ValueError(f"unknown failpoint {phase!r}")
            self.armed.add(phase)

    def check(self, phase: str) -> None:
        if phase in self.armed:
            self.fired.append(phase)
            raise FailpointError(phase)

    def install(self, monkeypatch: Any) -> None:
        """Wrap key tmux launch helpers so armed phases raise FailpointError."""
        from omg_cli.team import tmux as tmux_mod

        inj = self

        if "invocation_snapshot" in self.armed or "pre_side_effect" in self.armed:
            orig_snap = tmux_mod.snapshot_invoking_identity

            def _snap(*a: Any, **kw: Any) -> Any:
                inj.check("invocation_snapshot")
                out = orig_snap(*a, **kw)
                inj.check("pre_side_effect")
                return out

            monkeypatch.setattr(tmux_mod, "snapshot_invoking_identity", _snap)

        if "first_worker_split" in self.armed or "later_worker_split" in self.armed:
            orig_split = tmux_mod._split_worker_pane_gated
            state = {"n": 0}

            def _split(*a: Any, **kw: Any) -> Any:
                state["n"] += 1
                if state["n"] == 1:
                    inj.check("first_worker_split")
                else:
                    inj.check("later_worker_split")
                return orig_split(*a, **kw)

            monkeypatch.setattr(tmux_mod, "_split_worker_pane_gated", _split)

        if "nonce_publish" in self.armed:
            orig_pub = tmux_mod._publish_intent_nonce_on_pane

            def _pub(*a: Any, **kw: Any) -> Any:
                inj.check("nonce_publish")
                return orig_pub(*a, **kw)

            monkeypatch.setattr(tmux_mod, "_publish_intent_nonce_on_pane", _pub)

        if "pane_identity" in self.armed:
            orig_verify = tmux_mod._verify_worker_pane_membership

            def _verify(*a: Any, **kw: Any) -> Any:
                inj.check("pane_identity")
                return orig_verify(*a, **kw)

            monkeypatch.setattr(tmux_mod, "_verify_worker_pane_membership", _verify)

        if "layout" in self.armed:
            orig_layout = tmux_mod._apply_same_window_layout

            def _layout(*a: Any, **kw: Any) -> Any:
                inj.check("layout")
                return orig_layout(*a, **kw)

            monkeypatch.setattr(tmux_mod, "_apply_same_window_layout", _layout)

        if "focus_restore" in self.armed:
            orig_run = tmux_mod._tmux_run

            def _run(args: Sequence[str], **kw: Any) -> Any:
                if args and args[0] == "select-pane":
                    inj.check("focus_restore")
                return orig_run(args, **kw)

            monkeypatch.setattr(tmux_mod, "_tmux_run", _run)

        if "receipt" in self.armed:
            from omg_cli.team import plane as plane_mod

            orig_receipt = plane_mod._persist_team_launch_receipt

            def _receipt(*a: Any, **kw: Any) -> Any:
                inj.check("receipt")
                return orig_receipt(*a, **kw)

            monkeypatch.setattr(
                plane_mod, "_persist_team_launch_receipt", _receipt
            )

        if "team_json" in self.armed:
            from omg_cli.team import plane as plane_mod

            # Persist path used after tmux create — fail before durable commit.
            orig_persist = plane_mod._persist_team_identity_receipt

            def _persist(*a: Any, **kw: Any) -> Any:
                inj.check("team_json")
                return orig_persist(*a, **kw)

            monkeypatch.setattr(
                plane_mod, "_persist_team_identity_receipt", _persist
            )


class IsolatedTmuxServer:
    """Private tmux server bound to ``/tmp/omg-<nonce>.sock``."""

    def __init__(self, *, prefix: str = "omg104") -> None:
        if shutil.which("tmux") is None:
            raise RuntimeError("tmux not available on PATH")
        nonce = uuid.uuid4().hex[:10]
        # Keep socket path short — macOS/tmux have low path limits.
        self.socket_path = f"/tmp/{prefix}-{nonce}.sock"
        self._alive = False
        self.artifact_dir: Path | None = None

    def start(self) -> IsolatedTmuxServer:
        # Probe creates the server via new-session later; ensure stale sock gone.
        try:
            Path(self.socket_path).unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            p = Path(self.socket_path)
            if p.exists():
                p.unlink()
        self._alive = True
        return self

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", self.socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def require_ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = self.tmux(*args)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux {' '.join(args)} failed rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()}"
            )
        return proc

    def kill(self) -> None:
        if not self._alive:
            return
        try:
            self.tmux("kill-server")
        except Exception:
            pass
        try:
            Path(self.socket_path).unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            p = Path(self.socket_path)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        self._alive = False

    def __enter__(self) -> IsolatedTmuxServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        if any(exc):
            try:
                self.dump_artifacts(reason="fixture_exit_error")
            except Exception:
                pass
        self.kill()

    def dump_artifacts(self, *, reason: str, dest: Path | None = None) -> Path:
        """Write bounded redacted diagnostics under ARTIFACT_ROOT/<stamp>/."""
        if dest is None:
            ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
            base = ARTIFACT_ROOT / stamp
        else:
            base = dest
        base.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = base
        (base / "reason.txt").write_text(reason[:500] + "\n", encoding="utf-8")
        (base / "socket.txt").write_text(self.socket_path + "\n", encoding="utf-8")
        for name, argv in (
            (
                "layout.txt",
                (
                    "list-windows",
                    "-a",
                    "-F",
                    "#{session_name}:#{window_id} #{window_layout}",
                ),
            ),
            (
                "panes.txt",
                (
                    "list-panes",
                    "-a",
                    "-F",
                    "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t"
                    "#{pane_dead}\t#{pane_active}\t#{pane_current_path}",
                ),
            ),
            ("clients.txt", ("list-clients", "-F", "#{client_tty}\t#{session_name}")),
            ("sessions.txt", ("list-sessions", "-F", "#{session_id}\t#{session_name}")),
        ):
            proc = self.tmux(*argv)
            body = (proc.stdout or "") + (proc.stderr or "")
            home = str(Path.home())
            body = body.replace(home, "$HOME")[:50_000]
            # Always create the file (even empty) so CI upload finds the tree.
            (base / name).write_text(body if body else f"(empty rc={proc.returncode})\n", encoding="utf-8")
        # Marker so upload-artifact never sees an empty directory.
        (base / "DUMP_OK").write_text("1\n", encoding="utf-8")
        return base


class LeaderSession:
    """Leader session/window/pane inside an IsolatedTmuxServer."""

    def __init__(
        self,
        server: IsolatedTmuxServer,
        *,
        name: str | None = None,
        command: str = "sleep 3600",
    ) -> None:
        self.server = server
        self.session_name = name or f"omg-leader-{uuid.uuid4().hex[:8]}"
        self._command = command
        self.leader: LeaderPane | None = None

    def create(self) -> LeaderPane:
        proc = self.server.tmux(
            "new-session",
            "-d",
            "-s",
            self.session_name,
            "-n",
            "leader",
            "sh",
            "-c",
            self._command,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"new-session failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        info = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            f"{self.session_name}:leader",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{socket_path}",
        )
        parts = (info.stdout or "").strip().split("\t")
        if len(parts) != 5:
            raise RuntimeError(f"bad leader display: {info.stdout!r}")
        sid, wid, pane, pid_s, sock = parts
        assert sock == self.server.socket_path
        assert _SESSION_ID_RE.fullmatch(sid)
        assert _WINDOW_ID_RE.fullmatch(wid)
        assert _PANE_ID_RE.fullmatch(pane)
        self.leader = LeaderPane(
            pane_id=pane,
            window_id=wid,
            session_id=sid,
            session_name=self.session_name,
            pane_pid=int(pid_s),
            socket_path=sock,
        )
        # Detached CI runners (esp. macOS GHA) can present a tiny default
        # geometry; same_window scale/split then fails with "no space for a
        # new pane" or hollow-succeeds depending on path. Pin a roomy size.
        self.ensure_window_geometry(wid, width=160, height=48)
        return self.leader

    def ensure_window_geometry(
        self,
        window_id: str | None = None,
        *,
        width: int = 160,
        height: int = 48,
    ) -> None:
        """Resize *window_id* (default: leader) to at least width x height."""
        if self.leader is None:
            raise RuntimeError("leader not created")
        wid = window_id or self.leader.window_id
        probe = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            wid,
            "#{window_width}\t#{window_height}",
        )
        parts = (probe.stdout or "").strip().split("\t")
        try:
            cur_w = int(parts[0]) if len(parts) == 2 else 0
            cur_h = int(parts[1]) if len(parts) == 2 else 0
        except ValueError:
            cur_w, cur_h = 0, 0
        target_w = max(cur_w, int(width))
        target_h = max(cur_h, int(height))
        if cur_w == target_w and cur_h == target_h and cur_w > 0 and cur_h > 0:
            return
        self.server.require_ok(
            "resize-window",
            "-t",
            wid,
            "-x",
            str(target_w),
            "-y",
            str(target_h),
        )

    def tmux_env(self) -> dict[str, str]:
        if self.leader is None:
            raise RuntimeError("leader not created")
        # TMUX format: socket_path,server_pid,session_id_index — pane via TMUX_PANE.
        # server pid from list-sessions identity probe.
        probe = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            self.leader.pane_id,
            "#{pid}",
        )
        server_pid = (probe.stdout or "").strip() or "0"
        return {
            "TMUX": f"{self.server.socket_path},{server_pid},0",
            "TMUX_PANE": self.leader.pane_id,
        }

    def capture_topology(self) -> TopologySnapshot:
        if self.leader is None:
            raise RuntimeError("leader not created")
        return capture_topology(self.server, session=self.session_name)

    def capture_focus(self, window_id: str | None = None) -> str | None:
        """Return the active pane id in *window_id* (default: leader window).

        NOTE: this is window-local ``pane_active`` only — a non-visible window
        still has an active pane. Prefer :meth:`assert_leader_operator_visible`
        for operator visibility contracts (#104 B1).
        """
        if self.leader is None:
            raise RuntimeError("leader not created")
        wid = window_id or self.leader.window_id
        proc = self.server.require_ok(
            "list-panes",
            "-t",
            wid,
            "-F",
            "#{pane_id}\t#{pane_active}",
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "1":
                return parts[0]
        return None

    def window_is_session_active(self, window_id: str) -> bool:
        """True iff *window_id* is the session's current (visible) window."""
        proc = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            window_id,
            "#{window_active}",
        )
        return (proc.stdout or "").strip() == "1"

    def session_active_window_id(self) -> str | None:
        if self.leader is None:
            raise RuntimeError("leader not created")
        proc = self.server.require_ok(
            "list-windows",
            "-t",
            self.session_name,
            "-F",
            "#{window_id}\t#{window_active}",
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "1":
                return parts[0]
        return None

    def assert_leader_operator_visible(self, expected: LeaderPane) -> None:
        """Require session-visible leader window + active pane == leader pane.

        Guards against the hollow check that only inspects window-local
        ``pane_active`` while a foreign window is what the operator sees.
        """
        if not self.window_is_session_active(expected.window_id):
            current = self.session_active_window_id()
            raise AssertionError(
                "leader window is not session-visible "
                f"(want {expected.window_id}, session active={current!r})"
            )
        active_pane = self.capture_focus(expected.window_id)
        if active_pane != expected.pane_id:
            raise AssertionError(
                "leader window active pane mismatch "
                f"(want {expected.pane_id}, got {active_pane!r})"
            )

    def create_foreign_window(self, *, name: str = "foreign") -> ForeignWindow:
        """Extra window in the same session (must survive Team launch)."""
        if self.leader is None:
            raise RuntimeError("leader not created")
        self.server.require_ok(
            "new-window",
            "-d",
            "-t",
            self.session_name,
            "-n",
            name,
            "sleep",
            "3600",
        )
        info = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            f"{self.session_name}:{name}",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}",
        )
        sid, wid, pane, pid_s = (info.stdout or "").strip().split("\t")
        return ForeignWindow(
            session_name=self.session_name,
            session_id=sid,
            window_id=wid,
            pane_id=pane,
            pane_pid=int(pid_s),
        )

    def create_foreign_session(self, *, name: str | None = None) -> ForeignWindow:
        sname = name or f"omg-foreign-{uuid.uuid4().hex[:6]}"
        self.server.require_ok(
            "new-session",
            "-d",
            "-s",
            sname,
            "-n",
            "park",
            "sleep",
            "3600",
        )
        info = self.server.require_ok(
            "display-message",
            "-p",
            "-t",
            f"{sname}:park",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}",
        )
        sid, wid, pane, pid_s = (info.stdout or "").strip().split("\t")
        return ForeignWindow(
            session_name=sname,
            session_id=sid,
            window_id=wid,
            pane_id=pane,
            pane_pid=int(pid_s),
        )

    def attach_second_client(self) -> ForeignClient:
        """Attach a control-mode client; wait until list-clients sees it."""
        before = self.server.tmux(
            "list-clients", "-F", "#{client_tty}\t#{session_name}"
        )
        before_n = len(
            [ln for ln in (before.stdout or "").splitlines() if ln.strip()]
        )
        proc = subprocess.Popen(
            [
                "tmux",
                "-S",
                self.server.socket_path,
                "-C",
                "attach-session",
                "-t",
                self.session_name,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        client = ForeignClient(
            session_name=self.session_name,
            proc=proc,
            socket_path=self.server.socket_path,
        )

        def _attached() -> bool:
            cur = self.server.tmux(
                "list-clients", "-F", "#{client_tty}\t#{session_name}"
            )
            n = len([ln for ln in (cur.stdout or "").splitlines() if ln.strip()])
            return n > before_n

        try:
            wait_until(_attached, timeout_s=5.0, label="second client attach")
        except TimeoutError:
            client.close()
            raise
        return client

    def select_window(self, window_id: str) -> None:
        self.server.require_ok("select-window", "-t", window_id)

    def kill_pane(self, pane_id: str) -> None:
        self.server.tmux("kill-pane", "-t", pane_id)

    def capture_pane(self, pane_id: str, *, lines: int = 200) -> str:
        proc = self.server.tmux(
            "capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", pane_id
        )
        return proc.stdout or ""


def capture_topology(
    server: IsolatedTmuxServer, *, session: str | None = None
) -> TopologySnapshot:
    target_args: list[str]
    if session:
        # ``-s`` = all panes in the session (not only the current window).
        target_args = ["list-panes", "-s", "-t", session, "-F"]
    else:
        target_args = ["list-panes", "-a", "-F"]
    fmt = (
        "#{session_id}\t#{session_name}\t#{window_id}\t#{pane_id}\t"
        "#{pane_pid}\t#{pane_dead}\t#{pane_active}\t#{pane_current_path}\t"
        "#{pane_title}"
    )
    proc = server.require_ok(*target_args, fmt)
    panes: list[PaneSnapshot] = []
    active: str | None = None
    session_id = ""
    session_name = session or ""
    window_id = ""
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        sid, sname, wid, pane, pid_s, dead, act = parts[:7]
        path = parts[7] if len(parts) > 7 else ""
        title = parts[8] if len(parts) > 8 else ""
        snap = PaneSnapshot(
            pane_id=pane,
            window_id=wid,
            session_id=sid,
            session_name=sname,
            pane_pid=int(pid_s or "0"),
            pane_dead=dead == "1",
            active=act == "1",
            current_path=path,
            title=title,
        )
        panes.append(snap)
        if snap.active:
            active = snap.pane_id
        session_id = sid
        session_name = sname
        window_id = wid
    layout = ""
    if window_id:
        lay = server.tmux(
            "display-message", "-p", "-t", window_id, "#{window_layout}"
        )
        layout = (lay.stdout or "").strip()
    clients_proc = server.tmux("list-clients", "-F", "#{client_tty}\t#{session_name}")
    clients = tuple(
        ln.strip()
        for ln in (clients_proc.stdout or "").splitlines()
        if ln.strip()
    )
    return TopologySnapshot(
        session_id=session_id,
        session_name=session_name,
        window_id=window_id,
        panes=tuple(panes),
        active_pane_id=active,
        layout=layout,
        clients=clients,
    )


def provider_script(name: str) -> Path:
    path = PROVIDERS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    git("config", "user.email", "omg-test@example.com")
    git("config", "user.name", "omg-test")
    git("config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "initial")


def install_fixture_provider(
    monkeypatch: Any,
    script: Path,
    *,
    provider: str = "fixture",
    needs_pty: bool = False,
) -> None:
    """Redirect ``executor=fixture`` pane argv at a fake provider script."""
    from omg_cli.team import plane as plane_mod

    def _build(
        *,
        descriptor_path: Path | str | None = None,
        leader_root: Path | str | None = None,
        run_id: str | None = None,
        team_id: str | None = None,
        worker_id: str | None = None,
        owner_token: str | None = None,
        authority_generation: int = 0,
        authority_attempt: int = 1,
        publish_authority: bool = False,
    ) -> str:
        if descriptor_path is None:
            raise plane_mod.TeamError("descriptor_path required")
        # Forward every production authority kwarg. Do not accept **kwargs:
        # unknown names must TypeError instead of being silently dropped.
        return plane_mod.materialize_supervisor_pane_command(
            descriptor_path=descriptor_path,
            provider=provider,
            argv=[sys.executable, str(script)],
            prompt_delivery="prompt-file",
            needs_pty=needs_pty,
            leader_root=leader_root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            owner_token=owner_token,
            authority_generation=authority_generation,
            authority_attempt=authority_attempt,
            publish_authority=publish_authority,
        )

    monkeypatch.setattr(plane_mod, "build_fixture_pane_command", _build)


def run_omg_team_launch(
    *,
    root: Path,
    leader: LeaderSession,
    workers: int = 2,
    goal: str | None = None,
    env: Mapping[str, str] | None = None,
    extra_args: Sequence[str] = (),
    timeout_s: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Subprocess launch with leader TMUX/TMUX_PANE (fixture executor via driver).

    ``omg team launch`` has no ``--executor`` flag; the driver calls
    ``launch_team(..., executor='fixture')`` so the full runtime path still
    runs out-of-process with the leader's tmux binding.
    """
    if leader.leader is None:
        raise RuntimeError("leader not created")
    g = goal or "\n".join(f"{i}. lane {i}" for i in range(1, workers + 1))
    proc_env = os.environ.copy()
    proc_env.update(leader.tmux_env())
    proc_env["PYTHONPATH"] = (
        str(REPO_ROOT)
        + os.pathsep
        + proc_env.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)
    proc_env.setdefault("OMG_EXPERIMENTAL_TMUX_TEAM", "1")
    proc_env.pop("OMG_DISABLE_TMUX_TEAM", None)
    if env:
        proc_env.update({k: str(v) for k, v in env.items()})
    driver = (
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "from omg_cli.team.runtime import launch_team\n"
        "env={k:os.environ[k] for k in ("
        "'OMG_EXPERIMENTAL_TMUX_TEAM','TMUX','TMUX_PANE',"
        "'OMG_TEAM_FIXTURE_HOLD_S','OMG_TEAM_READY_TIMEOUT_MS'"
        ") if k in os.environ}\n"
        "meta=launch_team(\n"
        "  sys.argv[1], workers=int(sys.argv[2]), role='executor',\n"
        "  root=Path(sys.argv[3]), dry_run=False, check_binary=False,\n"
        "  env=env, team_id='team',\n"
        "  executor='fixture', detach=False)\n"
        "print(json.dumps(meta, ensure_ascii=False))\n"
    )
    argv = [sys.executable, "-c", driver, g, str(workers), str(root), *extra_args]
    return subprocess.run(
        argv,
        cwd=str(root),
        env=proc_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_s,
    )


def launch_team_inside(
    *,
    root: Path,
    leader: LeaderSession,
    workers: int = 2,
    monkeypatch: Any | None = None,
    env: Mapping[str, str] | None = None,
    provider: Path | None = None,
    view_mode: str | None = None,
) -> dict[str, Any]:
    """Call ``launch_team`` with ambient TMUX/TMUX_PANE from the leader pane."""
    from omg_cli.team.plane import EXPERIMENTAL_ENV
    from omg_cli.team.runtime import launch_team

    if leader.leader is None:
        raise RuntimeError("leader not created")
    if monkeypatch is not None:
        for k, v in leader.tmux_env().items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
        monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)
        if provider is not None:
            install_fixture_provider(monkeypatch, provider)
        if env:
            for k, v in env.items():
                monkeypatch.setenv(k, str(v))
    api_env = {EXPERIMENTAL_ENV: "1", **leader.tmux_env()}
    if env:
        api_env.update({k: str(v) for k, v in env.items()})
    return launch_team(
        "\n".join(f"{i}. lane {i}" for i in range(1, workers + 1)),
        workers=workers,
        role="executor",
        root=root,
        dry_run=False,
        check_binary=False,
        env=api_env,
        team_id="team",
        executor="fixture",
        detach=False,
        view_mode=view_mode,
    )


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 15.0,
    poll_s: float = 0.1,
    label: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll_s)
    raise TimeoutError(f"timed out waiting for {label}")


def poll_json(path: Path, *, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001 — bounded poll
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"json not ready at {path}: {last}")


__all__ = [
    "ARTIFACT_ROOT",
    "FAILPOINTS",
    "FailpointError",
    "FailureInjector",
    "ForeignClient",
    "ForeignWindow",
    "IsolatedTmuxServer",
    "LeaderPane",
    "LeaderSession",
    "PaneSnapshot",
    "PROVIDERS_DIR",
    "REPO_ROOT",
    "TopologySnapshot",
    "capture_topology",
    "failpoint_in_chain",
    "init_git_repo",
    "install_fixture_provider",
    "launch_team_inside",
    "poll_json",
    "provider_script",
    "run_omg_team_launch",
    "wait_until",
]


def failpoint_in_chain(exc: BaseException, phase: str) -> bool:
    """True if FailpointError(phase) appears in cause/context chain."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, FailpointError) and cur.phase == phase:
            return True
        cur = cur.__cause__ or cur.__context__
    return False

