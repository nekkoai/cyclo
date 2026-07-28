from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .component import (
    COMPONENT_INTERFACE,
    COMPONENT_SOCKET,
    CONTAINER_REQUIREMENT_ROOT,
    CONTAINER_SOCKET_ROOT,
    MAX_CONFIG_BYTES,
    PROVIDER_INTERFACE,
    Component,
    ComponentStatus,
    Declaration,
    Mount,
    canonical_directory,
    connect_unary,
    is_component_name,
    parse_declaration,
    regular_file,
)
from .component_runtime import (
    LABEL_COMPONENT_CLASS,
    LABEL_OWNED,
    ComponentController,
    ensure_directory,
)
from .errors import CycloError
from .gateway import Gateway
from .installation import (
    LABEL_INSTANCE,
    LABEL_SYSTEM,
    installation_id,
    provider_name,
)
from .model_ids import split_public_model_id


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    source: Path
    build_context: Path
    declaration: Declaration
    bindings: tuple[tuple[str, str], ...]
    arguments: tuple[str, ...]
    line: int

    def target(self, requirement: str) -> str:
        for name, target in self.bindings:
            if name == requirement:
                return target
        raise KeyError(requirement)


@dataclass(frozen=True)
class ProviderConfiguration:
    path: Path
    providers: tuple[ProviderDefinition, ...]
    generation: str


@dataclass(frozen=True)
class ProviderConnection:
    """The provider socket selected for a team launch plus observed components."""

    generation: str
    socket_path: Path
    components: tuple[ComponentStatus, ...]


def catalogue_ids(document: Mapping[str, object]) -> tuple[str, ...]:
    """Return ordered model IDs from a structurally valid Provider catalogue."""

    models = document.get("models")
    if not isinstance(models, list):
        raise CycloError("provider system returned an invalid model catalogue")
    result: list[str] = []
    seen: set[str] = set()
    for model in models:
        identifier = model.get("id") if isinstance(model, dict) else None
        if (
            split_public_model_id(identifier) is None
            or identifier in seen
            or not isinstance(model.get("inferenceFormat"), str)
            or not model["inferenceFormat"]
        ):
            raise CycloError(
                "provider system returned an invalid or duplicate model"
            )
        seen.add(identifier)
        result.append(identifier)
    return tuple(result)


def _strip_comment(line: str) -> str:
    marker = re.search(r"(?:^|\s)#", line)
    return line if marker is None else line[: marker.start()]


