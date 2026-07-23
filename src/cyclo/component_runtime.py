from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .component import Component, ComponentStatus, Mount, probe_component
from .errors import CycloError
from .installation import LABEL_INSTANCE, LABEL_SYSTEM


LABEL_OWNED = "io.cyclo.component"
LABEL_TYPE = "io.cyclo.component-type"
# Keep the persisted label key stable; its value names the component class.
LABEL_COMPONENT_CLASS = "io.cyclo.lifecycle"

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def ensure_directory(path: Path, mode: int) -> Path:
    """Create one private component-state directory without following a symlink."""

    if path.is_symlink():
        raise CycloError(f"component state path is a symlink: {path}")
    try:
        path.mkdir(parents=True, mode=mode, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise CycloError(f"component state path is not a directory: {path}")
        os.chmod(path, mode)
    except OSError as exc:
        raise CycloError(
            f"cannot prepare component state directory {path}: {exc}"
        ) from exc
    return path


class ComponentController:
    """Build and run one declared component through Docker."""

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
    def image_id(info: Mapping[str, object]) -> str:
        value = info.get("Id")
        if not isinstance(value, str) or not _IMAGE_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker image ID")
        return value

    @staticmethod
    def container_id(info: Mapping[str, object]) -> str:
        value = info.get("Id")
        if not isinstance(value, str) or not _CONTAINER_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker container ID")
        return value

    @staticmethod
    def expected_labels(component: Component) -> dict[str, str]:
        return {
            LABEL_OWNED: "1",
            LABEL_SYSTEM: component.system,
            LABEL_INSTANCE: component.name,
            LABEL_COMPONENT_CLASS: component.component_class,
            LABEL_TYPE: component.kind,
        }

    def require_owned(
        self,
        component: Component,
        info: Mapping[str, object],
        *,
        image: bool,
    ) -> None:
        labels = self.labels(info)
        expected = self.expected_labels(component)
        ownership = {
            key: expected[key]
            for key in (
                LABEL_OWNED,
                LABEL_SYSTEM,
                LABEL_INSTANCE,
                LABEL_COMPONENT_CLASS,
            )
        }
        if any(labels.get(key) != value for key, value in ownership.items()):
            kind = "image" if image else "container"
            raise CycloError(
                f"refusing Docker {kind} not owned by this Cyclo component"
            )
        if image:
            self.image_id(info)
            return
        self.container_id(info)
        raw_name = info.get("Name")
        name = (
            raw_name[1:]
            if isinstance(raw_name, str) and raw_name.startswith("/")
            else raw_name
        )
        if name != component.container:
            raise CycloError(
                f"refusing mislabeled Docker container: {component.container}"
            )

    def _validate_image(
        self,
        component: Component,
        info: Mapping[str, object],
    ) -> None:
        self.require_owned(component, info, image=True)
        labels = self.labels(info)
        if any(
            labels.get(key) != value
            for key, value in self.expected_labels(component).items()
        ):
            raise CycloError(
                f"Docker image has incomplete component labels: {component.image}"
            )
        config = info.get("Config")
        if not isinstance(config, Mapping):
            raise CycloError("cannot parse Docker image configuration")
        entrypoint = config.get("Entrypoint")
        user = config.get("User")
        health = config.get("Healthcheck")
        if (
            not isinstance(entrypoint, list)
            or not entrypoint
            or any(not isinstance(item, str) or not item for item in entrypoint)
        ):
            raise CycloError("component image must define OCI ENTRYPOINT")
        user_match = (
            re.fullmatch(r"(\d+):(\d+)", user)
            if isinstance(user, str)
            else None
        )
        if (
            user_match is None
            or int(user_match.group(1)) <= 0
            or int(user_match.group(2)) <= 0
        ):
            raise CycloError(
                "component image must define a positive numeric USER UID:GID"
            )
        test = health.get("Test") if isinstance(health, Mapping) else None
        if (
            not isinstance(test, list)
            or len(test) < 2
            or test[0] not in {"CMD", "CMD-SHELL"}
            or any(not isinstance(item, str) for item in test)
        ):
            raise CycloError("component image must define HEALTHCHECK")
        for field, message in (
            (
                "ExposedPorts",
                "Unix-socket component image must not expose TCP ports",
            ),
            ("Volumes", "component image must not declare OCI volumes"),
        ):
            value = config.get(field)
            if isinstance(value, Mapping) and value:
                raise CycloError(message)

    def build_image(
        self,
        image: str,
        arguments: Sequence[str],
        validate: Callable[[Mapping[str, object]], None],
        *,
        before_promote: Callable[[], None] | None = None,
    ) -> str:
        """Build, validate, then atomically move the official tag."""

        if not arguments or arguments[0] != "build":
            raise CycloError(
                "Cyclo image build arguments must start with 'build'"
            )
        repository = image.rsplit(":", 1)[0]
        candidate = f"{repository}:candidate-{os.getpid()}-{uuid.uuid4()}"
        directory = Path(tempfile.mkdtemp(prefix="cyclo-image-build-"))
        iidfile = directory / "image-id"
        try:
            self.call(
                [
                    "build",
                    "--tag",
                    candidate,
                    "--iidfile",
                    str(iidfile),
                    *arguments[1:],
                ],
                capture=False,
            )
            try:
                built_id = iidfile.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CycloError(
                    "Docker build did not publish an image ID"
                ) from exc
            if not _IMAGE_ID_RE.fullmatch(built_id):
                raise CycloError("Docker build returned an invalid image ID")
            built = self.inspect("image", built_id, missing=False)
            tagged = self.inspect("image", candidate, missing=False)
            assert built is not None and tagged is not None
            if self.image_id(tagged) != built_id:
                raise CycloError(
                    "Docker candidate tag does not reference the completed build"
                )
            validate(built)
            if before_promote is not None:
                before_promote()
            self.call(["image", "tag", "--", built_id, image])
            official = self.inspect("image", image, missing=False)
            assert official is not None
            if self.image_id(official) != built_id:
                raise CycloError(
                    "Docker official tag changed during build promotion"
                )
            return built_id
        finally:
            self.call(["image", "rm", "--", candidate], check=False)
            shutil.rmtree(directory, ignore_errors=True)

    def build(self, component: Component) -> str:
        current = self.inspect("image", component.image)
        if current is not None:
            self.require_owned(component, current, image=True)
        labels = [
            item
            for key, value in self.expected_labels(component).items()
            for item in ("--label", f"{key}={value}")
        ]
        return self.build_image(
            component.image,
            [
                "build",
                *labels,
                "--file",
                str(component.source / "Dockerfile"),
                str(component.build_context),
            ],
            lambda info: self._validate_image(component, info),
        )

    def require_image(self, component: Component) -> str:
        image = self.inspect("image", component.image)
        if image is None:
            raise CycloError(f"component image is not built: {component.name}")
        self._validate_image(component, image)
        return self.image_id(image)

    @staticmethod
    def container_state(container: Mapping[str, object]) -> str:
        state = container.get("State")
        if not isinstance(state, Mapping):
            raise CycloError("cannot parse Docker container state")
        status = str(state.get("Status") or "").lower()
        if state.get("Dead") is True or status == "dead":
            return "dead"
        if state.get("Restarting") is True or status == "restarting":
            return "restarting"
        if state.get("Paused") is True or status == "paused":
            return "paused"
        if state.get("Running") is True or status == "running":
            return "running"
        return "stopped"

    @staticmethod
    def engine_health(container: Mapping[str, object]) -> str:
        state = container.get("State")
        health = state.get("Health") if isinstance(state, Mapping) else None
        status = health.get("Status") if isinstance(health, Mapping) else None
        return (
            status
            if status in {"starting", "healthy", "unhealthy"}
            else "missing"
        )

    @staticmethod
    def mount_argument(mount: Mount) -> str:
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

    def _configuration_current(
        self,
        component: Component,
        image: Mapping[str, object],
        container: Mapping[str, object],
    ) -> bool:
        try:
            host = container["HostConfig"]
            config = container["Config"]
            image_config = image["Config"]
            if not all(
                isinstance(value, Mapping)
                for value in (host, config, image_config)
            ):
                return False
            restart = host.get("RestartPolicy")
            security = host.get("SecurityOpt")
            dropped = host.get("CapDrop")
            added = host.get("CapAdd")
            devices = host.get("Devices")
            device_requests = host.get("DeviceRequests")
            tmpfs = host.get("Tmpfs")
            ulimits = host.get("Ulimits")
            nofile = (
                next(
                    (
                        value
                        for value in ulimits
                        if isinstance(value, Mapping)
                        and value.get("Name") == "nofile"
                    ),
                    None,
                )
                if isinstance(ulimits, list)
                else None
            )
            network_settings = container.get("NetworkSettings")
            networks = (
                network_settings.get("Networks")
                if isinstance(network_settings, Mapping)
                else None
            )
            published_ports = (
                network_settings.get("Ports")
                if isinstance(network_settings, Mapping)
                else None
            )
            tmpfs_value = (
                tmpfs.get("/tmp") if isinstance(tmpfs, Mapping) else None
            )
            tmpfs_flags = (
                {flag for flag in tmpfs_value.split(",") if flag}
                if isinstance(tmpfs_value, str)
                else set()
            )
            allowed_tmpfs_flags = {
                "rw",
                "noexec",
                "nosuid",
                "nodev",
                "size=64m",
                "size=67108864",
            }
            if (
                host.get("NetworkMode") != component.network
                or host.get("ReadonlyRootfs") is not True
                or host.get("Privileged") is True
                or host.get("PidMode") != ""
                or host.get("IpcMode") != "private"
                or host.get("UTSMode") != ""
                or host.get("UsernsMode") != ""
                or host.get("CgroupnsMode") != "private"
                or host.get("PidsLimit") != 256
                or not isinstance(restart, Mapping)
                or restart.get("Name") != "unless-stopped"
                or security != ["no-new-privileges"]
                or not isinstance(dropped, list)
                or "ALL" not in {str(item).upper() for item in dropped}
                or added not in (None, [])
                or devices not in (None, [])
                or device_requests not in (None, [])
                or not isinstance(nofile, Mapping)
                or nofile.get("Soft") != 1024
                or nofile.get("Hard") != 1024
                or not isinstance(tmpfs, Mapping)
                or set(tmpfs) != {"/tmp"}
                or not {"rw", "noexec", "nosuid", "nodev"}.issubset(
                    tmpfs_flags
                )
                or not ({"size=64m", "size=67108864"} & tmpfs_flags)
                or not tmpfs_flags.issubset(allowed_tmpfs_flags)
                or not isinstance(networks, Mapping)
                or set(networks) != {component.network}
                or host.get("PortBindings") not in (None, {})
                or published_ports not in (None, {})
            ):
                return False
            if (
                config.get("User") != image_config.get("User")
                or config.get("Entrypoint") != image_config.get("Entrypoint")
                or config.get("Healthcheck") != image_config.get("Healthcheck")
                or config.get("Env") != image_config.get("Env")
                or config.get("WorkingDir") != image_config.get("WorkingDir")
                or config.get("Cmd")
                != (
                    list(component.arguments)
                    if component.arguments
                    else image_config.get("Cmd")
                )
            ):
                return False
            if any(
                self.labels(container).get(key) != value
                for key, value in self.expected_labels(component).items()
            ):
                return False
            actual_mounts = container.get("Mounts")
            if (
                not isinstance(actual_mounts, list)
                or len(actual_mounts) != len(component.mounts)
            ):
                return False
            observed = {
                mount.get("Destination"): mount
                for mount in actual_mounts
                if isinstance(mount, Mapping)
            }
            if len(observed) != len(actual_mounts):
                return False
            for expected in component.mounts:
                actual = observed.get(expected.destination)
                if (
                    not isinstance(actual, Mapping)
                    or actual.get("Type") != expected.type
                    or (
                        expected.type == "bind"
                        and actual.get("Source") != expected.source
                    )
                    or (
                        expected.type == "volume"
                        and actual.get("Name") != expected.source
                    )
                    or actual.get("RW") is not (not expected.read_only)
                ):
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def status(
        self,
        component: Component,
        *,
        error: str = "",
    ) -> ComponentStatus:
        image = self.inspect("image", component.image)
        container = self.inspect("container", component.container)
        valid_image = False
        image_diagnostic = ""
        if image is not None:
            self.require_owned(component, image, image=True)
            try:
                self._validate_image(component, image)
                valid_image = True
            except CycloError as exc:
                image_diagnostic = str(exc)
        if container is not None:
            self.require_owned(component, container, image=False)
        image_id = self.image_id(image) if image is not None else None
        if container is None:
            return ComponentStatus(
                component.name,
                component.kind,
                image_id,
                None,
                False,
                "absent",
                "missing",
                False,
                "unreachable",
                error or image_diagnostic,
            )

        container_id = self.container_id(container)
        container_image = container.get("Image")
        if (
            not isinstance(container_image, str)
            or not _IMAGE_ID_RE.fullmatch(container_image)
        ):
            raise CycloError("cannot parse Docker container image ID")
        container_state = self.container_state(container)
        current = bool(
            image is not None
            and valid_image
            and image_id == container_image
            and self._configuration_current(component, image, container)
        )
        health = "unreachable"
        probe_error = ""
        if container_state == "running" and current:
            health, probe_error = probe_component(component.socket_path)
        return ComponentStatus(
            component.name,
            component.kind,
            image_id,
            container_id,
            container_state == "running",
            container_state,
            self.engine_health(container),
            current,
            health,
            error or image_diagnostic or probe_error,
        )

    def _run(self, component: Component) -> str:
        existing = self.inspect("container", component.container)
        if existing is not None:
            self.require_owned(component, existing, image=False)
            raise CycloError(
                f"component container already exists; restart it: {component.name}"
            )
        image_id = self.require_image(component)
        labels = [
            item
            for key, value in self.expected_labels(component).items()
            for item in ("--label", f"{key}={value}")
        ]
        mounts = [
            item
            for mount in component.mounts
            for item in ("--mount", self.mount_argument(mount))
        ]
        result = self.call(
            [
                "run",
                "--detach",
                "--name",
                component.container,
                *labels,
                "--restart",
                "unless-stopped",
                "--stop-timeout",
                "10",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--ipc",
                "private",
                "--cgroupns",
                "private",
                "--pids-limit",
                "256",
                "--ulimit",
                "nofile=1024:1024",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m",
                "--network",
                component.network,
                *mounts,
                image_id,
                *component.arguments,
            ]
        )
        container_id = (result.stdout or "").strip()
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise CycloError("Docker run returned an invalid container ID")
        try:
            status = self.status(component)
            if (
                status.container_id != container_id
                or not status.running
                or not status.current
            ):
                raise CycloError(
                    f"component did not start with the requested isolation: "
                    f"{component.name}"
                )
        except BaseException:
            self._remove_started(component, container_id)
            raise
        return container_id

    def _remove_started(self, component: Component, identifier: str) -> None:
        try:
            container = self.inspect("container", identifier)
            if container is None:
                return
            self.require_owned(component, container, image=False)
            command = ["rm", "--force"]
            if not component.preserve_volumes:
                command.append("--volumes")
            command.append(identifier)
            self.call(command, check=False)
        except Exception:
            pass

    def wait_ready(
        self,
        component: Component,
        *,
        timeout: float = 20.0,
    ) -> ComponentStatus:
        deadline = time.monotonic() + timeout
        last = self.status(component)
        while time.monotonic() < deadline:
            last = self.status(component)
            if last.works:
                return last
            if not last.running or not last.current:
                logs = self.logs(component)
                raise CycloError(
                    f"component {component.name} stopped or changed during startup"
                    + (f"\n{logs}" if logs else "")
                )
            time.sleep(0.1)
        logs = self.logs(component)
        detail = f": {last.error}" if last.error else ""
        raise CycloError(
            f"timed out waiting for component {component.name}{detail}"
            + (f"\n{logs}" if logs else "")
        )

    def start_built(
        self,
        component: Component,
        *,
        replace: bool = False,
    ) -> ComponentStatus:
        """Start a component whose official image has already been built."""

        current = self.status(component)
        if current.works and not replace:
            return current
        if current.container_id:
            self.stop(component, current.container_id)
        identifier = self._run(component)
        try:
            return self.wait_ready(component)
        except BaseException:
            self._remove_started(component, identifier)
            raise

    def start(self, component: Component) -> ComponentStatus:
        self.build(component)
        return self.start_built(component)

    def restart(self, component: Component) -> ComponentStatus:
        self.build(component)
        return self.start_built(component, replace=True)

    def stop(
        self,
        component: Component,
        expected_id: str | None = None,
    ) -> bool:
        if expected_id is not None and not _CONTAINER_ID_RE.fullmatch(expected_id):
            raise CycloError("invalid expected container ID")
        container = self.inspect(
            "container",
            expected_id or component.container,
        )
        if container is None:
            return False
        self.require_owned(component, container, image=False)
        container_id = self.container_id(container)
        if expected_id is not None and expected_id != container_id:
            raise CycloError("Docker returned a different container than requested")
        if self.container_state(container) != "stopped":
            self.call(["stop", "--timeout", "10", container_id])
        command = ["rm", container_id]
        if not component.preserve_volumes:
            command.insert(1, "--volumes")
        self.call(command)
        return True

    def logs(self, component: Component, lines: int = 80) -> str:
        info = self.inspect("container", component.container)
        if info is None:
            return ""
        self.require_owned(component, info, image=False)
        result = self.call(
            ["logs", "--tail", str(lines), self.container_id(info)],
            check=False,
        )
        return ((result.stdout or "") + (result.stderr or "")).strip()
