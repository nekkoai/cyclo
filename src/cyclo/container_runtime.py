#!/usr/bin/env python3
"""Supervise AgentWS's queue runner and read-only web viewer in a Cyclo container."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


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


def main() -> int:
    runtime = Path(required("CYCLO_AGENTWS_RUNTIME"))
    roster = required("AGENTWS_TEAM_ROSTER")
    verbose = os.environ.get("CYCLO_VERBOSE") == "1"
    stopping = False
    processes: list[subprocess.Popen[bytes]] = []

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

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
        for command in (viewer, runner):
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


if __name__ == "__main__":
    raise SystemExit(main())
