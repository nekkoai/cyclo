from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import __version__
from .component import Component, ComponentStatus, probe_component
from .docker_engine import DockerEngine
from .errors import CycloError
from .installation import LABEL_INSTANCE, LABEL_SYSTEM


LABEL_OWNED = "io.cyclo.component"
LABEL_TYPE = "io.cyclo.component-type"
LABEL_RELEASE = "io.cyclo.release"
# Keep the persisted label key stable; its value names the component class.
LABEL_COMPONENT_CLASS = "io.cyclo.lifecycle"

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


class ComponentController(DockerEngine):
    """Build and run one declared component through Docker."""

    @staticmethod
    def expected_labels(component: Component) -> dict[str, str]:
        return {
            LABEL_OWNED: "1",
            LABEL_SYSTEM: component.system,
            LABEL_INSTANCE: component.name,
            LABEL_COMPONENT_CLASS: component.component_class,
            LABEL_TYPE: component.kind,
            LABEL_RELEASE: __version__,
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

    def _container_verifier(
        self,
        component: Component,
    ) -> Callable[[Mapping[str, object]], None]:
        def verify(info: Mapping[str, object]) -> None:
            self.require_owned(component, info, image=False)

        return verify

    def _validate_image(
        self,
        component: Component,
        info: Mapping[str, object],
        *,
        check_release: bool = True,
    ) -> None:
        self.require_owned(component, info, image=True)
        labels = self.labels(info)
        expected = self.expected_labels(component)
        release = expected.pop(LABEL_RELEASE)
        if any(
            labels.get(key) != value
            for key, value in expected.items()
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
        if check_release and labels.get(LABEL_RELEASE) != release:
            raise CycloError(
                "Docker image belongs to a different Cyclo release: "
                f"{component.image}"
            )

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
        directory = Path(tempfile.mkdtemp(prefix="cyclo-image-build-"))
        iidfile = directory / "image-id"
        try:
            self.call(
                [
                    "build",
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
            try:
                self.require_image_id(built_id)
            except CycloError as exc:
                raise CycloError(
                    "Docker build returned an invalid image ID"
                ) from exc
            built = self.inspect("image", built_id, missing=False)
            assert built is not None
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

    def ensure_image(self, component: Component) -> str:
        """Return the image, building when absent or from another release."""

        image = self.inspect("image", component.image)
        if image is None:
            return self.build(component)
        self._validate_image(component, image, check_release=False)
        if self.labels(image).get(LABEL_RELEASE) != __version__:
            return self.build(component)
        return self.image_id(image)

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
        expected_id: str | None = None,
    ) -> ComponentStatus:
        if expected_id is not None:
            try:
                self.require_container_id(expected_id)
            except CycloError as exc:
                raise CycloError("invalid expected container ID") from exc
        image = self.inspect("image", component.image)
        container = self.inspect(
            "container",
            expected_id or component.container,
        )
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
        if expected_id is not None and container_id != expected_id:
            raise CycloError("Docker returned a different container than requested")
        container_image = container.get("Image")
        if not isinstance(container_image, str):
            raise CycloError("cannot parse Docker container image ID")
        try:
            self.require_image_id(container_image)
        except CycloError as exc:
            raise CycloError("cannot parse Docker container image ID") from exc
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
        verify = self._container_verifier(component)
        container_id: str | None = None
        try:
            created, _result = self.create_container(
                component.container,
                [
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
                ],
                verify=verify,
            )
            container_id = created.id
            self.start_container(created)
            status = self.status(component, expected_id=container_id)
            if (
                status.container_id != container_id
                or not status.running
                or not status.current
            ):
                raise CycloError(
                    f"component did not start with the requested isolation: "
                    f"{component.name}"
                )
        except BaseException as exc:
            self._rollback_started(component, container_id, exc)
            raise
        assert container_id is not None
        return container_id

    def _rollback_started(
        self,
        component: Component,
        identifier: str | None,
        cause: BaseException,
    ) -> None:
        """Remove a verified failed launch and retain both failures if needed.

        If creation did not return a verified immutable ID, leave any possible
        residue untouched.  The next locked reconciliation may identify it by
        ownership; rollback must never mutate a container selected only by a
        reusable name.
        """

        if identifier is None:
            return

        try:
            verify = self._container_verifier(component)
            container = self.inspect_container(
                identifier,
                verify=verify,
            )
            if container is not None:
                self.remove_container(
                    container,
                    verify=verify,
                    timeout=10,
                    remove_volumes=not component.preserve_volumes,
                    force=True,
                )
        except Exception as cleanup:
            detail = (
                f"component {component.name} rollback failed: {cleanup}"
            )
            primary = str(cause) or cause.__class__.__name__
            raise CycloError(f"{primary}; {detail}") from cause

    def wait_ready(
        self,
        component: Component,
        expected_id: str,
        *,
        timeout: float = 20.0,
    ) -> ComponentStatus:
        deadline = time.monotonic() + timeout
        last = self.status(component, expected_id=expected_id)
        while time.monotonic() < deadline:
            last = self.status(component, expected_id=expected_id)
            if last.works:
                return last
            if not last.running or not last.current:
                logs = self.logs(component, expected_id=expected_id)
                raise CycloError(
                    f"component {component.name} stopped or changed during startup"
                    + (f"\n{logs}" if logs else "")
                )
            time.sleep(0.1)
        logs = self.logs(component, expected_id=expected_id)
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
            return self.wait_ready(component, identifier)
        except BaseException as exc:
            self._rollback_started(component, identifier, exc)
            raise

    def start(self, component: Component) -> ComponentStatus:
        current = self.status(component)
        if current.works:
            return current
        self.ensure_image(component)
        return self.start_built(component)

    def restart(self, component: Component) -> ComponentStatus:
        self.require_image(component)
        return self.start_built(component, replace=True)

    def refresh(self, component: Component) -> ComponentStatus:
        self.build(component)
        return self.start_built(component, replace=True)

    def stop(
        self,
        component: Component,
        expected_id: str | None = None,
    ) -> bool:
        if expected_id is not None:
            try:
                self.require_container_id(expected_id)
            except CycloError as exc:
                raise CycloError("invalid expected container ID") from exc
        verify = self._container_verifier(component)
        container = self.inspect_container(
            expected_id or component.container,
            verify=verify,
        )
        if container is None:
            return False
        if expected_id is not None and expected_id != container.id:
            raise CycloError("Docker returned a different container than requested")
        return self.remove_container(
            container,
            verify=verify,
            timeout=10,
            remove_volumes=not component.preserve_volumes,
        )

    def logs(
        self,
        component: Component,
        lines: int = 80,
        *,
        expected_id: str | None = None,
    ) -> str:
        if expected_id is not None:
            try:
                self.require_container_id(expected_id)
            except CycloError as exc:
                raise CycloError("invalid expected container ID") from exc
        info = self.inspect(
            "container",
            expected_id or component.container,
        )
        if info is None:
            return ""
        self.require_owned(component, info, image=False)
        container_id = self.container_id(info)
        if expected_id is not None and container_id != expected_id:
            raise CycloError("Docker returned a different container than requested")
        result = self.call(
            ["logs", "--tail", str(lines), container_id],
            check=False,
        )
        return ((result.stdout or "") + (result.stderr or "")).strip()