def load_provider_configuration(path: Path) -> ProviderConfiguration:
    """Parse host.conf and validate its provider interfaces and bindings."""

    selected = path.expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = Path(os.path.abspath(selected))
    try:
        raw = selected.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise CycloError(
            f"{selected}:1: cannot read configuration: {exc}"
        ) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise CycloError(
            f"{selected}:1: configuration exceeds {MAX_CONFIG_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CycloError(
            f"{selected}:1: configuration is not valid UTF-8"
        ) from exc

    providers: list[ProviderDefinition] = []
    available: dict[str, set[str]] = {
        "gateway": {COMPONENT_INTERFACE, PROVIDER_INTERFACE}
    }
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = _strip_comment(raw_line).strip()
        if not content:
            continue
        fields = content.split()
        if fields[0] != "provider" or len(fields) < 3:
            raise CycloError(
                f"{selected}:{line_number}: expected: provider NAME SOURCE "
                "[context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]"
            )
        name = fields[1]
        if (
            not is_component_name(name)
            or name == "gateway"
            or name in available
        ):
            raise CycloError(
                f"{selected}:{line_number}: invalid or duplicate provider "
                f"name {name!r}"
            )

        configured_source = fields[2]
        if configured_source == "~" or configured_source.startswith("~/"):
            raise CycloError(
                f"{selected}:{line_number}: component paths do not expand '~'"
            )
        lexical_source = Path(configured_source)
        if not lexical_source.is_absolute():
            lexical_source = selected.parent / lexical_source
        source = canonical_directory(lexical_source, "component source")
        regular_file(source / "Dockerfile", "component Dockerfile")
        declaration = parse_declaration(source / "component.conf")
        if PROVIDER_INTERFACE not in declaration.provides:
            raise CycloError(
                f"{selected}:{line_number}: provider component must provide "
                f"{PROVIDER_INTERFACE}"
            )
        if not any(
            requirement.service == PROVIDER_INTERFACE
            for requirement in declaration.requires
        ):
            raise CycloError(
                f"{selected}:{line_number}: provider component must require "
                f"an upstream {PROVIDER_INTERFACE}"
            )

        separators = [
            index for index, field in enumerate(fields) if field == "--"
        ]
        if len(separators) > 1:
            raise CycloError(
                f"{selected}:{line_number}: provider line contains more than "
                "one '--'"
            )
        boundary = separators[0] if separators else len(fields)
        arguments = tuple(fields[boundary + 1 :]) if separators else ()
        requirements = {
            requirement.name: requirement
            for requirement in declaration.requires
        }
        bindings: dict[str, str] = {}
        configured_context: str | None = None
        for setting in fields[3:boundary]:
            key, separator, value = setting.partition("=")
            if not separator or not key:
                raise CycloError(
                    f"{selected}:{line_number}: expected REQUIREMENT=TARGET "
                    f"before '--', got {setting!r}"
                )
            if key == "context":
                if "context" in requirements:
                    raise CycloError(
                        f"{selected}:{line_number}: requirement name "
                        "'context' is reserved"
                    )
                if configured_context is not None or not value:
                    raise CycloError(
                        f"{selected}:{line_number}: invalid duplicate or "
                        "empty context setting"
                    )
                configured_context = value
                continue
            requirement = requirements.get(key)
            if requirement is None or key in bindings or not value:
                raise CycloError(
                    f"{selected}:{line_number}: invalid or duplicate binding "
                    f"{setting!r}"
                )
            target_services = available.get(value)
            if target_services is None:
                raise CycloError(
                    f"{selected}:{line_number}: binding {key} targets unknown "
                    f"or later provider {value}"
                )
            if requirement.service not in target_services:
                raise CycloError(
                    f"{selected}:{line_number}: binding {key} requires "
                    f"{requirement.service}, but {value} does not provide it"
                )
            bindings[key] = value
        for requirement in declaration.requires:
            if requirement.name not in bindings:
                suggested = (
                    "gateway" if requirement.name == "upstream" else "TARGET"
                )
                raise CycloError(
                    f"{selected}:{line_number}: missing binding "
                    f"{requirement.name}={suggested}"
                )

        if configured_context is None:
            build_context = source
        else:
            if (
                configured_context == "~"
                or configured_context.startswith("~/")
            ):
                raise CycloError(
                    f"{selected}:{line_number}: build context paths do not "
                    "expand '~'"
                )
            lexical_context = Path(configured_context)
            if not lexical_context.is_absolute():
                lexical_context = source / lexical_context
            build_context = canonical_directory(
                lexical_context,
                "build context",
            )
            if not source.is_relative_to(build_context):
                raise CycloError(
                    f"{selected}:{line_number}: component source must be "
                    "inside its build context"
                )

        providers.append(
            ProviderDefinition(
                name=name,
                source=source,
                build_context=build_context,
                declaration=declaration,
                bindings=tuple(bindings.items()),
                arguments=arguments,
                line=line_number,
            )
        )
        available[name] = set(declaration.provides)

    return ProviderConfiguration(
        selected,
        tuple(providers),
        hashlib.sha256(raw).hexdigest(),
    )


