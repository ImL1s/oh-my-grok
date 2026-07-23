"""Bounded product-owned runner for repository workflows.

The callback is a pure receipt resolver, not an effect executor. Effects happen
outside this runner and enter through prevalidated canonical receipts. A Python
audit hook rejects ordinary process, network, native-loading, and filesystem
mutation APIs before the callback runs. This is a product contract and
defense-in-depth boundary, not a sandbox for deliberately hostile native code.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    append_locked_jsonl,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.contracts.workflow_contract import task_requires_terminable_executor
from omg_cli.contracts.writer_chain import canonical_json_bytes

from .permissions import admit_definition
from .planner import build_plan
from .replay import assess_replay, validate_effect_receipt
from .review import (
    WorkflowReviewError,
    evaluate_review,
    normalize_task_result,
    validate_success_task_receipt,
)
from .schema import compile_workflow


Executor = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]
_MAX_EXECUTOR_RESULT_BYTES = 1024 * 1024


class _ExecutorAuthorityError(PermissionError):
    pass


_FORBIDDEN_AUDIT_EVENTS = frozenset(
    {
        "ctypes.dlopen",
        "ctypes.dlsym",
        "os.chmod",
        "os.chown",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.kill",
        "os.link",
        "os.mkdir",
        "os.mkfifo",
        "os.mknod",
        "os.posix_spawn",
        "os.putenv",
        "os.remove",
        "os.rename",
        "os.removexattr",
        "os.rmdir",
        "os.spawn",
        "os.symlink",
        "os.setxattr",
        "os.system",
        "os.truncate",
        "os.unsetenv",
        "os.utime",
        "pty.spawn",
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "subprocess.Popen",
    }
)


def run_artifact_dir(root: Path | str, run_id: str) -> Path:
    return Path(root).resolve() / ".omg" / "artifacts" / "workflow-runs" / run_id


def _append(path: Path, row: Mapping[str, Any]) -> None:
    append_locked_jsonl(path, canonical_json_bytes(dict(row)))


def _execute_one(
    execute_task: Executor,
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    cancel_event: threading.Event,
    root: Path | str,
) -> dict[str, Any]:
    if cancel_event.is_set():
        raw: Mapping[str, Any] = {
            "task_id": task["task_id"],
            "stage_id": task["stage_id"],
            "matrix_index": task["matrix_index"],
            "actor_identity": task["actor_identity"],
            "plan_digest": plan["plan_digest"],
            "definition_digest": plan["definition_digest"],
            "run_generation": plan["run_generation"],
            "status": "cancelled",
            "output": {"verdict": "CANCELLED"},
        }
        return normalize_task_result(definition, plan, task, raw, root=root)
    context = {
        "workflow_input": plan["input"],
        "run_id": plan["run_id"],
        "plan_digest": plan["plan_digest"],
        "run_generation": plan["run_generation"],
        "cancel_event": cancel_event,
        "plan": plan,
    }
    last_error: Exception | None = None
    for _attempt in range(int(task["retry_budget"]) + 1):
        try:
            raw = execute_task(dict(task), context)
            result = normalize_task_result(
                definition, plan, task, raw, root=root
            )
            if task["effect_type"] is not None and result["status"] in {"passed", "approved"}:
                receipt = result.get("effect_receipt")
                if receipt is None:
                    result["status"] = "effect_unknown"
                else:
                    validate_effect_receipt(receipt, task=task, plan=plan)
            return result
        except _ExecutorAuthorityError as exc:
            failure = {
                "task_id": task["task_id"],
                "stage_id": task["stage_id"],
                "matrix_index": task["matrix_index"],
                "actor_identity": task["actor_identity"],
                "plan_digest": plan["plan_digest"],
                "definition_digest": plan["definition_digest"],
                "run_generation": plan["run_generation"],
                "status": "failed",
                "verdict": "NO_SHIP",
                "output": {"verdict": "NO_SHIP"},
                "error": f"E_WORKFLOW_EXECUTOR_AUTHORITY: {exc}",
            }
            return normalize_task_result(
                definition, plan, task, failure, root=root
            )
        except Exception as exc:  # executor/review boundary is fail-closed
            last_error = exc
    failure = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "matrix_index": task["matrix_index"],
        "actor_identity": task["actor_identity"],
        "plan_digest": plan["plan_digest"],
        "definition_digest": plan["definition_digest"],
        "run_generation": plan["run_generation"],
        "status": "failed",
        "verdict": "NO_SHIP",
        "output": {"verdict": "NO_SHIP"},
        "error": str(last_error),
    }
    return normalize_task_result(definition, plan, task, failure, root=root)


def _terminal_task_result(
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    status: str,
    error: str,
) -> dict[str, Any]:
    if status == "cancelled":
        verdict = "CANCELLED"
    elif status == "effect_unknown":
        verdict = "EFFECT_UNKNOWN"
    else:
        verdict = "NO_SHIP"
    return normalize_task_result(
        definition,
        plan,
        task,
        {
            "task_id": task["task_id"],
            "stage_id": task["stage_id"],
            "matrix_index": task["matrix_index"],
            "actor_identity": task["actor_identity"],
            "plan_digest": plan["plan_digest"],
            "definition_digest": plan["definition_digest"],
            "run_generation": plan["run_generation"],
            "status": status,
            "verdict": verdict,
            "output": {"verdict": verdict},
            "error": error,
        },
    )


def _task_requires_terminable_executor(task: Mapping[str, Any]) -> bool:
    """Return whether the task holds any authority that can escape a timeout."""
    return task_requires_terminable_executor(task)


def _receipt_resolver_audit(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write_flags = (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        )
        if (
            isinstance(mode, str)
            and any(character in mode for character in "wax+")
        ) or (isinstance(flags, int) and flags & write_flags):
            raise _ExecutorAuthorityError("filesystem mutation is forbidden")
    if event in _FORBIDDEN_AUDIT_EVENTS or event.startswith(
        ("socket.", "subprocess.", "ctypes.")
    ):
        raise _ExecutorAuthorityError(f"audit event is forbidden: {event}")


def _close_inherited_fds(*, keep: set[int]) -> None:
    """Close parent descriptors before untrusted callback code can observe them."""
    try:
        with os.scandir("/dev/fd") as entries:
            inherited = [
                int(entry.name)
                for entry in entries
                if entry.name.isdigit() and int(entry.name) not in keep
            ]
    except OSError:
        inherited = [
            descriptor
            for descriptor in range(3, int(os.sysconf("SC_OPEN_MAX")))
            if descriptor not in keep
        ]
    for descriptor in inherited:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _isolated_child(
    execute_task: Executor,
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    ready_sender: Any,
    result_sender: Any,
    root: Path | str,
) -> None:
    """Execute one callback in a dedicated POSIX process group."""
    try:
        _close_inherited_fds(
            keep={ready_sender.fileno(), result_sender.fileno()}
        )
        os.setsid()
        sys.addaudithook(_receipt_resolver_audit)
        ready_sender.send_bytes(b"READY")
        result = _execute_one(
            execute_task,
            definition,
            plan,
            task,
            threading.Event(),
            root,
        )
        payload = canonical_json_bytes(result)
        if len(payload) > _MAX_EXECUTOR_RESULT_BYTES:
            raise ValueError("executor result exceeds bounded IPC contract")
    except BaseException as exc:  # child must report a canonical failure
        failure = _terminal_task_result(
            definition,
            plan,
            task,
            status="failed",
            error=f"E_WORKFLOW_EXECUTOR: {exc}",
        )
        payload = canonical_json_bytes(failure)
    try:
        result_sender.send_bytes(payload)
    finally:
        ready_sender.close()
        result_sender.close()


def _process_group_gone(pgid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # A transient EPERM is not disappearance proof. Keep polling within
            # the existing bound; only ESRCH may prove that the group is gone.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _stop_isolated_task(state: Mapping[str, Any]) -> bool:
    """Cancel, terminate, reap, and read back disappearance of one task group."""
    process = state["process"]
    pgid = int(process.pid)
    ready = bool(state["ready"])
    try:
        if ready:
            os.killpg(pgid, signal.SIGTERM)
        elif process.is_alive():
            process.terminate()
    except ProcessLookupError:
        pass
    process.join(0.2)
    if process.is_alive() or (
        ready and not _process_group_gone(pgid, timeout=0.1)
    ):
        try:
            if ready:
                os.killpg(pgid, signal.SIGKILL)
            elif process.is_alive():
                process.kill()
        except ProcessLookupError:
            pass
        process.join(1.0)

    gone = not process.is_alive()
    if ready:
        gone = gone and _process_group_gone(pgid, timeout=1.0)
    state["ready_receiver"].close()
    state["result_receiver"].close()
    return gone


def _start_isolated_task(
    context: Any,
    execute_task: Executor,
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    root: Path | str,
) -> dict[str, Any]:
    ready_receiver, ready_sender = context.Pipe(duplex=False)
    result_receiver, result_sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_isolated_child,
        args=(
            execute_task,
            definition,
            plan,
            task,
            ready_sender,
            result_sender,
            root,
        ),
        name=f"omg-workflow-{task['task_id'][:12]}",
    )
    process.start()
    ready_sender.close()
    result_sender.close()
    state = {
        "process": process,
        "ready_receiver": ready_receiver,
        "result_receiver": result_receiver,
        "ready": False,
        "deadline": time.monotonic() + float(task["timeout_seconds"]),
        "task": task,
    }
    if ready_receiver.poll(0.5):
        state["ready"] = ready_receiver.recv_bytes() == b"READY"
    if not state["ready"]:
        _stop_isolated_task(state)
        raise RuntimeError("isolated executor failed process-group handshake")
    return state


def _run_wave(
    execute_task: Executor,
    definition: Mapping[str, Any],
    plan: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
    runnable: Sequence[str],
    cancellation: threading.Event,
    root: Path | str,
) -> list[dict[str, Any]]:
    """Run a bounded wave in killable per-task POSIX process groups."""
    results: list[dict[str, Any]] = []
    isolated_runnable: list[str] = []
    for task_id in runnable:
        task = tasks[task_id]
        if task["effect_type"] is not None:
            results.append(
                _terminal_task_result(
                    definition,
                    plan,
                    task,
                    status="failed",
                    error="E_WORKFLOW_EFFECT_EXECUTOR_UNSAFE",
                )
            )
        else:
            isolated_runnable.append(task_id)

    if not isolated_runnable:
        return results

    if "fork" not in multiprocessing.get_all_start_methods():
        for task_id in isolated_runnable:
            task = tasks[task_id]
            error = (
                "E_WORKFLOW_TERMINABLE_EXECUTOR_REQUIRED"
                if _task_requires_terminable_executor(task)
                else "E_WORKFLOW_TERMINABLE_EXECUTOR_UNAVAILABLE"
            )
            results.append(
                _terminal_task_result(
                    definition,
                    plan,
                    task,
                    status="failed",
                    error=error,
                )
            )
        return results

    context = multiprocessing.get_context("fork")
    workers = min(int(plan["max_parallelism"]), len(isolated_runnable))
    queued = list(isolated_runnable)
    active: dict[str, dict[str, Any]] = {}

    def start_available() -> None:
        while queued and len(active) < workers:
            task_id = queued.pop(0)
            task = tasks[task_id]
            try:
                active[task_id] = _start_isolated_task(
                    context,
                    execute_task,
                    definition,
                    plan,
                    task,
                    root,
                )
            except Exception as exc:
                results.append(
                    _terminal_task_result(
                        definition,
                        plan,
                        task,
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(task)
                            else "failed"
                        ),
                        error=f"E_WORKFLOW_EXECUTOR_START: {exc}",
                    )
                )

    start_available()
    while active or queued:
        if cancellation.is_set():
            for task_id, state in list(active.items()):
                gone = _stop_isolated_task(state)
                results.append(
                    _terminal_task_result(
                        definition,
                        plan,
                        tasks[task_id],
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(tasks[task_id])
                            else "cancelled"
                        ),
                        error=(
                            "E_WORKFLOW_CANCELLED"
                            if gone
                            else "E_WORKFLOW_TERMINATION_UNCONFIRMED"
                        ),
                    )
                )
                del active[task_id]
            for task_id in queued:
                results.append(
                    _terminal_task_result(
                        definition,
                        plan,
                        tasks[task_id],
                        status="cancelled",
                        error="E_WORKFLOW_CANCELLED",
                    )
                )
            queued.clear()
            break

        now = time.monotonic()
        for task_id, state in list(active.items()):
            task = tasks[task_id]
            receiver = state["result_receiver"]
            # Liveness must be observed before draining: the child publishes
            # its receipt and exits with no ordering guarantee against this
            # loop, so exit observed before an empty poll is the only sound
            # exited-without-result proof.
            exited_before_poll = not state["process"].is_alive()
            if receiver.poll():
                try:
                    payload = receiver.recv_bytes(_MAX_EXECUTOR_RESULT_BYTES)
                    decoded = json.loads(payload)
                    if not isinstance(decoded, dict):
                        raise ValueError("executor result must be an object")
                except (EOFError, OSError, ValueError, json.JSONDecodeError) as exc:
                    decoded = _terminal_task_result(
                        definition,
                        plan,
                        task,
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(task)
                            else "failed"
                        ),
                        error=f"E_WORKFLOW_EXECUTOR_IPC: {exc}",
                    )
                state["process"].join(0.2)
                gone = (
                    not state["process"].is_alive()
                    and _process_group_gone(
                        int(state["process"].pid), timeout=0.1
                    )
                )
                if not gone:
                    _stop_isolated_task(state)
                    decoded = _terminal_task_result(
                        definition,
                        plan,
                        task,
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(task)
                            else "failed"
                        ),
                        error="E_WORKFLOW_DESCENDANT_SURVIVED",
                    )
                else:
                    state["ready_receiver"].close()
                    state["result_receiver"].close()
                    if decoded.get("status") in {"passed", "approved"}:
                        try:
                            receipt = decoded.get("task_receipt")
                            if not isinstance(receipt, Mapping):
                                raise WorkflowReviewError(
                                    "normalized task receipt is missing"
                                )
                            validate_success_task_receipt(
                                definition,
                                plan,
                                task,
                                receipt,
                                root=root,
                            )
                        except WorkflowReviewError as exc:
                            decoded = _terminal_task_result(
                                definition,
                                plan,
                                task,
                                status="failed",
                                error=f"E_WORKFLOW_RECEIPT_RECHECK: {exc}",
                            )
                results.append(decoded)
                del active[task_id]
            elif now >= float(state["deadline"]):
                gone = _stop_isolated_task(state)
                results.append(
                    _terminal_task_result(
                        definition,
                        plan,
                        task,
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(task)
                            else "failed"
                        ),
                        error=(
                            "E_WORKFLOW_TIMEOUT"
                            if gone
                            else "E_WORKFLOW_TERMINATION_UNCONFIRMED"
                        ),
                    )
                )
                del active[task_id]
            elif exited_before_poll:
                state["process"].join()
                state["ready_receiver"].close()
                state["result_receiver"].close()
                results.append(
                    _terminal_task_result(
                        definition,
                        plan,
                        task,
                        status=(
                            "effect_unknown"
                            if _task_requires_terminable_executor(task)
                            else "failed"
                        ),
                        error="E_WORKFLOW_EXECUTOR_EXITED_WITHOUT_RESULT",
                    )
                )
                del active[task_id]
        start_available()
        if active:
            time.sleep(0.01)
    return results


def run_workflow(
    root: Path | str,
    definition: Mapping[str, Any],
    workflow_input: Mapping[str, Any],
    *,
    execute_task: Executor,
    repository_id: str = "OMG",
    run_generation: int = 0,
    repository_policy: Sequence[str],
    host_capabilities: Sequence[str],
    launch_receipt_permissions: Sequence[str],
    allowed_mcp: Sequence[str] | None = None,
    allowed_write_paths: Sequence[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    compiled = compile_workflow(definition)
    plan = build_plan(
        compiled,
        workflow_input,
        repository_id=repository_id,
        run_generation=run_generation,
    )
    artifact_dir = run_artifact_dir(root, plan["run_id"])
    ensure_managed_dir(artifact_dir)
    journal = artifact_dir / "journal.jsonl"
    admission = admit_definition(
        compiled,
        repository_policy=repository_policy,
        host_capabilities=host_capabilities,
        launch_receipt_permissions=launch_receipt_permissions,
        allowed_mcp=allowed_mcp,
        allowed_write_paths=allowed_write_paths,
    )
    _append(
        journal,
        {
            "event": "planned",
            "plan_digest": plan["plan_digest"],
            "run_generation": run_generation,
        },
    )
    if not admission["allowed"]:
        review = evaluate_review(compiled, plan, [], permission_denied=True)
        summary: dict[str, Any] = {
            "plan": plan,
            "admission": admission,
            "results": [],
            "review": review,
            "terminal": "blocked",
        }
        atomic_write_bytes(
            artifact_dir / "summary.json",
            canonical_json_bytes(summary),
            mode=DATA_FILE_MODE,
        )
        return summary
    cancellation = cancel_event or threading.Event()
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    results: list[dict[str, Any]] = []
    accepted: set[str] = set()
    for wave in plan["waves"]:
        if cancellation.is_set():
            break
        runnable = [
            task_id
            for task_id in wave
            if all(dependency in accepted for dependency in tasks[task_id]["dependencies"])
        ]
        if not runnable:
            continue
        wave_results = _run_wave(
            execute_task,
            compiled,
            plan,
            tasks,
            runnable,
            cancellation,
            root,
        )
        for result in wave_results:
            results.append(result)
            _append(journal, {"event": "task_result", "result": result})
            if result["status"] in {"passed", "approved"}:
                accepted.add(result["task_id"])
        if any(
            result["status"] not in {"passed", "approved"}
            for result in wave_results
        ):
            break
    task_order = {task["task_id"]: index for index, task in enumerate(plan["tasks"])}
    results.sort(key=lambda row: task_order[row["task_id"]])
    review = evaluate_review(compiled, plan, results, root=root)
    terminal = "cancelled" if cancellation.is_set() else review["terminal"]
    replay = assess_replay(plan, results)
    if replay["terminal"] == "effect_unknown":
        terminal = "effect_unknown"
    elif (
        replay["terminal"] == "blocked"
        and review.get("authority_error")
        != "E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE"
    ):
        terminal = "blocked"
    summary = {
        "plan": plan,
        "admission": admission,
        "results": results,
        "review": review,
        "replay": replay,
        "terminal": terminal,
    }
    _append(journal, {"event": "terminal", "terminal": terminal, "review": review})
    atomic_write_bytes(
        artifact_dir / "summary.json",
        canonical_json_bytes(summary),
        mode=DATA_FILE_MODE,
    )
    return summary


__all__ = ["Executor", "run_artifact_dir", "run_workflow"]
