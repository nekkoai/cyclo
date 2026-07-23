from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import CycloError
from .installation import installation_id, team_container_name, team_network_name


INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_AGENTWS_HOST = "127.0.0.1"


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
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not INSTANCE_RE.fullmatch(value)
    ):
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
    offline: bool
    image_override: str = ""
    agentws_host: str = DEFAULT_AGENTWS_HOST
    active: bool = False
    port: int | None = None
    created_at: str = ""
    updated_at: str = ""
    project_name: str = ""
    project_file: str = ""
    project_description: str = ""
    project_generation: str = ""
    project_mounts: list[dict[str, str]] = field(default_factory=list)
    launch_id: str = ""
    provider_socket_path: str = ""
    provider_generation: str = ""

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Instance":
        payload = dict(data)
        string_fields = (
            "id",
            "team_name",
            "team_path",
            "project_path",
            "generation",
            "container_name",
            "network_name",
            "image",
            "image_override",
            "agentws_host",
            "created_at",
            "updated_at",
            "project_name",
            "project_file",
            "project_description",
            "project_generation",
            "launch_id",
            "provider_socket_path",
            "provider_generation",
        )
        for name in string_fields:
            if name in payload and not isinstance(payload[name], str):
                raise TypeError(f"{name} must be a string")
        for name in (
            "team_write",
            "offline",
            "active",
        ):
            if name in payload and type(payload[name]) is not bool:
                raise TypeError(f"{name} must be a boolean")
        for name in ("providers", "models"):
            if name not in payload:
                continue
            value = payload[name]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise TypeError(f"{name} must be a list of strings")
        port = payload.get("port")
        if port is not None and (
            type(port) is not int or port < 1 or port > 65535
        ):
            raise TypeError("port must be null or an integer from 1 to 65535")
        instance = cls(**payload)  # type: ignore[arg-type]
        validate_instance_id(instance.id)
        if instance.provider_socket_path:
            provider_socket = Path(instance.provider_socket_path)
            if not provider_socket.is_absolute():
                raise TypeError("provider_socket_path must be empty or absolute")
            if provider_socket.name != "component.sock":
                raise TypeError("provider_socket_path must end in component.sock")
        if bool(instance.provider_socket_path) != bool(instance.provider_generation):
            raise TypeError(
                "provider_socket_path and provider_generation must be set together"
            )
        namespaced = re.fullmatch(
            rf"cyclo-[0-9a-f]{{12}}-team-{re.escape(instance.id)}",
            instance.container_name,
        )
        if namespaced is None:
            raise TypeError(
                "container_name must be a Cyclo team resource for instance "
                f"{instance.id!r}"
            )
        if instance.network_name != f"{instance.container_name}-net":
            raise TypeError(
                "network_name must match the Cyclo team container for instance "
                f"{instance.id!r}"
            )
        # Keep the persistence boundary and every project-state consumer on
        # the same decoder. The local import avoids a state/project_state
        # module cycle.
        from .project_state import decode_instance_project

        decode_instance_project(instance).require_valid()
        return instance

    def as_json(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_state_root()).expanduser().resolve()
        self.instances_dir = self.root / "instances"
        self.components_root = self.root / "components"
        self.lock_path = self.root / "control.lock"

    @property
    def system(self) -> str:
        """Stable installation identity derived from the canonical state root."""

        # Gateway and provider resources have always used the component-state
        # root as their namespace input. Reuse it so every Docker resource in
        # this installation carries one identity without renaming those stores.
        return installation_id(self.components_root)

    def _validate_resource_namespace(self, instance: Instance) -> None:
        expected = (
            team_container_name(self.system, instance.id),
            team_network_name(self.system, instance.id),
        )
        actual = (instance.container_name, instance.network_name)
        if actual != expected:
            raise CycloError(
                f"instance {instance.id!r} belongs to another Cyclo installation"
            )

    def ensure(self) -> None:
        for path in (self.root, self.instances_dir, self.components_root):
            if path.is_symlink():
                raise CycloError(f"refusing symlinked Cyclo state directory: {path}")
            if path.exists() and not path.is_dir():
                raise CycloError(f"Cyclo state path is not a directory: {path}")
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

    def workspace_root(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "workspace"

    def readonly_root(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "readonly"

    def load(self, identifier: str) -> Instance:
        identifier = validate_instance_id(identifier)
        path = self.metadata_path(identifier)
        try:
            data = self._read_metadata(path)
        except FileNotFoundError as exc:
            raise CycloError(f"Cyclo instance not found: {identifier}") from exc
        except (OSError, ValueError, TypeError, RecursionError, CycloError) as exc:
            raise CycloError(f"cannot read Cyclo instance {path}: {exc}") from exc
        try:
            instance = Instance.from_json(data)
            self._validate_resource_namespace(instance)
        except (TypeError, CycloError) as exc:
            raise CycloError(f"invalid Cyclo instance metadata {path}: {exc}") from exc
        if instance.id != identifier:
            raise CycloError(
                f"Cyclo instance metadata ID mismatch: requested {identifier!r}, found {instance.id!r}"
            )
        return instance

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, object]:
        """Read one regular, non-symlink JSON object without following links."""

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
                raise CycloError("run.json is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                data = json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(data, dict):
            raise CycloError("metadata is not a JSON object")
        return data

    def list(self) -> list[Instance]:
        """Return the complete instance fleet or fail on any unreadable record."""

        instances, errors = self.list_report()
        if errors:
            raise CycloError(
                "cannot enumerate Cyclo instance state: " + "; ".join(errors)
            )
        return instances

    def list_report(self) -> tuple[list[Instance], list[str]]:
        """Return readable instances and a separate error for every bad record."""

        if self.instances_dir.is_symlink():
            return [], [
                f"invalid Cyclo instances directory: {self.instances_dir}"
            ]
        if not self.instances_dir.exists():
            return [], []
        if not self.instances_dir.is_dir():
            return [], [
                f"invalid Cyclo instances directory: {self.instances_dir}"
            ]
        result: list[Instance] = []
        errors: list[str] = []
        try:
            directories = sorted(self.instances_dir.iterdir())
        except OSError as exc:
            return [], [
                f"cannot enumerate Cyclo instances directory "
                f"{self.instances_dir}: {exc}"
            ]
        for directory in directories:
            path = directory / "run.json"
            try:
                if directory.is_symlink() or not directory.is_dir():
                    raise CycloError("instance state entry is not a directory")
                data = self._read_metadata(path)
                instance = Instance.from_json(data)
                self._validate_resource_namespace(instance)
                validate_instance_id(instance.id)
                if instance.id != path.parent.name:
                    raise CycloError(
                        f"metadata ID {instance.id!r} does not match directory "
                        f"{path.parent.name!r}"
                    )
                result.append(instance)
            except (
                OSError,
                ValueError,
                TypeError,
                RecursionError,
                CycloError,
            ) as exc:
                detail = str(exc) or type(exc).__name__
                errors.append(f"invalid Cyclo instance metadata {path}: {detail}")
        return result, errors

    def save(self, instance: Instance) -> None:
        validate_instance_id(instance.id)
        payload = instance.as_json()
        # Refuse to persist an internally constructed record that the strict
        # reader would reject on the next command.
        try:
            Instance.from_json(payload)
            self._validate_resource_namespace(instance)
        except (TypeError, CycloError) as exc:
            raise CycloError(
                f"invalid Cyclo instance metadata for {instance.id!r}: {exc}"
            ) from exc
        directory = self.instance_dir(instance.id)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        if not instance.created_at:
            instance.created_at = utc_now()
        instance.updated_at = utc_now()
        path = self.metadata_path(instance.id)
        temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        payload = instance.as_json()
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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

    def materialize_agentws(
        self,
        identifier: str,
        template: Path,
        runtime_script: Path,
        *,
        project_manifest: str | None = None,
    ) -> Path:
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
            if project_manifest is not None:
                manifest_destination = temporary / "PROJECT.md"
                if manifest_destination.exists() or manifest_destination.is_symlink():
                    manifest_destination.unlink()
                manifest_destination.write_text(
                    project_manifest.rstrip() + "\n", encoding="utf-8"
                )
                os.chmod(manifest_destination, 0o444)
            self.replace_tree(temporary, runtime)
        except Exception:
            self._remove_tree(temporary)
            raise

        queue = self.queue_root(identifier)
        if queue.is_symlink():
            raise CycloError(f"refusing symlinked AgentWS state root: {queue}")
        queue.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (
            self.tasks_dir(identifier),
            self.jobs_dir(identifier),
            self.agents_dir(identifier),
        ):
            if path.is_symlink():
                raise CycloError(
                    f"refusing symlinked AgentWS state directory: {path}"
                )
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return runtime

    def _materialize_named_layout(
        self, target: Path, mount_names: list[str]
    ) -> Path:
        temporary = self.new_tree(target)
        try:
            for name in mount_names:
                validate_instance_id(name)
                child = temporary / name
                child.mkdir(mode=0o755)
            self.replace_tree(temporary, target)
        except Exception:
            self._remove_tree(temporary)
            raise
        return target

    def materialize_workspace_layout(
        self, identifier: str, mount_names: list[str]
    ) -> Path:
        """Create the inert namespace containing writable workspace mounts."""

        return self._materialize_named_layout(
            self.workspace_root(identifier), mount_names
        )

    def materialize_readonly_layout(
        self, identifier: str, mount_names: list[str]
    ) -> Path:
        """Create the inert namespace containing read-only supporting mounts."""

        return self._materialize_named_layout(
            self.readonly_root(identifier), mount_names
        )
