from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

from . import __version__
from .agentws_bundle import packaged_agentws_root
from .agentws_queue import read_agent_supervisor_status
from .component import (
    COMPONENT_INTERFACE,
    PROVIDER_INTERFACE,
    ComponentStatus,
    component_sources_root,
    parse_declaration,
)
from .component_runtime import ComponentController
from .dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DashboardSnapshot,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
)
from .docker import Docker, container_command, validate_mount_collection
from .errors import CycloError
from .gateway import Gateway
from .health import (
    INACTIVE_TEAM_HEALTH,
    ProviderHealth,
    instance_provider_health,
    read_provider_status,
    team_health,
)
from .instance_lifecycle import (
    active_instances,
    instance_lifecycle_label,
    stop_instance as stop_managed_instance,
    stop_instance_locked as stop_managed_instance_locked,
    stop_remove_instance_container,
)
from .installation import team_image_name
from .project import (
    ProjectDefinition,
    ProjectTeam,
    load_project,
    project_context_marker,
    read_project_context,
)
from .project_run import (
    RunBinding,
    container_spec,
    load_project_teams,
    preflight_binding,
    project_instance_id,
    project_run_bindings,
    start_binding_locked,
    validate_run_options,
    validate_pi_team_models,
)
from .project_state import decode_instance_project
from .providers import ProviderConnection, ProviderSystem, catalogue_ids
from .state import Instance, StateStore, slug, validate_instance_id
from .team import Team, init_team, load_team, require_team_repository, team_generation, verify_agentws_abi
from .team_templates import bundled_team_template_names
from .team_runtime_image import (
    ensure as ensure_team_runtime_image,
    ensure_derived as ensure_derived_team_image,
    require as require_team_runtime_image,
)


DEFAULT_HOST_CONFIG = Path("/etc/cyclo/host.conf")


def state_store(args: argparse.Namespace) -> StateStore:
    selected = args.state_root
    root = Path(selected).expanduser().resolve() if selected else None
    return StateStore(
        root,
        requested_host_config_scope="state" if selected else "system",
    )


def host_config(store: StateStore) -> Path:
    """Select provider configuration from the installation identity."""

    scope = store.host_config_scope
    if scope == "system":
        return DEFAULT_HOST_CONFIG
    if scope == "state":
        return store.root / "host.conf"
    raise CycloError("Cyclo installation has no host configuration scope")


def gateway(args: argparse.Namespace, store: StateStore) -> Gateway:
    return Gateway(
        store.components_root,
        controller=ComponentController(),
    )


def provider_system(
    args: argparse.Namespace,
    store: StateStore,
    *,
    load_config: bool = True,
) -> ProviderSystem:
    controller = ComponentController()
    proxy = Gateway(store.components_root, controller=controller)
    return ProviderSystem(
        store.components_root,
        host_config(store) if load_config else DEFAULT_HOST_CONFIG,
        gateway=proxy,
        controller=controller,
        load_config=load_config,
    )


def agentws_root() -> Path:
    root = packaged_agentws_root()
    verify_agentws_abi(root)
    return root


def cmd_team_init(args: argparse.Namespace) -> int:
    destination = init_team(
        Path(args.team),
        args.model,
        initialize_git=not args.no_git,
        template_name=args.template,
    )
    team = load_team(destination)
    print(f"initialized Cyclo team: {destination}")
    print(f"agents: {len(team.agents)}")
    print(f"next: edit {destination / 'team'} and {destination / 'roles'}")
    return 0


def _trusted_mount_roots(source: Path, host_config: Path) -> tuple[tuple[Path, str], ...]:
    return (
        (source, "bundled AgentWS runtime"),
        (component_sources_root(), "Cyclo component sources"),
        (host_config, "host provider configuration"),
        (Path(__file__).resolve().parents[2], "installed Cyclo controller"),
    )


def _validate_project_mounts(
    definition: ProjectDefinition,
    teams: tuple[tuple[ProjectTeam, Team], ...],
    store: StateStore,
    source: Path,
    host_config: Path,
) -> None:
    validate_mount_collection(
        ((team.root, f"team {selected.name!r}") for selected, team in teams),
        (
            (project_mount.path, f"mount {project_mount.name!r}")
            for project_mount in definition.mounts
        ),
        store.root,
        Path.home() / ".pi" / "agent",
        _trusted_mount_roots(source, host_config),
    )


def _looks_like_project_file(value: str | os.PathLike[str]) -> bool:
    path = Path(value).expanduser()
    if path.is_dir():
        return False
    return path.is_file() or path.name == "project.cyclo" or path.suffix == ".cyclo"


def cmd_validate(args: argparse.Namespace) -> int:
    if _looks_like_project_file(args.definition):
        definition = load_project(args.definition)
        teams = load_project_teams(definition)
        source = agentws_root()
        store = state_store(args)
        _validate_project_mounts(
            definition, teams, store, source, host_config(store)
        )
        print(f"project: {definition.name}")
        print(f"description: {definition.description}")
        print(f"definition: {definition.path}")
        print(f"generation: {definition.definition_sha256}")
        for selected, team in teams:
            print(
                f"team ({selected.mode}): {team.name} {team.root} "
                f"[{len(team.agents)} agents]"
            )
        for project_mount in definition.mounts:
            print(
                f"mount ({project_mount.mode}): {project_mount.name} "
                f"{project_mount.path} -> {project_mount.container_path}"
            )
        return 0

    team = load_team(args.definition)
    require_team_repository(team)
    agentws_root()
    print(f"team: {team.name}")
    print(f"repository: {team.root}")
    print(f"roster: {team.roster.name}")
    print(f"agents: {len(team.agents)}")
    print(f"providers: {', '.join(team.providers)}")
    print(f"generation: {team_generation(team)}")
    return 0


def cmd_team_templates(_args: argparse.Namespace) -> int:
    for name in bundled_team_template_names():
        print(name)
    return 0


def _project_init_path(value: str) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = Path(os.path.abspath(selected))
    if selected.name != "project.cyclo" and selected.suffix != ".cyclo":
        raise CycloError("project definition must be named project.cyclo or end in .cyclo")
    return selected


