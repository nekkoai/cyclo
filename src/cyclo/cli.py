from __future__ import annotations

import argparse
import json
import os
import shlex
import secrets
import sys
from pathlib import Path

from . import __version__
from .agentws_bundle import packaged_agentws_root
from .agentws_queue import read_agent_supervisor_status
from .dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DashboardSnapshot,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
)
from .docker import (
    Docker,
    container_command,
    validate_mount_collection,
    validate_mount_boundaries,
)
from .errors import CycloError
from .gateway import CredentialGateway
from .health import (
    INACTIVE_TEAM_HEALTH,
    RuntimeHealth,
    read_runtime_health,
    team_health,
)
from .host_config import DEFAULT_HOST_CONFIG, HostConfig
from .host_providers import provider_definition_spec
from .instance_lifecycle import (
    active_instances,
    attach_active_networks,
    rotate_client_tokens,
    stop_remove_instance_container,
    stop_instance as stop_managed_instance,
    token_rotation_failure,
)
from .provider_commands import run_provider_command
from .provider_runtime import ProviderRuntime
from .provider_service import ProviderService
from .runtime_container import provider_runtime_context_root
from .project_run import (
    RunBinding,
    container_spec,
    legacy_run_binding,
    load_project_teams,
    preflight_binding,
    project_run_bindings,
    start_binding,
    validate_run_options,
)
from .project import (
    ProjectDefinition,
    ProjectTeam,
    load_project,
)
from .project_state import decode_instance_project
from .state import Instance, StateStore, validate_instance_id
from .team import (
    Team,
    init_team,
    load_team,
    require_team_repository,
    team_generation,
    verify_agentws_abi,
)
from .team_templates import bundled_team_template_names
from .credential_gateway import cli as gateway_cli
from .credential_gateway import source as gateway_source


DEFAULT_RUNTIME_IMAGE = f"cyclo-runtime:{__version__}"
DEFAULT_GATEWAY_IMAGE = f"cyclo-gateway:{__version__}"
DEFAULT_PROVIDER_RUNTIME_IMAGE = f"cyclo-provider-runtime:{__version__}"
DEFAULT_STORE_VOLUME = "cyclo-gateway-store"


def state_store(args: argparse.Namespace) -> StateStore:
    root = Path(args.state_root).expanduser().resolve() if args.state_root else None
    return StateStore(root)


def host_configuration(args: argparse.Namespace) -> HostConfig:
    return HostConfig(args.host_config)


def agentws_root() -> Path:
    root = packaged_agentws_root()
    verify_agentws_abi(root)
    return root


def gateway(args: argparse.Namespace, store: StateStore) -> CredentialGateway:
    return CredentialGateway(
        store,
        gateway_image=args.gateway_image,
        store_volume=args.store_volume,
    )


def provider_service(args: argparse.Namespace, store: StateStore) -> ProviderService:
    return ProviderService(
        store,
        host_configuration(args),
        image=args.provider_runtime_image,
        gateway_image=args.gateway_image,
        store_volume=args.store_volume,
    )


def cmd_init(args: argparse.Namespace) -> int:
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


def _trusted_mount_roots(
    source: Path, model_runtime: ProviderService
) -> tuple[tuple[Path, str], ...]:
    return (
        (source, "bundled job-loop runtime"),
        (gateway_source.package_root(), "bundled credential-gateway runtime"),
        (provider_runtime_context_root(), "bundled provider runtime"),
        (model_runtime.host_config.path, "host provider configuration"),
        (Path(__file__).resolve().parents[2], "trusted Cyclo controller source"),
    )


