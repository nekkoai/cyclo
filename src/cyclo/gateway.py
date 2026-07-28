from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .component import (
    COMPONENT_INTERFACE,
    COMPONENT_SOCKET,
    CONTAINER_SOCKET_ROOT,
    PROVIDER_INTERFACE,
    Component,
    ComponentStatus,
    Mount,
    component_sources_root,
    parse_declaration,
)
from .component_runtime import (
    LABEL_COMPONENT_CLASS,
    LABEL_OWNED,
    LABEL_TYPE,
    ComponentController,
    ensure_directory,
)
from .errors import CycloError
from .installation import (
    LABEL_INSTANCE,
    LABEL_SYSTEM,
    gateway_name,
    installation_id,
)

_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
LABEL_TOOL = "io.cyclo.gateway-tool"


class Gateway:
    """The fixed credential gateway component and its private store.

    Lifecycle and one-shot methods require the installation lock at the CLI
    boundary; that lock is the liveness proof used for orphan reconciliation.
    """

    def __init__(
        self,
        components_root: Path,
        *,
        controller: ComponentController | None = None,
        network: str = "bridge",
    ) -> None:
        self.components_root = components_root.expanduser().resolve()
        if (
            not _NETWORK_RE.fullmatch(network)
            or network in {"default", "host", "none"}
        ):
            raise CycloError(
                "gateway network must be 'bridge' or a named Docker network"
            )
        self.network = network
        self.root = self.components_root / "gateway"
        self.config_dir = self.root / "config"
        self.socket_dir = self.root / "socket"
        self.socket_path = self.socket_dir / COMPONENT_SOCKET

        source = component_sources_root() / "gateway"
        declaration = parse_declaration(source / "component.conf")
        if (
            declaration.name != "gateway"
            or declaration.requires
            or set(declaration.provides)
            != {COMPONENT_INTERFACE, PROVIDER_INTERFACE}
        ):
            raise CycloError(
                "gateway component must provide exactly Component and Provider "
                "with no requirements"
            )
        system = installation_id(self.components_root)
        stem = gateway_name(system)
        self.store_volume = f"{stem}-state"
        self.component = Component(
            name="gateway",
            declaration=declaration,
            source=source,
            build_context=component_sources_root(),
            image=f"{stem}:latest",
            container=stem,
            system=system,
            arguments=(),
            mounts=(
                Mount(
                    self.store_volume,
                    "/var/lib/cyclo-gateway",
                    type="volume",
                ),
                Mount(
                    str(self.config_dir),
                    "/etc/cyclo-gateway",
                    read_only=True,
                ),
                Mount(str(self.socket_dir), str(CONTAINER_SOCKET_ROOT)),
            ),
            network=network,
            socket_path=self.socket_path,
            component_class="gateway",
            preserve_volumes=True,
        )
        self.controller = controller or ComponentController()

    def _prepare_root(self) -> None:
        ensure_directory(self.components_root, 0o700)
        ensure_directory(self.root, 0o700)

    def _prepare(self) -> None:
        self._prepare_root()
        ensure_directory(self.config_dir, 0o755)
        ensure_directory(self.socket_dir, 0o777)
        try:
            entries = list(self.socket_dir.iterdir())
        except OSError as exc:
            raise CycloError(
                f"cannot inspect gateway socket directory {self.socket_dir}: {exc}"
            ) from exc
        if entries and not self._socket_is_valid(entries):
            raise CycloError(
                "gateway socket directory must be empty or contain only "
                f"{COMPONENT_SOCKET}: {self.socket_dir}"
            )

    @staticmethod
    def _socket_is_valid(entries: Sequence[Path]) -> bool:
        try:
            return bool(
                len(entries) == 1
                and entries[0].name == COMPONENT_SOCKET
                and stat.S_ISSOCK(entries[0].lstat().st_mode)
                and not entries[0].is_symlink()
            )
        except OSError:
            return False

    def _volume_labels(self) -> dict[str, str]:
        return {
            LABEL_OWNED: "1",
            LABEL_SYSTEM: self.component.system,
            LABEL_INSTANCE: "gateway",
            LABEL_COMPONENT_CLASS: "gateway",
            LABEL_TYPE: "gateway-state",
        }

    def store_ready(self, *, create: bool = False) -> bool:
        volume = self.controller.inspect("volume", self.store_volume)
        if volume is None and create:
            labels = [
                item
                for key, value in self._volume_labels().items()
                for item in ("--label", f"{key}={value}")
            ]
            self.controller.call(
                ["volume", "create", *labels, "--name", self.store_volume]
            )
            volume = self.controller.inspect(
                "volume",
                self.store_volume,
                missing=False,
            )
        if volume is None:
            return False
        labels = volume.get("Labels")
        options = volume.get("Options")
        if (
            volume.get("Name") != self.store_volume
            or volume.get("Driver") != "local"
            or volume.get("Scope") != "local"
            or not isinstance(labels, Mapping)
            or dict(labels) != self._volume_labels()
            or (
                options not in (None, {})
                and not (isinstance(options, Mapping) and not options)
            )
        ):
            raise CycloError(
                f"refusing foreign gateway credential volume: "
                f"{self.store_volume}"
            )
        return True

    def _tool_component(
        self,
        info: Mapping[str, object],
    ) -> Component | None:
        labels = ComponentController.labels(info)
        if labels.get(LABEL_TOOL) != "1":
            return None
        raw_name = info.get("Name")
        name = (
            raw_name[1:]
            if isinstance(raw_name, str) and raw_name.startswith("/")
            else raw_name
        )
        prefix = f"{self.component.container}-tool-"
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or not re.fullmatch(r"[0-9a-f]{32}", name[len(prefix) :])
            or labels.get(LABEL_TYPE) != self.component.kind
        ):
            raise CycloError("refusing malformed gateway tool container")
        tool = replace(self.component, container=name)
        self.controller.require_owned(tool, info, image=False)
        return tool

    def _remove_abandoned_tools(self) -> int:
        """Remove labeled one-shot tools after their lock owner has exited."""

        result = self.controller.call(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={LABEL_TOOL}=1",
                "--filter",
                f"label={LABEL_SYSTEM}={self.component.system}",
            ]
        )
        removed = 0
        for identifier in sorted(set((result.stdout or "").splitlines())):
            if not identifier:
                continue
            info = self.controller.inspect("container", identifier)
            if info is None:
                continue
            if self.controller.container_id(info) != identifier:
                raise CycloError("Docker returned an invalid gateway tool")
            tool = self._tool_component(info)
            if tool is None:
                raise CycloError("Docker returned an unlabeled gateway tool")
            if self.controller.stop(tool, identifier):
                removed += 1
        return removed

    def _require_exclusive_store(self, allowed: str | None = None) -> None:
        # Reconcile every kind of abandoned gateway command, including
        # volume-free provider discovery, before checking store ownership.
        self._remove_abandoned_tools()
        result = self.controller.call(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"volume={self.store_volume}",
            ]
        )
        for identifier in set((result.stdout or "").splitlines()):
            if not identifier or identifier == allowed:
                continue
            info = self.controller.inspect(
                "container",
                identifier,
                missing=False,
            )
            assert info is not None
            if self.controller.container_id(info) != identifier:
                raise CycloError(
                    "Docker returned an invalid credential-volume user"
                )
            raise CycloError(
                f"credential volume is mounted by another container: "
                f"{identifier}"
            )

    def build(self) -> str:
        return self.controller.build(self.component)

    def ensure_image(self) -> str:
        return self.controller.ensure_image(self.component)

    def status(self, *, error: str = "") -> ComponentStatus:
        status = self.controller.status(self.component, error=error)
        store_ready = self.store_ready()
        if status.container_id and not store_ready and not status.error:
            status = replace(status, error="credential store is absent")
        if status.health == "ready":
            try:
                entries = list(self.socket_dir.iterdir())
            except OSError:
                entries = []
            if not self._socket_is_valid(entries):
                status = replace(
                    status,
                    health="not-ready",
                    error="gateway socket directory is invalid",
                )
        return status

    def _require_working(self, status: ComponentStatus) -> ComponentStatus:
        if status.works:
            return status
        cleanup_error = ""
        if status.container_id:
            try:
                self.controller.stop(
                    self.component,
                    status.container_id,
                )
            except Exception as exc:
                cleanup_error = f"; cleanup failed: {exc}"
        detail = status.error or status.health
        raise CycloError(
            "gateway did not become ready"
            + (f": {detail}" if detail else "")
            + cleanup_error
        )

    def _activate(
        self,
        operation: Callable[[Component], ComponentStatus],
    ) -> ComponentStatus:
        self._prepare()
        self.store_ready(create=True)
        current = self.controller.status(self.component)
        self._require_exclusive_store(current.container_id)
        status = operation(self.component)
        return self._require_working(
            self.status(error=status.error)
        )

    def start(self) -> ComponentStatus:
        return self._activate(self.controller.start)

    def stop(self) -> bool:
        self._remove_abandoned_tools()
        return self.controller.stop(self.component)

    def restart(self) -> ComponentStatus:
        return self._activate(self.controller.restart)

    def refresh(self) -> ComponentStatus:
        return self._activate(self.controller.refresh)

    def logs(self, lines: int = 80) -> str:
        return self.controller.logs(self.component, lines)

    def _tool(
        self,
        command: Sequence[str],
        *,
        volume: bool,
        network: str = "none",
        interactive: bool = False,
        input_data: str | None = None,
        capture: bool = True,
        volume_read_only: bool = False,
        create_volume: bool = True,
        config: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if volume:
            if not self.store_ready(create=create_volume):
                raise CycloError("gateway credential store is absent")
            current = self.controller.status(self.component)
            self._require_exclusive_store(current.container_id)
        image_id = self.controller.require_image(self.component)
        # `docker run --rm` can outlive an interrupted Docker client. Give the
        # one-shot container an owned identity before attaching to it.
        tool = replace(
            self.component,
            container=(
                f"{self.component.container}-tool-{uuid.uuid4().hex}"
            ),
        )
        labels = [
            item
            for key, value in {
                **ComponentController.expected_labels(tool),
                LABEL_TOOL: "1",
            }.items()
            for item in ("--label", f"{key}={value}")
        ]
        arguments = [
            "create",
            "--name",
            tool.container,
            *labels,
            *(["--interactive"] if interactive else []),
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
            network,
        ]
        if volume:
            arguments.extend(
                [
                    "--mount",
                    (
                        f"type=volume,src={self.store_volume},"
                        "dst=/var/lib/cyclo-gateway"
                        + (",readonly" if volume_read_only else "")
                    ),
                ]
            )
        if config:
            arguments.extend(
                [
                    "--mount",
                    (
                        f"type=bind,src={self.config_dir},"
                        "dst=/etc/cyclo-gateway,readonly"
                    ),
                ]
            )
        arguments.extend([image_id, *command])
        identifier: str | None = None
        try:
            created = self.controller.call(arguments)
            created_id = (created.stdout or "").strip()
            if not _CONTAINER_ID_RE.fullmatch(created_id):
                raise CycloError(
                    "Docker create returned an invalid gateway tool container ID"
                )
            identifier = created_id
            if created.stderr and not capture:
                print(created.stderr, end="", file=sys.stderr)
            result = self.controller.call(
                [
                    "start",
                    "--attach",
                    *(["--interactive"] if interactive else []),
                    identifier,
                ],
                capture=capture,
                input_data=input_data,
            )
        except BaseException as cause:
            self._remove_tool_container(
                tool,
                identifier,
                cause=cause,
            )
            raise
        self._remove_tool_container(tool, identifier)
        if created.stderr and capture:
            result = subprocess.CompletedProcess(
                result.args,
                result.returncode,
                result.stdout,
                created.stderr + (result.stderr or ""),
            )
        return result

    def _remove_tool_container(
        self,
        tool: Component,
        identifier: str | None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        try:
            self.controller.stop(tool, identifier)
        except Exception as cleanup:
            if cause is None:
                raise
            primary = str(cause) or cause.__class__.__name__
            raise CycloError(
                f"{primary}; gateway tool cleanup failed: {cleanup}"
            ) from cause

    def providers(self) -> str:
        self.ensure_image()
        self._remove_abandoned_tools()
        return (
            self._tool(["providers"], volume=False).stdout or ""
        ).rstrip()

    def usage(self) -> dict[str, object]:
        result = self._tool(
            ["usage"],
            volume=True,
            volume_read_only=True,
            create_volume=False,
        )
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise CycloError(
                "gateway usage command returned invalid JSON"
            ) from exc
        if not isinstance(document, dict):
            raise CycloError(
                "gateway usage command did not return an object"
            )
        return document

    def login(self, arguments: Sequence[str]) -> ComponentStatus:
        if not arguments or any(
            not isinstance(item, str) or not item for item in arguments
        ):
            raise CycloError("gateway login requires a provider")
        self._prepare()
        self.ensure_image()
        normalized = list(arguments)
        indexes = [
            index
            for index, value in enumerate(normalized)
            if value == "--api-key-env"
        ]
        if len(indexes) > 1:
            raise CycloError("--api-key-env may be used only once")
        input_data: str | None = None
        if indexes:
            index = indexes[0]
            name = (
                normalized[index + 1]
                if index + 1 < len(normalized)
                else ""
            )
            if not _ENVIRONMENT_RE.fullmatch(name):
                raise CycloError(
                    "--api-key-env requires an environment variable name"
                )
            value = os.environ.get(name)
            if not value:
                raise CycloError(
                    f"environment variable {name} is empty or unset"
                )
            normalized[index : index + 2] = ["--api-key-stdin"]
            input_data = value + "\n"
        api_key = "--api-key-stdin" in normalized
        self._tool(
            ["login", *normalized],
            volume=True,
            network="none" if api_key else self.network,
            interactive=True,
            input_data=input_data,
            capture=False,
            config=True,
        )
        status = self.controller.restart(self.component)
        return self._require_working(
            self.status(error=status.error)
        )

    def destroy_store(self) -> bool:
        volume = self.controller.inspect("volume", self.store_volume)
        if volume is None:
            return False
        self.store_ready()
        current = self.controller.status(self.component)
        self._require_exclusive_store(current.container_id)
        self.stop()
        self._require_exclusive_store()
        self.controller.call(["volume", "rm", self.store_volume])
        return True
