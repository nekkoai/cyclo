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
from .component_stack import (
    COMPONENT_INTERFACE,
    PROVIDER_INTERFACE,
    ComponentDocker,
    Gateway,
    ProviderStack,
    StackStatus,
    bundle_root,
    parse_declaration,
)
from .dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DashboardSnapshot,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
)
from .docker import Docker, container_command, validate_mount_collection
from .errors import CycloError
from .health import (
    INACTIVE_TEAM_HEALTH,
    ProviderHealth,
    instance_provider_health,
    read_provider_status,
    team_health,
)
from .instance_lifecycle import active_instances, stop_instance as stop_managed_instance
from .project import ProjectDefinition, ProjectTeam, load_project
from .project_run import (
    RunBinding,
    container_spec,
    load_project_teams,
    preflight_binding,
    project_run_bindings,
    start_binding,
    validate_run_options,
    validate_team_models,
)
from .project_state import decode_instance_project
from .state import Instance, StateStore, validate_instance_id
from .team import Team, init_team, load_team, require_team_repository, team_generation, verify_agentws_abi
from .team_templates import bundled_team_template_names


DEFAULT_HOST_CONFIG = Path("/etc/cyclo/host.conf")
DEFAULT_TEAM_IMAGE = f"cyclo-team:{__version__}"


def state_store(args: argparse.Namespace) -> StateStore:
    root = Path(args.state_root).expanduser().resolve() if args.state_root else None
    return StateStore(root)


def gateway(args: argparse.Namespace, store: StateStore) -> Gateway:
    return Gateway(store.components_root, docker=ComponentDocker())


def provider_stack(
    args: argparse.Namespace,
    store: StateStore,
    *,
    load_config: bool = True,
) -> ProviderStack:
    docker = ComponentDocker()
    proxy = Gateway(store.components_root, docker=docker)
    return ProviderStack(
        store.components_root,
        Path(args.host_config),
        gateway=proxy,
        docker=docker,
        load_config=load_config,
    )


def agentws_root() -> Path:
    root = packaged_agentws_root()
    verify_agentws_abi(root)
    return root


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