def _validate_project_mounts(
    definition: ProjectDefinition,
    teams: tuple[tuple[ProjectTeam, Team], ...],
    store: StateStore,
    proxy: CredentialGateway,
    model_runtime: ProviderService,
    source: Path,
) -> None:
    validate_mount_collection(
        (
            (team.root, f"team {selected.name!r}")
            for selected, team in teams
        ),
        (
            (project_mount.path, f"mount {project_mount.name!r}")
            for project_mount in definition.mounts
        ),
        store.root,
        proxy.host_pi_agent_dir,
        _trusted_mount_roots(source, model_runtime),
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
        proxy = gateway(args, store)
        model_runtime = provider_service(args, store)
        _validate_project_mounts(
            definition, teams, store, proxy, model_runtime, source
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


def cmd_templates(_args: argparse.Namespace) -> int:
    for name in bundled_team_template_names():
        print(name)
    return 0


def _announce_binding(binding: RunBinding, store: StateStore) -> None:
    instance = binding.instance
    print(f"started Cyclo instance: {instance.id}")
    team_mode = "writable" if instance.team_write else "read-only"
    print(f"team definition ({team_mode}): {binding.team.root}")
    if not instance.project_file:
        print(f"project root (writable): {binding.project_root}")
    else:
        print(f"project: {instance.project_name}")
        print(f"project definition: {instance.project_file}")
        for project_mount in binding.project_mounts:
            print(
                f"mount ({project_mount.mode}): {project_mount.name} "
                f"{project_mount.path} -> {project_mount.container_path}"
            )
    if instance.port is not None:
        print(f"AgentWS: http://{instance.agentws_host}:{instance.port}")
        if not dashboard_host_is_loopback(instance.agentws_host):
            print(
                "WARNING: AgentWS has no authentication and is exposed on a "
                "non-loopback address; anyone who can reach this host can view "
                "team activity."
            )
    else:
        print("AgentWS UI: not published in --offline mode")
    print(f"state: {store.queue_root(instance.id)}")


def cmd_run(args: argparse.Namespace) -> int:
    source = agentws_root()
    store = state_store(args)
    proxy = gateway(args, store)
    model_runtime = provider_service(args, store)
    docker = Docker()

    if args.project is None:
        definition = load_project(args.definition)
        configured_teams = load_project_teams(definition)
        validate_run_options(
            args, project_file=True, team_count=len(configured_teams)
        )
        _validate_project_mounts(
            definition,
            configured_teams,
            store,
            proxy,
            model_runtime,
            source,
        )
        bindings = project_run_bindings(args, definition, configured_teams)
    else:
        validate_run_options(args, project_file=False, team_count=1)
        binding = legacy_run_binding(args)
        validate_mount_boundaries(
            binding.team.root,
            binding.project_root,
            store.root,
            proxy.host_pi_agent_dir,
            _trusted_mount_roots(source, model_runtime),
        )
        bindings = (binding,)

    if args.dry_run:
        for binding in bindings:
            if len(bindings) > 1:
                print(f"# instance {binding.instance.id}")
            print(shlex.join(container_command(container_spec(binding, store, args))))
        return 0

    model_runtime.require_running()

    for binding in bindings:
        preflight_binding(binding, store, docker)

    started: list[RunBinding] = []
    current: RunBinding | None = None
    try:
        for index, binding in enumerate(bindings):
            current = binding
            start_binding(
                args,
                binding,
                source,
                store,
                model_runtime,
                docker,
                build=args.build and index == 0,
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
                        f"{current.instance.id}: cannot verify the in-flight "
                        f"launch for rollback: {exc}"
                    )
            else:
                if (
                    persisted.active
                    and persisted.launch_id == current.instance.launch_id
                ):
                    rollback.append(current)
        for binding in reversed(rollback):
            try:
                stop_instance(
                    args,
                    store,
                    binding.instance.id,
                    expected_launch_id=binding.instance.launch_id,
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
            return docker.logs(binding.instance.container_name, follow=True)
        except KeyboardInterrupt:
            stop_instance(args, store, binding.instance.id)
    return 0


def stop_instance(
    args: argparse.Namespace,
    store: StateStore,
    identifier: str,
    *,
    expected_launch_id: str | None = None,
) -> None:
    stop_managed_instance(
        store,
        Docker(),
        provider_service(args, store),
        identifier,
        expected_launch_id=expected_launch_id,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    store = state_store(args)
    target = args.target

    # Preserve the original instance-ID interface even when an instance happens
    # to have a project-like name such as ``foo.cyclo``.
    try:
        candidate_id = validate_instance_id(target)
    except CycloError:
        candidate_id = None
    if candidate_id is not None and store.metadata_path(candidate_id).is_file():
        stop_instance(args, store, candidate_id)
        print(f"stopped Cyclo instance: {candidate_id}")
        return 0

    selected = Path(target).expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = Path(os.path.abspath(selected))
    canonical = selected.resolve()
    targets = [
        instance
        for instance in store.list()
        if instance.project_file
        and Path(instance.project_file).expanduser().resolve() == canonical
    ]

    if not targets:
        if not _looks_like_project_file(selected):
            stop_instance(args, store, target)
            print(f"stopped Cyclo instance: {target}")
            return 0
        raise CycloError(
            f"no Cyclo instances found for project definition {selected}"
        )

    failures: list[str] = []
    for instance in targets:
        try:
            stop_instance(args, store, instance.id)
        except Exception as exc:
            failures.append(f"{instance.id}: {exc}")
        else:
            print(f"stopped Cyclo instance: {instance.id}")
    if failures:
        raise CycloError(
            f"project definition {selected} stop incomplete: "
            + "; ".join(failures)
        )
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    rows = []
    shared_runtime_health: RuntimeHealth | None = None
    model_runtime: ProviderService | None = None
    for instance in store.list():
        running = docker.container_running(instance.container_name)
        if running and instance.active:
            state = "running"
        elif running:
            state = "orphan"
        elif instance.active:
            state = "stale"
        else:
            state = "stopped"
        if state == "running":
            if shared_runtime_health is None:
                model_runtime = model_runtime or provider_service(args, store)
                shared_runtime_health = read_runtime_health(model_runtime)
            try:
                supervisor = read_agent_supervisor_status(
                    store.queue_root(instance.id)
                )
                suspended_agents = supervisor.suspended_agents
                planner_attention_jobs = supervisor.planner_attention_jobs
                supervisor_error = supervisor.error
            except Exception as exc:
                suspended_agents = ()
                planner_attention_jobs = ()
                supervisor_error = str(exc)
            health = team_health(
                shared_runtime_health,
                suspended_agents,
                supervisor_error,
                planner_attention_jobs,
            ).label()
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
    widths = [
        max(len(row[index]) for row in [header, *rows])
        for index in range(len(header))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return 0


class _DashboardUsageReader:
    """Read gateway usage without creating or reconciling gateway state."""

    def __init__(self, args: argparse.Namespace, store: StateStore) -> None:
        self.args = args
        self.store = store

    def usage(self) -> dict[str, object]:
        token = self.store.gateway_registry / "gateway-token"
        if token.is_symlink() or not token.is_file():
            raise CycloError("gateway has not been provisioned")
        proxy = gateway(self.args, self.store)
        if not Docker().container_running(proxy.container_name):
            raise CycloError("gateway is not running")
        return proxy.usage()


def cmd_dashboard(args: argparse.Namespace) -> int:
    store = state_store(args)
    snapshot = DashboardSnapshot(
        store,
        docker=Docker(),
        usage_reader=_DashboardUsageReader(args, store),
        runtime_reader=provider_service(args, store),
    )
    server = make_dashboard_server(
        snapshot.build,
        host=args.host,
        port=args.port,
        static_assets=packaged_dashboard_assets(),
    )
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    print(f"Cyclo dashboard: http://{host}:{port}/", flush=True)
    if not dashboard_host_is_loopback(host):
        print(
            "WARNING: dashboard has no authentication and is exposed on a "
            "non-loopback address; anyone who can reach this host can view "
            "team activity.",
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
    if not project.configured:
        assert project.path is not None
        return (
            f"project root: {project.path}",
            "task paths are relative to this project root; no container mount "
            "path is required",
        )

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
        "write only below configured /workspace/<name> paths; read-only "
        "inputs are below /readonly/<name>",
    )


def cmd_task(args: argparse.Namespace) -> int:
    store = state_store(args)
    instance = store.load(args.instance)
    if args.task_id in {".", ".."} or not args.task_id or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for char in args.task_id
    ):
        raise CycloError("task ID may use only letters, numbers, dot, underscore, and hyphen")
    spec = Path(args.spec).expanduser().resolve()
    if not spec.is_file():
        raise CycloError(f"task specification not found: {spec}")
    project_summary = _task_project_summary(instance)
    docker = Docker()
    if not docker.container_running(instance.container_name):
        raise CycloError(f"Cyclo instance is not running: {instance.id}")
    container_spec = f"/tmp/cyclo-task-{args.task_id}-{secrets.token_hex(8)}.md"
    docker.copy_to(instance.container_name, spec, container_spec)
    try:
        status = docker.exec(
            instance.container_name,
            ["/agentws/bin/task-create", args.task_id, container_spec],
            check=False,
        )
        if status != 0:
            raise CycloError(f"AgentWS task creation failed with status {status}")
    finally:
        docker.exec(
            instance.container_name,
            ["rm", "-f", container_spec],
            check=False,
            user="0:0",
        )
    for line in project_summary:
        print(line)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    instance = state_store(args).load(args.instance)
    return Docker().logs(instance.container_name, follow=args.follow)


def cmd_path(args: argparse.Namespace) -> int:
    store = state_store(args)
    store.load(args.instance)
    print(store.queue_root(args.instance))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    data = gateway(args, state_store(args)).usage()
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    # This command refreshes only the runtime's in-memory concrete catalogue:
    # no lifecycle, stale-instance mutation, token rotation, or network repair.
    catalog = provider_service(args, state_store(args)).catalog(refresh=True)
    names: list[str] = []
    for provider, info in sorted(catalog.items()):
        provider_models = info.get("models") if isinstance(info, dict) else None
        if not isinstance(provider_models, list):
            continue
        for model in provider_models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if isinstance(model_id, str) and model_id:
                names.append(f"{provider}/{model_id}")
    if not names:
        raise CycloError(
            "provider runtime returned no models; run `cyclo gateway providers` to list "
            "login choices, then use the listed "
            "`cyclo gateway login PROVIDER` command"
        )
    for model in names:
        print(model)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    model_runtime = provider_service(args, store)
    with store.locked():
        stale: list[Instance] = []
        running = active_instances(store, docker, stale=stale)
        inactive = [instance for instance in store.list() if not instance.active]
        attach_active_networks(docker, model_runtime, running)
        model_runtime.update_clients(running)
        rotation_errors = rotate_client_tokens(
            model_runtime,
            [item.id for item in stale] + [item.id for item in inactive],
        )
        cleanup_errors: list[str] = []
        cleaned = 0
        for instance in inactive:
            try:
                existed = docker.container_exists(instance.container_name)
                stop_remove_instance_container(docker, instance)
                docker.remove_network(
                    instance.network_name, model_runtime.container_name
                )
                if existed:
                    cleaned += 1
            except Exception as exc:
                cleanup_errors.append(f"{instance.id}: {exc}")
        if cleanup_errors:
            raise CycloError(
                "runtime capabilities were repaired, but inactive Cyclo Docker resources "
                "could not be cleaned: " + "; ".join(cleanup_errors)
            )
        if rotation_errors:
            raise token_rotation_failure(rotation_errors)
    print(
        f"repaired runtime capabilities and {len(running)} active Cyclo network(s); "
        f"cleaned {cleaned} orphaned container(s)"
    )
    return 0


def cmd_runtime(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = provider_service(args, store)
    if args.runtime_action == "status":
        status = runtime.status()
        state = "running" if status.running else "stopped" if status.exists else "absent"
        freshness = "current" if status.current else "stale"
        print(f"{runtime.container_name}\t{state}\t{freshness}")
        return 0
    with store.locked():
        if args.runtime_action in {"start", "restart"}:
            docker = Docker()
            running = active_instances(store, docker)
            if args.runtime_action == "start":
                status = runtime.status()
                if status.running and status.current:
                    # start() will legitimately retain this process, so apply
                    # and acknowledge the current capability state first.
                    runtime.update_clients(running)
                    runtime.start(build=args.build)
                elif status.exists:
                    # Reject stale or stopped containers before changing the
                    # registries they may still have cached.
                    runtime.start(build=args.build)
                else:
                    # A new process reads both mounted registries at startup.
                    runtime.update_clients(running, apply_runtime=False)
                    runtime.start(build=args.build)
                verb = "started"
            else:
                # Build first because that cannot alter live authority. Once
                # registry publication begins, the old cached authority must
                # already be gone even if replacement startup later fails.
                if args.build:
                    runtime.build()
                runtime.stop()
                runtime.update_clients(running, apply_runtime=False)
                runtime.start(build=False)
                verb = "restarted"
            attach_active_networks(docker, runtime, running)
            if running:
                # Startup seeding happens before the replacement has team
                # interfaces. Publish and acknowledge the address bindings now.
                runtime.update_clients(running)
            print(f"{verb} provider runtime: {runtime.container_name}")
            return 0
        if args.runtime_action == "stop":
            runtime.stop()
            print(f"stopped provider runtime: {runtime.container_name}")
            return 0
    raise CycloError(f"unknown provider-runtime action: {args.runtime_action}")


def cmd_provider(args: argparse.Namespace) -> int:
    store = state_store(args)
    return run_provider_command(
        args,
        store,
        host_configuration(args),
        lambda: provider_service(args, store),
    )


def _reload_runtime_after_gateway_change(
    args: argparse.Namespace,
    store: StateStore,
) -> None:
    runtime = provider_service(args, store)
    if runtime.status().running:
        runtime.refresh_catalog_control()


def cmd_gateway_restart(args: argparse.Namespace, *, build: bool = False) -> int:
    store = state_store(args)
    proxy = gateway(args, store)
    with store.locked():
        # Gateway lifecycle is explicit and independent. Preserve the existing
        # gateway client registry; runtime capabilities live elsewhere.
        proxy.restart(build=build)
        _reload_runtime_after_gateway_change(args, store)
    print("restarted gateway")
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    login_selection: argparse.Namespace | None = None

    def restart_handler(restart_args: argparse.Namespace) -> int:
        selected = argparse.Namespace(**vars(args))
        selected.gateway_image = restart_args.image
        selected.store_volume = restart_args.store_volume
        return cmd_gateway_restart(selected, build=restart_args.build)

    def login_guard(login_args: argparse.Namespace) -> None:
        nonlocal login_selection
        # The delegated parser resolves command-local overrides.  Build the
        # gateway from those final values, not the outer defaults.
        selected = argparse.Namespace(**vars(args))
        selected.gateway_image = login_args.image
        selected.store_volume = login_args.store_volume
        proxy = gateway(selected, state_store(selected))
        proxy.validate_login()
        login_selection = selected

    if args.gateway_help:
        return gateway_cli.main(["--help"], restart_handler=restart_handler)
    if not args.arguments:
        raise CycloError(
            "gateway requires providers, login, status, restart, or destroy-store"
        )
    action, *rest = args.arguments
    if action not in {"providers", "login", "status", "restart", "destroy-store"}:
        raise CycloError(
            "cyclo gateway accepts providers, login, status, restart, or destroy-store; "
            "use cyclo models or cyclo usage for gateway queries"
        )
    delegated = [action, "--image", args.gateway_image]
    if action != "providers":
        delegated.extend(["--store-volume", args.store_volume])
    if action == "restart":
        return gateway_cli.main(
            [*delegated, *rest],
            restart_handler=restart_handler,
        )
    if action == "login":
        result = gateway_cli.main([*delegated, *rest], login_guard=login_guard)
    else:
        result = gateway_cli.main([*delegated, *rest])
    if action == "login" and result == 0:
        selected = login_selection or args
        _reload_runtime_after_gateway_change(selected, state_store(selected))
    return result


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    configured_providers = ()
    store = state_store(args)
    try:
        root = agentws_root()
        print(f"ok  bundled job-loop ABI: {root}")
    except CycloError as exc:
        failures += 1
        print(f"no  bundled job-loop ABI: {exc}")
    try:
        instances = store.list()
        print(f"ok  persisted instance state: {len(instances)} instance(s)")
    except CycloError as exc:
        failures += 1
        print(f"no  persisted instance state: {exc}")
    try:
        proxy = gateway(args, store)
        print(f"ok  Cyclo credential gateway API: {proxy.gateway.__file__}")
    except CycloError as exc:
        failures += 1
        print(f"no  Cyclo credential gateway API: {exc}")
    try:
        config = host_configuration(args)
        configured_providers = config.load()
        if config.path.exists():
            print(
                f"ok  host provider configuration: {config.path} "
                f"({len(configured_providers)} provider(s))"
            )
        else:
            print(
                f"ok  host provider configuration: {config.path} "
                "(not present; gateway only)"
            )
    except CycloError as exc:
        failures += 1
        print(f"no  host provider configuration: {exc}")
    docker = Docker()
    ok, detail = docker.available()
    if ok:
        print(f"ok  Docker daemon: {detail}")
        runtime = provider_service(args, store)
        runtime_operational = False
        try:
            status = runtime.status()
            if not status.running:
                raise CycloError(
                    "provider runtime is not running; run `cyclo runtime start`"
                )
            if not status.current:
                raise CycloError(
                    "provider runtime is stale; run `cyclo runtime restart` "
                    "(add `--build` only if Cyclo reports that the image is stale)"
                )
            runtime.probe_operational(timeout=2.0)
            runtime_operational = True
            print(
                f"ok  provider runtime: {runtime.container_name} "
                "(current and operational)"
            )
        except CycloError as exc:
            failures += 1
            print(f"no  provider runtime: {exc}")
        catalog: dict[str, dict] = {}
        if runtime_operational:
            try:
                catalog = runtime.catalog()
                print(f"ok  provider runtime catalog: {len(catalog)} provider(s)")
            except CycloError as exc:
                failures += 1
                print(f"no  provider runtime catalog: {exc}")
        component_runtime = ProviderRuntime(runtime.state_root)
        for definition in configured_providers:
            try:
                component_spec = provider_definition_spec(
                    runtime.state_root, definition
                )
                component = component_runtime.status(
                    component_spec.identity, component_spec
                )
            except CycloError as exc:
                failures += 1
                print(
                    f"no  configured provider status: {definition.prefix}: {exc}"
                )
                continue
            if not component.container_exists:
                failures += 1
                print(f"no  configured provider absent: {definition.prefix}")
                continue
            stale_parts = []
            if not component.image_current:
                stale_parts.append("image")
            if not component.configuration_current:
                stale_parts.append("configuration")
            if stale_parts:
                failures += 1
                print(
                    f"no  configured provider stale: {definition.prefix} "
                    f"({', '.join(stale_parts)})"
                )
                continue
            if not component.container_running:
                failures += 1
                print(f"no  configured provider stopped: {definition.prefix}")
                continue
            if definition.prefix not in catalog:
                failures += 1
                print(
                    "no  configured provider missing from runtime catalog: "
                    f"{definition.prefix}"
                )
                continue
            print(f"ok  configured provider: {definition.prefix}")
    else:
        failures += 1
        print(f"no  Docker daemon: {detail}")
    return 1 if failures else 0


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        default=os.environ.get("CYCLO_STATE_ROOT"),
        help="Cyclo state directory (default: $XDG_STATE_HOME/cyclo)",
    )
    parser.add_argument(
        "--host-config",
        default=str(DEFAULT_HOST_CONFIG),
        help="host provider configuration (default: /etc/cyclo/host.conf)",
    )
    parser.add_argument(
        "--gateway-image",
        default=os.environ.get("CYCLO_GATEWAY_IMAGE", DEFAULT_GATEWAY_IMAGE),
        help="credential gateway image",
    )
    parser.add_argument(
        "--provider-runtime-image",
        default=os.environ.get(
            "CYCLO_PROVIDER_RUNTIME_IMAGE", DEFAULT_PROVIDER_RUNTIME_IMAGE
        ),
        help="provider runtime image",
    )
    parser.add_argument(
        "--store-volume",
        default=os.environ.get("CYCLO_GATEWAY_STORE", DEFAULT_STORE_VOLUME),
        help=(
            "Docker volume containing gateway credentials, subscriptions, and "
            "retained usage history"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyclo", description="Run Git-defined agent teams in a secure model loop")
    parser.add_argument("--version", action="version", version=f"cyclo {__version__}")
    add_common_options(parser)
    commands = parser.add_subparsers(required=True)

    init = commands.add_parser("init", help="create a minimal AgentWS-compatible team repository")
    init.add_argument("team")
    init.add_argument("--model", required=True, help="proxy model assigned to the initial agents (provider/model)")
    init.add_argument(
        "--template",
        help="bundled team template: plan-execute-verify, test-driven-repair, or adversarial-audit",
    )
    init.add_argument("--no-git", action="store_true", help="do not run git init")
    init.set_defaults(func=cmd_init)

    validate = commands.add_parser(
        "validate", help="validate a team repository or project.cyclo"
    )
    validate.add_argument("definition", help="team directory or project.cyclo file")
    validate.set_defaults(func=cmd_validate)

    templates = commands.add_parser("templates", help="list team templates bundled with Cyclo")
    templates.set_defaults(func=cmd_templates)

    run = commands.add_parser(
        "run",
        help="start every team in project.cyclo, or use legacy TEAM PROJECT syntax",
        description=(
            "Start all teams and named mounts declared by project.cyclo. "
            "The compatibility form `cyclo run TEAM PROJECT` starts one team "
            "with its definition read-only by default and its project root "
            "writable by default."
        ),
    )
    run.add_argument(
        "definition",
        help="project.cyclo, or a team repository in compatibility mode",
    )
    run.add_argument(
        "project",
        nargs="?",
        help="compatibility-mode project root directory (writable by default)",
    )
    run.add_argument(
        "--name",
        help=(
            "compatibility-mode instance name (project.cyclo uses its name "
            "and team repositories)"
        ),
    )
    run.add_argument("--image", default=os.environ.get("CYCLO_RUNTIME_IMAGE", DEFAULT_RUNTIME_IMAGE))
    run.add_argument(
        "--team-write",
        action="store_true",
        help=(
            "compatibility mode: allow the team to modify its definition "
            "(project.cyclo declares this per team)"
        ),
    )
    run.add_argument("--offline", action="store_true", help="block direct outbound network access; the model proxy remains reachable")
    run.add_argument(
        "--host",
        default=DEFAULT_DASHBOARD_HOST,
        help=(
            "host address for the AgentWS viewer (default: 127.0.0.1); "
            "0.0.0.0 exposes the unauthenticated viewer"
        ),
    )
    run.add_argument("--port", type=int, default=0, help="host AgentWS port; 0 chooses a free Docker port")
    run.add_argument("--verbose", action="store_true", help="mirror AgentWS agent transcripts to container logs")
    run.add_argument("--foreground", action="store_true", help="follow logs and stop on Ctrl-C")
    run.add_argument("--build", action="store_true", help="rebuild Cyclo's bundled agent runtime image")
    run.add_argument("--dry-run", action="store_true", help="print the redacted team Docker command without changing state")
    run.set_defaults(func=cmd_run)

    stop = commands.add_parser(
        "stop",
        help="stop one instance or every instance launched from project.cyclo",
    )
    stop.add_argument("target", help="instance ID or project.cyclo file")
    stop.set_defaults(func=cmd_stop)

    ps = commands.add_parser("ps", help="list Cyclo instances")
    ps.set_defaults(func=cmd_ps)

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the local read-only dashboard for all Cyclo instances",
        description=(
            "Serve the read-only dashboard. It has no authentication; binding "
            "a non-loopback host exposes team activity to reachable clients."
        ),
    )
    dashboard.add_argument(
        "--host",
        default=DEFAULT_DASHBOARD_HOST,
        help=(
            "listen address (default: 127.0.0.1); non-loopback addresses such "
            "as 0.0.0.0 expose the unauthenticated dashboard"
        ),
    )
    dashboard.add_argument(
        "--port",
        type=int,
        default=0,
        help="host port; 0 chooses a free port",
    )
    dashboard.set_defaults(func=cmd_dashboard)

    task = commands.add_parser("task", help="create an AgentWS task for an instance")
    task.add_argument("instance")
    task.add_argument("task_id")
    task.add_argument("spec")
    task.set_defaults(func=cmd_task)

    logs = commands.add_parser("logs", help="show an instance's container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("instance")
    logs.set_defaults(func=cmd_logs)

    path = commands.add_parser("path", help="print an instance's AgentWS state path")
    path.add_argument("instance")
    path.set_defaults(func=cmd_path)

    usage = commands.add_parser("usage", help="show proxy token usage by team, generation, provider, and model")
    usage.set_defaults(func=cmd_usage)

    models = commands.add_parser(
        "models",
        help="list provider-runtime model names accepted in a team roster",
        description=(
            "Refresh the running provider runtime from the credential gateway, "
            "then list provider/model names from logged-in accounts and explicit "
            "host.conf components. The gateway must be available. A provider is "
            "the route prefix before the slash."
        ),
        epilog=(
            "Before the first login, run `cyclo gateway providers` to list provider "
            "names and authentication methods."
        ),
    )
    models.set_defaults(func=cmd_models)

    repair = commands.add_parser(
        "repair",
        help="repair runtime capabilities/networks and clean interrupted stops",
        description=(
            "Repair an already-running provider runtime: reattach it to active "
            "team networks, republish address-bound capabilities, revoke stale "
            "clients, and clean interrupted stops. This never starts or rebuilds "
            "a shared service."
        ),
    )
    repair.set_defaults(func=cmd_repair)

    runtime_parser = commands.add_parser(
        "runtime",
        help="explicitly manage the shared provider runtime",
        description=(
            "Manage the provider runtime independently from the credential "
            "gateway and provider component containers."
        ),
    )
    runtime_commands = runtime_parser.add_subparsers(
        dest="runtime_action", required=True
    )
    runtime_help = {
        "start": (
            "start the current runtime image; refuse an existing stale runtime"
        ),
        "restart": (
            "explicitly replace the runtime container to apply host.conf changes; "
            "do not rebuild unless --build is supplied"
        ),
        "stop": "stop and remove only the runtime container",
        "status": "show runtime state without changing it",
    }
    for action in ("start", "restart"):
        selected = runtime_commands.add_parser(
            action,
            help=runtime_help[action],
            description=runtime_help[action],
        )
        selected.add_argument(
            "--build",
            action="store_true",
            help="explicitly rebuild the provider runtime image first",
        )
        selected.set_defaults(func=cmd_runtime)
    for action in ("stop", "status"):
        selected = runtime_commands.add_parser(
            action,
            help=runtime_help[action],
            description=runtime_help[action],
        )
        selected.set_defaults(func=cmd_runtime)

    provider_parser = commands.add_parser(
        "provider",
        help="explicitly build and manage host provider containers",
        description=(
            "Explicitly manage provider component images and containers. These "
            "commands never start, build, or replace the credential gateway."
        ),
    )
    provider_commands = provider_parser.add_subparsers(
        dest="provider_action", required=True
    )
    provider_help = {
        "build": "build selected provider image(s) without launching them",
        "start": "start or reuse selected current provider(s) and wait for readiness",
        "restart": "replace selected provider container(s) and wait for readiness",
        "stop": "stop selected provider(s) and remove their runtime routes",
        "status": "show selected provider state without changing it",
    }
    for action in ("build", "start", "restart", "stop", "status"):
        selected = provider_commands.add_parser(
            action,
            help=provider_help[action],
            description=provider_help[action],
        )
        target = selected.add_mutually_exclusive_group(required=True)
        target.add_argument("provider_prefix", metavar="PREFIX", nargs="?")
        target.add_argument(
            "--all",
            dest="all_providers",
            action="store_true",
            help="apply the explicit action to every selected provider",
        )
        if action == "restart":
            selected.add_argument(
                "--build",
                action="store_true",
                help="explicitly rebuild selected provider image(s) first",
            )
        selected.set_defaults(func=cmd_provider)

    gateway_parser = commands.add_parser(
        "gateway",
        help=(
            "discover providers and manage Cyclo's isolated gateway store for "
            "credentials, subscriptions, and retained usage history"
        ),
        add_help=False,
    )
    gateway_parser.add_argument(
        "-h",
        "--help",
        dest="gateway_help",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    gateway_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    gateway_parser.set_defaults(func=cmd_gateway)

    doctor = commands.add_parser("doctor", help="check Cyclo's bundled runtimes and Docker dependency")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
