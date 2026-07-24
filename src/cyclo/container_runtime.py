#!/usr/bin/env python3
"""Supervise AgentWS's queue runner and read-only web viewer in a Cyclo container."""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


RUNTIME_LOCK_FD_ENV = "CYCLO_RUNTIME_LOCK_FD"
MAX_PROJECT_CONFIG_BYTES = 1024 * 1024


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"cyclo runtime: missing ${name}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    return value


def terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def acquire_runtime_lock(runtime: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(runtime, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(f"runtime root is not a directory: {runtime}")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def require_project_config(runtime: Path) -> Path:
    """Require the immutable, instance-wide project configuration."""

    config = runtime / "project.cyclo"
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(config, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"not a regular file: {config}")
        chunks: list[bytes] = []
        remaining = MAX_PROJECT_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_PROJECT_CONFIG_BYTES:
            raise OSError(f"file is too large: {config}")
        if not content.strip():
            raise OSError(f"file is empty: {config}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OSError(f"file is not valid UTF-8: {config}") from exc
    finally:
        os.close(descriptor)
    return config


def main() -> int:
    runtime = Path(required("CYCLO_AGENTWS_RUNTIME"))
    roster = required("AGENTWS_TEAM_ROSTER")
    try:
        require_project_config(runtime)
    except OSError as exc:
        print(
            f"cyclo runtime: invalid project configuration "
            f"{runtime / 'project.cyclo'}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 70
    verbose = os.environ.get("CYCLO_VERBOSE") == "1"
    stopping = False
    processes: list[subprocess.Popen[bytes]] = []
    runtime_lock = -1

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    viewer = [
        str(runtime / "tools" / "agentws"),
        "--root",
        str(runtime),
        "--no-team",
        "--no-console",
        "--host",
        "0.0.0.0",
        "--port",
        "4137",
        "--pin-root",
        "--read-only",
    ]
    runner = [str(runtime / "tools" / "run_agentws")]
    if verbose:
        runner.append("--verbose")
    runner.append(roster)

    try:
        runtime_lock = acquire_runtime_lock(runtime)
    except OSError as exc:
        print(
            f"cyclo runtime: cannot acquire the queue lifetime lock: {exc}",
            file=sys.stderr,
            flush=True,
        )
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        return 70

    try:
        recovery_environment = os.environ.copy()
        recovery_environment[RUNTIME_LOCK_FD_ENV] = str(runtime_lock)
        recovery = subprocess.Popen(
            [str(runtime / "bin" / "job-reset-orphans"), "--all-active"],
            env=recovery_environment,
            pass_fds=(runtime_lock,),
            start_new_session=True,
        )
        processes.append(recovery)
        while recovery.poll() is None and not stopping:
            time.sleep(0.05)
        if stopping:
            return 0
        recovery_status = recovery.wait()
        processes.remove(recovery)
        if recovery_status != 0:
            print(
                "cyclo runtime: cannot recover queue state from the previous "
                f"container generation (status {recovery_status})",
                file=sys.stderr,
                flush=True,
            )
            return recovery_status or 1

        for command in (viewer, runner):
            if stopping:
                return 0
            processes.append(subprocess.Popen(command, start_new_session=True))
        while not stopping:
            for process, label in zip(processes, ("viewer", "team")):
                status = process.poll()
                if status is not None:
                    print(f"cyclo runtime: AgentWS {label} exited with status {status}", file=sys.stderr, flush=True)
                    return status or 1
            time.sleep(0.25)
        return 0
    finally:
        terminate(processes)
        os.close(runtime_lock)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
