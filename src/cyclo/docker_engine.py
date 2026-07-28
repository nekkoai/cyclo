from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence

from .errors import CycloError


_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class DockerContainerState(str, Enum):
    ABSENT = "absent"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    STOPPED = "stopped"
    DEAD = "dead"

    @property
    def operational(self) -> bool:
        return self is DockerContainerState.RUNNING

    @property
    def lifecycle_active(self) -> bool:
        return self in {
            DockerContainerState.RUNNING,
            DockerContainerState.PAUSED,
            DockerContainerState.RESTARTING,
        }


class DockerMount(Protocol):
    source: str
    destination: str
    read_only: bool
    type: str


ContainerVerifier = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class VerifiedContainer:
    """One ownership-checked observation of an immutable Docker container."""

    id: str
    state: DockerContainerState
    info: Mapping[str, object] = field(repr=False, compare=False)


def docker_container_state(
    container: Mapping[str, object],
) -> DockerContainerState:
    state = container.get("State")
    if not isinstance(state, Mapping):
        raise CycloError("cannot parse Docker container state")
    status = state.get("Status")
    if status is not None and not isinstance(status, str):
        raise CycloError("cannot parse Docker container state")
    normalized = status.lower() if isinstance(status, str) else ""
    if state.get("Dead") is True or normalized == "dead":
        return DockerContainerState.DEAD
    if state.get("Restarting") is True or normalized == "restarting":
        return DockerContainerState.RESTARTING
    if state.get("Paused") is True or normalized == "paused":
        return DockerContainerState.PAUSED
    if state.get("Running") is True or normalized == "running":
        return DockerContainerState.RUNNING
    if state.get("Running") is False or normalized in {
        "",
        "created",
        "exited",
        "removing",
        "stopped",
    }:
        return DockerContainerState.STOPPED
    raise CycloError(f"cannot parse Docker container state: {status!r}")