class ProviderSystem:
    """Compose the fixed gateway with providers declared in host.conf."""

    def __init__(
        self,
        components_root: Path,
        config_path: Path,
        *,
        gateway: Gateway | None = None,
        controller: ComponentController | None = None,
        load_config: bool = True,
    ) -> None:
        self.components_root = components_root.expanduser().resolve()
        self.config_path = config_path.expanduser()
        if (
            gateway is not None
            and controller is not None
            and gateway.controller is not controller
        ):
            raise CycloError(
                "gateway and provider system must share one component controller"
            )
        self.controller = (
            controller
            or (gateway.controller if gateway is not None else None)
            or ComponentController()
        )
        self.gateway = gateway or Gateway(
            self.components_root,
            controller=self.controller,
        )
        self.configuration = (
            load_provider_configuration(self.config_path)
            if load_config
            else ProviderConfiguration(
                Path(os.path.abspath(self.config_path)),
                (),
                "",
            )
        )
        self.provider_components = self._make_components()
        self.components = (
            self.gateway.component,
            *self.provider_components,
        )

    @property
    def system(self) -> str:
        return installation_id(self.components_root)

    @property
    def sockets_root(self) -> Path:
        return self.components_root / "sockets"

    @property
    def configured_socket_path(self) -> Path:
        if not self.configuration.providers:
            return self.gateway.socket_path
        return self.socket_path(self.configuration.providers[-1].name)

    def socket_dir(self, name: str) -> Path:
        return self.sockets_root / name

    def socket_path(self, name: str) -> Path:
        return self.socket_dir(name) / COMPONENT_SOCKET

    def _make_components(self) -> tuple[Component, ...]:
        socket_dirs = {"gateway": self.gateway.socket_dir}
        socket_dirs.update(
            {
                provider.name: self.socket_dir(provider.name)
                for provider in self.configuration.providers
            }
        )
        result: list[Component] = []
        for provider in self.configuration.providers:
            stem = provider_name(self.system, provider.name)
            mounts = [
                Mount(
                    str(socket_dirs[provider.name]),
                    str(CONTAINER_SOCKET_ROOT),
                )
            ]
            for requirement in provider.declaration.requires:
                mounts.append(
                    Mount(
                        str(
                            socket_dirs[
                                provider.target(requirement.name)
                            ]
                        ),
                        str(
                            CONTAINER_REQUIREMENT_ROOT / requirement.name
                        ),
                        read_only=True,
                    )
                )
            result.append(
                Component(
                    name=provider.name,
                    declaration=provider.declaration,
                    source=provider.source,
                    build_context=provider.build_context,
                    image=f"{stem}:latest",
                    container=stem,
                    system=self.system,
                    arguments=provider.arguments,
                    mounts=tuple(mounts),
                    network="none",
                    socket_path=self.socket_path(provider.name),
                )
            )
        return tuple(result)

    def _prepare(
        self,
        providers: tuple[ProviderDefinition, ...],
    ) -> None:
        ensure_directory(self.components_root, 0o700)
        ensure_directory(self.sockets_root, 0o700)
        for provider in providers:
            output = ensure_directory(
                self.socket_dir(provider.name),
                0o777,
            )
            requirements = ensure_directory(
                output / "requirements",
                0o755,
            )
            for requirement in provider.declaration.requires:
                ensure_directory(
                    requirements / requirement.name,
                    0o755,
                )

    def component(self, name: str) -> Component:
        for component in self.components:
            if component.name == name:
                return component
        raise CycloError(f"unknown configured component: {name}")

    def check(self) -> int:
        return len(self.provider_components)

    @staticmethod
    def _unavailable_status(
        component: Component,
        error: str,
    ) -> ComponentStatus:
        return ComponentStatus(
            component.name,
            component.kind,
            None,
            None,
            False,
            "unknown",
            "missing",
            False,
            "unreachable",
            error,
        )

    def status_component(
        self,
        name: str,
        *,
        error: str = "",
    ) -> ComponentStatus:
        """Inspect exactly one component without touching unrelated providers."""

        component = self.component(name)
        try:
            if name == "gateway":
                return self.gateway.status(error=error)
            return self.controller.status(component, error=error)
        except CycloError as exc:
            observed = str(exc)
            detail = (
                f"{error}; inspection failed: {observed}"
                if error and observed not in error
                else error or observed
            )
            return self._unavailable_status(component, detail)

    def statuses(
        self,
        errors: Mapping[str, str] | None = None,
    ) -> tuple[ComponentStatus, ...]:
        failures = errors or {}
        return tuple(
            self.status_component(
                component.name,
                error=failures.get(component.name, ""),
            )
            for component in self.components
        )

    def _usable_components(
        self,
        statuses: tuple[ComponentStatus, ...],
    ) -> dict[str, bool]:
        by_name = {status.name: status for status in statuses}
        gateway = by_name.get("gateway")
        usable = {"gateway": bool(gateway and gateway.works)}
        for definition in self.configuration.providers:
            status = by_name.get(definition.name)
            usable[definition.name] = bool(
                status
                and status.works
                and all(
                    usable.get(target, False)
                    for _requirement, target in definition.bindings
                )
            )
        return usable

    def _dependency_names(self, name: str) -> tuple[str, ...]:
        definitions = {
            definition.name: definition
            for definition in self.configuration.providers
        }
        needed: set[str] = set()

        def add_dependencies(selected: str) -> None:
            definition = definitions.get(selected)
            if definition is None:
                return
            for _requirement, target in definition.bindings:
                if target in needed:
                    continue
                needed.add(target)
                add_dependencies(target)

        add_dependencies(name)
        return tuple(
            component.name
            for component in self.components
            if component.name in needed
        )

    def connection(
        self,
        statuses: tuple[ComponentStatus, ...] | None = None,
    ) -> ProviderConnection:
        observed = self.statuses() if statuses is None else statuses
        by_name = {status.name: status for status in observed}
        usable = self._usable_components(observed)
        gateway_status = by_name.get("gateway")
        if not usable["gateway"]:
            detail = gateway_status.error if gateway_status else "not found"
            raise CycloError(
                "credential gateway is not working"
                + (f": {detail}" if detail else "")
            )

        selected = self.gateway.socket_path
        for definition in self.configuration.providers:
            if usable[definition.name]:
                selected = self.socket_path(definition.name)
        return ProviderConnection(
            self.configuration.generation,
            selected,
            observed,
        )

    def build(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (component.name, self.controller.build(component))
            for component in self.provider_components
        )

    def build_component(self, name: str) -> str:
        if name == "gateway":
            return self.gateway.build()
        return self.controller.build(self.component(name))

    def start_component(
        self,
        name: str,
        *,
        restart: bool = False,
    ) -> ComponentStatus:
        if name == "gateway":
            return (
                self.gateway.restart()
                if restart
                else self.gateway.start()
            )
        component = self.component(name)
        definition = next(
            provider
            for provider in self.configuration.providers
            if provider.name == name
        )
        self._prepare((definition,))
        dependency_names = self._dependency_names(name)
        observed = tuple(
            self.status_component(dependency)
            for dependency in dependency_names
        )
        usable = self._usable_components(observed)
        unavailable = [
            dependency
            for dependency in dependency_names
            if not usable.get(dependency, False)
        ]
        if unavailable:
            raise CycloError(
                f"cannot start component {name}; required component "
                f"unavailable: {', '.join(unavailable)}"
            )
        return (
            self.controller.restart(component)
            if restart
            else self.controller.start(component)
        )

    def stop_component(self, name: str) -> bool:
        if name == "gateway":
            return self.gateway.stop()
        return self.controller.stop(self.component(name))

    def component_logs(self, name: str, lines: int = 80) -> str:
        if name == "gateway":
            return self.gateway.logs(lines)
        return self.controller.logs(self.component(name), lines)

    def start(self) -> ProviderConnection:
        errors: dict[str, str] = {}
        gateway_status = self.gateway.start()
        if not gateway_status.works:
            raise CycloError("credential gateway is not working")
        self._prepare(())
        working = {"gateway": True}
        by_name = {
            provider.name: provider
            for provider in self.configuration.providers
        }
        for component in self.provider_components:
            definition = by_name[component.name]
            unavailable = [
                target
                for _requirement, target in definition.bindings
                if not working.get(target, False)
            ]
            if unavailable:
                detail = (
                    "required component unavailable: "
                    + ", ".join(unavailable)
                )
                current = self.status_component(
                    component.name,
                    error=detail,
                )
                detail = current.error or detail
                if current.container_id:
                    try:
                        self.controller.stop(component, current.container_id)
                    except CycloError as exc:
                        detail += f"; cleanup failed: {exc}"
                errors[component.name] = detail
                working[component.name] = False
                continue
            try:
                self._prepare((definition,))
                status = self.controller.start(component)
            except CycloError as exc:
                errors[component.name] = str(exc)
                working[component.name] = False
            else:
                working[component.name] = status.works
        self._stop_unconfigured(
            {component.name for component in self.provider_components}
        )
        return self.connection(self.statuses(errors))

    def restart(self) -> ProviderConnection:
        gateway = self.gateway.status()
        if not gateway.works:
            raise CycloError("credential gateway is not working")
        for component in self.provider_components:
            self.controller.require_image(component)
        self.stop()
        return self.start()

    def refresh(self) -> ProviderConnection:
        self.gateway.build()
        self.build()
        self.stop()
        self.gateway.restart()
        return self.start()

    def stop(self) -> tuple[str, ...]:
        stopped: list[str] = []
        for component in reversed(self.provider_components):
            if self.controller.stop(component):
                stopped.append(component.name)
        for name in self._stop_unconfigured(set()):
            if name not in stopped:
                stopped.append(name)
        return tuple(stopped)

    def _stop_unconfigured(self, configured: set[str]) -> tuple[str, ...]:
        result = self.controller.call(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={LABEL_OWNED}=1",
                "--filter",
                f"label={LABEL_SYSTEM}={self.system}",
                "--filter",
                f"label={LABEL_COMPONENT_CLASS}=provider",
            ]
        )
        stopped: list[str] = []
        for identifier in dict.fromkeys(
            (result.stdout or "").splitlines()
        ):
            if not identifier:
                continue
            info = self.controller.inspect(
                "container",
                identifier,
                missing=False,
            )
            assert info is not None
            labels = self.controller.labels(info)
            name = labels.get(LABEL_INSTANCE)

            def verify_orphan(candidate: Mapping[str, object]) -> None:
                candidate_labels = self.controller.labels(candidate)
                raw_candidate_name = candidate.get("Name")
                candidate_name = (
                    raw_candidate_name[1:]
                    if isinstance(raw_candidate_name, str)
                    and raw_candidate_name.startswith("/")
                    else raw_candidate_name
                )
                if (
                    self.controller.container_id(candidate) != identifier
                    or candidate_labels.get(LABEL_OWNED) != "1"
                    or candidate_labels.get(LABEL_SYSTEM) != self.system
                    or candidate_labels.get(LABEL_COMPONENT_CLASS) != "provider"
                    or not is_component_name(name)
                    or candidate_labels.get(LABEL_INSTANCE) != name
                    or candidate_name != provider_name(self.system, str(name))
                ):
                    raise CycloError(
                        "invalid Cyclo ownership labels on container "
                        f"{identifier}"
                    )

            container = self.controller.verify_container(
                info,
                verify=verify_orphan,
            )
            assert isinstance(name, str)
            if name in configured:
                continue
            self.controller.remove_container(
                container,
                verify=verify_orphan,
                timeout=10,
                remove_volumes=True,
            )
            stopped.append(name)
        return tuple(stopped)

    @staticmethod
    def _validate_models_document(
        response: dict[str, object],
    ) -> dict[str, object]:
        document = {**response, "models": response.get("models", [])}
        catalogue_ids(document)
        return document

    def _models_document_at(self, socket_path: Path) -> dict[str, object]:
        return self._validate_models_document(
            connect_unary(
                socket_path,
                PROVIDER_INTERFACE,
                "ListModels",
                timeout=10.0,
            )
        )

    def catalogue(
        self,
        connection: ProviderConnection,
    ) -> tuple[ProviderConnection, dict[str, object]]:
        """Select the newest usable provider that returns a valid catalogue."""

        observed = connection
        usable = self._usable_components(observed.components)
        candidates = [
            "gateway",
            *(
                definition.name
                for definition in self.configuration.providers
                if usable.get(definition.name, False)
            ),
        ]
        failures: dict[str, str] = {}
        for name in reversed(candidates):
            socket_path = (
                self.gateway.socket_path
                if name == "gateway"
                else self.socket_path(name)
            )
            try:
                document = self._models_document_at(socket_path)
            except CycloError as exc:
                failures[name] = str(exc)
                continue
            statuses = tuple(
                replace(
                    status,
                    error=(
                        f"model catalogue unavailable: {failures[status.name]}"
                    ),
                )
                if status.name in failures
                else status
                for status in observed.components
            )
            return (
                ProviderConnection(
                    observed.generation,
                    socket_path,
                    statuses,
                ),
                document,
            )
        detail = "; ".join(
            f"{name}: {error}" for name, error in failures.items()
        )
        raise CycloError(
            "no working provider returned a valid model catalogue"
            + (f": {detail}" if detail else "")
        )
