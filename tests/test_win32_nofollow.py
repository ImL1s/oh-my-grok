"""Hermetic Windows no-follow confinement (#77).

Drives the real ``read_confined_regular_file`` / ``write_confined_regular_file``
/ ``apply_hash_edit`` / catalog pin entries under a fake win32 (monkeypatched
``os.name``, missing ``O_NOFOLLOW``, injected ctypes API). Does not mock those
production functions to return success.

Live Windows CreateFileW proof is the ``test_real_windows_*`` cases plus the
``windows-nofollow`` CI job. This host may be Darwin; those cases skip here.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Callable

import pytest

from omg_cli.agents_catalog import AgentsCatalogError, _read_plugin_regular_text
from omg_cli.hash_edit import (
    HashEditConcurrencyError,
    HashEditPathError,
    apply_hash_edit,
    plan_hash_edit,
    read_confined_regular_file,
    write_confined_regular_file,
)
from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.hash_edit.planner import HashEditCurrentFact
from omg_cli.skills_catalog import SkillsCatalogError, _atomic_write_text, _refuse_symlink_dest
from omg_cli.win32_nofollow import (
    FILE_CREATE,
    FILE_DIRECTORY_FILE,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_NON_DIRECTORY_FILE,
    FILE_OPEN,
    FILE_OPEN_REPARSE_POINT,
    FileInfo,
    GENERIC_WRITE,
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
    OBJ_CASE_INSENSITIVE,
    OBJ_DONT_REPARSE,
    OPEN_EXISTING,
    Win32NofollowError,
    windows_nofollow_ready,
    write_path_regular,
)

pytestmark = pytest.mark.platform

_SECRET = b"LEAKED-OUTSIDE-BYTES-NOT-FOR-CONFINED-READ"
_SAFE = b"before\nalpha\nafter\n"


class _Handle:
    __slots__ = ("path", "fd", "is_dir", "reparse_tag", "writable")

    def __init__(
        self,
        path: str,
        *,
        fd: int | None,
        is_dir: bool,
        reparse_tag: int,
        writable: bool,
    ) -> None:
        self.path = path
        self.fd = fd
        self.is_dir = is_dir
        self.reparse_tag = reparse_tag
        self.writable = writable


class FakeWin32API:
    """POSIX-backed CreateFileW/NtCreateFile stand-in that does not follow."""

    def __init__(self) -> None:
        self._next = 0x1000
        self.handles: dict[int, _Handle] = {}
        self.create_file_calls: list[tuple[str, int]] = []
        self.nt_create_calls: list[tuple[str, str, int, int]] = []
        self.forced_reparse: dict[str, tuple[int, bool]] = {}
        self.before_nt_create: Callable[[str, str], None] | None = None

    def _alloc(
        self,
        path: str,
        *,
        fd: int | None,
        is_dir: bool,
        reparse_tag: int,
        writable: bool,
    ) -> int:
        handle = self._next
        self._next += 1
        self.handles[handle] = _Handle(
            path, fd=fd, is_dir=is_dir, reparse_tag=reparse_tag, writable=writable
        )
        return handle

    def _require(self, handle: int) -> _Handle:
        try:
            return self.handles[handle]
        except KeyError as exc:
            raise Win32NofollowError("unavailable", "invalid handle") from exc

    def _follow(self, *, flags: int = 0, options: int = 0, oa_attributes: int = 0) -> bool:
        if flags & FILE_FLAG_OPEN_REPARSE_POINT:
            return False
        if options & FILE_OPEN_REPARSE_POINT:
            return False
        if oa_attributes & OBJ_DONT_REPARSE:
            return False
        return True

    def _classify(self, path: str) -> tuple[str, int, bool]:
        path = os.path.abspath(path)
        planted = self.forced_reparse.get(path)
        if planted is not None:
            return "reparse", planted[0], planted[1]
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise Win32NofollowError("missing", f"target does not exist: {path}") from exc
        if stat.S_ISLNK(st.st_mode):
            return "reparse", IO_REPARSE_TAG_SYMLINK, False
        if stat.S_ISDIR(st.st_mode):
            return "dir", 0, True
        if not stat.S_ISREG(st.st_mode):
            raise Win32NofollowError("not_regular", "target must be a regular file")
        return "file", 0, False

    def _open_file(self, path: str, *, writable: bool, create: bool) -> int:
        flags = os.O_RDWR if writable else os.O_RDONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError as exc:
            raise Win32NofollowError("write", f"cannot create {path}") from exc
        except FileNotFoundError as exc:
            raise Win32NofollowError("missing", f"target does not exist: {path}") from exc
        except OSError as exc:
            raise Win32NofollowError("unavailable", str(exc)) from exc
        return fd

    def create_file(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
    ) -> int:
        del share
        self.create_file_calls.append((path, flags))
        path = os.path.abspath(path)
        if self._follow(flags=flags):
            path = os.path.realpath(path)
        writable = bool(access & GENERIC_WRITE)
        create = disposition != OPEN_EXISTING and disposition != FILE_OPEN
        kind, tag, is_dir = self._classify(path) if not create else ("file", 0, False)
        if create:
            fd = self._open_file(path, writable=True, create=True)
            return self._alloc(path, fd=fd, is_dir=False, reparse_tag=0, writable=True)
        if kind == "reparse":
            return self._alloc(
                path, fd=None, is_dir=is_dir, reparse_tag=tag, writable=False
            )
        if kind == "dir":
            return self._alloc(path, fd=None, is_dir=True, reparse_tag=0, writable=False)
        fd = self._open_file(path, writable=writable, create=False)
        return self._alloc(path, fd=fd, is_dir=False, reparse_tag=0, writable=writable)

    def nt_create(
        self,
        root_handle: int,
        name: str,
        *,
        access: int,
        share: int,
        disposition: int,
        options: int,
        oa_attributes: int = OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
    ) -> int:
        del share
        parent = self._require(root_handle)
        if self.before_nt_create is not None:
            self.before_nt_create(parent.path, name)
        child = os.path.abspath(os.path.join(parent.path, name))
        self.nt_create_calls.append((parent.path, name, options, oa_attributes))
        if self._follow(options=options, oa_attributes=oa_attributes):
            child = os.path.realpath(child)
        writable = bool(access & GENERIC_WRITE)
        create = disposition == FILE_CREATE
        if create:
            fd = self._open_file(child, writable=True, create=True)
            return self._alloc(child, fd=fd, is_dir=False, reparse_tag=0, writable=True)
        kind, tag, is_dir = self._classify(child)
        if kind == "reparse":
            return self._alloc(
                child, fd=None, is_dir=is_dir, reparse_tag=tag, writable=False
            )
        if options & FILE_DIRECTORY_FILE and not is_dir:
            raise Win32NofollowError("not_regular", f"{name} is not a directory")
        if options & FILE_NON_DIRECTORY_FILE and is_dir:
            raise Win32NofollowError("not_regular", f"{name} must be a regular file")
        if kind == "dir":
            return self._alloc(child, fd=None, is_dir=True, reparse_tag=0, writable=False)
        fd = self._open_file(child, writable=writable, create=False)
        return self._alloc(child, fd=fd, is_dir=False, reparse_tag=0, writable=writable)

    def close(self, handle: int) -> None:
        item = self.handles.pop(handle, None)
        if item is not None and item.fd is not None:
            os.close(item.fd)

    def get_info(self, handle: int) -> FileInfo:
        item = self._require(handle)
        if item.reparse_tag:
            return FileInfo(
                attributes=0x400 | (0x10 if item.is_dir else 0),
                size=0,
                links=1,
                volume=1,
                index=handle,
                mode=0o666,
                is_directory=item.is_dir,
                is_reparse=True,
                reparse_tag=item.reparse_tag,
            )
        if item.fd is not None:
            st = os.fstat(item.fd)
        else:
            st = os.lstat(item.path)
        is_dir = stat.S_ISDIR(st.st_mode)
        return FileInfo(
            attributes=0x10 if is_dir else 0x80,
            size=int(st.st_size),
            links=int(st.st_nlink),
            volume=int(st.st_dev),
            index=int(st.st_ino),
            mode=stat.S_IMODE(st.st_mode),
            is_directory=is_dir,
            is_reparse=False,
            reparse_tag=0,
        )

    def read(self, handle: int, n: int) -> bytes:
        item = self._require(handle)
        if item.fd is None:
            raise Win32NofollowError("symlink", "cannot read a reparse point")
        if n <= 0:
            return b""
        return os.read(item.fd, n)

    def write(self, handle: int, data: bytes) -> int:
        item = self._require(handle)
        if item.fd is None:
            raise Win32NofollowError("write", "cannot write a reparse point")
        return os.write(item.fd, data)

    def flush(self, handle: int) -> None:
        item = self._require(handle)
        if item.fd is not None:
            os.fsync(item.fd)

    def rename_replace(self, handle: int, dest_name: str, *, root_handle: int) -> None:
        src = self._require(handle)
        parent = self._require(root_handle)
        dest = os.path.join(parent.path, dest_name)
        if os.path.lexists(dest) and os.path.islink(dest):
            raise Win32NofollowError("symlink", f"target may not be a symlink: {dest_name}")
        os.replace(src.path, dest)
        if src.fd is not None:
            os.close(src.fd)
            src.fd = None
        self.handles.pop(handle, None)

    def unlink(self, root_handle: int, name: str) -> None:
        parent = self._require(root_handle)
        path = os.path.join(parent.path, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> FakeWin32API:
    if os.name == "nt":
        pytest.skip("injected FakeWin32API is POSIX-backed; real Windows uses CtypesWin32API")
    api = FakeWin32API()
    monkeypatch.setattr("omg_cli.win32_nofollow._API", api)
    # Do not set os.name=nt: pathlib.Path would become WindowsPath and
    # cannot be constructed on Darwin. Injected _API makes the Windows
    # backend ready; force POSIX nofollow off so production dispatch
    # uses the real Windows walk.
    monkeypatch.setattr("omg_cli.hash_edit.apply._posix_nofollow_ready", lambda: False)
    monkeypatch.setattr("omg_cli.agents_catalog._posix_nofollow_ready", lambda: False)
    return api


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(current: str, **overrides: object) -> dict[str, object]:
    old_text = str(overrides.pop("old_text", "alpha"))
    replacement = str(overrides.pop("replacement", "beta"))
    before_context = str(overrides.pop("before_context", "before\n"))
    after_context = str(overrides.pop("after_context", "\nafter"))
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-win32-1",
        "producer": "omg.hash_edit.win32-test",
        "path": str(overrides.pop("path", "docs/example.md")),
        "base_sha256": _digest_text(current),
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": _digest_text(old_text),
        "replacement_sha256": _digest_text(replacement),
        "before_context_sha256": _digest_text(before_context),
        "after_context_sha256": _digest_text(after_context),
    }
    payload.update(overrides)
    return payload


def _assert_nofollow_flags(api: FakeWin32API) -> None:
    assert api.create_file_calls, "CreateFileW must open the workspace root"
    for _path, flags in api.create_file_calls:
        assert flags & FILE_FLAG_OPEN_REPARSE_POINT
    assert api.nt_create_calls, "NtCreateFile must walk relative components"
    for _parent, _name, options, oa in api.nt_create_calls:
        assert options & FILE_OPEN_REPARSE_POINT
        assert oa & OBJ_DONT_REPARSE


def test_windows_read_regular_file(tmp_path: Path, fake_win32: FakeWin32API) -> None:
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_bytes(_SAFE)
    assert windows_nofollow_ready() is True
    assert read_confined_regular_file(tmp_path, "docs/example.md") == _SAFE
    _assert_nofollow_flags(fake_win32)


def test_windows_read_rejects_symlink_leaf(tmp_path: Path, fake_win32: FakeWin32API) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").symlink_to(outside)
    with pytest.raises(HashEditPathError, match="symlink"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert outside.read_bytes() == _SECRET
    _assert_nofollow_flags(fake_win32)


def test_windows_read_rejects_symlink_ancestor(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    real = tmp_path / "real-docs"
    real.mkdir()
    (real / "example.md").write_bytes(_SECRET)
    (tmp_path / "docs").symlink_to(real, target_is_directory=True)
    with pytest.raises(HashEditPathError, match="symlink"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert (real / "example.md").read_bytes() == _SECRET


def test_windows_read_rejects_mount_point_reparse(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").write_bytes(_SAFE)
    fake_win32.forced_reparse[str(docs.resolve())] = (IO_REPARSE_TAG_MOUNT_POINT, True)
    with pytest.raises(HashEditPathError, match="symlink"):
        read_confined_regular_file(tmp_path, "docs/example.md")


def test_windows_read_symlink_swap_during_leaf_open(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_bytes(_SAFE)

    def _swap(parent: str, name: str) -> None:
        if name != "example.md":
            return
        leaf = Path(parent) / name
        if leaf.is_symlink():
            return
        leaf.unlink()
        leaf.symlink_to(outside)

    fake_win32.before_nt_create = _swap
    with pytest.raises(HashEditPathError, match="symlink"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert outside.read_bytes() == _SECRET


def test_windows_read_does_not_use_path_open(
    tmp_path: Path, fake_win32: FakeWin32API, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_bytes(_SAFE)

    def _banned(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Path.open/read_bytes must not be used for confined I/O")

    monkeypatch.setattr(Path, "open", _banned)
    monkeypatch.setattr(Path, "read_bytes", _banned)
    monkeypatch.setattr(Path, "read_text", _banned)
    monkeypatch.setattr(Path, "write_bytes", _banned)
    assert read_confined_regular_file(tmp_path, "docs/example.md") == _SAFE


def test_windows_write_regular_and_expected(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    target = tmp_path / "a.py"
    original = b"original-bytes\n"
    after = b"after-bytes\n"
    target.write_bytes(after)
    write_confined_regular_file(tmp_path, "a.py", original, expected=after)
    assert target.read_bytes() == original
    with pytest.raises(HashEditConcurrencyError, match="expected"):
        write_confined_regular_file(tmp_path, "a.py", after, expected=b"other")
    assert target.read_bytes() == original
    _assert_nofollow_flags(fake_win32)


def test_windows_write_refuses_symlink_dest(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.symlink_to(outside)
    with pytest.raises(HashEditPathError, match="symlink"):
        write_confined_regular_file(tmp_path, "docs/example.md", b"new-bytes\n")
    assert outside.read_bytes() == _SECRET


def test_windows_apply_splices_without_following(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    current = "before\nalpha\nafter\n"
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_text(current, encoding="utf-8")
    payload = _payload(current)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
    )
    result = apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == "before\nbeta\nafter\n"
    assert result.path == "docs/example.md"
    assert result.after_sha256 == _digest_text("before\nbeta\nafter\n")
    _assert_nofollow_flags(fake_win32)


def test_windows_apply_noop_revalidates_under_exclusive_lock(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    current = "before\nalpha\nafter\n"
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_text(current, encoding="utf-8")
    payload = _payload(current, old_text="alpha", replacement="alpha")
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
    )
    result = apply_hash_edit(tmp_path, payload, plan)
    assert result.after_sha256 == _digest_text(current)
    assert target.read_text(encoding="utf-8") == current
    dest_opens = [
        call for call in fake_win32.nt_create_calls if call[1] == "example.md"
    ]
    assert len(dest_opens) >= 2
    _assert_nofollow_flags(fake_win32)

    swapped = {"n": 0}

    def _swap(parent: str, name: str) -> None:
        if name != "example.md":
            return
        swapped["n"] += 1
        if swapped["n"] == 2:
            Path(parent, name).write_text("before\nhacked\nafter\n", encoding="utf-8")

    fake_win32.before_nt_create = _swap
    fake_win32.nt_create_calls.clear()
    with pytest.raises(HashEditConcurrencyError):
        apply_hash_edit(tmp_path, payload, plan)
    assert "hacked" in target.read_text(encoding="utf-8")


def test_windows_apply_rejects_symlink_leaf(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    current = "before\nalpha\nafter\n"
    outside = tmp_path / "outside.md"
    outside.write_text(current, encoding="utf-8")
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.symlink_to(outside)
    payload = _payload(current)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
    )
    with pytest.raises(HashEditPathError, match="symlink"):
        apply_hash_edit(tmp_path, payload, plan)
    assert outside.read_text(encoding="utf-8") == current


def test_fail_closed_without_windows_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omg_cli.hash_edit.apply._posix_nofollow_ready", lambda: False)
    monkeypatch.setattr("omg_cli.win32_nofollow._API", None)

    def _boom() -> object:
        raise OSError("kernel32 missing")

    monkeypatch.setattr("omg_cli.win32_nofollow.CtypesWin32API", _boom)
    assert windows_nofollow_ready() is False
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_bytes(_SAFE)
    with pytest.raises(HashEditPathError, match="O_NOFOLLOW"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert target.read_bytes() == _SAFE


def test_agent_catalog_windows_rejects_symlink(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# leaked\n", encoding="utf-8")
    (agents / "omg-executor.md").symlink_to(outside)
    with pytest.raises(AgentsCatalogError, match="missing agent"):
        _read_plugin_regular_text(tmp_path, "agents/omg-executor.md")
    assert outside.read_text(encoding="utf-8") == "# leaked\n"
    _assert_nofollow_flags(fake_win32)


def test_agent_catalog_windows_reads_regular(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    body = (
        "---\nname: omg-executor\ncapabilityMode: read-write\n"
        "permissionMode: default\n---\n# ok\n"
    )
    (agents / "omg-executor.md").write_text(body, encoding="utf-8")
    assert _read_plugin_regular_text(tmp_path, "agents/omg-executor.md") == body


def test_skills_windows_refuse_and_write(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    dest = tmp_path / "proj" / "SKILL.md"
    dest.parent.mkdir()
    dest.write_text("old\n", encoding="utf-8")
    _refuse_symlink_dest(dest)
    _atomic_write_text(dest, "new\n")
    assert dest.read_text(encoding="utf-8") == "new\n"
    outside = tmp_path / "outside.md"
    outside.write_text("nope\n", encoding="utf-8")
    dest.unlink()
    dest.symlink_to(outside)
    with pytest.raises(SkillsCatalogError, match="symlink"):
        _refuse_symlink_dest(dest)
    with pytest.raises(SkillsCatalogError, match="symlink"):
        _atomic_write_text(dest, "hijack\n")
    assert outside.read_text(encoding="utf-8") == "nope\n"


def test_write_path_regular_rejects_ancestor_reparse(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    dest = docs / "parity" / "SKILL.md"
    dest.parent.mkdir()
    dest.write_text("old\n", encoding="utf-8")
    fake_win32.forced_reparse[str(docs.resolve())] = (IO_REPARSE_TAG_MOUNT_POINT, True)
    with pytest.raises(Win32NofollowError, match="symlink|reparse"):
        write_path_regular(dest, b"new\n")
    assert dest.read_text(encoding="utf-8") == "old\n"


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_read_write_regular(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_bytes(_SAFE)
    assert read_confined_regular_file(tmp_path, "docs/example.md") == _SAFE
    write_confined_regular_file(tmp_path, "docs/example.md", b"replaced\n", expected=_SAFE)
    assert target.read_bytes() == b"replaced\n"


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_rejects_mount_point(tmp_path: Path) -> None:
    import subprocess

    real = tmp_path / "real-docs"
    real.mkdir()
    (real / "example.md").write_bytes(_SECRET)
    dest = tmp_path / "docs"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dest), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not dest.exists():
        pytest.skip(completed.stderr.strip() or "cannot create junction")
    with pytest.raises(HashEditPathError, match="symlink|reparse"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert (real / "example.md").read_bytes() == _SECRET


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_rejects_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    docs = tmp_path / "docs"
    docs.mkdir()
    link = docs / "example.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege missing")
    with pytest.raises(HashEditPathError, match="symlink|reparse"):
        read_confined_regular_file(tmp_path, "docs/example.md")
    assert outside.read_bytes() == _SECRET