class DockerEngine:
    """Small synchronous Docker CLI boundary shared by Cyclo runtimes."""

    def call(
        self,
        arguments: Sequence[str],
        *,
        capture: bool = True,
        check: bool = True,
        input_data: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.run(
                ["docker", *arguments],
                text=True,
                input=input_data,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CycloError("Docker is not installed or not on PATH") from exc
        if check and process.returncode != 0:
            detail = ((process.stderr or "") + (process.stdout or "")).strip()
            raise CycloError(
                f"Docker command failed ({process.returncode}): "
                f"{detail or 'docker ' + ' '.join(arguments)}"
            )
        return process

    def available(self) -> tuple[bool, str]:
        try:
            result = self.call(
                ["info", "--format", "{{.ServerVersion}}"],
                capture=True,
                check=False,
            )
        except CycloError as exc:
            return False, str(exc)
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, detail

    def inspect(
        self,
        kind: str,
        reference: str,
        *,
        missing: bool = True,
    ) -> dict[str, object] | None:
        result = self.call(
            [kind, "inspect", "--", reference],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            detail = ((result.stderr or "") + (result.stdout or "")).strip()
            lowered = detail.lower()
            markers = {
                "container": ("no such container", "no such object"),
                "image": ("no such image", "no such object"),
                "network": ("no such network", "not found"),
                "volume": ("no such volume",),
            }.get(kind, ())
            if (
                missing
                and reference.lower() in lowered
                and any(marker in lowered for marker in markers)
            ):
                return None
            raise CycloError(
                f"cannot inspect Docker {kind} {reference}: "
                f"{detail or 'unknown Docker error'}"
            )
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise CycloError(
                f"cannot parse Docker {kind} inspection for {reference}"
            ) from exc
        if (
            not isinstance(document, list)
            or len(document) != 1
            or not isinstance(document[0], dict)
        ):
            raise CycloError(f"invalid Docker {kind} inspection for {reference}")
        return document[0]

    @staticmethod
    def labels(info: Mapping[str, object]) -> dict[str, str]:
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if labels is None:
            return {}
        if not isinstance(labels, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise CycloError("cannot parse Docker resource labels")
        return dict(labels)

    @staticmethod
    def require_image_id(value: object) -> str:
        if not isinstance(value, str) or not _IMAGE_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker image ID")
        return value

    @staticmethod
    def require_resource_id(value: object) -> str:
        if not isinstance(value, str) or not _CONTAINER_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker resource ID")
        return value

    @classmethod
    def require_container_id(cls, value: object) -> str:
        try:
            return cls.require_resource_id(value)
        except CycloError as exc:
            raise CycloError("cannot parse Docker container ID") from exc

    @classmethod
    def image_id(cls, info: Mapping[str, object]) -> str:
        return cls.require_image_id(info.get("Id"))

    @classmethod
    def container_id(cls, info: Mapping[str, object]) -> str:
        return cls.require_container_id(info.get("Id"))

    @classmethod
    def resource_id(cls, info: Mapping[str, object]) -> str:
        return cls.require_resource_id(info.get("Id"))

    @staticmethod
    def container_state(container: Mapping[str, object]) -> str:
        """Preserve the component-facing string state API."""

        return docker_container_state(container).value

    @staticmethod
    def mount_argument(mount: DockerMount) -> str:
        if mount.type not in {"bind", "volume"}:
            raise CycloError(f"unsupported Docker mount type: {mount.type}")
        if "," in mount.source or "," in mount.destination:
            raise CycloError(
                f"Docker mount paths cannot contain a comma: {mount.source}"
            )
        result = (
            f"type={mount.type},src={mount.source},dst={mount.destination}"
        )
        return result + (",readonly" if mount.read_only else "")

    def verify_container(
        self,
        info: Mapping[str, object],
        *,
        verify: ContainerVerifier,
    ) -> VerifiedContainer:
        verify(info)
        return VerifiedContainer(
            self.container_id(info),
            docker_container_state(info),
            info,
        )

    def inspect_container(
        self,
        reference: str,
        *,
        verify: ContainerVerifier,
        missing: bool = True,
    ) -> VerifiedContainer | None:
        info = self.inspect("container", reference, missing=missing)
        if info is None:
            return None
        return self.verify_container(info, verify=verify)

    def create_container(
        self,
        name: str,
        arguments: Sequence[str],
        *,
        verify: ContainerVerifier,
    ) -> tuple[VerifiedContainer, subprocess.CompletedProcess[str]]:
        created = self.call(["create", "--name", name, *arguments])
        reported_id = (created.stdout or "").strip()
        if not _CONTAINER_ID_RE.fullmatch(reported_id):
            raise CycloError("Docker create returned an invalid container ID")
        container = self.inspect_container(
            reported_id,
            verify=verify,
            missing=False,
        )
        assert container is not None
        if container.state is not DockerContainerState.STOPPED:
            raise CycloError(
                f"Docker create produced an active container: {name}"
            )
        if reported_id != container.id:
            raise CycloError(
                "Docker create returned a different container than inspection"
            )
        return container, created

    def start_container(
        self,
        container: VerifiedContainer,
        *,
        arguments: Sequence[str] = (),
        capture: bool = True,
        input_data: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.require_container_id(container.id)
        return self.call(
            ["start", *arguments, container.id],
            capture=capture,
            input_data=input_data,
        )

    def remove_container(
        self,
        container: VerifiedContainer,
        *,
        verify: ContainerVerifier,
        timeout: int,
        remove_volumes: bool = False,
        force: bool = False,
        reject_active: bool = False,
    ) -> bool:
        """Reverify and remove only the immutable ID represented by container."""

        self.require_container_id(container.id)
        current = self.inspect_container(container.id, verify=verify)
        if current is None:
            return False
        if current.id != container.id:
            raise CycloError("Docker returned a different container than requested")
        state = current.state
        if reject_active and state.lifecycle_active:
            raise CycloError(
                f"refusing to remove active Docker container ({state.value})"
            )
        if force:
            command = ["rm", "--force"]
            if remove_volumes:
                command.append("--volumes")
            self.call([*command, current.id])
            return True
        if state is DockerContainerState.PAUSED:
            self.call(["unpause", current.id])
            state = DockerContainerState.RUNNING
        if state.lifecycle_active:
            self.call(["stop", "--timeout", str(timeout), current.id])
        command = ["rm"]
        if state is DockerContainerState.DEAD:
            command.append("--force")
        if remove_volumes:
            command.append("--volumes")
        self.call([*command, current.id])
        return True
