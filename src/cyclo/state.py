from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .docker_endpoint import local_docker_endpoint
from .errors import CycloError
from .installation import installation_id


INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOST_CONFIG_SCOPES = frozenset({"system", "local"})
INSTANCE_INTENTS = frozenset({"running", "stopped"})
STATE_SCHEMA = 1


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
            f"invalid Cyclo instance ID {value!r}; use 1-64 letters, numbers, "
            "dot, underscore, or hyphen"
        )
    return value


@dataclass
class Instance:
    """Cyclo's durable domain record; it contains no Docker lifecycle state."""

    id: str
    team_name: str
    team_path: str
    generation: str
    models: list[str]
    image: str
    image_override: str
    team_write: bool
    offline: bool
    verbose: bool
    agentws_host: str
    intent: str
    requested_port: int
    team_roster: str
    team_protocol: bool
    pi_default_provider: str
    pi_default_model: str
    project_name: str
    project_file: str
    project_description: str
    project_generation: str
    project_config: str
    project_mounts: list[dict[str, str]]
    runtime_version: str
    created_at: str = ""
    updated_at: str = ""
    schema: int = STATE_SCHEMA

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "Instance":
        expected = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - expected)
        if unknown:
            # In particular, reject pre-DComp container/network/launch fields
            # instead of silently carrying a second lifecycle model.
            raise TypeError("unknown state field(s): " + ", ".join(unknown))
        required = {
            item.name
            for item in fields(cls)
            if item.name not in {"created_at", "updated_at", "schema"}
        }
        absent = sorted(required - set(data))
        if absent:
            raise TypeError("missing state field(s): " + ", ".join(absent))
        payload = dict(data)
        payload.setdefault("created_at", "")
        payload.setdefault("updated_at", "")
        payload.setdefault("schema", STATE_SCHEMA)
        if payload["schema"] != STATE_SCHEMA:
            raise TypeError(f"unsupported Cyclo state schema: {payload['schema']!r}")
        text_fields = (
            "id",
            "team_name",
            "team_path",
            "generation",
            "image",
            "image_override",
            "agentws_host",
            "intent",
            "team_roster",
            "pi_default_provider",
            "pi_default_model",
            "project_name",
            "project_file",
            "project_description",
            "project_generation",
            "project_config",
            "runtime_version",
            "created_at",
            "updated_at",
        )
        for name in text_fields:
            if not isinstance(payload[name], str):
                raise TypeError(f"{name} must be a string")
        for name in ("team_write", "offline", "verbose", "team_protocol"):
            if type(payload[name]) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if payload["intent"] not in INSTANCE_INTENTS:
            raise TypeError("intent must be running or stopped")
        if (
            not isinstance(payload["models"], list)
            or not payload["models"]
            or not all(
                isinstance(item, str) and item for item in payload["models"]
            )
        ):
            raise TypeError("models must be a non-empty list of strings")
        if not isinstance(payload["project_mounts"], list):
            raise TypeError("project_mounts must be a list")
        port = payload["requested_port"]
        if type(port) is not int or not 0 <= port <= 65535:
            raise TypeError("requested_port must be an integer from 0 to 65535")
        instance = cls(**payload)  # type: ignore[arg-type]
        validate_instance_id(instance.id)
        for name in (
            "team_name",
            "team_path",
            "generation",
            "image",
            "agentws_host",
            "team_roster",
            "pi_default_provider",
            "pi_default_model",
            "project_name",
            "project_file",
            "project_description",
            "project_generation",
            "project_config",
            "runtime_version",
        ):
            if not getattr(instance, name).strip():
                raise TypeError(f"{name} is required")
        if (
            not instance.image.startswith("sha256:")
            or len(instance.image) != 71
            or any(character not in "0123456789abcdef" for character in instance.image[7:])
        ):
            raise TypeError("image must be an immutable sha256 Docker image ID")
        for name in ("team_path", "project_file"):
            if not Path(getattr(instance, name)).is_absolute():
                raise TypeError(f"{name} must be absolute")
        if (
            Path(instance.team_roster).name != instance.team_roster
            or instance.team_roster in {".", ".."}
        ):
            raise TypeError("team_roster must be a direct file name")

        from .project_state import decode_instance_project

        decode_instance_project(instance).require_valid()
        return instance

    def as_json(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    """Private, durable Cyclo intent and AgentWS state.

    DComp owns all container, network, and volume lifecycle state separately.
    """

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
            raise CycloError("host configuration scope must be 'system' or 'local'")
        self.root = (root or default_state_root()).expanduser().resolve()
        self.instances_dir = self.root / "instances"
        self.lock_path = self.root / "control.lock"
        self.pending_batch_path = self.root / "pending-instance-batch.json"
        self.host_config_scope_path = self.root / "host-config.scope"
        self.docker_endpoint_path = self.root / "docker-endpoint"
        self._requested_host_config_scope = scope
        self._selected_host_config_scope: str | None = None
        self._docker_endpoint: str | None = None
        self._ensured = False
        self._lock_owner: int | None = None

    @property
    def system(self) -> str:
        return installation_id(self.root)

    @property
    def host_config_scope(self) -> str:
        if self._selected_host_config_scope is None:
            persisted = self._read_one_line(
                self.host_config_scope_path,
                allowed={"system", "local"},
                label="host configuration scope",
            )
            if (
                persisted is not None
                and persisted != self._requested_host_config_scope
            ):
                raise CycloError(
                    "Cyclo installation was initialized with another host "
                    "configuration scope"
                )
            self._selected_host_config_scope = (
                persisted or self._requested_host_config_scope
            )
        return self._selected_host_config_scope

    @property
    def bound_docker_endpoint(self) -> str | None:
        if self._docker_endpoint is None:
            self._docker_endpoint = self._read_one_line(
                self.docker_endpoint_path,
                allowed=None,
                label="Docker endpoint binding",
            )
        return self._docker_endpoint

    @property
    def observed_docker_endpoint(self) -> str:
        return self.bound_docker_endpoint or local_docker_endpoint()

    @property
    def docker_endpoint(self) -> str:
        selected = local_docker_endpoint()
        persisted = self.bound_docker_endpoint
        if persisted is not None:
            if persisted != selected:
                raise CycloError(
                    "Cyclo installation is bound to another Docker daemon: "
                    f"{persisted}"
                )
            return persisted
        self.ensure()
        self._write_once(self.docker_endpoint_path, selected + "\n", mode=0o600)
        persisted = self._read_one_line(
            self.docker_endpoint_path,
            allowed=None,
            label="Docker endpoint binding",
        )
        if persisted != selected:
            raise CycloError(
                "Cyclo installation was concurrently bound to another Docker daemon"
            )
        self._docker_endpoint = selected
        return selected

    def ensure(self) -> None:
        if self._ensured:
            return
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            for path in (self.instances_dir,):
                if path.is_symlink():
                    raise CycloError(
                        f"refusing symlinked Cyclo state directory: {path}"
                    )
                path.mkdir(mode=0o700, exist_ok=True)
                os.chmod(path, 0o700)
            self._sync_directory(self.instances_dir)
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
        try:
            stream = self.lock_path.open("a+", encoding="utf-8")
            os.chmod(self.lock_path, 0o600)
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), flags)
        except BlockingIOError as exc:
            raise CycloError("Cyclo state is busy") from exc
        except OSError as exc:
            raise CycloError(f"cannot lock Cyclo state {self.lock_path}: {exc}") from exc
        try:
            if bind_host_config:
                self._bind_host_config_scope()
            self._lock_owner = threading.get_ident()
            try:
                yield
            finally:
                self._lock_owner = None
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def instance_dir(self, identifier: str) -> Path:
        path = self.instances_dir / validate_instance_id(identifier)
        if path.is_symlink():
            raise CycloError(f"refusing symlinked Cyclo instance directory: {path}")
        return path

    def metadata_path(self, identifier: str) -> Path:
        return self.instance_dir(identifier) / "run.json"

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
        self._recover_pending_batch()
        path = self.metadata_path(identifier)
        try:
            data = self._read_json(path)
            instance = Instance.from_json(data)
        except FileNotFoundError as exc:
            raise CycloError(f"Cyclo instance not found: {identifier}") from exc
        except (OSError, ValueError, TypeError, RecursionError, CycloError) as exc:
            raise CycloError(f"invalid Cyclo instance state {path}: {exc}") from exc
        if instance.id != identifier:
            raise CycloError(
                f"Cyclo instance metadata ID mismatch: expected {identifier!r}, "
                f"found {instance.id!r}"
            )
        return instance

    def list(self) -> list[Instance]:
        result, errors = self.list_report()
        if errors:
            raise CycloError(
                "cannot enumerate Cyclo instance state: " + "; ".join(errors)
            )
        return result

    def list_report(self) -> tuple[list[Instance], list[str]]:
        self.ensure()
        try:
            with self._state_locked():
                return self._list_report_locked()
        except CycloError as exc:
            return [], [str(exc)]

    def _list_report_locked(self) -> tuple[list[Instance], list[str]]:
        self._recover_pending_batch_locked()
        result: list[Instance] = []
        errors: list[str] = []
        try:
            paths = sorted(self.instances_dir.iterdir())
        except OSError as exc:
            return [], [f"cannot enumerate {self.instances_dir}: {exc}"]
        for path in paths:
            if path.name.startswith("."):
                continue
            try:
                validate_instance_id(path.name)
                if path.is_symlink() or not path.is_dir():
                    raise CycloError("instance entry is not a directory")
                # A crash between mkdir and the first atomic run.json publish
                # may leave an empty directory. It contains no committed
                # instance and a later save can safely reuse it.
                if not self.metadata_path(path.name).exists():
                    if not any(path.iterdir()):
                        continue
                    raise CycloError("instance directory has no run.json")
                result.append(self.load(path.name))
            except (OSError, ValueError, TypeError, CycloError) as exc:
                errors.append(f"{path}: {exc}")
        return sorted(result, key=lambda item: item.id), errors

    def save(self, instance: Instance) -> None:
        self.ensure()
        self._recover_pending_batch()
        document = self._prepare_instance(instance)
        self._write_instance_document(instance.id, document)

    def save_many(self, instances: Iterable[Instance]) -> None:
        """Publish a cohort so every StateStore reader observes all or none.

        The batch record is durable before the first per-instance publication.
        A process that dies partway through leaves that record behind; the next
        StateStore operation completes the same idempotent publication before
        returning any inventory.
        """

        self.ensure()
        selected = tuple(instances)
        with self._state_locked():
            self._save_many_locked(selected)

    def _save_many_locked(self, selected: tuple[Instance, ...]) -> None:
        self._recover_pending_batch_locked()
        if not selected:
            return
        documents: list[dict[str, object]] = []
        identifiers: set[str] = set()
        for instance in selected:
            if instance.id in identifiers:
                raise CycloError(
                    f"duplicate Cyclo instance in batch: {instance.id}"
                )
            identifiers.add(instance.id)
            documents.append(self._prepare_instance(instance))
        payload = {
            "schema": STATE_SCHEMA,
            "instances": documents,
        }
        content = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        try:
            self._replace_file(self.pending_batch_path, content, mode=0o600)
            self._publish_instance_batch(documents)
            self.pending_batch_path.unlink()
            self._sync_directory(self.root)
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(f"cannot publish Cyclo instance batch: {exc}") from exc

    def _prepare_instance(self, instance: Instance) -> dict[str, object]:
        validate_instance_id(instance.id)
        now = utc_now()
        if not instance.created_at:
            instance.created_at = now
        instance.updated_at = now
        # Validate exactly what will cross the persistence boundary.
        try:
            Instance.from_json(instance.as_json())
        except (TypeError, ValueError, CycloError) as exc:
            raise CycloError(
                f"invalid Cyclo instance {instance.id!r}: {exc}"
            ) from exc
        return instance.as_json()

    def _write_instance_document(
        self,
        identifier: str,
        document: dict[str, object],
    ) -> None:
        validate_instance_id(identifier)
        directory = self.instance_dir(identifier)
        try:
            metadata_path = self.metadata_path(identifier)
            try:
                metadata = metadata_path.lstat()
            except FileNotFoundError:
                metadata_exists = False
            else:
                metadata_exists = True
            if metadata_exists:
                if not stat.S_ISREG(metadata.st_mode):
                    raise CycloError(
                        f"invalid existing Cyclo instance state {metadata_path}: "
                        "metadata is not a regular file"
                    )
                self._validate_existing_instance_document(
                    identifier,
                    metadata_path,
                )
            elif directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise CycloError(
                        f"invalid Cyclo instance directory: {directory}"
                    )
                if any(directory.iterdir()):
                    raise CycloError(
                        f"uncommitted Cyclo instance directory is not empty: "
                        f"{directory}"
                    )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            content = (
                json.dumps(
                    document,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            self._replace_file(metadata_path, content, mode=0o600)
            self._sync_directory(directory)
            self._sync_directory(self.instances_dir)
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"cannot save Cyclo instance {identifier!r}: {exc}"
            ) from exc

    def _validate_existing_instance_document(
        self,
        identifier: str,
        path: Path,
    ) -> None:
        try:
            existing = Instance.from_json(self._read_json(path))
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            RecursionError,
            CycloError,
        ) as exc:
            raise CycloError(
                f"invalid existing Cyclo instance state {path}: {exc}"
            ) from exc
        if existing.id != identifier:
            raise CycloError(
                f"existing Cyclo instance state ID mismatch: expected "
                f"{identifier!r}, found {existing.id!r}"
            )

    def _recover_pending_batch(self) -> None:
        if not self.pending_batch_path.exists():
            return
        with self._state_locked():
            self._recover_pending_batch_locked()

    @contextmanager
    def _state_locked(self) -> Iterator[None]:
        if self._lock_owner == threading.get_ident():
            yield
            return
        with self.locked(bind_host_config=False):
            yield

    def _recover_pending_batch_locked(self) -> None:
        if not self.pending_batch_path.exists():
            return
        try:
            payload = self._read_json(self.pending_batch_path)
            if payload.get("schema") != STATE_SCHEMA:
                raise CycloError("unsupported pending instance batch schema")
            raw_instances = payload.get("instances")
            if not isinstance(raw_instances, list) or not raw_instances:
                raise CycloError("pending instance batch has no instances")
            documents: list[dict[str, object]] = []
            identifiers: set[str] = set()
            for raw in raw_instances:
                if not isinstance(raw, dict):
                    raise CycloError(
                        "pending instance batch contains a non-object"
                    )
                instance = Instance.from_json(raw)
                if instance.id in identifiers:
                    raise CycloError(
                        f"pending instance batch repeats {instance.id!r}"
                    )
                identifiers.add(instance.id)
                documents.append(instance.as_json())
            self._publish_instance_batch(documents)
            self.pending_batch_path.unlink()
            self._sync_directory(self.root)
        except CycloError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise CycloError(f"cannot recover pending instance batch: {exc}") from exc

    def _publish_instance_batch(
        self,
        documents: list[dict[str, object]],
    ) -> None:
        for document in documents:
            identifier = document.get("id")
            if not isinstance(identifier, str):
                raise CycloError("pending instance batch has an invalid ID")
            self._write_instance_document(identifier, document)

    def remove(self, identifier: str) -> None:
        instance = self.load(identifier)
        if instance.intent != "stopped":
            raise CycloError(
                f"refusing to forget running Cyclo instance: {identifier}"
            )
        directory = self.instance_dir(identifier)
        quarantine = self.instances_dir / (
            f".deleted-{identifier}-{os.getpid()}-{os.urandom(6).hex()}"
        )
        try:
            os.replace(directory, quarantine)
            self._sync_directory(self.instances_dir)
            shutil.rmtree(quarantine)
            self._sync_directory(self.instances_dir)
        except OSError as exc:
            raise CycloError(
                f"cannot delete Cyclo instance {identifier!r}: {exc}"
            ) from exc

    def _bind_host_config_scope(self) -> None:
        selected = self.host_config_scope
        persisted = self._read_one_line(
            self.host_config_scope_path,
            allowed={"system", "local"},
            label="host configuration scope",
        )
        if persisted is None:
            self._write_once(
                self.host_config_scope_path,
                selected + "\n",
                mode=0o600,
            )
            persisted = self._read_one_line(
                self.host_config_scope_path,
                allowed={"system", "local"},
                label="host configuration scope",
            )
        if persisted != selected:
            raise CycloError(
                "Cyclo installation was initialized with another host "
                "configuration scope"
            )

    def _read_one_line(
        self,
        path: Path,
        *,
        allowed: set[str] | None,
        label: str,
    ) -> str | None:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > 4096
            ):
                raise CycloError(f"invalid {label}: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                content = stream.read(4097)
        except FileNotFoundError:
            return None
        except CycloError:
            raise
        except (OSError, UnicodeError) as exc:
            raise CycloError(f"cannot read {label} {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not content.endswith("\n") or content.count("\n") != 1:
            raise CycloError(f"invalid {label}: {path}")
        value = content[:-1]
        if not value or (allowed is not None and value not in allowed):
            raise CycloError(f"invalid {label}: {path}")
        return value

    def _write_once(self, path: Path, text: str, *, mode: int) -> None:
        temporary = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, mode)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                return
            self._sync_directory(path.parent)
        except OSError as exc:
            raise CycloError(f"cannot publish private state file {path}: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CycloError("metadata is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                data = json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(data, dict):
            raise CycloError("metadata is not a JSON object")
        return data

    def _replace_file(self, path: Path, content: bytes, *, mode: int) -> None:
        # Keep the temporary beside the instance directories, not inside a new
        # instance directory. If the host dies before the rename, inventory
        # still sees the instance directory as an unpublished empty entry.
        temporary = self.instances_dir / (
            f".{path.parent.name}.{path.name}.tmp."
            f"{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
