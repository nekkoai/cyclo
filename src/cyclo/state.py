from __future__ import annotations

import fcntl
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
LAUNCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FINAL_DELETION_RE = re.compile(
    r"^\.purged-([A-Za-z0-9][A-Za-z0-9._-]{0,63})-([0-9a-f]{32})\.json$"
)
DEFAULT_AGENTWS_HOST = "127.0.0.1"
HOST_CONFIG_SCOPES = frozenset({"system", "local"})
INSTANCE_INTENTS = frozenset({"running", "stopped", "deleting"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "cyclo").resolve()


def slug(value: str, limit: int = 28) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._").lower()
    return (result or "team")[:limit]


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
    launch_id: str
    verbose: bool = False
    image_override: str = ""
    agentws_host: str = DEFAULT_AGENTWS_HOST
    intent: str = "stopped"
    requested_port: int = 0
    port: int | None = None
    created_at: str = ""
    updated_at: str = ""
    project_name: str = ""
    project_file: str = ""
    project_description: str = ""
    project_generation: str = ""
    project_mounts: list[dict[str, str]] = field(default_factory=list)
    provider_socket_path: str = ""
    provider_generation: str = ""

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Instance":
        payload = dict(data)
        if "active" in payload:
            raise TypeError("active is not supported; intent is required")
        if "intent" not in payload:
            raise TypeError("intent is required")
        if "requested_port" not in payload:
            raise TypeError("requested_port is required")
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
            "intent",
        )
        for name in string_fields:
            if name in payload and not isinstance(payload[name], str):
                raise TypeError(f"{name} must be a string")
        for name in (
            "team_write",
            "offline",
            "verbose",
        ):
            if name in payload and type(payload[name]) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if payload["intent"] not in INSTANCE_INTENTS:
            raise TypeError(
                "intent must be one of running, stopped, or deleting"
            )
        for name in ("providers", "models"):
            if name not in payload:
                continue
            value = payload[name]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise TypeError(f"{name} must be a list of strings")
        requested_port = payload["requested_port"]
        if (
            type(requested_port) is not int
            or requested_port < 0
            or requested_port > 65535
        ):
            raise TypeError(
                "requested_port must be an integer from 0 to 65535"
            )
        port = payload.get("port")
        if port is not None and (
            type(port) is not int or port < 1 or port > 65535
        ):
            raise TypeError("port must be null or an integer from 1 to 65535")
        instance = cls(**payload)  # type: ignore[arg-type]
        validate_instance_id(instance.id)
        if not LAUNCH_ID_RE.fullmatch(instance.launch_id):
            raise TypeError("launch_id must be a 32-character lowercase hex value")
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
    def __init__(
        self,
        root: Path | None = None,
        *,
        requested_host_config_scope: str | None = None,
    ) -> None:
        scope = requested_host_config_scope
        if scope is None:
            scope = "local" if root is not None else "system"
        if scope not in HOST_CONFIG_SCOPES:
            raise CycloError(
                "host configuration scope must be 'system' or 'local'"
            )
        self.root = (root or default_state_root()).expanduser().resolve()
        self.instances_dir = self.root / "instances"
        self.deletions_dir = self.root / "deletions"
        self.components_root = self.root / "components"
        self.lock_path = self.root / "control.lock"
        self.host_config_scope_path = self.root / "host-config.scope"
        self._requested_host_config_scope = scope
        self._selected_host_config_scope: str | None = None
        self._ensured = False

    @property
    def host_config_scope(self) -> str:
        if self._selected_host_config_scope is None:
            self._selected_host_config_scope = (
                self._read_host_config_scope()
                or self._requested_host_config_scope
            )
        return self._selected_host_config_scope

    def _read_host_config_scope(self) -> str | None:
        descriptor = -1
        try:
            descriptor = os.open(
                self.host_config_scope_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CycloError(
                    f"host configuration scope is not a regular file: "
                    f"{self.host_config_scope_path}"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise CycloError(
                    f"host configuration scope is not private: "
                    f"{self.host_config_scope_path}"
                )
            if metadata.st_size > 8:
                raise CycloError(
                    f"invalid host configuration scope: "
                    f"{self.host_config_scope_path}"
                )
            with os.fdopen(descriptor, "r", encoding="ascii") as stream:
                descriptor = -1
                content = stream.read(9)
        except FileNotFoundError:
            return None
        except CycloError:
            raise
        except (OSError, UnicodeError) as exc:
            raise CycloError(
                f"cannot read host configuration scope "
                f"{self.host_config_scope_path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if content not in {"system\n", "local\n"}:
            raise CycloError(
                f"invalid host configuration scope: "
                f"{self.host_config_scope_path}"
            )
        return content.rstrip("\n")

    def _bind_host_config_scope(self) -> None:
        if self._selected_host_config_scope is None:
            return
        persisted = self._read_host_config_scope()
        if persisted == self._selected_host_config_scope:
            try:
                self._sync_directory(self.root)
            except OSError as exc:
                raise CycloError(
                    f"cannot persist host configuration scope "
                    f"{self.host_config_scope_path}: {exc}"
                ) from exc
            return
        if persisted is not None:
            raise CycloError(
                "Cyclo installation configuration was initialized by another "
                "process; retry the command"
            )
        temporary = self.host_config_scope_path.with_name(
            f".{self.host_config_scope_path.name}.tmp."
            f"{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            temporary.write_text(
                self._selected_host_config_scope + "\n",
                encoding="ascii",
            )
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, self.host_config_scope_path)
            self._sync_directory(self.root)
        except OSError as exc:
            raise CycloError(
                f"cannot persist host configuration scope "
                f"{self.host_config_scope_path}: {exc}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def system(self) -> str:
        """Stable installation identity derived from its component-state root."""

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
        if self._ensured:
            return
        ancestry = [self.root]
        while ancestry[-1] != ancestry[-1].parent:
            ancestry.append(ancestry[-1].parent)
        try:
            # Create one level at a time instead of using parents=True. A retry
            # re-syncs the complete chain even when an earlier failed attempt
            # left every name visible but not yet power-durable.
            for path in reversed(ancestry):
                if path.is_symlink():
                    raise CycloError(
                        f"refusing symlinked Cyclo state directory: {path}"
                    )
                if path.exists() and not path.is_dir():
                    raise CycloError(
                        f"Cyclo state path is not a directory: {path}"
                    )
                path.mkdir(
                    mode=0o700 if path == self.root else 0o777,
                    exist_ok=True,
                )
            os.chmod(self.root, 0o700)
            for path in ancestry:
                self._sync_directory(path)

            for path in (
                self.instances_dir,
                self.deletions_dir,
                self.components_root,
            ):
                if path.is_symlink():
                    raise CycloError(
                        f"refusing symlinked Cyclo state directory: {path}"
                    )
                if path.exists() and not path.is_dir():
                    raise CycloError(
                        f"Cyclo state path is not a directory: {path}"
                    )
                path.mkdir(mode=0o700, exist_ok=True)
                os.chmod(path, 0o700)
                self._sync_directory(path)
            self._sync_directory(self.root)
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"cannot prepare Cyclo state directory {self.root}: {exc}"
            ) from exc
        self._ensured = True

    @contextmanager
    def locked(
        self,
        *,
        blocking: bool = True,
        bind_host_config: bool = True,
    ) -> Iterator[None]:
        self.ensure()
        stream = None
        try:
            stream = self.lock_path.open("a+", encoding="utf-8")
            os.chmod(self.lock_path, 0o600)
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(stream.fileno(), operation)
        except BlockingIOError as exc:
            if stream is not None:
                stream.close()
            raise CycloError("Cyclo state is busy") from exc
        except OSError as exc:
            if stream is not None:
                stream.close()
            raise CycloError(f"cannot lock Cyclo state {self.lock_path}: {exc}") from exc
        operation_failed = False
        try:
            if bind_host_config:
                self._bind_host_config_scope()
            yield
        except BaseException:
            operation_failed = True
            raise
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                if not operation_failed:
                    raise CycloError(
                        f"cannot unlock Cyclo state {self.lock_path}: {exc}"
                    ) from exc
            finally:
                try:
                    stream.close()
                except OSError:
                    if not operation_failed:
                        raise

    def instance_dir(self, identifier: str) -> Path:
        path = self.instances_dir / validate_instance_id(identifier)
        if path.is_symlink():
            raise CycloError(f"refusing symlinked Cyclo instance directory: {path}")
        return path

    def metadata_path(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "run.json"

    def deletion_dir(self, identifier: str) -> Path:
        path = self.deletions_dir / validate_instance_id(identifier)
        if path.is_symlink():
            raise CycloError(f"refusing symlinked Cyclo deletion directory: {path}")
        return path

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

    def load_for_removal(
        self,
        identifier: str,
        *,
        missing_ok: bool = False,
    ) -> Instance | None:
        """Load one exact instance from ordinary or retryable deletion state."""

        identifier = validate_instance_id(identifier)
        directory = self.instance_dir(identifier)
        deletion = self.deletion_dir(identifier)
        finalized = self._final_deletion_paths(identifier)
        if len(finalized) > 1:
            raise CycloError(
                f"Cyclo instance {identifier!r} has multiple finalized "
                "deletion tombstones"
            )
        if directory.exists() and (deletion.exists() or finalized):
            raise CycloError(
                f"Cyclo instance {identifier!r} exists in both active and "
                "deletion state"
            )
        if finalized:
            instance = self._load_final_deletion(finalized[0])
            if deletion.exists():
                self._validate_deletion_transition(
                    identifier,
                    deletion,
                    instance,
                )
            return instance
        if deletion.exists():
            return self._load_deletion(identifier, deletion)
        if (
            missing_ok
            and not directory.exists()
            and not directory.is_symlink()
            and not deletion.is_symlink()
        ):
            return None
        return self.load(identifier)

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

    def list_deletions(self) -> list[Instance]:
        """Return every exact, validated deletion tombstone or fail closed."""

        if self.deletions_dir.is_symlink():
            raise CycloError(
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            )
        if not self.deletions_dir.exists():
            return []
        if not self.deletions_dir.is_dir():
            raise CycloError(
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            )
        try:
            directories = sorted(self.deletions_dir.iterdir())
        except OSError as exc:
            raise CycloError(
                f"cannot enumerate Cyclo deletions directory "
                f"{self.deletions_dir}: {exc}"
            ) from exc
        result: dict[str, Instance] = {}
        errors: list[str] = []
        for path in directories:
            if FINAL_DELETION_RE.fullmatch(path.name) is None:
                continue
            try:
                instance = self._load_final_deletion(path)
                if instance.id in result:
                    raise CycloError(
                        f"multiple finalized tombstones for {instance.id!r}"
                    )
                result[instance.id] = instance
            except (
                OSError,
                ValueError,
                TypeError,
                RecursionError,
                CycloError,
            ) as exc:
                detail = str(exc) or type(exc).__name__
                errors.append(
                    f"invalid Cyclo deletion state {path}: {detail}"
                )
        for directory in directories:
            if FINAL_DELETION_RE.fullmatch(directory.name):
                continue
            try:
                identifier = validate_instance_id(directory.name)
                finalized = result.get(identifier)
                if finalized is None:
                    result[identifier] = self._load_deletion(
                        identifier,
                        directory,
                    )
                else:
                    self._validate_deletion_transition(
                        identifier,
                        directory,
                        finalized,
                    )
            except (
                OSError,
                ValueError,
                TypeError,
                RecursionError,
                CycloError,
            ) as exc:
                detail = str(exc) or type(exc).__name__
                errors.append(
                    f"invalid Cyclo deletion state {directory}: {detail}"
                )
        if errors:
            raise CycloError(
                "cannot enumerate Cyclo deletion state: " + "; ".join(errors)
            )
        return [result[identifier] for identifier in sorted(result)]

    def list_report(self) -> tuple[list[Instance], list[str]]:
        """Return readable instances and a separate error for every bad record."""

        if self.deletions_dir.is_symlink():
            return [], [
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            ]
        if self.deletions_dir.exists() and not self.deletions_dir.is_dir():
            return [], [
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            ]
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
        self.ensure()
        directory = self.instance_dir(instance.id)
        deletion = self.deletion_dir(instance.id)
        if deletion.exists() or self._final_deletion_paths(instance.id):
            raise CycloError(
                f"Cyclo instance deletion is still pending: {instance.id}; "
                "run cyclo repair before reusing the name"
            )
        if not instance.created_at:
            instance.created_at = utc_now()
        instance.updated_at = utc_now()
        payload = instance.as_json()
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CycloError(
                    f"refusing invalid Cyclo instance directory: {directory}"
                )
            # Updating state is not an adoption path. Unknown or incomplete
            # directories remain visible to strict inventory and untouched.
            self.load(instance.id)
            os.chmod(directory, 0o700)
            self._write_metadata(directory / "run.json", payload)
            return

        # A direct child of instances/ is authoritative inventory. Construct a
        # first record beside instances/ and publish the complete directory in
        # one rename, so interruption cannot expose an ID without run.json.
        staging = self.root / (
            f".instance.{instance.id}.new.{os.getpid()}."
            f"{os.urandom(6).hex()}"
        )
        try:
            staging.mkdir(mode=0o700)
            self._write_metadata(staging / "run.json", payload)
            if directory.exists() or directory.is_symlink():
                raise CycloError(
                    f"Cyclo instance state appeared during publication: "
                    f"{instance.id}"
                )
            os.rename(staging, directory)
            self._sync_directory(directory.parent)
        finally:
            self._remove_tree(staging)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_metadata(
        path: Path,
        payload: dict[str, object],
    ) -> None:
        temporary = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            StateStore._sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def remove_instance(
        self, identifier: str, *, expected_launch_id: str
    ) -> bool:
        """Idempotently retire and purge one exact stopped launch.

        Deletion intent is persisted in the ordinary instance record before its
        directory moves to the deterministic deletions/INSTANCE path. A retry
        can therefore continue from either side of the rename.
        """

        identifier = validate_instance_id(identifier)
        if not LAUNCH_ID_RE.fullmatch(expected_launch_id):
            raise CycloError(
                f"invalid launch identity for Cyclo instance: {identifier}"
            )
        directory = self.instance_dir(identifier)
        deletion = self.deletion_dir(identifier)
        self.ensure()
        finalized = self._final_deletion_paths(identifier)
        if len(finalized) > 1:
            raise CycloError(
                f"instance {identifier!r} was replaced before it could be removed"
            )
        if finalized:
            current = self._load_final_deletion(finalized[0])
            if current.launch_id != expected_launch_id:
                raise CycloError(
                    f"instance {identifier!r} was replaced before it could be removed"
                )
            if directory.exists():
                raise CycloError(
                    f"Cyclo instance {identifier!r} exists in both active and "
                    "deletion state"
                )
            if deletion.exists():
                self._remove_empty_deletion_directory(
                    identifier,
                    deletion,
                    current,
                )
            self._purge_final_deletion(
                identifier,
                finalized[0],
                expected_launch_id=expected_launch_id,
            )
            return True
        if directory.exists() and deletion.exists():
            raise CycloError(
                f"Cyclo instance {identifier!r} exists in both active and "
                "deletion state"
            )
        if deletion.exists():
            current = self._load_deletion(identifier, deletion)
            if current.launch_id != expected_launch_id:
                raise CycloError(
                    f"instance {identifier!r} was replaced before it could be removed"
                )
            self._purge_deletion(
                identifier,
                deletion,
                expected_launch_id=current.launch_id,
            )
            return True
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CycloError(
                f"cannot inspect Cyclo instance state {directory}: {exc}"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
            raise CycloError(
                f"refusing invalid Cyclo instance directory: {directory}"
            )
        current = self.load(identifier)
        if current.launch_id != expected_launch_id:
            raise CycloError(
                f"instance {identifier!r} was replaced before it could be removed"
            )
        if current.intent == "running":
            raise CycloError(
                f"Cyclo instance is still intended to run: {identifier}"
            )
        if current.intent != "deleting":
            current.intent = "deleting"
            current.port = None
            self.save(current)
        try:
            os.rename(directory, deletion)
            # Make the destination durable before the source removal.  A power
            # loss may then expose both names, which fails closed; it must not
            # lose the only authoritative launch record.
            self._sync_directory(self.deletions_dir)
            self._sync_directory(self.instances_dir)
        except OSError as exc:
            raise CycloError(
                f"cannot retire Cyclo instance state {directory}: {exc}"
            ) from exc
        self._purge_deletion(
            identifier,
            deletion,
            expected_launch_id=current.launch_id,
        )
        return True

    def _final_deletion_paths(self, identifier: str) -> list[Path]:
        """Return finalized tombstones belonging to one instance ID."""

        identifier = validate_instance_id(identifier)
        if self.deletions_dir.is_symlink():
            raise CycloError(
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            )
        if not self.deletions_dir.exists():
            return []
        if not self.deletions_dir.is_dir():
            raise CycloError(
                f"invalid Cyclo deletions directory: {self.deletions_dir}"
            )
        try:
            paths = sorted(self.deletions_dir.iterdir())
        except OSError as exc:
            raise CycloError(
                f"cannot enumerate Cyclo deletions directory "
                f"{self.deletions_dir}: {exc}"
            ) from exc
        return [
            path
            for path in paths
            if (
                (match := FINAL_DELETION_RE.fullmatch(path.name))
                and match.group(1) == identifier
            )
        ]

    def _load_deletion(self, identifier: str, directory: Path) -> Instance:
        try:
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
                raise CycloError("deletion state entry is not a directory")
            instance = Instance.from_json(
                self._read_metadata(directory / "run.json")
            )
            self._validate_resource_namespace(instance)
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            CycloError,
        ) as exc:
            detail = str(exc) or type(exc).__name__
            raise CycloError(
                f"invalid Cyclo deletion state {directory}: {detail}"
            ) from exc
        if instance.id != identifier:
            raise CycloError(
                f"Cyclo deletion metadata ID mismatch: requested "
                f"{identifier!r}, found {instance.id!r}"
            )
        if instance.intent != "deleting":
            raise CycloError(
                f"Cyclo deletion state is not marked deleting: {identifier}"
            )
        return instance

    def _final_deletion_path(
        self, identifier: str, expected_launch_id: str
    ) -> Path:
        return self.deletions_dir / (
            f".purged-{identifier}-{expected_launch_id}.json"
        )

    def _load_final_deletion(self, path: Path) -> Instance:
        match = FINAL_DELETION_RE.fullmatch(path.name)
        if match is None:
            raise CycloError(f"invalid finalized Cyclo deletion name: {path}")
        identifier, launch_id = match.groups()
        try:
            instance = Instance.from_json(self._read_metadata(path))
            self._validate_resource_namespace(instance)
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            CycloError,
        ) as exc:
            detail = str(exc) or type(exc).__name__
            raise CycloError(
                f"invalid finalized Cyclo deletion state {path}: {detail}"
            ) from exc
        if instance.id != identifier:
            raise CycloError(
                f"Cyclo deletion metadata ID mismatch: tombstone "
                f"{identifier!r}, found {instance.id!r}"
            )
        if instance.launch_id != launch_id:
            raise CycloError(
                f"Cyclo deletion launch mismatch: tombstone "
                f"{launch_id!r}, found {instance.launch_id!r}"
            )
        if instance.intent != "deleting":
            raise CycloError(
                f"Cyclo deletion state is not marked deleting: {identifier}"
            )
        return instance

    def _validate_deletion_transition(
        self,
        identifier: str,
        deletion: Path,
        finalized: Instance,
    ) -> None:
        try:
            metadata = deletion.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or deletion.is_symlink():
                raise CycloError("deletion state entry is not a directory")
            children = list(deletion.iterdir())
        except (OSError, CycloError) as exc:
            detail = str(exc) or type(exc).__name__
            raise CycloError(
                f"invalid Cyclo deletion state {deletion}: {detail}"
            ) from exc
        if children:
            raise CycloError(
                f"invalid Cyclo deletion state {deletion}: finalized "
                "tombstone has a non-empty pending directory"
            )
        if finalized.id != identifier:
            raise CycloError(
                f"Cyclo deletion metadata ID mismatch: requested "
                f"{identifier!r}, found {finalized.id!r}"
            )

    def _remove_empty_deletion_directory(
        self,
        identifier: str,
        deletion: Path,
        finalized: Instance,
    ) -> None:
        self._validate_deletion_transition(
            identifier,
            deletion,
            finalized,
        )
        try:
            os.rmdir(deletion)
            self._sync_directory(self.deletions_dir)
        except OSError as exc:
            raise CycloError(
                f"Cyclo instance {identifier!r} was deleted, but its empty "
                f"deletion directory could not be removed at {deletion}: {exc}"
            ) from exc

    def _purge_deletion(
        self,
        identifier: str,
        deletion: Path,
        *,
        expected_launch_id: str,
    ) -> None:
        try:
            # Keep the exact-launch marker until every other child is gone.
            # SIGKILL during recursive cleanup therefore leaves enough
            # authoritative metadata for a retry to revalidate the launch.
            for child in sorted(deletion.iterdir()):
                if child.name == "run.json":
                    continue
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    shutil.rmtree(child)
            self._sync_directory(deletion)
            current = self._load_deletion(identifier, deletion)
            if current.launch_id != expected_launch_id:
                raise CycloError(
                    f"instance {identifier!r} was replaced before it could be removed"
                )
            finalized = self._final_deletion_path(
                identifier, expected_launch_id
            )
            os.rename(deletion / "run.json", finalized)
            # As above, persist the destination before the source removal.
            self._sync_directory(self.deletions_dir)
            self._sync_directory(deletion)
            self._remove_empty_deletion_directory(
                identifier,
                deletion,
                current,
            )
            self._purge_final_deletion(
                identifier,
                finalized,
                expected_launch_id=expected_launch_id,
            )
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"Cyclo instance {identifier!r} was retired, but its inert "
                f"state could not be removed at {deletion}: {exc}"
            ) from exc

    def _purge_final_deletion(
        self,
        identifier: str,
        finalized: Path,
        *,
        expected_launch_id: str,
    ) -> None:
        current = self._load_final_deletion(finalized)
        if current.id != identifier or current.launch_id != expected_launch_id:
            raise CycloError(
                f"instance {identifier!r} was replaced before it could be removed"
            )
        try:
            finalized.unlink()
            self._sync_directory(self.deletions_dir)
        except OSError as exc:
            raise CycloError(
                f"Cyclo instance {identifier!r} was deleted, but its inert "
                f"tombstone could not be removed at {finalized}: {exc}"
            ) from exc

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
        project_config: str,
    ) -> Path:
        if not project_config.strip():
            raise CycloError("Cyclo project configuration must not be empty")
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
            project_destination = temporary / "project.cyclo"
            if project_destination.exists() or project_destination.is_symlink():
                project_destination.unlink()
            project_destination.write_text(
                project_config.rstrip() + "\n", encoding="utf-8"
            )
            os.chmod(project_destination, 0o444)
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
