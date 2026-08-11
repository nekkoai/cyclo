from __future__ import annotations

import errno
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from ..errors import CycloError
from ..images import Images
from ..state import Instance, StateStore


_TOOL_LABEL = "io.cyclo.task-tool"
_TASKS = "/agentws/tasks"
_JOBS = "/agentws/jobs"
_SPEC = "/run/cyclo/queue-spec.md"
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_SPEC_TOOLS = frozenset({"task-create", "job-create"})
_TOOL_AUTHORITY = {
    "task-list": (("tasks", True),),
    "task-show": (("tasks", True),),
    "task-comment": (("tasks", False),),
    "task-state": (("tasks", False),),
    "task-create": (("tasks", False), ("jobs", False)),
    "job-create": (("tasks", True), ("jobs", False)),
}


class TaskAdmin:
    """Run AgentWS administration inside a container with queue-only authority."""

    def __init__(self, store: StateStore, instance: Instance) -> None:
        self.store = store
        self.instance = instance
        self.images = Images(endpoint=store.docker_endpoint)

    def run(
        self,
        tool: str,
        arguments: Sequence[str] = (),
        *,
        specification: bytes | None = None,
    ) -> int:
        if os.getuid() == 0:
            raise CycloError("Cyclo task administration refuses host root")
        if tool not in _TOOL_AUTHORITY:
            raise CycloError(f"invalid AgentWS task tool: {tool!r}")
        if (specification is not None) != (tool in _SPEC_TOOLS):
            raise CycloError(
                "a specification is required exactly for task-create or job-create"
            )
        self._ensure_queue()
        with self._staged_specification(specification) as staged:
            self._remove_abandoned()
            name = (
                f"cyclo-{self.store.system}-task-tool-"
                f"{uuid.uuid4().hex}"
            )
            command = [
                "run",
                "--rm",
                "--init",
                "--read-only",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--name",
                name,
                "--label",
                f"{_TOOL_LABEL}={self.store.system}",
                "--network",
                "none",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "256",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--env",
                "CYCLO_ADMIN_TOOL=1",
            ]
            queue_paths = {
                "tasks": (self.store.tasks_dir(self.instance.id), _TASKS),
                "jobs": (self.store.jobs_dir(self.instance.id), _JOBS),
            }
            for queue, read_only in _TOOL_AUTHORITY[tool]:
                source, target = queue_paths[queue]
                command.extend(
                    ("--mount", _bind(source, target, read_only=read_only))
                )
            selected_arguments = list(arguments)
            if staged is not None:
                command.extend(("--mount", _bind(staged, _SPEC, read_only=True)))
                selected_arguments.append(_SPEC)
            command.extend(
                (
                    self.instance.image,
                    f"/agentws/bin/{tool}",
                    *selected_arguments,
                )
            )
            result = self.images.command(command, check=False, capture=False)
            return result.returncode

    def _ensure_queue(self) -> None:
        paths = (
            self.store.queue_root(self.instance.id),
            self.store.tasks_dir(self.instance.id),
            self.store.jobs_dir(self.instance.id),
        )
        try:
            for path in paths:
                self.store.prepare_directory(path, "AgentWS state directory")
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"cannot prepare AgentWS task queue for "
                f"{self.instance.id}: {exc}"
            ) from exc

    @contextmanager
    def _staged_specification(
        self,
        content: bytes | None,
    ) -> Iterator[Path | None]:
        if content is None:
            yield None
            return
        self.store.ensure()
        descriptor, name = tempfile.mkstemp(
            prefix=".queue-spec-",
            dir=self.store.root,
        )
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o400)
            yield path
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            path.unlink(missing_ok=True)

    def _remove_abandoned(self) -> None:
        result = self.images.command(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={_TOOL_LABEL}={self.store.system}",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise CycloError("cannot enumerate abandoned task tools")
        for identifier in sorted(set((result.stdout or "").splitlines())):
            if not identifier:
                continue
            removed = self.images.command(
                ["container", "rm", "--force", "--", identifier],
                check=False,
            )
            if removed.returncode != 0:
                raise CycloError(
                    f"cannot remove abandoned task tool {identifier}"
                )


def _bind(source: Path, target: str, *, read_only: bool = False) -> str:
    try:
        selected = source.resolve(strict=True)
    except OSError as exc:
        raise CycloError(f"task tool bind source is unavailable: {source}") from exc
    value = (
        f"type=bind,src={_csv(os.fspath(selected))},"
        f"dst={_csv(target)}"
    )
    return value + (",readonly" if read_only else "")


def _csv(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise CycloError("task tool mount path contains an unsupported character")
    if "," not in value and '"' not in value:
        return value
    return '"' + value.replace('"', '""') + '"'


def read_task_specification(path: Path) -> bytes:
    """Read one bounded regular file without following any path symlink."""

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise CycloError("safe task specification reads are unavailable")
    selected = Path(os.path.abspath(path.expanduser()))
    parts = selected.parts[1:]
    if not parts:
        raise CycloError(f"task specification is not a regular file: {selected}")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open("/", directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | os.O_NONBLOCK
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise CycloError(
                "task specification path must contain only real directories "
                f"and a regular file: {selected}"
            ) from exc
        raise CycloError(f"task specification is unavailable: {selected}") from exc
    finally:
        os.close(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise CycloError(
                f"task specification is not a regular file: {selected}"
            )
        with os.fdopen(file_descriptor, "rb", closefd=True) as stream:
            file_descriptor = -1
            content = stream.read(_MAX_SPEC_BYTES + 1)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    if len(content) > _MAX_SPEC_BYTES:
        raise CycloError(
            f"task specification exceeds the {_MAX_SPEC_BYTES}-byte limit: "
            f"{selected}"
        )
    return content
