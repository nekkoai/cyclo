from __future__ import annotations

import errno
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SAFE_QUEUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KNOWN_JOB_STATUSES = ("pending", "claimed", "running", "done", "failed")
JOB_STATUSES = (*KNOWN_JOB_STATUSES, "unknown")
SUPERVISOR_DIRECTORY = ".team-runs"
SUSPENDED_SUFFIX = ".suspended"
SUPERVISOR_READY_FILE = "supervisor.ready"
_OPEN_DIRECTORY = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_FILE = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class QueueLimits:
    """Hard entry and byte limits for one dashboard queue scan."""

    max_entries: int = 4096
    max_read_bytes: int = 2 * 1024 * 1024
    recent_tasks: int = 8
    recent_activity: int = 12

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_read_bytes",
            "recent_tasks",
            "recent_activity",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass
class _ScanBudget:
    limits: QueueLimits
    entries: int = 0
    read_bytes: int = 0
    truncated: bool = False

    def take_entry(self) -> bool:
        if self.entries >= self.limits.max_entries:
            self.truncated = True
            return False
        self.entries += 1
        return True

    def read_allowance(self, requested: int) -> int:
        remaining = self.limits.max_read_bytes - self.read_bytes
        if remaining <= 0:
            self.truncated = True
            return 0
        return min(requested, remaining)


@dataclass(frozen=True)
class AgentSupervisorStatus:
    """Host-visible state written by the AgentWS team supervisor."""

    suspended_agents: tuple[str, ...] = ()
    planner_attention_jobs: tuple[str, ...] = ()
    error: str = ""


def empty_counts() -> dict[str, dict[str, int]]:
    return {
        "tasks": {"total": 0, "open": 0, "closed": 0},
        "jobs": {"total": 0, **{name: 0 for name in JOB_STATUSES}},
        "agents": {"total": 0, "active": 0},
    }


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _open_directory(path: Path) -> int:
    """Open every path component without following a mutable parent symlink."""

    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", _OPEN_DIRECTORY | _OPEN_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            replacement = _open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = replacement
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, _OPEN_DIRECTORY | _OPEN_NOFOLLOW, dir_fd=parent_fd)


