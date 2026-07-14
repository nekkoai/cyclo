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
from .dashboard import DashboardSnapshot, make_dashboard_server, packaged_dashboard_assets
from .docker import ContainerSpec, Docker, container_command, validate_mount_boundaries
from .errors import CycloError
from .gateway import CredentialGateway
from .state import Instance, StateStore, instance_id
from .team import (
    init_team,
    load_team,
    require_team_repository,
    resolve_directory,
    team_generation,
    verify_agentws_abi,
)
from .team_templates import bundled_team_template_names
from .vendor_gateway import cli as gateway_cli
from .vendor_gateway import source as gateway_source


DEFAULT_RUNTIME_IMAGE = f"cyclo-runtime:{__version__}"
DEFAULT_GATEWAY_IMAGE = f"cyclo-gateway:{__version__}"
DEFAULT_STORE_VOLUME = "cyclo-gateway-store"


def state_store(args: argparse.Namespace) -> StateStore:
    root = Path(args.state_root).expanduser().resolve() if args.state_root else None
    return StateStore(root)


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


def new_instance(args: argparse.Namespace, team, project: Path) -> Instance:
    identifier = instance_id(team.root, project, args.name)
    return Instance(
        id=identifier,
        team_name=team.name,
        team_path=str(team.root),
        project_path=str(project),
        generation=team_generation(team),
        providers=list(team.providers),
        models=sorted({agent.model for agent in team.agents}),
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image=args.image,
        team_write=args.team_write,
        project_read_only=args.project_read_only,
        offline=args.offline,
        active=True,
    )


def active_instances(
    store: StateStore,
    docker: Docker,
    *,
    candidate: Instance | None = None,
    stale: list[Instance] | None = None,
) -> list[Instance]:
    result: list[Instance] = []
    for instance in store.list():
        if candidate is not None and instance.id == candidate.id:
            result.append(candidate)
            continue
        if not instance.active:
            continue
        if docker.container_running(instance.container_name):
            result.append(instance)
            continue
        instance.active = False
        store.save(instance)
        if stale is not None:
            stale.append(instance)
    if candidate is not None and all(item.id != candidate.id for item in result):
        result.append(candidate)
    return result


def attach_active_networks(
    docker: Docker,
    proxy: CredentialGateway,
    instances: list[Instance],
) -> None:
    # The credential gateway reconciles registry changes by replacing the
    # gateway container. Reattach the replacement to every private team network.
    if not instances:
        return
    gateway_container_id = proxy.container_id
    for instance in instances:
        network_id = docker.ensure_network(
            instance.network_name, offline=instance.offline
        )
        docker.connect_gateway(
            network_id, gateway_container_id, proxy.container_name
        )


def rotate_client_tokens(
    proxy: CredentialGateway,
    identifiers: list[str],
) -> list[str]:
    """Best-effort local token-file rotation after registry reconciliation.

    Publishing a new gateway registry is what revokes a live capability. Token
    file deletion prevents that capability from being reused on a future run,
    but must never prevent the replacement gateway from being reattached to
    otherwise healthy team networks.
    """
    errors: list[str] = []
    for identifier in dict.fromkeys(identifiers):
        try:
            proxy.rotate_client_token(identifier)
        except Exception as exc:
            errors.append(f"{identifier}: {exc}")
    return errors