def cmd_project_init(args: argparse.Namespace) -> int:
    destination = _project_init_path(args.definition)
    name = args.name or slug(
        destination.parent.name if destination.name == "project.cyclo" else destination.stem,
        64,
    )
    description = args.description or f"Cyclo project {name}."
    lines = [
        f"name {name}",
        f"description {description}",
    ]
    if args.context_file:
        context = read_project_context(args.context_file)
        marker = project_context_marker(context)
        lines.extend(["", f"context <<{marker}", context, marker])
    lines.append("")
    for path, mode in args.team:
        if mode not in {"ro", "rw"}:
            raise CycloError(f"invalid team access mode {mode!r}; expected ro or rw")
        lines.append(f"team {path} {mode}")
    lines.append("")
    for mount_name, path, mode in args.mount:
        if mode not in {"ro", "rw"}:
            raise CycloError(f"invalid mount access mode {mode!r}; expected ro or rw")
        lines.append(f"mount {mount_name} {path} {mode}")
    content = "\n".join(lines) + "\n"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.init-{os.getpid()}-{secrets.token_hex(8)}"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o644,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            initialized = load_project(temporary)
            load_project_teams(initialized)
        except CycloError as exc:
            raise CycloError(str(exc).replace(str(temporary), str(destination))) from exc
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise CycloError(f"refusing to overwrite project definition: {destination}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    print(f"initialized Cyclo project: {destination}")
    print(f"next: cyclo validate {destination}")
    print(f"then: cyclo run {destination}")
    return 0


def _announce_binding(binding: RunBinding, store: StateStore) -> None:
    instance = binding.instance
    print(f"started Cyclo instance: {instance.id}")
    team_mode = "writable" if instance.team_write else "read-only"
    print(f"team definition ({team_mode}): {binding.team.root}")
    print(f"project: {instance.project_name}")
    print(f"project definition: {instance.project_file}")
    for project_mount in binding.project_mounts:
        print(
            f"mount ({project_mount.mode}): {project_mount.name} "
            f"{project_mount.path} -> {project_mount.container_path}"
        )
    if instance.port is not None:
        if instance.agentws_host in {"0.0.0.0", "::"}:
            print(f"AgentWS listening on {instance.agentws_host}:{instance.port}")
        else:
            print(f"AgentWS: http://{instance.agentws_host}:{instance.port}")
        if not dashboard_host_is_loopback(instance.agentws_host):
            print(
                "WARNING: AgentWS has no authentication and is exposed on a "
                "non-loopback address."
            )
    else:
        print("AgentWS UI: not published in --offline mode")
    print(f"state: {store.queue_root(instance.id)}")


def _prepare_team_images(
    bindings: tuple[RunBinding, ...],
    *,
    base_image: str,
    base_image_id: str | None = None,
) -> str | None:
    """Make every selected team image ready before any team container starts."""

    overrides = {binding.instance.image_override for binding in bindings}
    if len(overrides) != 1:
        raise CycloError("project teams have inconsistent image overrides")
    override = overrides.pop()
    if override:
        selected = require_team_runtime_image(override)
        for binding in bindings:
            binding.instance.image = selected
        return base_image_id

    selected_base = base_image_id or ensure_team_runtime_image(base_image)
    prepared: dict[str, str] = {}
    for binding in bindings:
        dockerfile = binding.team.dockerfile
        if dockerfile is None:
            binding.instance.image = selected_base
            continue
        tag = binding.instance.image
        if tag in prepared:
            binding.instance.image = prepared[tag]
            continue
        binding.instance.image = ensure_derived_team_image(
            tag,
            binding.team.root,
            selected_base,
        )
        prepared[tag] = binding.instance.image
    return selected_base


def cmd_run(args: argparse.Namespace) -> int:
    source = agentws_root()
    store = state_store(args)
    base_image = team_image_name(store.system, __version__)
    docker = Docker()
    definition = load_project(args.project)
    configured_teams = load_project_teams(definition)
    validate_run_options(args, team_count=len(configured_teams))
    _validate_project_mounts(
        definition,
        configured_teams,
        store,
        source,
        host_config(store),
    )
    bindings = project_run_bindings(
        args,
        definition,
        configured_teams,
        system=store.system,
        base_image=base_image,
        version=__version__,
    )

    providers = provider_system(args, store)
    if args.dry_run:
        for binding in bindings:
            binding.instance.provider_socket_path = str(
                providers.configured_socket_path
            )
            binding.instance.provider_generation = (
                providers.configuration.generation
            )
            if len(bindings) > 1:
                print(f"# instance {binding.instance.id}")
            print(shlex.join(container_command(container_spec(binding, store, args))))
        return 0

    with store.locked():
        connection = providers.start()
        connection, catalogue = providers.catalogue(connection)
        _warn_unavailable_providers(connection, providers.gateway.socket_path)
        for binding in bindings:
            validate_pi_team_models(binding.team, catalogue)
            binding.instance.provider_socket_path = str(connection.socket_path)
            binding.instance.provider_generation = connection.generation
        for binding in bindings:
            preflight_binding(binding, store, docker)
        _prepare_team_images(
            bindings,
            base_image=base_image,
        )
        started: list[RunBinding] = []
        current: RunBinding | None = None
        try:
            for binding in bindings:
                current = binding
                start_binding_locked(
                    args,
                    binding,
                    source,
                    store,
                    docker,
                )
                started.append(binding)
                current = None
        except BaseException:
            rollback_errors: list[str] = []
            rollback = list(started)
            if current is not None and all(
                item.instance.id != current.instance.id for item in rollback
            ):
                try:
                    persisted = store.load(current.instance.id)
                except CycloError as exc:
                    if store.metadata_path(current.instance.id).is_file():
                        rollback_errors.append(
                            f"{current.instance.id}: "
                            f"cannot verify in-flight launch: {exc}"
                        )
                else:
                    if (
                        persisted.active
                        and persisted.launch_id == current.instance.launch_id
                    ):
                        rollback.append(current)
            for binding in reversed(rollback):
                try:
                    stop_managed_instance_locked(
                        store,
                        docker,
                        binding.instance,
                    )
                except Exception as exc:
                    rollback_errors.append(f"{binding.instance.id}: {exc}")
            if rollback_errors:
                print(
                    "warning: project startup rollback was incomplete: "
                    + "; ".join(rollback_errors),
                    file=sys.stderr,
                )
            raise

    for binding in bindings:
        _announce_binding(binding, store)

    if args.foreground:
        binding = bindings[0]
        try:
            return docker.logs(
                binding.instance, system=store.system, follow=True
            )
        except KeyboardInterrupt:
            stop_instance(args, store, binding.instance)
    return 0


def _refresh_projects(
    args: argparse.Namespace,
    instances: list[Instance],
) -> list[argparse.Namespace]:
    grouped: dict[Path, list[Instance]] = {}
    legacy: list[str] = []
    for instance in instances:
        project = decode_instance_project(instance).require_valid()
        if not project.configured or project.definition is None:
            legacy.append(instance.id)
            continue
        definition = Path(os.path.abspath(project.definition.expanduser()))
        grouped.setdefault(definition, []).append(instance)
    if legacy:
        raise CycloError(
            "cannot refresh legacy instances without project.cyclo: "
            + ", ".join(sorted(legacy))
            + "; stop them and recreate them from project.cyclo"
        )

    result: list[argparse.Namespace] = []
    for path, selected in sorted(grouped.items(), key=lambda item: str(item[0])):
        definition = load_project(path)
        expected_ids = {
            project_instance_id(definition, team) for team in definition.teams
        }
        active_ids = {instance.id for instance in selected}
        if active_ids != expected_ids:
            missing = sorted(expected_ids - active_ids)
            unexpected = sorted(active_ids - expected_ids)
            details = []
            if missing:
                details.append("inactive: " + ", ".join(missing))
            if unexpected:
                details.append("no longer configured: " + ", ".join(unexpected))
            raise CycloError(
                f"cannot refresh partially active project {path}: "
                + "; ".join(details)
            )
        launch_settings = {
            (
                instance.image_override or None,
                instance.offline,
                instance.agentws_host,
            )
            for instance in selected
        }
        if len(launch_settings) != 1:
            raise CycloError(
                f"active instances for {path} have inconsistent launch settings"
            )
        image, offline, host = launch_settings.pop()
        port = (
            selected[0].port or 0
            if len(definition.teams) == 1 and len(selected) == 1
            else 0
        )
        result.append(
            argparse.Namespace(
                state_root=args.state_root,
                project=str(path),
                image=image,
                offline=offline,
                host=host,
                port=port,
                verbose=False,
                foreground=False,
                dry_run=False,
            )
        )
    return result


def cmd_refresh(args: argparse.Namespace) -> int:
    """Rebuild installed images and restart the active Cyclo system."""

    store = state_store(args)
    docker = Docker()
    with store.locked():
        instances = active_instances(store, docker)
    projects = _refresh_projects(args, instances)

    providers = provider_system(args, store)
    stop_failures: list[str] = []
    for instance in instances:
        try:
            stop_instance(args, store, instance)
        except Exception as exc:
            stop_failures.append(f"{instance.id}: {exc}")
        else:
            print(f"stopped Cyclo instance: {instance.id}")
    if stop_failures:
        raise CycloError(
            "refresh could not stop the active fleet: " + "; ".join(stop_failures)
        )

    print("rebuilding and restarting provider system")
    with store.locked():
        providers.refresh()

    start_failures: list[str] = []
    for project_args in projects:
        try:
            cmd_run(project_args)
        except Exception as exc:
            start_failures.append(f"{project_args.project}: {exc}")
    if start_failures:
        raise CycloError(
            "refresh could not restart every project: " + "; ".join(start_failures)
        )
    print("Cyclo refresh complete")
    return 0


def stop_instance(
    _args: argparse.Namespace,
    store: StateStore,
    instance: Instance,
) -> None:
    stop_managed_instance(
        store,
        Docker(),
        instance,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    store = state_store(args)
    target = args.target
    try:
        candidate_id = validate_instance_id(target)
    except CycloError:
        candidate_id = None
    if candidate_id is not None and store.metadata_path(candidate_id).is_file():
        candidate = store.load(candidate_id)
        stop_instance(args, store, candidate)
        print(f"stopped Cyclo instance: {candidate_id}")
        return 0

    selected = Path(os.path.abspath(Path(target).expanduser()))
    canonical = selected.resolve()
    targets = [
        instance
        for instance in store.list()
        if instance.project_file
        and Path(instance.project_file).expanduser().resolve() == canonical
    ]
    if not targets:
        raise CycloError(f"no Cyclo instances found for project definition {selected}")

    failures: list[str] = []
    for instance in targets:
        try:
            stop_instance(args, store, instance)
        except Exception as exc:
            failures.append(f"{instance.id}: {exc}")
        else:
            print(f"stopped Cyclo instance: {instance.id}")
    if failures:
        raise CycloError(f"project stop incomplete: {'; '.join(failures)}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Retire one stopped instance and delete its durable AgentWS state."""

    store = state_store(args)
    if args.confirm != args.instance:
        raise CycloError(
            "refusing to forget instance; --confirm must exactly match "
            f"{args.instance!r}"
        )
    docker = Docker()
    with store.locked():
        instance = store.load(args.instance)
        if instance.active:
            raise CycloError(
                f"Cyclo instance is still active: {instance.id}; stop it first"
            )
        state = docker.container_lifecycle_state(
            instance,
            system=store.system,
        )
        if state.lifecycle_active:
            raise CycloError(
                f"Cyclo instance container is still {state.value}: "
                f"{instance.id}; stop it first"
            )
        stop_remove_instance_container(
            docker,
            instance,
            system=store.system,
        )
        docker.remove_network(
            instance.network_name,
            instance.id,
            system=store.system,
        )
        store.remove_instance(
            instance.id,
            expected_launch_id=instance.launch_id,
        )
    print(f"forgot Cyclo instance: {instance.id}")
    return 0


def _shared_provider_health(
    args: argparse.Namespace,
    store: StateStore,
) -> tuple[ProviderHealth, ProviderConnection | None]:
    return read_provider_status(provider_system(args, store))


def _instance_lifecycle_state(
    instance: Instance, docker: Docker, *, system: str
) -> str:
    return instance_lifecycle_label(
        instance,
        docker.container_lifecycle_state(instance, system=system),
    )


def _running_instance_health(
    store: StateStore,
    instance: Instance,
    provider: ProviderHealth,
) -> str:
    try:
        supervisor = read_agent_supervisor_status(store.queue_root(instance.id))
        return team_health(
            provider,
            supervisor.suspended_agents,
            supervisor.error,
            supervisor.planner_attention_jobs,
        ).label()
    except Exception as exc:
        return team_health(provider, supervisor_error=str(exc)).label()


def cmd_ps(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    rows = []
    shared: tuple[ProviderHealth, ProviderConnection | None] | None = None
    for instance in store.list():
        try:
            state = _instance_lifecycle_state(
                instance,
                docker,
                system=store.system,
            )
        except CycloError as exc:
            rows.append(
                (
                    instance.id,
                    "unknown",
                    f"unknown ({exc})",
                    instance.team_name,
                    instance.project_name or Path(instance.project_path).name,
                    str(instance.port or ""),
                )
            )
            continue
        if state == "running":
            if shared is None:
                shared = _shared_provider_health(args, store)
            provider = instance_provider_health(shared[0], shared[1], instance)
            health = _running_instance_health(store, instance, provider)
        else:
            health = INACTIVE_TEAM_HEALTH.label()
        rows.append(
            (
                instance.id,
                state,
                health,
                instance.team_name,
                instance.project_name or Path(instance.project_path).name,
                str(instance.port or ""),
            )
        )
    if not rows:
        print("no Cyclo instances")
        return 0
    header = ("INSTANCE", "STATE", "HEALTH", "TEAM", "PROJECT", "PORT")
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(len(header))]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    store = state_store(args)
    instance = store.load(args.instance)
    docker = Docker()
    state = _instance_lifecycle_state(instance, docker, system=store.system)
    health = INACTIVE_TEAM_HEALTH.label()
    if state == "running":
        shared = _shared_provider_health(args, store)
        provider = instance_provider_health(shared[0], shared[1], instance)
        health = _running_instance_health(store, instance, provider)

    print(f"instance: {instance.id}")
    print(f"state: {state}")
    print(f"health: {health}")
    print(f"team: {instance.team_name}")
    print(f"team repository: {instance.team_path}")
    print(f"team definition: {'writable' if instance.team_write else 'read-only'}")
    print(f"project: {instance.project_name or Path(instance.project_path).name}")
    print(f"project definition: {instance.project_file or 'legacy/unavailable'}")
    print(f"image: {instance.image}")
    print(f"models: {', '.join(instance.models) or 'none'}")
    print(f"providers: {', '.join(instance.providers) or 'none'}")
    print(f"network: {'offline' if instance.offline else 'online'}")
    if instance.port is None:
        print("AgentWS: not published")
    elif instance.agentws_host in {"0.0.0.0", "::"}:
        print(f"AgentWS listening on {instance.agentws_host}:{instance.port}")
    else:
        print(f"AgentWS: http://{instance.agentws_host}:{instance.port}")
    print(f"queue: {store.queue_root(instance.id)}")
    if instance.project_mounts:
        print("mounts:")
        for mount in instance.project_mounts:
            name = str(mount.get("name", ""))
            path = str(mount.get("path", ""))
            mode = str(mount.get("mode", ""))
            destination = f"/workspace/{name}" if mode == "rw" else f"/readonly/{name}"
            print(f"  {name} ({mode}): {path} -> {destination}")
    else:
        print("mounts: unavailable")
    return 0


class _GatewayUsageReader:
    def __init__(self, proxy: Gateway) -> None:
        self.proxy = proxy

    def usage(self) -> dict[str, object]:
        return self.proxy.usage()


def cmd_dashboard(args: argparse.Namespace) -> int:
    store = state_store(args)
    snapshot = DashboardSnapshot(
        store,
        docker=Docker(),
        usage_reader=_GatewayUsageReader(gateway(args, store)),
        provider_reader=provider_system(args, store),
    )
    server = make_dashboard_server(
        snapshot.build,
        host=args.host,
        port=args.port,
        static_assets=packaged_dashboard_assets(),
    )
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    if host in {"0.0.0.0", "::"}:
        print(f"Cyclo dashboard listening on {host}:{port}", flush=True)
    else:
        print(f"Cyclo dashboard: http://{host}:{port}/", flush=True)
    if not dashboard_host_is_loopback(host):
        print(
            "WARNING: dashboard has no authentication and is exposed on a non-loopback address.",
            flush=True,
        )
    print("Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def _task_project_summary(instance: Instance) -> tuple[str, ...]:
    project = decode_instance_project(instance).require_valid()
    writable = [
        f"  {mount.name}: {mount.container_path}"
        for mount in project.mounts
        if mount.writable
    ]
    readonly = [
        f"  {mount.name}: {mount.container_path}"
        for mount in project.mounts
        if mount.read_only
    ]
    return (
        f"project: {project.name}",
        f"project definition: {project.definition}",
        "writable workspace mounts:",
        *(writable or ["  none"]),
        "read-only mounts:",
        *(readonly or ["  none"]),
        "write only below configured /workspace/<name> paths; read-only inputs are below /readonly/<name>",
    )


def _task_target(args: argparse.Namespace) -> tuple[Instance, Docker, str]:
    store = state_store(args)
    instance = store.load(args.instance)
    docker = Docker()
    if not docker.container_running(instance, system=store.system):
        raise CycloError(f"Cyclo instance is not running: {instance.id}")
    return instance, docker, store.system


def _validate_task_id(task_id: str) -> None:
    if (
        not task_id
        or not task_id[0].isalnum()
        or any(
            char
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
            for char in task_id
        )
    ):
        raise CycloError(
            "task ID must start with a letter or number and contain only "
            "letters, numbers, dot, underscore, and hyphen"
        )


def _exec_task_command(args: argparse.Namespace, command: list[str]) -> int:
    instance, docker, system = _task_target(args)
    return docker.exec(instance, command, system=system, check=False)


def cmd_task_list(args: argparse.Namespace) -> int:
    return _exec_task_command(args, ["/agentws/bin/task-list"])


def cmd_task_show(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    return _exec_task_command(args, ["/agentws/bin/task-show", args.task_id])


def cmd_task_run(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    spec = Path(args.spec).expanduser().resolve()
    if not spec.is_file():
        raise CycloError(f"task specification not found: {spec}")
    instance, docker, system = _task_target(args)
    container_spec_path = f"/tmp/cyclo-task-{args.task_id}-{secrets.token_hex(8)}.md"

    def remove_copied_spec() -> None:
        cleanup_status = docker.exec(
            instance,
            ["rm", "-f", container_spec_path],
            system=system,
            check=False,
            user="0:0",
        )
        if cleanup_status != 0:
            raise CycloError(
                f"container rm exited with status {cleanup_status}"
            )

    def clean_up_copied_spec() -> None:
        try:
            remove_copied_spec()
        except Exception as cleanup:
            print(
                f"warning: copied task specification cleanup failed: {cleanup}",
                file=sys.stderr,
            )

    try:
        docker.copy_to(instance, spec, container_spec_path, system=system)
        result = docker.exec(
            instance,
            ["/agentws/bin/task-create", args.task_id, container_spec_path],
            system=system,
            check=False,
        )
        if result != 0:
            raise CycloError(f"AgentWS task creation failed with status {result}")
    except BaseException:
        clean_up_copied_spec()
        raise
    # task-create has committed the task at this point. A best-effort removal
    # failure must not turn that durable success into a reported failure whose
    # retry would collide with the task that now exists.
    clean_up_copied_spec()
    for line in _task_project_summary(instance):
        print(line)
    return 0


def cmd_task_comment(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    return _exec_task_command(
        args,
        ["/agentws/bin/task-comment", args.task_id, " ".join(args.message)],
    )


def cmd_task_state(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    command = ["/agentws/bin/task-state", args.task_id, args.task_state]
    if args.message:
        command.extend(["-m", args.message])
    return _exec_task_command(args, command)


def cmd_logs(args: argparse.Namespace) -> int:
    store = state_store(args)
    instance = store.load(args.instance)
    return Docker().logs(instance, system=store.system, follow=args.follow)


def cmd_path(args: argparse.Namespace) -> int:
    store = state_store(args)
    store.load(args.instance)
    print(store.queue_root(args.instance))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    print(json.dumps(gateway(args, state_store(args)).usage(), indent=2, sort_keys=True))
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    store = state_store(args)
    providers = provider_system(args, store)
    with store.locked():
        connection = providers.start()
        connection, catalogue = providers.catalogue(connection)
        _warn_unavailable_providers(
            connection,
            providers.gateway.socket_path,
        )
        models = catalogue_ids(catalogue)
    if not models:
        raise CycloError(
            "provider system returned no models; run "
            "`cyclo gateway providers` and log in"
        )
    for model in models:
        print(model)
    return 0


def _component_state(status: ComponentStatus) -> str:
    if status.works:
        return "ready"
    if status.container_state == "unknown":
        return "unknown"
    if not status.container_id:
        return "absent"
    if not status.current:
        return "stale"
    if not status.running:
        return status.container_state
    if status.engine_health == "unhealthy":
        return "unhealthy"
    return "not-ready"


def _print_component_statuses(
    statuses: tuple[ComponentStatus, ...],
) -> None:
    def short_image(status: ComponentStatus) -> str:
        return (
            status.image_id[:19]
            if status.image_id is not None
            else "-"
        )

    rows = [
        (
            status.name,
            status.kind,
            _component_state(status),
            short_image(status),
            status.engine_health,
            status.health,
            " ".join(status.error.split()),
        )
        for status in statuses
    ]
    header = (
        "NAME",
        "TYPE",
        "STATE",
        "IMAGE",
        "ENGINE",
        "HEALTH",
        "ERROR",
    )
    widths = [
        max(len(row[index]) for row in [header, *rows])
        for index in range(len(header))
    ]
    print(
        "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(header)
        )
    )
    for row in rows:
        print(
            "  ".join(
                value.ljust(widths[index])
                for index, value in enumerate(row)
            ).rstrip()
        )


def _warn_unavailable_providers(
    connection: ProviderConnection,
    gateway_socket: Path,
) -> None:
    unavailable = [
        component.name
        for component in connection.components
        if component.name != "gateway" and not component.works
    ]
    if unavailable:
        selected = (
            "using the gateway provider"
            if connection.socket_path == gateway_socket
            else "continuing with the last working provider"
        )
        print(
            "warning: ignoring unavailable provider component(s): "
            + ", ".join(unavailable)
            + f"; {selected}",
            file=sys.stderr,
        )


def cmd_providers(args: argparse.Namespace) -> int:
    store = state_store(args)
    if args.providers_action == "stop":
        providers = provider_system(args, store, load_config=False)
        with store.locked():
            stopped = providers.stop()
        print("stopped: " + (", ".join(stopped) if stopped else "nothing running"))
        return 0

    providers = provider_system(args, store)
    if args.providers_action == "check":
        print(f"ok: {providers.check()} provider component(s)")
        return 0
    if args.providers_action == "build":
        with store.locked():
            built = providers.build()
        for name, image_id in built:
            print(f"{name}\t{image_id}")
        return 0
    if args.providers_action == "status":
        statuses = providers.statuses()
        _print_component_statuses(statuses)
        return 0 if all(status.works for status in statuses) else 1
    with store.locked():
        if args.providers_action == "start":
            connection = providers.start()
        elif args.providers_action == "restart":
            connection = providers.restart()
        else:
            raise CycloError(f"unknown providers action: {args.providers_action}")
    _print_component_statuses(connection.components)
    return 0 if all(item.works for item in connection.components) else 1


def _print_gateway_status(
    status: ComponentStatus,
    store_volume: str,
) -> int:
    state = _component_state(status)
    print(f"gateway\t{state}")
    print(f"image\t{status.image_id or '-'}")
    print(f"store\t{store_volume}")
    if status.error:
        print(f"error\t{status.error}")
    return 0 if status.works else 1


def cmd_component(args: argparse.Namespace) -> int:
    store = state_store(args)
    name = getattr(args, "name", None)
    action = args.component_action
    if action == "logs" and args.lines <= 0:
        raise CycloError("--lines must be a positive integer")
    providers = provider_system(
        args,
        store,
        load_config=name != "gateway",
    )
    if action in {"list", "status"}:
        statuses = (
            (providers.status_component(args.name),)
            if action == "status" and args.name
            else providers.statuses()
        )
        _print_component_statuses(statuses)
        if action == "list":
            return 0
        return 0 if all(status.works for status in statuses) else 1

    assert isinstance(name, str)
    if action == "logs":
        output = providers.component_logs(name, args.lines)
        if output:
            print(output)
        return 0

    with store.locked():
        if action == "build":
            print(providers.build_component(name))
            return 0
        if action == "start":
            status = providers.start_component(name)
        elif action == "restart":
            status = providers.start_component(name, restart=True)
        elif action == "stop":
            print(
                f"stopped {name}"
                if providers.stop_component(name)
                else f"{name} was not running"
            )
            return 0
        else:
            raise CycloError(f"unknown component action: {action}")
    _print_component_statuses((status,))
    return 0 if status.works else 1


def cmd_gateway(args: argparse.Namespace) -> int:
    store = state_store(args)
    proxy = gateway(args, store)
    action = args.gateway_action
    if action == "providers":
        with store.locked():
            print(proxy.providers())
        return 0
    if action == "status":
        return _print_gateway_status(proxy.status(), proxy.store_volume)
    if action == "login":
        login_args = [args.provider]
        if args.account:
            login_args.extend(["--as", args.account])
        if args.api_key_env:
            login_args.extend(["--api-key-env", args.api_key_env])
        if args.api_key_stdin:
            login_args.append("--api-key-stdin")
        with store.locked():
            status = proxy.login(login_args)
        return _print_gateway_status(status, proxy.store_volume)
    with store.locked():
        if action == "build":
            status = proxy.refresh()
            print(status.image_id)
            return _print_gateway_status(status, proxy.store_volume)
        if action == "start":
            return _print_gateway_status(proxy.start(), proxy.store_volume)
        if action == "restart":
            return _print_gateway_status(proxy.restart(), proxy.store_volume)
        if action == "stop":
            print("stopped gateway" if proxy.stop() else "gateway was not running")
            return 0
        if action == "destroy-store":
            if args.confirm != proxy.store_volume:
                raise CycloError(
                    f"refusing to destroy gateway store; --confirm must equal {proxy.store_volume}"
                )
            print("destroyed gateway store" if proxy.destroy_store() else "gateway store was absent")
            return 0
    raise CycloError(f"unknown gateway action: {action}")


def cmd_repair(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    repaired = 0
    recovered: list[Instance] = []
    removed = 0
    failures: list[str] = []
    with store.locked():
        stale: list[Instance] = []
        active_instances(store, docker, stale=stale, recovered=recovered)
        repaired += len(stale)
        for instance in store.list():
            if instance.active:
                continue
            try:
                if stop_remove_instance_container(
                    docker,
                    instance,
                    system=store.system,
                ):
                    removed += 1
            except Exception as exc:
                failures.append(f"{instance.id} container cleanup: {exc}")
            try:
                docker.remove_network(
                    instance.network_name,
                    instance.id,
                    system=store.system,
                )
            except Exception as exc:
                failures.append(f"{instance.id} network cleanup: {exc}")
    print(
        f"repaired {repaired} stale record(s); "
        f"recovered {len(recovered)} interrupted start(s); "
        f"removed {removed} inactive container(s)"
    )
    if failures:
        raise CycloError("repair incomplete: " + "; ".join(failures))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    store = state_store(args)
    try:
        root = agentws_root()
        print(f"ok  bundled AgentWS ABI: {root}")
    except CycloError as exc:
        failures += 1
        print(f"no  bundled AgentWS ABI: {exc}")
    try:
        components = component_sources_root()
        gateway_declaration = parse_declaration(
            components / "gateway" / "component.conf"
        )
        if set(gateway_declaration.provides) != {COMPONENT_INTERFACE, PROVIDER_INTERFACE}:
            raise CycloError("gateway declaration does not provide Component and Provider")
        print(f"ok  component ABI: {components}")
    except CycloError as exc:
        failures += 1
        print(f"no  bundled component ABI: {exc}")
    try:
        instances = store.list()
        print(f"ok  persisted instance state: {len(instances)} instance(s)")
    except CycloError as exc:
        failures += 1
        print(f"no  persisted instance state: {exc}")

    controller = ComponentController()
    ok, detail = controller.available()
    if not ok:
        print(f"no  Docker daemon: {detail}")
        return 1
    print(f"ok  Docker daemon: {detail}")

    try:
        providers = provider_system(args, store)
        print(
            "ok  host provider configuration: "
            f"{providers.configuration.path} "
            f"({len(providers.configuration.providers)} component(s))"
        )
    except CycloError as exc:
        failures += 1
        print(f"no  host provider configuration: {exc}")
        return 1

    try:
        statuses = providers.statuses()
    except CycloError as exc:
        failures += 1
        print(f"no  component inspection: {exc}")
        return 1

    catalogue_models: tuple[str, ...] | None = None
    catalogue_error = ""
    gateway_status = statuses[0]
    if gateway_status.works:
        try:
            connection, catalogue = providers.catalogue(
                providers.connection(statuses)
            )
            statuses = connection.components
            catalogue_models = catalogue_ids(catalogue)
        except CycloError as exc:
            catalogue_error = str(exc)

    for status in statuses:
        state = _component_state(status)
        label = (
            "credential gateway"
            if status.name == "gateway"
            else f"provider component {status.name}"
        )
        if status.works:
            print(f"ok  {label}: ready")
        else:
            failures += 1
            detail = f" ({status.error})" if status.error else ""
            print(f"no  {label}: {state}{detail}")
    if gateway_status.works:
        if catalogue_error:
            failures += 1
            print(f"no  provider catalogue: {catalogue_error}")
        else:
            assert catalogue_models is not None
            print(
                "ok  selected provider catalogue: "
                f"{len(catalogue_models)} model(s)"
            )
    return 1 if failures else 0


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        default=os.environ.get("CYCLO_STATE_ROOT"),
        help=(
            "Cyclo installation directory; when the provider graph is first "
            "applied, explicit roots use STATE_ROOT/host.conf and the "
            "implicit root uses /etc/cyclo/host.conf"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclo",
        description="Run Git-defined agent teams through composable model providers",
        epilog=(
            "Everyday:  validate, run, stop, ps, inspect, logs, task, dashboard\n"
            "Authoring: team, project\n"
            "Host:      models, usage, component, gateway, providers, doctor, "
            "refresh, repair"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"cyclo {__version__}")
    add_common_options(parser)
    commands = parser.add_subparsers(required=True)

    team = commands.add_parser("team", help="create team repositories from templates")
    team_commands = team.add_subparsers(dest="team_action", required=True)
    team_init = team_commands.add_parser("init", help="create a team repository")
    team_init.add_argument("team", help="new team-repository directory")
    team_init.add_argument("--model", required=True, help="initial provider/model")
    team_init.add_argument(
        "--template",
        choices=bundled_team_template_names(),
        help="bundled team template",
    )
    team_init.add_argument(
        "--no-git",
        action="store_true",
        help="do not initialize the destination as a Git repository",
    )
    team_init.set_defaults(func=cmd_team_init)

    team_templates = team_commands.add_parser(
        "templates", help="list bundled team templates"
    )
    team_templates.set_defaults(func=cmd_team_templates)

    project = commands.add_parser("project", help="create project definitions")
    project_commands = project.add_subparsers(dest="project_action", required=True)
    project_init = project_commands.add_parser(
        "init",
        help="create a project.cyclo from existing teams and mounts",
        description=(
            "Create a project definition from one or more --team PATH MODE and "
            "--mount NAME PATH MODE declarations. MODE is ro or rw. Optional "
            "--context FILE embeds project layout guidance."
        ),
    )
    project_init.add_argument("definition", help="new project.cyclo path")
    project_init.add_argument("--name", help="project name (default: derived from path)")
    project_init.add_argument(
        "--description",
        help="project description (default: derived from name)",
    )
    project_init.add_argument(
        "--context",
        dest="context_file",
        metavar="FILE",
        help="embed project layout and source guidance from FILE",
    )
    project_init.add_argument(
        "--team",
        action="append",
        nargs=2,
        required=True,
        metavar=("PATH", "MODE"),
        help="team repository and ro/rw access; repeatable",
    )
    project_init.add_argument(
        "--mount",
        action="append",
        nargs=3,
        required=True,
        metavar=("NAME", "PATH", "MODE"),
        help="named project/input mount and ro/rw access; repeatable",
    )
    project_init.set_defaults(func=cmd_project_init)

    validate = commands.add_parser("validate", help="validate a team or project.cyclo")
    validate.add_argument("definition", help="team directory or project.cyclo")
    validate.set_defaults(func=cmd_validate)

    refresh = commands.add_parser(
        "refresh",
        help="rebuild installed images and restart the active Cyclo system",
    )
    refresh.set_defaults(func=cmd_refresh)

    run = commands.add_parser("run", help="start every team in project.cyclo")
    run.add_argument("project", help="project.cyclo")
    run.add_argument(
        "--image",
        default=os.environ.get("CYCLO_TEAM_IMAGE"),
        help="operator-supplied image for every team; bypasses team Dockerfiles",
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help="block direct network access; the mounted model-provider socket remains available",
    )
    run.add_argument("--host", default=DEFAULT_DASHBOARD_HOST, help="AgentWS bind address")
    run.add_argument("--port", type=int, default=0, help="AgentWS port; 0 chooses a free port")
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="print the team Docker command")
    run.set_defaults(func=cmd_run)

    stop = commands.add_parser("stop", help="stop an instance or a whole project")
    stop.add_argument("target", help="instance ID or project.cyclo")
    stop.set_defaults(func=cmd_stop)
    forget = commands.add_parser(
        "forget",
        help="delete a stopped instance and its durable AgentWS state",
        description=(
            "Permanently delete one stopped instance record, including all "
            "tasks, jobs, transcripts, and runtime state."
        ),
    )
    forget.add_argument("instance", help="stopped instance ID from cyclo ps")
    forget.add_argument(
        "--confirm",
        required=True,
        metavar="INSTANCE",
        help="exact instance ID, required because durable work is deleted",
    )
    forget.set_defaults(func=cmd_forget)
    ps = commands.add_parser("ps", help="list team instances")
    ps.set_defaults(func=cmd_ps)
    inspect = commands.add_parser("inspect", help="show one instance in detail")
    inspect.add_argument("instance", help="instance ID from cyclo ps")
    inspect.set_defaults(func=cmd_inspect)

    dashboard = commands.add_parser("dashboard", help="serve the read-only fleet dashboard")
    dashboard.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.set_defaults(func=cmd_dashboard)

    task = commands.add_parser("task", help="inspect and control tasks in an instance")
    task_commands = task.add_subparsers(dest="task_action", required=True)

    task_list = task_commands.add_parser("list", help="list tasks")
    task_list.add_argument("instance", help="instance ID from cyclo ps")
    task_list.set_defaults(func=cmd_task_list)

    task_show = task_commands.add_parser("show", help="show a task, its log, and its result")
    task_show.add_argument("instance", help="instance ID from cyclo ps")
    task_show.add_argument("task_id", help="task ID")
    task_show.set_defaults(func=cmd_task_show)

    task_run = task_commands.add_parser("run", help="create a task and enqueue its planner")
    task_run.add_argument("instance", help="instance ID from cyclo ps")
    task_run.add_argument("task_id", help="new task ID")
    task_run.add_argument("spec", help="task specification file")
    task_run.set_defaults(func=cmd_task_run)

    task_comment = task_commands.add_parser("comment", help="append a task comment")
    task_comment.add_argument("instance", help="instance ID from cyclo ps")
    task_comment.add_argument("task_id", help="task ID")
    task_comment.add_argument("message", nargs="+", help="comment text")
    task_comment.set_defaults(func=cmd_task_comment)

    for action, state in (("complete", "done"), ("reopen", "open")):
        task_state = task_commands.add_parser(action, help=f"{action} a task")
        task_state.add_argument("instance", help="instance ID from cyclo ps")
        task_state.add_argument("task_id", help="task ID")
        task_state.add_argument("-m", "--message", help="state-change note")
        task_state.set_defaults(func=cmd_task_state, task_state=state)
    logs = commands.add_parser("logs", help="show team-container logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow new output")
    logs.add_argument("instance", help="instance ID from cyclo ps")
    logs.set_defaults(func=cmd_logs)
    path = commands.add_parser("path", help="print an instance's AgentWS state path")
    path.add_argument("instance", help="instance ID from cyclo ps")
    path.set_defaults(func=cmd_path)

    usage = commands.add_parser("usage", help="show global gateway usage by provider and model")
    usage.set_defaults(func=cmd_usage)
    models = commands.add_parser("models", help="list models at the outer provider endpoint")
    models.epilog = "Before login, use `cyclo gateway providers` to see available providers."
    models.set_defaults(func=cmd_models)

    repair = commands.add_parser(
        "repair",
        help="reconcile interrupted team starts and stops",
        description=(
            "Recover exact published ports after interrupted starts, mark stale "
            "active records stopped, and remove inactive Cyclo containers and "
            "networks. Durable task and job state is preserved."
        ),
    )
    repair.set_defaults(func=cmd_repair)

    component = commands.add_parser(
        "component",
        help="inspect and control individual host components",
        description=(
            "List, inspect, build, start, stop, restart, or read logs from one "
            "configured component. The inventory is the fixed gateway plus "
            "providers declared in host.conf."
        ),
    )
    component_commands = component.add_subparsers(
        dest="component_action",
        required=True,
    )
    component_list = component_commands.add_parser(
        "list",
        help="list configured components and their current status",
    )
    component_list.set_defaults(func=cmd_component)
    component_status = component_commands.add_parser(
        "status",
        help="show all components or one named component",
    )
    component_status.add_argument(
        "name",
        nargs="?",
        help="configured component name; omit to show all",
    )
    component_status.set_defaults(func=cmd_component)
    for action, help_text in (
        ("build", "build and validate one component image"),
        (
            "start",
            "start one component, installing a current-release image if needed",
        ),
        ("stop", "stop one component"),
        ("restart", "restart one component from its installed image"),
    ):
        selected = component_commands.add_parser(action, help=help_text)
        selected.add_argument("name")
        selected.set_defaults(func=cmd_component)
    component_logs = component_commands.add_parser(
        "logs",
        help="show recent logs from one component",
    )
    component_logs.add_argument("name")
    component_logs.add_argument(
        "--lines",
        type=int,
        default=80,
        help="number of log lines (default: 80)",
    )
    component_logs.set_defaults(func=cmd_component)

    providers = commands.add_parser(
        "providers",
        help="manage Provider components declared in host.conf",
        description=(
            "Validate, build, start, inspect, or stop the Provider components "
            "declared in host.conf. Each component reports its own status; "
            "the gateway remains the independent root provider."
        ),
    )
    provider_commands = providers.add_subparsers(dest="providers_action", required=True)
    provider_help = {
        "check": "validate host.conf and component declarations",
        "build": "ask Docker to build every provider image without restarting",
        "start": "start each provider, installing current-release images if needed",
        "stop": "stop provider components without stopping the gateway",
        "status": "show each provider component independently",
    }
    for action, help_text in provider_help.items():
        selected = provider_commands.add_parser(action, help=help_text)
        selected.set_defaults(func=cmd_providers)
    restart = provider_commands.add_parser(
        "restart", help="restart provider components from their installed images"
    )
    restart.set_defaults(func=cmd_providers)

    gateway_parser = commands.add_parser(
        "gateway",
        help=(
            "manage the isolated gateway store for credentials, subscriptions, "
            "and retained usage history"
        ),
        description=(
            "The gateway owns credentials, subscriptions, and retained usage history. "
            "It exposes the root Component and Provider Unix socket, and does not read "
            "host.conf or project files."
        ),
    )
    gateway_commands = gateway_parser.add_subparsers(dest="gateway_action", required=True)
    provider_catalogue = gateway_commands.add_parser(
        "providers",
        help="list providers available for login",
        description=(
            "Providers are upstream AI services supported by the gateway. This command "
            "does not read or mount the gateway credential store."
        ),
    )
    provider_catalogue.set_defaults(func=cmd_gateway)
    gateway_help = {
        "build": "build through Docker and restart the gateway",
        "start": "start the gateway, installing its current-release image if needed",
        "stop": "stop the gateway without deleting its credential store",
        "status": "show gateway readiness, image identity, and store volume",
    }
    for action, help_text in gateway_help.items():
        selected = gateway_commands.add_parser(action, help=help_text)
        selected.set_defaults(func=cmd_gateway)
    destroy = gateway_commands.add_parser(
        "destroy-store",
        help="irreversibly delete the gateway store",
        description=(
            "This irreversibly deletes gateway credentials, subscriptions, and retained usage "
            "history. Confirmation must exactly match the Docker volume name."
        ),
    )
    destroy.add_argument(
        "--confirm",
        metavar="VOLUME",
        required=True,
        help="exact gateway Docker volume name",
    )
    destroy.set_defaults(func=cmd_gateway)
    gateway_restart = gateway_commands.add_parser(
        "restart", help="restart the gateway from its installed image"
    )
    gateway_restart.set_defaults(func=cmd_gateway)
    login = gateway_commands.add_parser(
        "login",
        help="store credentials for a provider account",
        description=(
            "Authenticate a catalogue provider/account name. The account name becomes "
            "the model prefix (default: PROVIDER). Login publishes the updated "
            "catalogue automatically."
        ),
    )
    login.add_argument("provider", help="gateway provider ID")
    login.add_argument(
        "--as",
        dest="account",
        help="catalogue provider/account name (default: PROVIDER)",
    )
    key = login.add_mutually_exclusive_group()
    key.add_argument("--api-key-env")
    key.add_argument("--api-key-stdin", action="store_true")
    login.set_defaults(func=cmd_gateway)

    doctor = commands.add_parser("doctor", help="check the installed system without changing it")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    """Permit root options before or after a subcommand, up to ``--``."""

    global_options: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            remaining.extend(argv[index:])
            break
        if argument == "--state-root":
            global_options.append(argument)
            index += 1
            if index >= len(argv):
                break
            global_options.append(argv[index])
        elif argument.startswith("--state-root="):
            global_options.append(argument)
        else:
            remaining.append(argument)
        index += 1
    return [*global_options, *remaining]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    selected_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_global_options(selected_argv))
    try:
        return int(args.func(args))
    except CycloError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
