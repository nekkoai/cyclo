from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import CycloError


INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "cyclo").resolve()


def slug(value: str, limit: int = 28) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._").lower()
    return (result or "team")[:limit]


def instance_id(team: Path, project: Path, name: str | None = None) -> str:
    if name:
        cleaned = slug(name, 48)
        if cleaned != name.lower():
            raise CycloError("instance name may use only letters, numbers, dot, underscore, and hyphen")
        return validate_instance_id(cleaned)
    digest = hashlib.sha256(f"{team.resolve()}\0{project.resolve()}".encode("utf-8")).hexdigest()[:12]
    return validate_instance_id(f"{slug(team.name)}-{digest}")


def validate_instance_id(value: str) -> str:
    if value in {".", ".."} or not INSTANCE_RE.fullmatch(value):
        raise CycloError(
            f"invalid Cyclo instance ID {value!r}; use 1-64 letters, numbers, dot, underscore, or hyphen"
        )
    return value


@dataclass
class Instance:
    id: str
    team_name: str
    team_path: str
    project_path: str
    generation: str
    providers: list[str]
    models: list[str]
    container_name: str
    network_name: str
    image: str
    team_write: bool
    project_read_only: bool
    offline: bool
    active: bool = False
    port: int | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Instance":
        return cls(**data)  # type: ignore[arg-type]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_state_root()).expanduser().resolve()
        self.instances_dir = self.root / "instances"
        self.gateway_registry = self.root / "gateway"
        self.lock_path = self.root / "control.lock"

    def ensure(self) -> None:
        for path in (self.root, self.instances_dir, self.gateway_registry):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.ensure()
        try:
            with self.lock_path.open("a+", encoding="utf-8") as stream:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise CycloError(f"cannot lock Cyclo state {self.lock_path}: {exc}") from exc

    def instance_dir(self, identifier: str) -> Path:
        path = self.instances_dir / validate_instance_id(identifier)
        if path.is_symlink():
            raise CycloError(f"refusing symlinked Cyclo instance directory: {path}")
        return path

    def metadata_path(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "run.json"

    def runtime_root(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "runtime"

    def queue_root(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "agentws-state"

    def tasks_dir(self, identifier: str) -> Path:
        return self.queue_root(identifier) / "tasks"

    def jobs_dir(self, identifier: str) -> Path:
        return self.queue_root(identifier) / "jobs"

    def agents_dir(self, identifier: str) -> Path:
        return self.queue_root(identifier) / "agents"

    def pi_root(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "pi"

    def load(self, identifier: str) -> Instance:
        identifier = validate_instance_id(identifier)
        path = self.metadata_path(identifier)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CycloError(f"Cyclo instance not found: {identifier}") from exc
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise CycloError(f"cannot read Cyclo instance {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CycloError(f"invalid Cyclo instance metadata: {path}")
        try:
            instance = Instance.from_json(data)
        except TypeError as exc:
            raise CycloError(f"invalid Cyclo instance metadata {path}: {exc}") from exc
        if instance.id != identifier:
            raise CycloError(
                f"Cyclo instance metadata ID mismatch: requested {identifier!r}, found {instance.id!r}"
            )
        return instance

    def list(self) -> list[Instance]:
        if not self.instances_dir.is_dir():
            return []
        result: list[Instance] = []
        for path in sorted(self.instances_dir.glob("*/run.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    instance = Instance.from_json(data)
                    validate_instance_id(instance.id)
                    if instance.id == path.parent.name:
                        result.append(instance)
            except (OSError, json.JSONDecodeError, TypeError, CycloError):
                continue
        return result

    def save(self, instance: Instance) -> None:
        validate_instance_id(instance.id)
        directory = self.instance_dir(instance.id)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        if not instance.created_at:
            instance.created_at = utc_now()
        instance.updated_at = utc_now()
        path = self.metadata_path(instance.id)
        temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps(instance.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
        except FileNotFoundError:
            pass

    def new_tree(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = target.with_name(f".{target.name}.new.{os.getpid()}.{os.urandom(6).hex()}")
        temporary.mkdir(mode=0o700)
        return temporary

    def replace_tree(self, temporary: Path, target: Path) -> None:
        quarantine = target.with_name(f".{target.name}.old.{os.getpid()}.{os.urandom(6).hex()}")
        moved_old = False
        installed = False
        try:
            if target.exists() or target.is_symlink():
                os.replace(target, quarantine)
                moved_old = True
            try:
                os.replace(temporary, target)
                installed = True
            except Exception as install_error:
                if moved_old and not target.exists() and not target.is_symlink():
                    try:
                        os.replace(quarantine, target)
                    except Exception as restore_error:
                        raise CycloError(
                            f"failed to install {target} and restore its previous tree; "
                            f"the previous tree is preserved at {quarantine}: {restore_error}"
                        ) from install_error
                raise
        finally:
            self._remove_tree(temporary)
            if moved_old and installed:
                self._remove_tree(quarantine)

    def materialize_agentws(self, identifier: str, template: Path, runtime_script: Path) -> Path:
        runtime = self.runtime_root(identifier)
        temporary = self.new_tree(runtime)

        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {
                name
                for name in names
                if name in {"tasks", "jobs", "agents", "__pycache__"} or name.endswith(".pyc")
            }

        try:
            # Copy into an empty sibling and atomically install it. symlinks=True
            # prevents a source symlink from making the host copy traverse elsewhere.
            shutil.copytree(
                template,
                temporary,
                dirs_exist_ok=True,
                ignore=ignore,
                symlinks=True,
            )
            for name in ("tasks", "jobs", "agents"):
                (temporary / name).mkdir(parents=True, exist_ok=True)
            runtime_bytes = runtime_script.read_bytes()
            runtime_destination = temporary / ".cyclo-runtime.py"
            if runtime_destination.exists() or runtime_destination.is_symlink():
                runtime_destination.unlink()
            runtime_destination.write_bytes(runtime_bytes)
            os.chmod(runtime_destination, 0o555)
            self.replace_tree(temporary, runtime)
        except Exception:
            self._remove_tree(temporary)
            raise

        queue = self.queue_root(identifier)
        if queue.is_symlink():
            raise CycloError(f"refusing symlinked AgentWS state root: {queue}")
        queue.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (self.tasks_dir(identifier), self.jobs_dir(identifier), self.agents_dir(identifier)):
            if path.is_symlink():
                raise CycloError(f"refusing symlinked AgentWS state directory: {path}")
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return runtime