def token_rotation_failure(errors: list[str]) -> CycloError:
    return CycloError(
        "gateway registry was reconciled, but obsolete local capability files "
        "could not be rotated: " + "; ".join(errors)
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


def cmd_validate(args: argparse.Namespace) -> int:
    team = load_team(args.team)
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


def cmd_run(args: argparse.Namespace) -> int:
    team = load_team(args.team)
    require_team_repository(team)
    project = resolve_directory(args.project, "project")
    source = agentws_root()
    store = state_store(args)
    proxy = gateway(args, store)
    docker = Docker()
    validate_mount_boundaries(
        team.root,
        project,
        store.root,
        proxy.host_pi_agent_dir,
        (
            (source, "bundled job-loop runtime"),
            (gateway_source.package_root(), "bundled credential-gateway runtime"),
            (Path(__file__).resolve().parents[2], "trusted Cyclo controller source"),
        ),
    )
    if args.port < 0 or args.port > 65535:
        raise CycloError("port must be 0 or an integer from 1 to 65535")
    if args.offline and args.port:
        raise CycloError("--port cannot be used with --offline because no host UI is published")
    instance = new_instance(args, team, project)
    runtime = store.runtime_root(instance.id)
    pi_root = store.pi_root(instance.id)
    spec = ContainerSpec(
        instance=instance,
        team=team,
        project=project,
        runtime_root=runtime,
        tasks_dir=store.tasks_dir(instance.id),
        jobs_dir=store.jobs_dir(instance.id),
        agents_dir=store.agents_dir(instance.id),
        pi_root=pi_root,
        gateway_container=proxy.container_name,
        port=args.port,
        verbose=args.verbose,
    )
    if args.dry_run:
        print(shlex.join(container_command(spec)))
        return 0

    with store.locked():
        if docker.container_running(instance.container_name):
            raise CycloError(f"Cyclo instance is already running: {instance.id}")
        if store.metadata_path(instance.id).is_file():
            previous = store.load(instance.id)
            if (
                Path(previous.team_path).resolve() != team.root
                or Path(previous.project_path).resolve() != project
            ):
                raise CycloError(
                    f"instance name {instance.id!r} is already bound to a different team or project"
                )
        store.materialize_agentws(
            instance.id,
            source / "template",
            Path(__file__).with_name("container_runtime.py"),
        )
        store.save(instance)
        stale: list[Instance] = []
        running = active_instances(store, docker, candidate=instance, stale=stale)
        try:
            proxy.ensure_runtime_image(instance.image, build=args.build)
            # A stopped/crashed instance must never resurrect a previously
            # issued capability when its stable binding name is reused.
            proxy.rotate_client_token(instance.id)
            proxy.prepare_instance(
                instance,
                team,
                project,
                running,
                build_gateway=args.build_gateway,
            )
            rotation_errors = rotate_client_tokens(proxy, [item.id for item in stale])
            attach_active_networks(docker, proxy, running)
            if rotation_errors:
                raise token_rotation_failure(rotation_errors)
            instance.port = docker.start(spec)
            docker.wait_ready(instance.container_name, instance.port)
            store.save(instance)
        except Exception:
            instance.active = False
            instance.port = None
            store.save(instance)
            try:
                stale = []
                remaining = active_instances(store, docker, stale=stale)
                proxy.reconcile(remaining)
                rotation_errors = rotate_client_tokens(
                    proxy, [instance.id, *[item.id for item in stale]]
                )
                attach_active_networks(docker, proxy, remaining)
                if rotation_errors:
                    print(
                        f"warning: {token_rotation_failure(rotation_errors)}",
                        file=sys.stderr,
                    )
            except Exception as cleanup_error:
                print(
                    f"warning: failed to finish gateway rollback for {instance.id}: {cleanup_error}",
                    file=sys.stderr,
                )
            try:
                docker.stop_remove(instance.container_name, instance.id)
                docker.remove_network(instance.network_name, proxy.container_name)
            except Exception:
                pass
            raise

    print(f"started Cyclo instance: {instance.id}")
    print(f"team: {team.root}")
    print(f"project: {project}")
    if instance.port is not None:
        print(f"AgentWS: http://127.0.0.1:{instance.port}")
    else:
        print("AgentWS UI: not published in --offline mode")
    print(f"state: {store.queue_root(instance.id)}")
    if args.foreground:
        try:
            return docker.logs(instance.container_name, follow=True)
        except KeyboardInterrupt:
            stop_instance(args, store, instance.id)
    return 0


def stop_instance(args: argparse.Namespace, store: StateStore, identifier: str) -> None:
    docker = Docker()
    proxy = gateway(args, store)
    with store.locked():
        instance = store.load(identifier)
        instance.active = False
        instance.port = None
        store.save(instance)
        revoke_error: Exception | None = None
        rotation_errors: list[str] = []
        repair_error: Exception | None = None
        cleanup_error: Exception | None = None
        stale: list[Instance] = []
        remaining: list[Instance] = []
        try:
            remaining = active_instances(store, docker, stale=stale)
            proxy.reconcile(remaining)
        except Exception as exc:
            revoke_error = exc
        rotation_errors = rotate_client_tokens(
            proxy, [instance.id, *[item.id for item in stale]]
        )
        if revoke_error is None:
            try:
                attach_active_networks(docker, proxy, remaining)
            except Exception as exc:
                repair_error = exc
        try:
            docker.stop_remove(instance.container_name, instance.id)
            docker.remove_network(instance.network_name, proxy.container_name)
        except Exception as exc:
            cleanup_error = exc
        if revoke_error is not None:
            raise CycloError(
                f"instance stopped in metadata but proxy capability revocation failed: {revoke_error}"
            ) from revoke_error
        if cleanup_error is not None:
            raise CycloError(
                f"proxy capability was revoked but Docker cleanup failed: {cleanup_error}"
            ) from cleanup_error
        if repair_error is not None:
            raise CycloError(
                f"capability was revoked and the instance stopped, but active network repair failed: {repair_error}"
            ) from repair_error
        if rotation_errors:
            raise token_rotation_failure(rotation_errors)


def cmd_stop(args: argparse.Namespace) -> int:
    stop_instance(args, state_store(args), args.instance)
    print(f"stopped Cyclo instance: {args.instance}")
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    rows = []
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
        rows.append(
            (
                instance.id,
                state,
                instance.team_name,
                Path(instance.project_path).name,
                str(instance.port or ""),
            )
        )
    if not rows:
        print("no Cyclo instances")
        return 0
    widths = [max(len(row[index]) for row in [("INSTANCE", "STATE", "TEAM", "PROJECT", "PORT"), *rows]) for index in range(5)]
    header = ("INSTANCE", "STATE", "TEAM", "PROJECT", "PORT")
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
    )
    server = make_dashboard_server(
        snapshot.build,
        port=args.port,
        static_assets=packaged_dashboard_assets(),
    )
    port = int(server.server_address[1])
    print(f"Cyclo dashboard: http://127.0.0.1:{port}/", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


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
    store = state_store(args)
    docker = Docker()
    proxy = gateway(args, store)
    with store.locked():
        stale: list[Instance] = []
        running = active_instances(store, docker, stale=stale)
        catalog = proxy.catalog(running, build=args.build_gateway)
        rotation_errors = rotate_client_tokens(proxy, [item.id for item in stale])
        attach_active_networks(docker, proxy, running)
        if rotation_errors:
            raise token_rotation_failure(rotation_errors)
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
        raise CycloError("gateway returned no models")
    for model in names:
        print(model)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    store = state_store(args)
    docker = Docker()
    proxy = gateway(args, store)
    with store.locked():
        stale: list[Instance] = []
        running = active_instances(store, docker, stale=stale)
        inactive = [instance for instance in store.list() if not instance.active]
        proxy.reconcile(running, build=args.build_gateway)
        rotation_errors = rotate_client_tokens(
            proxy, [item.id for item in stale] + [item.id for item in inactive]
        )
        attach_active_networks(docker, proxy, running)
        cleanup_errors: list[str] = []
        cleaned = 0
        for instance in inactive:
            try:
                existed = docker.container_exists(instance.container_name)
                docker.stop_remove(instance.container_name, instance.id)
                docker.remove_network(instance.network_name, proxy.container_name)
                if existed:
                    cleaned += 1
            except Exception as exc:
                cleanup_errors.append(f"{instance.id}: {exc}")
        if cleanup_errors:
            raise CycloError(
                "gateway was reconciled, but inactive Cyclo Docker resources "
                "could not be cleaned: " + "; ".join(cleanup_errors)
            )
        if rotation_errors:
            raise token_rotation_failure(rotation_errors)
    print(
        f"reconciled gateway and {len(running)} active Cyclo network(s); "
        f"cleaned {cleaned} orphaned container(s)"
    )
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    if args.gateway_help:
        return gateway_cli.main(["--help"])
    if not args.arguments:
        raise CycloError("gateway requires login, status, or destroy-store")
    action, *rest = args.arguments
    if action not in {"login", "status", "destroy-store"}:
        raise CycloError(
            "cyclo gateway accepts login, status, or destroy-store; "
            "use cyclo models or cyclo usage for gateway queries"
        )
    return gateway_cli.main(
        [
            action,
            "--image",
            args.gateway_image,
            "--store-volume",
            args.store_volume,
            *rest,
        ]
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    try:
        root = agentws_root()
        print(f"ok  bundled job-loop ABI: {root}")
    except CycloError as exc:
        failures += 1
        print(f"no  bundled job-loop ABI: {exc}")
    try:
        proxy = gateway(args, state_store(args))
        print(f"ok  Cyclo credential gateway API: {proxy.gateway.__file__}")
    except CycloError as exc:
        failures += 1
        print(f"no  Cyclo credential gateway API: {exc}")
    ok, detail = Docker().available()
    if ok:
        print(f"ok  Docker daemon: {detail}")
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
        "--gateway-image",
        default=os.environ.get("CYCLO_GATEWAY_IMAGE", DEFAULT_GATEWAY_IMAGE),
        help="credential gateway image",
    )
    parser.add_argument(
        "--store-volume",
        default=os.environ.get("CYCLO_GATEWAY_STORE", DEFAULT_STORE_VOLUME),
        help=(
            "Docker volume containing gateway credentials, subscriptions, "
            "and retained usage history"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyclo", description="Run Git-defined agent teams in a secure model loop")
    parser.add_argument("--version", action="version", version=f"cyclo {__version__}")
    add_common_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a minimal AgentWS-compatible team repository")
    init.add_argument("team")
    init.add_argument("--model", required=True, help="proxy model assigned to the initial agents (provider/model)")
    init.add_argument(
        "--template",
        help="bundled team template: plan-execute-verify, test-driven-repair, or adversarial-audit",
    )
    init.add_argument("--no-git", action="store_true", help="do not run git init")
    init.set_defaults(func=cmd_init)

    validate = commands.add_parser("validate", help="validate a team repository")
    validate.add_argument("team")
    validate.set_defaults(func=cmd_validate)

    templates = commands.add_parser("templates", help="list team templates bundled with Cyclo")
    templates.set_defaults(func=cmd_templates)

    run = commands.add_parser("run", help="start a team against a project")
    run.add_argument("team")
    run.add_argument("project")
    run.add_argument("--name", help="stable instance name (default: derived from team and project paths)")
    run.add_argument("--image", default=os.environ.get("CYCLO_RUNTIME_IMAGE", DEFAULT_RUNTIME_IMAGE))
    run.add_argument("--team-write", action="store_true", help="allow the team to modify its own repository")
    run.add_argument("--project-read-only", action="store_true", help="mount the project read-only")
    run.add_argument("--offline", action="store_true", help="block direct outbound network access; the model proxy remains reachable")
    run.add_argument("--port", type=int, default=0, help="host AgentWS port; 0 chooses a free Docker port")
    run.add_argument("--verbose", action="store_true", help="mirror AgentWS agent transcripts to container logs")
    run.add_argument("--foreground", action="store_true", help="follow logs and stop on Ctrl-C")
    run.add_argument("--build", action="store_true", help="rebuild Cyclo's bundled agent runtime image")
    run.add_argument("--build-gateway", action="store_true", help="rebuild the gateway image")
    run.add_argument("--dry-run", action="store_true", help="print the redacted team Docker command without changing state")
    run.set_defaults(func=cmd_run)

    stop = commands.add_parser("stop", help="stop an instance and revoke its proxy capability")
    stop.add_argument("instance")
    stop.set_defaults(func=cmd_stop)

    ps = commands.add_parser("ps", help="list Cyclo instances")
    ps.set_defaults(func=cmd_ps)

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the local read-only dashboard for all Cyclo instances",
    )
    dashboard.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port; 0 chooses a free port",
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

    models = commands.add_parser("models", help="list gateway provider/model names accepted in a team roster")
    models.add_argument("--build-gateway", action="store_true")
    models.set_defaults(func=cmd_models)

    repair = commands.add_parser(
        "repair",
        help="reconcile the gateway/networks and clean interrupted stops",
    )
    repair.add_argument("--build-gateway", action="store_true")
    repair.set_defaults(func=cmd_repair)

    gateway_parser = commands.add_parser(
        "gateway",
        help=(
            "manage Cyclo's isolated gateway store for credentials, "
            "subscriptions, and retained usage history"
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
