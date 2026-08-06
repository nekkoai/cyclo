#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Durably publish the planner-visible terminal notice for a source job."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VALID_SOURCE_STATUSES = {"claimed", "running"}


def _read_regular_text(path: Path, maximum: int) -> str | None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(maximum + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > maximum:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_regular_file(path: Path) -> bool:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        return stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def planner_notification_id(task_id: str, job_id: str) -> str:
    digest = hashlib.sha256(f"{task_id}\0{job_id}".encode()).hexdigest()[:20]
    stem = job_id[:40].rstrip("._-")
    return f"{stem}-planner-terminal-{digest}"


def planner_notification_spec(task_id: str, job_id: str, source_role: str) -> str:
    return f"""# Notify Planner: {job_id}

## Task
{task_id}

## Source Job
{job_id}

## Source Role
{source_role}

## Event
source job terminal transition

## Settlement
AgentWS publishes this notice before the source job becomes terminal, but the
queue keeps it unclaimable until the source status is done or failed. Once this
job can be claimed, the source evidence is stable and ready to inspect.

## Follow-Up Jobs Created
Inspect the other jobs attached to this task. The source worker may also have
created a direct success-path handoff.

## Evidence
Read jobs/{job_id}/status, jobs/{job_id}/log.md, the task log, and any linked
artifacts after the source settles.

## When Done
Record the source outcome on the task, create any missing route required by the
team protocol, and then mark this planner notification done.
"""


def _notification_matches(
    notification_dir: Path,
    *,
    task_id: str,
    source_job_id: str,
    expected_spec: str,
) -> bool:
    try:
        directory_info = notification_dir.lstat()
        workspace_info = (notification_dir / "workspace").lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(directory_info.st_mode) or not stat.S_ISDIR(
        workspace_info.st_mode
    ):
        return False
    role = _read_regular_text(notification_dir / "role", 1024)
    task = _read_regular_text(notification_dir / "task-id", 1024)
    status = _read_regular_text(notification_dir / "status", 1024)
    source = _read_regular_text(
        notification_dir / ".terminal-notice-source", 1024
    )
    spec = _read_regular_text(notification_dir / "spec.md", 256 * 1024)
    return (
        role is not None
        and role.strip() == "planner"
        and task is not None
        and task.strip() == task_id
        and status is not None
        and status.strip() == "pending"
        and source is not None
        and source.strip() == source_job_id
        and spec == expected_spec
        and _is_regular_file(notification_dir / "log.md")
        and _is_regular_file(notification_dir / ".control.lock")
    )


def ensure_planner_notification(
    root: Path,
    job_dir: Path,
    job_id: str,
) -> str | None:
    """Return the notice ID, or ``None`` when the source itself is planner."""

    if not NAME_RE.fullmatch(job_id):
        raise RuntimeError(f"invalid source job ID '{job_id}'")
    try:
        job_info = job_dir.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect source job '{job_id}': {exc}") from exc
    if not stat.S_ISDIR(job_info.st_mode):
        raise RuntimeError(f"source job '{job_id}' is not a real directory")

    source_role_text = _read_regular_text(job_dir / "role", 1024)
    task_text = _read_regular_text(job_dir / "task-id", 1024)
    source_status_text = _read_regular_text(job_dir / "status", 1024)
    source_role = source_role_text.strip() if source_role_text is not None else ""
    task_id = task_text.strip() if task_text is not None else ""
    source_status = (
        source_status_text.strip() if source_status_text is not None else ""
    )
    if not NAME_RE.fullmatch(source_role) or not NAME_RE.fullmatch(task_id):
        raise RuntimeError(f"invalid role or task ID for source job '{job_id}'")
    if source_role == "planner":
        return None
    if source_status not in VALID_SOURCE_STATUSES:
        raise RuntimeError(
            f"cannot prepare planner notification for source job '{job_id}' "
            f"from status '{source_status or 'invalid'}'"
        )

    notification_id = planner_notification_id(task_id, job_id)
    notification_dir = root / "jobs" / notification_id
    spec = planner_notification_spec(task_id, job_id, source_role)
    if notification_dir.exists() or notification_dir.is_symlink():
        if _notification_matches(
            notification_dir,
            task_id=task_id,
            source_job_id=job_id,
            expected_spec=spec,
        ):
            return notification_id
        raise RuntimeError(
            f"planner notification collision for '{job_id}': {notification_id}"
        )

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="agentws-planner-notification-",
            suffix=".md",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(spec)
        process = subprocess.run(
            [
                str(root / "bin" / "job-create"),
                notification_id,
                "--role",
                "planner",
                "--task-id",
                task_id,
                "--terminal-notice-for",
                job_id,
                str(temporary),
            ],
            cwd=root,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        if _notification_matches(
            notification_dir,
            task_id=task_id,
            source_job_id=job_id,
            expected_spec=spec,
        ):
            return notification_id
        detail = (process.stderr or "").strip() or f"exit status {process.returncode}"
        raise RuntimeError(
            f"planner notification creation failed for '{job_id}': {detail}"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"planner notification creation failed for '{job_id}': {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
