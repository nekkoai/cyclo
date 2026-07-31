from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from . import __version__
from .authority import trusted_host_roots, validate_project_authority
from .dcomp import DCompClient, DCompStatus
from .dcomp_system import (
    DCOMP_COMPONENT_PORT,
    Bind,
    Component,
    Endpoint,
    Link,
    MaterializedSystem,
    Materializer,
    PROVIDER_SERVICE,
    PublishedPort,
    System,
    Volume,
    component_name,
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
from .resources import components_root, package_root
from .state import Instance, StateStore
from .team import Team


TEAM_DASHBOARD_PORT = 4137
GATEWAY_STORE = "/var/lib/cyclo-gateway"
AGENTWS_ROOT = "/agentws"
TEAM_ROOT = "/team"
PI_ROOT = "/home/cyclo/.pi"
PI_SETTINGS_TEMPLATE = "/opt/cyclo/pi-settings.json"


@dataclass(frozen=True)
class HostImages:
    gateway: Image
    providers: Mapping[str, Image]


@dataclass(frozen=True)
class TeamImage:
    image: Image
    base: Image


@dataclass(frozen=True)
class AppliedSystem:
    definition: MaterializedSystem
    status: DCompStatus


@dataclass(frozen=True)
class InstanceFiles:
    project_config: Path
    pi_settings: Path


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
        self._gateway_image_cache: Image | None = None
        self._provider_image_cache: dict[str, Image] = {}
        self._team_base_image_cache: Image | None = None

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
    ) -> TeamImage:
        require_non_root_team_host()
        self._bind_docker()
        base = self._team_base_image()
        if override:
            image = self.images.inspect(override)
            if image is None:
                raise CycloError(f"team runtime image is not built: {override}")
            self._validate_team_image(image)
            return TeamImage(image, base)
        if team.dockerfile is None:
            return TeamImage(base, base)
        identity = hashlib.sha256(str(team.root).encode("utf-8")).hexdigest()[:12]
        reference = (
            f"cyclo-{self.store.system}-team-"
            f"{component_name('team', team.name)[5:33]}-{identity}:{__version__}"
        )
        image = self.images.build(
            reference,
            dockerfile=team.dockerfile,
            context=team.root,
            build_args=(("CYCLO_TEAM_BASE", base.reference),),
            labels=(("io.cyclo.team-base", base.id),),
        )
        self._validate_team_image(image)
        label = _labels(image.config).get("io.cyclo.team-base")
        if label != base.id:
            raise CycloError(
                f"derived team image {reference!r} was not built from the "
                "current Cyclo team base"
            )
        return TeamImage(image, base)

    def materialize_instance(self, instance: Instance) -> InstanceFiles:
        """Create only the mutable and instance-specific files a team consumes."""

        self.store.ensure()
        instance_root = self.store.instance_dir(instance.id)
        try:
            instance_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(instance_root, 0o700)
            for path in (
                self.store.tasks_dir(instance.id),
                self.store.jobs_dir(instance.id),
                self.store.agents_dir(instance.id),
                self.store.pi_root(instance.id),
            ):
                if path.is_symlink():
                    raise CycloError(
                        f"refusing symlinked AgentWS state directory: {path}"
                    )
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(path, 0o700)
        except OSError as exc:
            raise CycloError(
                f"cannot prepare team state for {instance.id}: {exc}"
            ) from exc

        config = (
            instance_root
            / "project-config"
            / instance.project_generation
            / "project.cyclo"
        )
        self._immutable_file(
            config,
            instance.project_config.rstrip().encode("utf-8") + b"\n",
            mode=0o444,
        )
        return InstanceFiles(
            project_config=config,
            pi_settings=self._materialize_pi_settings(instance),
        )

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
            component = self._team_component(instance)
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
        return component_name("team", identifier)

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

    def _team_component(self, instance: Instance) -> Component:
        if not instance.image.startswith("sha256:"):
            raise CycloError(
                f"instance {instance.id!r} is not bound to an immutable team image"
            )
        project = decode_instance_project(instance).require_valid()
        files = self.materialize_instance(instance)
        team_root = Path(instance.team_path).resolve(strict=True)
        roster = Path(instance.team_roster)
        if roster.name != instance.team_roster:
            raise CycloError(f"invalid team roster for instance {instance.id!r}")
        binds = [
            Bind(self.store.tasks_dir(instance.id), f"{AGENTWS_ROOT}/tasks", False),
            Bind(self.store.jobs_dir(instance.id), f"{AGENTWS_ROOT}/jobs", False),
            Bind(self.store.agents_dir(instance.id), f"{AGENTWS_ROOT}/agents", False),
            Bind(files.project_config, f"{AGENTWS_ROOT}/project.cyclo", True),
            Bind(files.pi_settings, PI_SETTINGS_TEMPLATE, True),
            Bind(self.store.pi_root(instance.id), PI_ROOT, False),
            Bind(team_root, TEAM_ROOT, not instance.team_write),
        ]
        binds.extend(
            Bind(mount.path, str(mount.container_path), mount.read_only)
            for mount in project.mounts
        )
        arguments = [
            "python3",
            "/usr/local/bin/cyclo-team-runtime",
            "--roster",
            f"{TEAM_ROOT}/{instance.team_roster}",
            "--generation",
            instance.generation,
            "--project-generation",
            instance.project_generation,
        ]
        if instance.team_protocol:
            arguments.extend(("--team-protocol", f"{TEAM_ROOT}/AGENTS.md"))
        if instance.verbose:
            arguments.append("--verbose")
        ports = (
            ()
            if instance.offline
            else (
                PublishedPort(
                    instance.agentws_host,
                    instance.requested_port,
                    TEAM_DASHBOARD_PORT,
                ),
            )
        )
        return Component(
            self.component_for_instance(instance.id),
            instance.image,
            inputs=(provider_endpoint(),),
            binds=tuple(binds),
            arguments=tuple(arguments),
            ports=ports,
            egress=not instance.offline,
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

    def _team_base_image(self) -> Image:
        if self._team_base_image_cache is not None:
            return self._team_base_image_cache
        root = package_root()
        reference = f"cyclo-{self.store.system}-team:{__version__}"
        image = self.images.build(
            reference,
            dockerfile=root / "components" / "team-runtime" / "Dockerfile",
            context=root,
            build_args=(
                ("CYCLO_HOST_UID", str(os.getuid())),
                ("CYCLO_HOST_GID", str(os.getgid())),
            ),
        )
        self._validate_team_image(image)
        self._team_base_image_cache = image
        return image

    @staticmethod
    def _validate_team_image(image: Image) -> None:
        entrypoint = image.config.get("Entrypoint")
        if entrypoint != ["/usr/local/bin/cyclo-container-entrypoint"]:
            raise CycloError(
                "team image must inherit Cyclo's runtime ENTRYPOINT unchanged"
            )
        user = image.config.get("User", "")
        if user not in {"", "0", "root"}:
            raise CycloError(
                "team image must let Cyclo's entrypoint drop host identity"
            )
        if not image.has_healthcheck:
            raise CycloError("team image must inherit Cyclo's OCI HEALTHCHECK")

    def _materialize_pi_settings(self, instance: Instance) -> Path:
        content = (
            json.dumps(
                {
                    "defaultProvider": instance.pi_default_provider,
                    "defaultModel": instance.pi_default_model,
                    "defaultThinkingLevel": "xhigh",
                    "packages": [
                        "npm:pi-web-access",
                        "npm:pi-lens",
                        "npm:pi-simplify",
                        "/opt/cyclo/pi-adapter",
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        path = (
            self.store.instance_dir(instance.id)
            / "runtime-config"
            / digest
            / "pi-settings.json"
        )
        self._immutable_file(path, content, mode=0o444)
        return path

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

    @staticmethod
    def _immutable_file(path: Path, content: bytes, *, mode: int) -> None:
        try:
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise CycloError(f"invalid immutable runtime file: {path}")
                if path.read_bytes() != content:
                    raise CycloError(
                        f"immutable runtime file has unexpected content: {path}"
                    )
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
            )
            try:
                with temporary.open("xb") as stream:
                    os.chmod(temporary, mode)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(f"cannot materialize runtime file {path}: {exc}") from exc

def _labels(config: Mapping[str, object]) -> Mapping[str, str]:
    value = config.get("Labels")
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def require_non_root_team_host() -> None:
    if os.getuid() == 0:
        raise CycloError(
            "refusing to run a Cyclo team as host root because that would "
            "make the agent root inside its container"
        )
