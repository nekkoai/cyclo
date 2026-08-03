from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from .errors import CycloError
from .images import Image
from .runtime import CycloRuntime, GATEWAY_STORE


_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_LABEL = "io.cyclo.gateway-tool"
_GATEWAY_COMPONENT = "gateway"
_CREDENTIALS_VOLUME = "credentials"


class GatewayAdmin:
    """One-shot gateway administration; DComp still owns the gateway service."""

    def __init__(self, runtime: CycloRuntime) -> None:
        self.runtime = runtime

    def providers(self) -> str:
        image = self.runtime.build_gateway()
        result = self._tool(image, ("providers",), capture=True)
        return (result.stdout or "").rstrip()

    def usage(self) -> dict[str, object]:
        volume = self._prepare_store()
        image = self.runtime.build_gateway()
        result = self._tool(
            image,
            ("usage",),
            volume=volume,
            read_only=True,
            capture=True,
        )
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise CycloError("gateway usage returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise CycloError("gateway usage did not return an object")
        return document

    def restart(self) -> None:
        self._prepare_store()
        self._restart_gateway()

    def login(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not arguments or any(not item for item in arguments):
            raise CycloError("gateway login requires a provider")
        volume = self._prepare_store()
        image = self.runtime.build_gateway()
        selected = list(arguments)
        indexes = [
            index for index, value in enumerate(selected) if value == "--api-key-env"
        ]
        if len(indexes) > 1:
            raise CycloError("--api-key-env may be used only once")
        input_data: str | None = None
        source_environment = os.environ if environment is None else environment
        if indexes:
            index = indexes[0]
            name = selected[index + 1] if index + 1 < len(selected) else ""
            if not _ENVIRONMENT_RE.fullmatch(name):
                raise CycloError(
                    "--api-key-env requires an environment variable name"
                )
            value = source_environment.get(name)
            if not value:
                raise CycloError(f"environment variable {name} is empty or unset")
            selected[index : index + 2] = ["--api-key-stdin"]
            input_data = value + "\n"
        self._tool(
            image,
            ("login", *selected),
            volume=volume,
            network="none" if "--api-key-stdin" in selected else "bridge",
            input_data=input_data,
            capture=False,
            interactive=True,
        )
        self._restart_gateway()

    def _restart_gateway(self) -> None:
        self.runtime.dcomp.restart(self.runtime.name, _GATEWAY_COMPONENT)
        observed = self.runtime.wait_status()
        gateway = observed.component(_GATEWAY_COMPONENT)
        if (
            gateway is None
            or gateway.status != "running"
            or gateway.health != "healthy"
        ):
            detail = gateway.problem if gateway is not None else "component is absent"
            raise CycloError(
                "gateway did not become ready after login"
                + (f": {detail}" if detail else "")
            )

    def credential_volume(self) -> str:
        return self.runtime.dcomp.volume(
            self.runtime.name,
            _GATEWAY_COMPONENT,
            _CREDENTIALS_VOLUME,
        )

    def destroy_store(self, confirmation: str) -> str:
        volume = self.credential_volume()
        if confirmation != volume:
            raise CycloError(
                "confirmation must exactly match the gateway volume name: "
                f"{volume}"
            )
        self.runtime.dcomp.down(self.runtime.name)
        result = self.runtime.images.command(
            ["volume", "rm", "--", volume],
            check=False,
        )
        if result.returncode != 0:
            inspected = self.runtime.images.command(
                ["volume", "inspect", "--", volume],
                check=False,
            )
            if inspected.returncode != 0:
                return volume
            detail = (result.stderr or result.stdout or "").strip()
            raise CycloError(
                "cannot delete gateway store"
                + (f": {detail}" if detail else "")
            )
        return volume

    def _prepare_store(self) -> str:
        """Ensure DComp has verified the store without reconciling unrelated work."""

        status = self.runtime.status()
        if status.operation:
            self.runtime.dcomp.resume(self.runtime.name)
            status = self.runtime.status()
        if not status.desired:
            self.runtime.apply_gateway()
            return self.credential_volume()
        gateway = status.component(_GATEWAY_COMPONENT)
        if (
            gateway is None
            or not gateway.container_id
            or gateway.status == "missing"
        ):
            raise CycloError(
                "gateway component is absent from the applied system; "
                "run `cyclo repair` before gateway administration"
            )
        return self.credential_volume()

    def _tool(
        self,
        image: Image,
        command: Sequence[str],
        *,
        volume: str | None = None,
        read_only: bool = False,
        network: str = "none",
        input_data: str | None = None,
        capture: bool,
        interactive: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self._remove_abandoned_tools()
        name = f"cyclo-{self.runtime.store.system}-gateway-tool-{uuid.uuid4().hex}"
        arguments = ["run"]
        if interactive:
            arguments.append("--interactive")
        arguments.extend(
            [
                "--rm",
                "--name",
                name,
                "--label",
                f"{_TOOL_LABEL}={self.runtime.name}",
                "--network",
                network,
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "256",
            ]
        )
        if volume is not None:
            mount = (
                f"type=volume,src={volume},dst={GATEWAY_STORE}"
            )
            if read_only:
                mount += ",readonly"
            arguments.extend(("--mount", mount))
        arguments.extend((image.id, *command))
        result = self.runtime.images.command(
            arguments,
            check=False,
            input_data=input_data,
            capture=capture,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise CycloError(
                f"gateway {' '.join(command[:1]) or 'tool'} failed with status "
                f"{result.returncode}"
                + (f": {detail}" if detail else "")
            )
        return result

    def _remove_abandoned_tools(self) -> None:
        result = self.runtime.images.command(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={_TOOL_LABEL}={self.runtime.name}",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise CycloError("cannot enumerate abandoned gateway tools")
        identifiers = sorted(set((result.stdout or "").splitlines()))
        for identifier in identifiers:
            if not identifier:
                continue
            removed = self.runtime.images.command(
                ["container", "rm", "--force", "--", identifier],
                check=False,
            )
            if removed.returncode != 0:
                raise CycloError(
                    f"cannot remove abandoned gateway tool {identifier}"
                )