def read_agent_supervisor_status(
    root: Path,
    *,
    max_entries: int = 4096,
    max_ready_age_seconds: float = 15.0,
    now: float | None = None,
) -> AgentSupervisorStatus:
    """Read supervisor readiness and durable attention without following links."""

    if max_entries < 0:
        raise ValueError("max_entries must not be negative")
    if max_ready_age_seconds <= 0:
        raise ValueError("max_ready_age_seconds must be positive")
    selected_now = time.time() if now is None else now
    descriptors: list[int] = []
    suspended: set[str] = set()
    planner_attention: set[str] = set()
    errors: list[str] = []
    entries_seen = 0

    def take_entry() -> bool:
        nonlocal entries_seen
        if entries_seen >= max_entries:
            errors.append(f"supervisor scan exceeded {max_entries} entries")
            return False
        entries_seen += 1
        return True

    def read_small_regular(directory_fd: int, name: str) -> str | None:
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                _OPEN_FILE | _OPEN_NOFOLLOW,
                dir_fd=directory_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            return os.read(descriptor, 256).decode("utf-8", errors="replace")
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    try:
        root_fd = _open_directory(root)
        descriptors.append(root_fd)
        try:
            agents_fd = _open_directory_at(root_fd, "agents")
            descriptors.append(agents_fd)
            runs_fd = _open_directory_at(agents_fd, SUPERVISOR_DIRECTORY)
            descriptors.append(runs_fd)
        except OSError as exc:
            errors.append(
                "supervisor state directory unavailable: "
                f"{exc.strerror or str(exc) or type(exc).__name__}"
            )
        else:
            try:
                ready = os.stat(
                    SUPERVISOR_READY_FILE,
                    dir_fd=runs_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                errors.append("supervisor readiness marker missing")
            except OSError as exc:
                errors.append(
                    "cannot inspect supervisor readiness marker: "
                    f"{exc.strerror or str(exc) or type(exc).__name__}"
                )
            else:
                if not stat.S_ISREG(ready.st_mode):
                    errors.append("supervisor readiness marker is not a regular file")
                else:
                    age = max(0.0, selected_now - ready.st_mtime)
                    if age > max_ready_age_seconds:
                        errors.append(
                            "supervisor heartbeat stale: "
                            f"{age:.1f}s old (maximum {max_ready_age_seconds:.1f}s)"
                        )

            with os.scandir(runs_fd) as entries:
                for entry in entries:
                    if not take_entry():
                        break
                    if not entry.name.endswith(SUSPENDED_SUFFIX):
                        continue
                    agent_id = entry.name[: -len(SUSPENDED_SUFFIX)]
                    if not SAFE_QUEUE_ID.fullmatch(agent_id):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        suspended.add(agent_id)

        try:
            jobs_fd = _open_directory_at(root_fd, "jobs")
            descriptors.append(jobs_fd)
        except OSError as exc:
            errors.append(
                "planner attention state unavailable: "
                f"{exc.strerror or str(exc) or type(exc).__name__}"
            )
        else:
            with os.scandir(jobs_fd) as entries:
                for entry in entries:
                    if not take_entry():
                        break
                    if not SAFE_QUEUE_ID.fullmatch(entry.name):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        job_fd = _open_directory_at(jobs_fd, entry.name)
                    except OSError:
                        continue
                    try:
                        role = read_small_regular(job_fd, "role")
                        status_value = read_small_regular(job_fd, "status")
                        if (
                            role is not None
                            and role.strip() == "planner"
                            and status_value is not None
                            and status_value.strip() == "failed"
                        ):
                            planner_attention.add(entry.name)
                    finally:
                        os.close(job_fd)

        return AgentSupervisorStatus(
            tuple(sorted(suspended)),
            tuple(sorted(planner_attention)),
            "; ".join(dict.fromkeys(errors)),
        )
    except OSError as exc:
        detail = exc.strerror or str(exc) or type(exc).__name__
        return AgentSupervisorStatus(
            tuple(sorted(suspended)),
            tuple(sorted(planner_attention)),
            detail,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular_at(
    directory_fd: int,
    name: str,
    budget: _ScanBudget,
    *,
    max_bytes: int,
    default: str = "",
) -> str:
    allowance = budget.read_allowance(max_bytes)
    if allowance == 0:
        return default
    try:
        descriptor = os.open(name, _OPEN_FILE | _OPEN_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        return default
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return default
        raw = os.read(descriptor, allowance)
        budget.read_bytes += len(raw)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return default
    finally:
        os.close(descriptor)


def _regular_file_at(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _latest_regular_mtime_at(
    directory_fd: int, baseline: float, names: tuple[str, ...]
) -> float:
    result = baseline
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            result = max(result, info.st_mtime)
    return result


def _first_title(value: str, fallback: str) -> str:
    for line in value.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title[:160]
    for paragraph in value.split("\n\n"):
        title = " ".join(paragraph.split())
        if title:
            return title[:160]
    return fallback


def _iter_queue_directories(
    parent_fd: int,
    budget: _ScanBudget,
    errors: list[str],
    *,
    ignored_names: frozenset[str] = frozenset(),
):
    try:
        entries = os.scandir(parent_fd)
    except OSError as exc:
        errors.append(f"cannot list queue directory: {exc.strerror or exc}")
        return
    with entries:
        for entry in entries:
            if not budget.take_entry():
                return
            if entry.name in ignored_names:
                continue
            if not SAFE_QUEUE_ID.fullmatch(entry.name):
                errors.append("ignored an unsafe queue entry name")
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child_fd = _open_directory_at(parent_fd, entry.name)
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                    errors.append(
                        f"cannot inspect queue entry {entry.name}: "
                        f"{exc.strerror or exc}"
                    )
                continue
            try:
                yield entry.name, child_fd, os.fstat(child_fd)
            finally:
                os.close(child_fd)


def _open_category(root_fd: int, name: str, errors: list[str]) -> int | None:
    try:
        return _open_directory_at(root_fd, name)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
            errors.append(f"AgentWS queue directory is unavailable: {name}")
        else:
            errors.append(
                f"cannot open AgentWS queue directory {name}: {exc.strerror or exc}"
            )
        return None


def scan_agentws_queue(
    root: Path, limits: QueueLimits | None = None
) -> dict[str, object]:
    """Read a shallow, bounded AgentWS queue without following symlinks."""

    limits = limits or QueueLimits()
    budget = _ScanBudget(limits)
    errors: list[str] = []
    task_rows: list[dict[str, object]] = []
    job_rows: list[dict[str, object]] = []
    agent_ids: set[str] = set()

    try:
        root_fd = _open_directory(root)
    except OSError as exc:
        return {
            "counts": empty_counts(),
            "recent_tasks": [],
            "recent_activity": [],
            "errors": [f"AgentWS queue is unavailable: {exc.strerror or exc}"],
        }

    try:
        tasks_fd = _open_category(root_fd, "tasks", errors)
        if tasks_fd is not None:
            try:
                for task_id, task_fd, info in _iter_queue_directories(
                    tasks_fd, budget, errors
                ):
                    state = _read_regular_at(
                        task_fd, "state", budget, max_bytes=128, default="open"
                    ).strip().lower() or "open"
                    task_rows.append(
                        {
                            "id": task_id,
                            "state": (
                                "closed"
                                if state != "open"
                                or _regular_file_at(task_fd, "result.md")
                                else "open"
                            ),
                            "updated_ts": _latest_regular_mtime_at(
                                task_fd,
                                info.st_mtime,
                                ("state", "spec.md", "log.md", "result.md"),
                            ),
                        }
                    )
            finally:
                os.close(tasks_fd)

        jobs_fd = _open_category(root_fd, "jobs", errors)
        if jobs_fd is not None:
            try:
                for job_id, job_fd, info in _iter_queue_directories(
                    jobs_fd, budget, errors
                ):
                    status_value = _read_regular_at(
                        job_fd, "status", budget, max_bytes=64, default="unknown"
                    ).strip().lower() or "unknown"
                    if status_value not in KNOWN_JOB_STATUSES:
                        status_value = "unknown"
                    job_rows.append(
                        {
                            "id": job_id,
                            "status": status_value,
                            "task_id": _read_regular_at(
                                job_fd, "task-id", budget, max_bytes=128
                            ).strip(),
                            "agent_id": _read_regular_at(
                                job_fd, "agent-id", budget, max_bytes=128
                            ).strip(),
                            "updated_ts": _latest_regular_mtime_at(
                                job_fd,
                                info.st_mtime,
                                (
                                    "status",
                                    "log.md",
                                    "spec.md",
                                    "agent-id",
                                    "task-id",
                                    "role",
                                ),
                            ),
                        }
                    )
            finally:
                os.close(jobs_fd)

        agents_fd = _open_category(root_fd, "agents", errors)
        if agents_fd is not None:
            try:
                for agent_id, _agent_fd, _info in _iter_queue_directories(
                    agents_fd,
                    budget,
                    errors,
                    ignored_names=frozenset({SUPERVISOR_DIRECTORY}),
                ):
                    agent_ids.add(agent_id)
            finally:
                os.close(agents_fd)

        recent_tasks = [
            dict(item)
            for item in sorted(
                task_rows,
                key=lambda item: (-float(item["updated_ts"]), str(item["id"])),
            )[: limits.recent_tasks]
        ]
        if recent_tasks:
            tasks_fd = _open_category(root_fd, "tasks", errors)
            if tasks_fd is not None:
                try:
                    for task in recent_tasks:
                        task_id = str(task["id"])
                        try:
                            task_fd = _open_directory_at(tasks_fd, task_id)
                        except OSError:
                            task["title"] = task_id
                            continue
                        try:
                            spec = _read_regular_at(
                                task_fd, "spec.md", budget, max_bytes=4096
                            )
                            task["title"] = _first_title(spec, task_id)
                        finally:
                            os.close(task_fd)
                finally:
                    os.close(tasks_fd)
        for task in recent_tasks:
            task["updated_at"] = _timestamp(float(task.pop("updated_ts")))

        activity: list[dict[str, object]] = [
            {
                "kind": "task",
                "id": str(task["id"]),
                "state": str(task["state"]),
                "updated_ts": float(task["updated_ts"]),
            }
            for task in task_rows
        ]
        activity.extend(
            {
                "kind": "job",
                "id": str(job["id"]),
                "status": str(job["status"]),
                "task_id": str(job["task_id"]),
                "agent": str(job["agent_id"]),
                "updated_ts": float(job["updated_ts"]),
            }
            for job in job_rows
        )
        recent_activity = sorted(
            activity,
            key=lambda item: (-float(item["updated_ts"]), str(item["id"])),
        )[: limits.recent_activity]
        for item in recent_activity:
            item["updated_at"] = _timestamp(float(item.pop("updated_ts")))

        if budget.truncated:
            errors.append(
                f"queue scan truncated at {limits.max_entries} entries or "
                f"{limits.max_read_bytes} bytes"
            )
        statuses = {name: 0 for name in JOB_STATUSES}
        for item in job_rows:
            status_value = str(item["status"])
            if status_value in statuses:
                statuses[status_value] += 1
        if statuses["unknown"]:
            count = statuses["unknown"]
            errors.append(
                f"{count} job{'s have' if count != 1 else ' has'} an unknown "
                "or unreadable status"
            )
        active_agent_ids = {
            str(item["agent_id"])
            for item in job_rows
            if item["status"] in {"claimed", "running"} and item["agent_id"]
        }
        task_open = sum(item["state"] == "open" for item in task_rows)
        return {
            "counts": {
                "tasks": {
                    "total": len(task_rows),
                    "open": task_open,
                    "closed": len(task_rows) - task_open,
                },
                "jobs": {"total": len(job_rows), **statuses},
                "agents": {
                    "total": len(agent_ids),
                    "active": len(agent_ids & active_agent_ids),
                },
            },
            "recent_tasks": recent_tasks,
            "recent_activity": recent_activity,
            "errors": list(dict.fromkeys(errors)),
        }
    finally:
        os.close(root_fd)