def _trusted_mount_roots(source: Path, host_config: Path) -> tuple[tuple[Path, str], ...]:
    return (
        (source, "bundled AgentWS runtime"),
        (bundle_root(), "bundled Cyclo components"),
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
            definition, teams, store, source, Path(args.host_config)
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


def _catalogue_ids(document: dict[str, object]) -> set[str]:
    raw_models = document.get("models")
    if not isinstance(raw_models, list):
        raise CycloError("provider stack returned an invalid model catalogue")
    result: set[str] = set()
    for raw in raw_models:
        model = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(model, str) or not model or model in result:
            raise CycloError("provider stack returned an invalid or duplicate model ID")
        result.add(model)
    return result


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
        print(f"AgentWS: http://{instance.agentws_host}:{instance.port}")
        if not dashboard_host_is_loopback(instance.agentws_host):
            print(
                "WARNING: AgentWS has no authentication and is exposed on a "
                "non-loopback address."
            )
    else:
        print("AgentWS UI: not published in --offline mode")
    print(f"state: {store.queue_root(instance.id)}")


def cmd_run(args: argparse.Namespace) -> int:
    source = agentws_root()
    store = state_store(args)
    docker = Docker()
    definition = load_project(args.project)
    configured_teams = load_project_teams(definition)
    validate_run_options(args, team_count=len(configured_teams))
    _validate_project_mounts(
        definition,
        configured_teams,
        store,
        source,
        Path(args.host_config),
    )
    bindings = project_run_bindings(args, definition, configured_teams)

    stack = provider_stack(args, store)
    if args.dry_run:
        for binding in bindings:
            binding.instance.provider_socket_path = str(stack.provider_socket_path)
            binding.instance.provider_generation = stack.assembly.generation
            if len(bindings) > 1:
                print(f"# instance {binding.instance.id}")
            print(shlex.join(container_command(container_spec(binding, store, args))))
        return 0

    status = stack.require_ready()
    available_models = _catalogue_ids(stack.models_document())
    for binding in bindings:
        validate_team_models(binding.team, available_models)
        binding.instance.provider_socket_path = str(status.provider_socket_path)
        binding.instance.provider_generation = status.generation

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
                        f"{current.instance.id}: cannot verify in-flight launch: {exc}"
                    )
            else:
                if persisted.active and persisted.launch_id == current.instance.launch_id:
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
    _args: argparse.Namespace,
    store: StateStore,
    identifier: str,
    *,
    expected_launch_id: str | None = None,
) -> None:
    stop_managed_instance(
        store,
        Docker(),
        identifier,
        expected_launch_id=expected_launch_id,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    store = state_store(args)
    target = args.target
    try:
        candidate_id = validate_instance_id(target)
    except CycloError:
        candidate_id = None
    if candidate_id is not None and store.metadata_path(candidate_id).is_file():
        stop_instance(args, store, candidate_id)
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
            stop_instance(args, store, instance.id)
        except Exception as exc:
            failures.append(f"{instance.id}: {exc}")
        else:
            print(f"stopped Cyclo instance: {instance.id}")
    if failures:
        raise CycloError(f"project stop incomplete: {'; '.join(failures)}")
    return 0


def _shared_provider_health(
    args: argparse.Namespace,
    store: StateStore,
) -> tuple[ProviderHealth, StackStatus | None]:
    return read_provider_status(provider_stack(args, store))


def cmd_ps(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    rows = []
    shared: tuple[ProviderHealth, StackStatus | None] | None = None
    for instance in store.list():
        running = docker.container_running(instance.container_name)
        state = (
            "running"
            if running and instance.active
            else "orphan"
            if running
            else "stale"
            if instance.active
            else "stopped"
        )
        if state == "running":
            if shared is None:
                shared = _shared_provider_health(args, store)
            provider = instance_provider_health(shared[0], shared[1], instance)
            try:
                supervisor = read_agent_supervisor_status(store.queue_root(instance.id))
                health = team_health(
                    provider,
                    supervisor.suspended_agents,
                    supervisor.error,
                    supervisor.planner_attention_jobs,
                ).label()
            except Exception as exc:
                health = team_health(provider, supervisor_error=str(exc)).label()
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
        provider_reader=provider_stack(args, store),
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
    docker = Docker()
    if not docker.container_running(instance.container_name):
        raise CycloError(f"Cyclo instance is not running: {instance.id}")
    container_spec_path = f"/tmp/cyclo-task-{args.task_id}-{secrets.token_hex(8)}.md"
    docker.copy_to(instance.container_name, spec, container_spec_path)
    try:
        result = docker.exec(
            instance.container_name,
            ["/agentws/bin/task-create", args.task_id, container_spec_path],
            check=False,
        )
        if result != 0:
            raise CycloError(f"AgentWS task creation failed with status {result}")
    finally:
        docker.exec(
            instance.container_name,
            ["rm", "-f", container_spec_path],
            check=False,
            user="0:0",
        )
    for line in _task_project_summary(instance):
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
    print(json.dumps(gateway(args, state_store(args)).usage(), indent=2, sort_keys=True))
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    models = provider_stack(args, state_store(args)).model_ids()
    if not models:
        raise CycloError(
            "provider stack returned no models; run `cyclo gateway providers` and log in"
        )
    for model in models:
        print(model)
    return 0


def _component_state(status: object) -> str:
    ready = bool(getattr(status, "ready", False))
    docker = getattr(status, "docker")
    if ready:
        return "ready"
    if not docker.container_id:
        return "absent"
    if not docker.current:
        return "stale"
    if not docker.running:
        return docker.lifecycle
    return "not-ready"


def _print_stack_status(status: StackStatus) -> None:
    print(f"gateway\t{_component_state(status.gateway)}")
    for component in status.components:
        print(f"{component.instance}\t{_component_state(component)}")


def cmd_providers(args: argparse.Namespace) -> int:
    store = state_store(args)
    if args.providers_action == "stop":
        stack = provider_stack(args, store, load_config=False)
        with store.locked():
            stopped = stack.stop()
        print("stopped: " + (", ".join(stopped) if stopped else "nothing running"))
        return 0

    stack = provider_stack(args, store)
    if args.providers_action == "check":
        print(f"ok: {stack.check()} provider component(s)")
        return 0
    if args.providers_action == "build":
        with store.locked():
            built = stack.build()
        for instance, image_id in built:
            print(f"{instance}\t{image_id}")
        return 0
    if args.providers_action == "status":
        status = stack.status()
        _print_stack_status(status)
        return 0 if status.ready else 1
    with store.locked():
        if args.providers_action == "start":
            status = stack.start()
        elif args.providers_action == "restart":
            status = stack.restart(build=args.build)
        else:
            raise CycloError(f"unknown providers action: {args.providers_action}")
    _print_stack_status(status)
    return 0 if status.ready else 1


def _print_gateway_status(status: object, store_volume: str) -> int:
    state = _component_state(status)
    freshness = "current" if status.docker.current else "stale"
    print(f"gateway\t{state}\t{freshness}")
    print(f"store\t{store_volume}")
    return 0 if status.ready else 1


def cmd_gateway(args: argparse.Namespace) -> int:
    store = state_store(args)
    proxy = gateway(args, store)
    action = args.gateway_action
    if action == "providers":
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
            proxy.login(login_args)
        print("restart the gateway to refresh its model catalogue")
        return 0
    with store.locked():
        if action == "build":
            image = proxy.build()
            print(image)
            return _print_gateway_status(proxy.restart(build=False), proxy.store_volume)
        if action == "start":
            return _print_gateway_status(proxy.start(), proxy.store_volume)
        if action == "restart":
            return _print_gateway_status(proxy.restart(build=args.build), proxy.store_volume)
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
    removed = 0
    with store.locked():
        stale: list[Instance] = []
        active_instances(store, docker, stale=stale)
        repaired += len(stale)
        for instance in store.list():
            if instance.active:
                continue
            if docker.container_exists(instance.container_name):
                docker.stop_remove(instance.container_name, instance.id)
                removed += 1
            docker.remove_network(instance.network_name)
    print(f"repaired {repaired} stale record(s); removed {removed} inactive container(s)")
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
        bundle = bundle_root()
        gateway_declaration = parse_declaration(bundle / "gateway" / "component.conf")
        if set(gateway_declaration.provides) != {COMPONENT_INTERFACE, PROVIDER_INTERFACE}:
            raise CycloError("gateway declaration does not provide Component and Provider")
        print(f"ok  bundled component ABI: {bundle}")
    except CycloError as exc:
        failures += 1
        print(f"no  bundled component ABI: {exc}")
    try:
        instances = store.list()
        print(f"ok  persisted instance state: {len(instances)} instance(s)")
    except CycloError as exc:
        failures += 1
        print(f"no  persisted instance state: {exc}")

    docker = ComponentDocker()
    ok, detail = docker.available()
    if not ok:
        print(f"no  Docker daemon: {detail}")
        return 1
    print(f"ok  Docker daemon: {detail}")

    try:
        stack = provider_stack(args, store)
        print(
            f"ok  host provider configuration: {stack.assembly.path} "
            f"({len(stack.assembly.providers)} component(s))"
        )
    except CycloError as exc:
        failures += 1
        print(f"no  host provider configuration: {exc}")
        return 1

    try:
        status = stack.status()
        if not status.gateway.ready:
            raise CycloError(f"gateway is {_component_state(status.gateway)}")
        print("ok  credential gateway: current and ready")
    except CycloError as exc:
        failures += 1
        print(f"no  credential gateway: {exc}")
        return 1

    for component in status.components:
        state = _component_state(component)
        if component.ready:
            print(f"ok  provider component {component.instance}: ready")
        else:
            failures += 1
            print(f"no  provider component {component.instance}: {state}")
    if status.ready:
        try:
            models = stack.model_ids()
            print(f"ok  outer provider catalogue: {len(models)} model(s)")
        except CycloError as exc:
            failures += 1
            print(f"no  outer provider catalogue: {exc}")
    return 1 if failures else 0


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        default=os.environ.get("CYCLO_STATE_ROOT"),
        help="Cyclo state directory (default: $XDG_STATE_HOME/cyclo)",
    )
    parser.add_argument(
        "--host-config",
        default=os.environ.get("CYCLO_HOST_CONFIG", str(DEFAULT_HOST_CONFIG)),
        help="provider assembly (default: /etc/cyclo/host.conf)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclo",
        description="Run Git-defined agent teams through composable model providers",
    )
    parser.add_argument("--version", action="version", version=f"cyclo {__version__}")
    add_common_options(parser)
    commands = parser.add_subparsers(required=True)

    init = commands.add_parser("init", help="create a team repository")
    init.add_argument("team")
    init.add_argument("--model", required=True, help="initial provider/model")
    init.add_argument("--template", choices=bundled_team_template_names())
    init.add_argument("--no-git", action="store_true")
    init.set_defaults(func=cmd_init)

    validate = commands.add_parser("validate", help="validate a team or project.cyclo")
    validate.add_argument("definition")
    validate.set_defaults(func=cmd_validate)
    templates = commands.add_parser("templates", help="list bundled team templates")
    templates.set_defaults(func=cmd_templates)

    run = commands.add_parser("run", help="start every team in project.cyclo")
    run.add_argument("project", help="project.cyclo")
    run.add_argument("--image", default=os.environ.get("CYCLO_TEAM_IMAGE", DEFAULT_TEAM_IMAGE))
    run.add_argument(
        "--offline",
        action="store_true",
        help="block direct network access; the mounted model-provider socket remains available",
    )
    run.add_argument("--host", default=DEFAULT_DASHBOARD_HOST, help="AgentWS bind address")
    run.add_argument("--port", type=int, default=0, help="AgentWS port; 0 chooses a free port")
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--build", action="store_true", help="rebuild the bundled team image")
    run.add_argument("--dry-run", action="store_true", help="print the team Docker command")
    run.set_defaults(func=cmd_run)

    stop = commands.add_parser("stop", help="stop an instance or a whole project")
    stop.add_argument("target", help="instance ID or project.cyclo")
    stop.set_defaults(func=cmd_stop)
    ps = commands.add_parser("ps", help="list team instances")
    ps.set_defaults(func=cmd_ps)

    dashboard = commands.add_parser("dashboard", help="serve the read-only fleet dashboard")
    dashboard.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.set_defaults(func=cmd_dashboard)

    task = commands.add_parser("task", help="create an AgentWS task")
    task.add_argument("instance")
    task.add_argument("task_id")
    task.add_argument("spec")
    task.set_defaults(func=cmd_task)
    logs = commands.add_parser("logs", help="show team-container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("instance")
    logs.set_defaults(func=cmd_logs)
    path = commands.add_parser("path", help="print an instance's AgentWS state path")
    path.add_argument("instance")
    path.set_defaults(func=cmd_path)

    usage = commands.add_parser("usage", help="show global gateway usage by provider and model")
    usage.set_defaults(func=cmd_usage)
    models = commands.add_parser("models", help="list models at the outer provider endpoint")
    models.epilog = "Before login, use `cyclo gateway providers` to see available providers."
    models.set_defaults(func=cmd_models)

    repair = commands.add_parser("repair", help="clean interrupted team-container stops")
    repair.set_defaults(func=cmd_repair)

    providers = commands.add_parser(
        "providers",
        help="manage the ordered Provider components declared in host.conf",
        description=(
            "Build, start, inspect, or stop the ordered Provider component stack. "
            "The fixed credential gateway is its independent root."
        ),
    )
    provider_commands = providers.add_subparsers(dest="providers_action", required=True)
    for action in ("check", "build", "start", "stop", "status"):
        selected = provider_commands.add_parser(action)
        selected.set_defaults(func=cmd_providers)
    restart = provider_commands.add_parser("restart")
    restart.add_argument("--build", action="store_true")
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
    for action in ("build", "start", "stop", "status"):
        selected = gateway_commands.add_parser(action, help=f"{action} the gateway")
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
    gateway_restart = gateway_commands.add_parser("restart", help="restart the gateway")
    gateway_restart.add_argument("--build", action="store_true")
    gateway_restart.set_defaults(func=cmd_gateway)
    login = gateway_commands.add_parser(
        "login",
        help="store credentials for a provider account",
        description=(
            "Authenticate a catalogue provider/account name. The account name becomes "
            "the model prefix (default: PROVIDER)."
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
