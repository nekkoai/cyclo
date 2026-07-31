# SPDX-License-Identifier: MIT
"""Read-only implementations for the AgentWS task CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from queue_reader import (
    ConfinedQueueReader,
    terminal_safe,
    validate_queue_id,
    validate_task_state,
)


AGENTWS_ROOT = Path(__file__).resolve().parents[1]


def _fail(message: str) -> int:
    print(f"error: {terminal_safe(message)}", file=sys.stderr)
    return 1


def _state(queue: ConfinedQueueReader, task_id: str) -> str:
    try:
        value = queue.read_file((task_id, "state"), max_bytes=128)[0]
    except FileNotFoundError:
        value = "open"
    except OSError as exc:
        raise ValueError(f"task '{task_id}' has an unreadable state") from exc
    return validate_task_state(value.strip() or "open")


def _tasks_dir() -> Path:
    return Path(os.environ.get("TASKS_DIR", AGENTWS_ROOT / "tasks"))


def list_main(arguments: list[str]) -> int:
    if arguments:
        print("Usage: task-list")
        return 1
    try:
        with ConfinedQueueReader(_tasks_dir()) as queue:
            for task_id in queue.list_directories(()):
                try:
                    validate_queue_id(task_id, "task ID")
                except ValueError as exc:
                    raise ValueError(
                        "tasks directory contains an invalid task ID"
                    ) from exc
                print(f"{task_id}\t{_state(queue, task_id)}")
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
    return 0


def show_main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("Usage: task-show <task-id>")
        return 1
    try:
        task_id = validate_queue_id(arguments[0], "task ID")
    except ValueError as exc:
        return _fail(str(exc))

    try:
        with ConfinedQueueReader(_tasks_dir()) as queue:
            if not queue.has_directory((task_id,)):
                return _fail(f"task '{task_id}' does not exist")
            state = _state(queue, task_id)
            spec = queue.read_file(
                (task_id, "spec.md"), max_bytes=160 * 1024
            )[0]
            log = queue.read_file(
                (task_id, "log.md"), max_bytes=512 * 1024
            )[0]
            result = (
                queue.read_file(
                    (task_id, "result.md"), max_bytes=160 * 1024
                )[0]
                if queue.has_regular_file((task_id, "result.md"))
                else None
            )
    except (OSError, ValueError) as exc:
        return _fail(str(exc))

    print(f"# Task {task_id}")
    print()
    print(f"State: {state}")
    print()
    print("## Spec")
    sys.stdout.write(terminal_safe(spec))
    print()
    print("## Log")
    sys.stdout.write(terminal_safe(log))
    if result is not None:
        print()
        print("## Result")
        sys.stdout.write(terminal_safe(result))
    return 0
