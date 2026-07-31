from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from . import __version__
from .authority import trusted_host_roots, validate_project_authority
from .dcomp import DCompClient, DCompStatus
from .dcomp_system import (
    DCOMP_COMPONENT_PORT,
    Component,
    Link,
    MaterializedSystem,
    Materializer,
    PROVIDER_SERVICE,
    PublishedPort,
    System,
    Volume,
    provider_endpoint,
)
from .errors import CycloError
from .host import Host, Provider, load_host
from .images import Image, Images
from .mounts import (
    validate_mount_authority,
    validate_strict_source_root_separation,
    validate_team_mount_separation,
)
from .project import ProjectDefinition, ProjectTeam
from .project_state import decode_instance_project
from .resources import components_root
from .state import Instance, StateStore
from .team import Team
from .team.component import (
    TEAM_DASHBOARD_PORT,
    make_team_component,
    team_component_name,
)
from .team.image import TeamImageBuilder, require_non_root_team_host


GATEWAY_STORE = "/var/lib/cyclo-gateway"


@dataclass(frozen=True)
class HostImages:
    gateway: Image
    providers: Mapping[str, Image]


@dataclass(frozen=True)
class AppliedSystem:
    definition: MaterializedSystem
    status: DCompStatus


class CycloRuntime:
    """Compile Cyclo domain state and delegate all runtime lifecycle to DComp."""

    def __init__(
        self,
        store: StateStore,
        host_config: Path,
        *,
        dcomp: DCompClient | None = None,
        images: Images | None = None,
    ) -> None:
        self.store = store
        self.host_config = host_config
        self._host: Host | None = None
        self.name = f"cyclo-{store.system}"
        self.generated_root = store.root / "system"
        self.materializer = Materializer(self.generated_root)
        self.dcomp = dcomp or DCompClient(store)
        self.images = images or Images(endpoint=store.bound_docker_endpoint)
        self.team_images = TeamImageBuilder(self.images, store.system)
        self._gateway_image_cache: Image | None = None
        self._provider_image_cache: dict[str, Image] = {}

    @property
    def host(self) -> Host:
        """Load provider topology only when an operation actually needs it."""

        if self._host is None:
            self._host = load_host(self.host_config)
        return self._host

    @property
    def gateway_volume(self) -> str:
        return f"dcomp.{self.name}.volume.gateway.credentials"

    def prepare(self) -> None:
        self.store.ensure()

    def build_host(self, *, rebuild: bool = False) -> HostImages:
        self._bind_docker()
        self.prepare()
        gateway = self.build_gateway(rebuild=rebuild)
        providers: dict[str, Image] = {}
        for provider in self.host.providers:
            if rebuild or provider.name not in self._provider_image_cache:
                self._provider_image_cache[provider.name] = self._provider_image(
                    provider
                )
            providers[provider.name] = self._provider_image_cache[provider.name]
        return HostImages(gateway, providers)

    def build_gateway(self, *, rebuild: bool = False) -> Image:
        """Ask Docker to build the gateway, using its native build cache."""

        self._bind_docker()
        self.prepare()
        if self._gateway_image_cache is None or rebuild:
            self._gateway_image_cache = self._gateway_image()
        return self._gateway_image_cache

    def build_team(
        self,
        team: Team,
        *,
        override: str = "",
    ) -> Image:
        require_non_root_team_host()
        self._bind_docker()
        return self.team_images.build(team, override=override)

    def system(
        self,
        host_images: HostImages,
        instances: Iterable[Instance],
    ) -> System:
        active = tuple(
            sorted(
                (instance for instance in instances if instance.intent == "running"),
                key=lambda item: item.id,
            )
        )
        self.validate_instances(active)
        components: list[Component] = [
            self._gateway_component(
                host_images.gateway,
                publish_provider=not self.host.providers,
            )
        ]
        links: list[Link] = []

        for provider in self.host.providers:
            image = host_images.providers.get(provider.name)
            if image is None:
                raise CycloError(
                    f"missing built image for provider {provider.name!r}"
                )
            ports: tuple[PublishedPort, ...] = ()
            outer = provider.name == self.host.outer_component
            if outer:
                ports = (PublishedPort("127.0.0.1", 0, 50051),)
            components.append(
                Component(
                    provider.name,
                    image.id,
                    inputs=provider.contract.inputs,
                    outputs=provider.contract.outputs,
                    arguments=provider.arguments,
                    ports=ports,
                    # DComp requires an externally routed base network for a
                    # published host socket. Provider links still use their
                    # dedicated private networks.
                    egress=outer,
                )
            )
            links.extend(
                Link(
                    provider.name,
                    binding.input,
                    binding.component,
                    binding.output,
                )
                for binding in provider.bindings
            )

        for instance in active:
            component = make_team_component(self.store, instance)
            components.append(component)
            links.append(
                Link(
                    component.name,
                    "provider",
                    self.host.outer_component,
                    self.host.outer_output,
                )
            )
        return System(self.name, tuple(components), tuple(links))

    def validate_project_mounts(
        self,
        definition: ProjectDefinition,
        teams: Iterable[tuple[ProjectTeam, Team]],
    ) -> None:
        validate_project_authority(
            self.store,
            self.host,
            definition,
            teams,
            docker_endpoint=self.store.docker_endpoint,
            dcomp_executable=self.dcomp.executable,
        )

    def validate_instances(self, instances: Iterable[Instance]) -> None:
        """Recheck persisted mount authority before emitting any DComp bind."""

        selected = tuple(instances)
        if selected:
            require_non_root_team_host()
        groups: dict[
            tuple[str, str],
            tuple[dict[Path, str], dict[Path, str]],
        ] = {}
        all_teams: dict[Path, str] = {}
        all_mounts: dict[Path, str] = {}
        for instance in selected:
            project = decode_instance_project(instance).require_valid()
            key = (instance.project_file, instance.project_generation)
            teams, mounts = groups.setdefault(key, ({}, {}))
            try:
                team_path = Path(instance.team_path).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise CycloError(
                    f"cannot resolve team path for instance {instance.id!r}: {exc}"
                ) from exc
            teams.setdefault(team_path, f"team of instance {instance.id!r}")
            all_teams.setdefault(team_path, f"team of instance {instance.id!r}")
            for mount in project.mounts:
                mounts.setdefault(
                    mount.path,
                    f"mount {mount.name!r} of project {instance.project_name!r}",
                )
                all_mounts.setdefault(
                    mount.path,
                    f"mount {mount.name!r} of project {instance.project_name!r}",
                )
        if not groups:
            return
        endpoint = self.store.docker_endpoint
        trusted = self._trusted_host_roots()
        for teams, mounts in groups.values():
            validate_mount_authority(
                teams.items(),
                mounts.items(),
                state_root=self.store.root,
                docker_endpoint=endpoint,
                trusted_roots=trusted,
            )
        validate_strict_source_root_separation(
            (*all_teams.items(), *all_mounts.items())
        )
        validate_team_mount_separation(all_teams.items(), all_mounts.items())

    def materialize(
        self,
        host_images: HostImages,
        instances: Iterable[Instance],
    ) -> MaterializedSystem:
        return self.materializer.materialize(
            self.system(host_images, instances)
        )

    def apply(
        self,
        instances: Iterable[Instance],
        *,
        rebuild_host: bool = False,
    ) -> AppliedSystem:
        selected = tuple(instances)
        self.validate_instances(
            instance for instance in selected if instance.intent == "running"
        )
        host_images = self.build_host(rebuild=rebuild_host)
        definition = self.materialize(host_images, selected)
        return self._apply_definition(definition)

    def apply_gateway(self) -> AppliedSystem:
        """Apply only the fixed gateway, for isolated credential administration."""

        gateway = self.build_gateway()
        definition = self.materializer.materialize(
            System(
                self.name,
                (self._gateway_component(gateway, publish_provider=True),),
                (),
            )
        )
        return self._apply_definition(definition, wait=False)

    def _apply_definition(
        self,
        definition: MaterializedSystem,
        *,
        wait: bool = True,
    ) -> AppliedSystem:
        self.dcomp.check(definition.path)
        status = self.dcomp.status(self.name)
        if status.operation:
            self.dcomp.resume(self.name)
        self.dcomp.up(definition.path)
        observed = self.wait_status() if wait else self.status()
        return AppliedSystem(definition, observed)

    def rebuild_gateway(self, instances: Iterable[Instance]) -> AppliedSystem:
        """Rebuild, apply, and restart the gateway as one user operation."""

        self.build_gateway(rebuild=True)
        applied = self.apply(instances)
        self.dcomp.restart(self.name, "gateway")
        return AppliedSystem(applied.definition, self.wait_status())

    def status(self) -> DCompStatus:
        return self.dcomp.status(self.name)

    def wait_status(self, timeout: float = 30.0) -> DCompStatus:
        """Wait only for Docker health checks to settle; do not repair or reroute."""

        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            settling = bool(
                status.operation
                or any(
                    component.status in {"created", "restarting"}
                    or component.health == "starting"
                    for component in status.components
                )
            )
            if not settling or time.monotonic() >= deadline:
                return status
            time.sleep(0.25)

    def component_for_instance(self, identifier: str) -> str:
        return team_component_name(identifier)

    def outer_port(self, status: DCompStatus | None = None) -> int:
        observed = status or self.status()
        component = observed.component(self.host.outer_component)
        if component is None:
            raise CycloError(
                f"outer provider component {self.host.outer_component!r} is absent"
            )
        matches = [
            port.host_port
            for port in component.published_ports
            if port.protocol == "tcp"
            and port.container_port == DCOMP_COMPONENT_PORT
            and port.host_ip == "127.0.0.1"
        ]
        if len(matches) != 1 or matches[0] <= 0:
            raise CycloError("outer provider has no usable loopback endpoint")
        return matches[0]

    def team_port(
        self,
        instance: Instance,
        status: DCompStatus | None = None,
    ) -> int | None:
        if instance.offline:
            return None
        observed = status or self.status()
        component = observed.component(self.component_for_instance(instance.id))
        if component is None:
            raise CycloError(f"team component is absent: {instance.id}")
        matches = [
            port.host_port
            for port in component.published_ports
            if port.protocol == "tcp"
            and port.container_port == TEAM_DASHBOARD_PORT
            and port.host_ip == instance.agentws_host
        ]
        if len(matches) != 1 or matches[0] <= 0:
            raise CycloError(
                f"team component has no usable AgentWS endpoint: {instance.id}"
            )
        return matches[0]

    def require_instances_ready(
        self,
        instances: Iterable[Instance],
        status: DCompStatus,
    ) -> None:
        """Require readiness only for requested teams and their provider path."""

        selected = tuple(instances)
        if not selected:
            return
        if status.operation:
            phase = f" ({status.phase})" if status.phase else ""
            raise CycloError(
                f"DComp operation {status.operation!r} is still pending{phase}"
            )
        self._require_component_ready(
            status,
            self.host.outer_component,
            "provider dependency",
        )
        for instance in selected:
            self._require_component_ready(
                status,
                self.component_for_instance(instance.id),
                f"team {instance.id!r}",
            )

    def _gateway_component(
        self,
        image: Image,
        *,
        publish_provider: bool,
    ) -> Component:
        ports = (
            (PublishedPort("127.0.0.1", 0, DCOMP_COMPONENT_PORT),)
            if publish_provider
            else ()
        )
        return Component(
            "gateway",
            image.id,
            outputs=(provider_endpoint(),),
            volumes=(Volume("credentials", GATEWAY_STORE),),
            ports=ports,
            egress=True,
        )

    def _gateway_image(self) -> Image:
        root = components_root()
        reference = f"cyclo-{self.store.system}-gateway:{__version__}"
        return self.images.build(
            reference,
            dockerfile=root / "gateway" / "Dockerfile",
            context=root,
        )

    def _provider_image(self, provider: Provider) -> Image:
        dockerfile = provider.source / "Dockerfile"
        if not dockerfile.exists():
            image = self.images.inspect(provider.contract.image)
            if image is None:
                raise CycloError(
                    f"provider image is not built: {provider.contract.image}"
                )
            if not image.has_healthcheck:
                raise CycloError(
                    f"provider image has no OCI HEALTHCHECK: "
                    f"{provider.contract.image}"
                )
            return image
        reference = (
            f"cyclo-{self.store.system}-provider-{provider.name}:{__version__}"
        )
        return self.images.build(
            reference,
            dockerfile=dockerfile,
            context=provider.context,
        )

    @staticmethod
    def _require_component_ready(
        status: DCompStatus,
        name: str,
        label: str,
    ) -> None:
        component = status.component(name)
        if component is None:
            raise CycloError(f"{label} component {name!r} is absent")
        if component.status == "running" and component.health == "healthy":
            return
        state = f"status={component.status}, health={component.health}"
        if component.exit_code:
            state += f", exit={component.exit_code}"
        if component.problem:
            state += f": {component.problem}"
        raise CycloError(f"{label} component {name!r} is not ready ({state})")

    def _bind_docker(self) -> None:
        endpoint = self.store.docker_endpoint
        self.images.endpoint = endpoint
        self.dcomp.bind_docker(endpoint)

    def _trusted_host_roots(self) -> tuple[tuple[Path, str], ...]:
        return trusted_host_roots(
            self.host,
            dcomp_executable=self.dcomp.executable,
        )
