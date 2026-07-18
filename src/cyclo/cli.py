from __future__ import annotations

import argparse
import json
import os
import shlex
import secrets
import sys
import time
from pathlib import Path

from . import __version__
from .agentws_bundle import packaged_agentws_root
from .dashboard import (
    DEFAULT_DASHBOARD_HOST,
    DashboardSnapshot,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
)
from .docker import (
    ContainerSpec,
    Docker,
    container_command,
    validate_mount_boundaries,
)
from .errors import CycloError
from .gateway import CredentialGateway
from .host_config import DEFAULT_HOST_CONFIG, HostConfig
from .host_providers import HostProviders, provider_definition_spec
from .provider_runtime import ProviderRuntime
from .provider_service import ProviderService, provider_runtime_context_root
from .state import Instance, StateStore, instance_id
from .team_runtime_image import ensure as ensure_team_runtime_image
from .team import (
    init_team,
    load_team,
    require_team_repository,
    resolve_directory,
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
    return HostConfig(getattr(args, "host_config", DEFAULT_HOST_CONFIG))


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
        image=getattr(
            args, "provider_runtime_image", DEFAULT_PROVIDER_RUNTIME_IMAGE
        ),
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
        agentws_host=args.host,
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
    runtime: ProviderService,
    instances: list[Instance],
) -> None:
    """Attach the already-running provider runtime to team networks."""

    if not instances:
        return
    status = runtime.status()
    if not status.running or not status.container_id:
        raise CycloError(
            "provider runtime is not running; run `cyclo runtime start`"
        )
    for instance in instances:
        network_id = docker.ensure_network(
            instance.network_name, offline=instance.offline
        )
        docker.connect_runtime(
            network_id, status.container_id, runtime.container_name
        )


def rotate_client_tokens(
    proxy: ProviderService,
    identifiers: list[str],
) -> list[str]:
    """Best-effort local token-file rotation after client-registry publication.

    Publishing the runtime and gateway client registries revokes a live
    capability. Token-file deletion prevents that capability from being reused
    on a future run, but must never prevent the provider runtime from remaining
    attached to otherwise healthy team networks.
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
        "client registries were published, but obsolete local capability files "
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
    model_runtime = provider_service(args, store)
    docker = Docker()
    validate_mount_boundaries(
        team.root,
        project,
        store.root,
        proxy.host_pi_agent_dir,
        (
            (source, "bundled job-loop runtime"),
            (gateway_source.package_root(), "bundled credential-gateway runtime"),
            (provider_runtime_context_root(), "bundled provider runtime"),
            (model_runtime.host_config.path, "host provider configuration"),
            (Path(__file__).resolve().parents[2], "trusted Cyclo controller source"),
        ),
    )
    if args.port < 0 or args.port > 65535:
        raise CycloError("port must be 0 or an integer from 1 to 65535")
    if args.offline and args.port:
        raise CycloError("--port cannot be used with --offline because no host UI is published")
    agentws_host_is_loopback = dashboard_host_is_loopback(args.host)
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
        port=args.port,
        verbose=args.verbose,
    )
    if args.dry_run:
        print(shlex.join(container_command(spec)))
        return 0

    # Team lifecycle may update capabilities and network membership, but it
    # never provisions any shared service or provider container.
    model_runtime.require_running()

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
            ensure_team_runtime_image(instance.image, build=args.build)
            # A stopped/crashed instance must never resurrect a previously
            # issued capability when its stable binding name is reused.
            model_runtime.rotate_client_token(instance.id)
            # Bind the shared runtime to this team's private network before
            # issuing a capability. Authentication is pinned to the runtime's
            # local address on that network.
            attach_active_networks(docker, model_runtime, running)
            model_runtime.prepare_instance(
                instance,
                team,
                running,
            )
            rotation_errors = rotate_client_tokens(
                model_runtime, [item.id for item in stale]
            )
            if rotation_errors:
                raise token_rotation_failure(rotation_errors)
            instance.port = docker.start(spec)
            docker.wait_ready(
                instance.container_name,
                instance.port,
                host=instance.agentws_host,
            )
            store.save(instance)
        except Exception:
            instance.active = False
            instance.port = None
            store.save(instance)
            try:
                stale = []
                remaining = active_instances(store, docker, stale=stale)
                network_error: Exception | None = None
                try:
                    attach_active_networks(docker, model_runtime, remaining)
                except Exception as exc:
                    # Network drift must not prevent the revocation publication
                    # below. Missing bindings are deliberately unusable.
                    network_error = exc
                model_runtime.update_clients(remaining)
                rotation_errors = rotate_client_tokens(
                    model_runtime, [instance.id, *[item.id for item in stale]]
                )
                if network_error is not None:
                    raise CycloError(
                        f"active network repair failed after capability revocation: "
                        f"{network_error}"
                    ) from network_error
                if rotation_errors:
                    print(
                        f"warning: {token_rotation_failure(rotation_errors)}",
                        file=sys.stderr,
                    )
            except Exception as cleanup_error:
                print(
                    f"warning: failed to finish runtime rollback for {instance.id}: {cleanup_error}",
                    file=sys.stderr,
                )
            try:
                docker.stop_remove(instance.container_name, instance.id)
                docker.remove_network(
                    instance.network_name, model_runtime.container_name
                )
            except Exception:
                pass
            raise

    print(f"started Cyclo instance: {instance.id}")
    team_mode = "writable" if instance.team_write else "read-only"
    project_mode = "read-only" if instance.project_read_only else "writable"
    print(f"team definition ({team_mode}): {team.root}")
    print(f"project root ({project_mode}): {project}")
    if instance.port is not None:
        print(f"AgentWS: http://{instance.agentws_host}:{instance.port}")
        if not agentws_host_is_loopback:
            print(
                "WARNING: AgentWS has no authentication and is exposed on a "
                "non-loopback address; anyone who can reach this host can view "
                "team activity."
            )
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
    model_runtime = provider_service(args, store)
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
        except Exception as exc:
            revoke_error = exc
        else:
            try:
                # Repair first so the single publication below contains the
                # current per-team interface addresses. If repair fails, still
                # publish: empty bindings fail closed and the stopped team's
                # grant must be revoked.
                attach_active_networks(docker, model_runtime, remaining)
            except Exception as exc:
                repair_error = exc
            try:
                model_runtime.update_clients(remaining)
            except Exception as exc:
                revoke_error = exc
        rotation_errors = rotate_client_tokens(
            model_runtime, [instance.id, *[item.id for item in stale]]
        )
        try:
            docker.stop_remove(instance.container_name, instance.id)
            docker.remove_network(
                instance.network_name, model_runtime.container_name
            )
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
    print(f"project root: {instance.project_path}")
    print(
        "task paths are relative to this project root; no container mount path "
        "is required"
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
                docker.stop_remove(instance.container_name, instance.id)
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


def _selected_provider_definitions(
    args: argparse.Namespace, definitions=None
):
    definitions = (
        host_configuration(args).load()
        if definitions is None
        else tuple(definitions)
    )
    if args.all_providers:
        return definitions
    if args.provider_prefix is None:
        raise CycloError("provider command requires PREFIX or --all")
    selected = tuple(
        definition
        for definition in definitions
        if definition.prefix == args.provider_prefix
    )
    if not selected:
        raise CycloError(
            f"provider {args.provider_prefix!r} is not defined in "
            f"{host_configuration(args).path}"
        )
    return selected


def cmd_provider(args: argparse.Namespace) -> int:
    store = state_store(args)
    action = args.provider_action
    if action == "status":
        definitions = host_configuration(args).load()
        configured = {definition.prefix: definition for definition in definitions}
        runtime = ProviderRuntime(store.provider_runtime_root)
        if not args.all_providers:
            assert args.provider_prefix is not None
        if args.all_providers:
            identities_by_prefix = {
                identity.prefix: identity for identity in runtime.owned_identities()
            }
            for definition in definitions:
                identities_by_prefix.setdefault(
                    definition.prefix, runtime.identity(definition.prefix)
                )
            identities = tuple(
                identities_by_prefix[prefix]
                for prefix in sorted(identities_by_prefix)
            )
        else:
            identities = (runtime.identity(args.provider_prefix),)
        for identity in identities:
            definition = configured.get(identity.prefix)
            spec = (
                provider_definition_spec(store.provider_runtime_root, definition)
                if definition is not None
                else None
            )
            status = runtime.status(identity, spec)
            state = (
                "running"
                if status.container_running
                else "stopped"
                if status.container_exists
                else "absent"
            )
            detail = ""
            if definition is None:
                detail = "\tunconfigured"
            elif status.container_exists:
                detail = (
                    "\tcurrent"
                    if status.image_current and status.configuration_current
                    else "\tstale"
                )
            elif status.image_exists and not status.image_current:
                detail = "\tstale"
            print(f"{identity.prefix}\t{state}{detail}")
        return 0

    with store.locked():
        host = HostProviders(store.provider_runtime_root)
        service = provider_service(args, store)
        if action == "stop":
            if args.all_providers:
                by_prefix = {
                    identity.prefix: identity
                    for identity in host.runtime.owned_identities()
                }
                known_prefixes = {
                    str(record["prefix"])
                    for record in host.published_expectations()
                    if isinstance(record.get("prefix"), str)
                }
                known_prefixes.update(service.provider_client_prefixes())
                for prefix in known_prefixes:
                    by_prefix.setdefault(prefix, host.runtime.identity(prefix))
                identities = tuple(
                    by_prefix[prefix] for prefix in sorted(by_prefix)
                )
            else:
                assert args.provider_prefix is not None
                identities = (host.runtime.identity(args.provider_prefix),)
            prefixes = tuple(identity.prefix for identity in identities)
            # Revoke both component capabilities before touching Docker. If
            # container cleanup fails, a hostile process remains unable to
            # register, receive new routed work, or call an upstream model.
            errors: list[str] = []
            try:
                # This is the first fail-closed cut: removing expected state
                # disables ingress authentication, routing, and registration.
                with service.capability_update_guard():
                    host.remove_expectations(prefixes)
                    service.reload_control(require_current=False)
            except CycloError as exc:
                errors.append(f"expectation revocation: {exc}")
            try:
                service.remove_provider_clients(prefixes)
            except CycloError as exc:
                errors.append(f"upstream-capability revocation: {exc}")
            for identity in identities:
                try:
                    host.runtime.stop(identity)
                except CycloError as exc:
                    errors.append(f"container cleanup for {identity.prefix}: {exc}")
                else:
                    print(f"stopped provider: {identity.prefix}")
            if errors:
                raise CycloError("provider stop incomplete: " + "; ".join(errors))
            return 0

        definitions = host_configuration(args).load()
        selected_definitions = _selected_provider_definitions(args, definitions)
        selected_prefixes = {
            definition.prefix for definition in selected_definitions
        }
        if action == "build":
            for definition in selected_definitions:
                host.runtime.build(host.build_spec(definition))
                print(f"built provider: {definition.prefix}")
            return 0

        prepared = host.prepare(
            definitions, selected_prefixes=selected_prefixes
        )

        if action not in {"start", "restart"}:
            raise CycloError(f"unknown provider action: {action}")
        service.require_running()
        docker = Docker()
        running = active_instances(store, docker)
        for item in prepared:
            spec = host.spec(item)
            if action == "restart" and args.build:
                host.runtime.build(spec)
            elif action == "start":
                host.runtime.require_startable(spec)
            else:
                host.runtime.require_current_image(spec)

            previous_expectations = host.published_expectations()
            previous_clients = service.provider_clients()
            remaining_provider_clients = tuple(
                record
                for record in previous_clients
                if record.get("provider_prefix") != item.definition.prefix
            )
            if action == "restart":
                # Revoke and acknowledge the old route before retiring its
                # process. This removes the durable registration, so a
                # same-generation launch cannot be mistaken for an idempotent
                # renewal through socket-inode reuse.
                with service.capability_update_guard():
                    host.remove_expectations((item.definition.prefix,))
                    service.reload_control(require_current=False)
                service.update_clients(
                    running,
                    provider_clients=remaining_provider_clients,
                )
                host.runtime.stop(item.identity)
                # The replacement receives new ingress and upstream bearers.
                # Atomic file replacement leaves no still-mounted old process
                # with the new capability bytes.
                host.rotate_capabilities(item)

            # Publish only this selected prefix, immediately before its explicit
            # launch. Existing omitted-provider routes and containers are kept.
            launched = False
            try:
                host.upsert_expectations((host.expectation(item),))
                provider_clients = service.merged_provider_clients(
                    (host.client_record(item),)
                )
                service.update_clients(
                    running, provider_clients=provider_clients
                )
                # The catalog carries millisecond timestamps. Floor the launch
                # marker to that same precision so a registration in this exact
                # millisecond is still recognized as fresh.
                launch_started_at = (time.time_ns() // 1_000_000) / 1000
                if action == "start":
                    status = host.runtime.start(spec)
                    launched = status.container_restarted
                    verb = "started" if launched else "running"
                else:
                    status = host.runtime.start(spec)
                    launched = status.container_restarted
                    verb = "restarted"
                service.wait_provider(
                    item.definition.prefix,
                    status.generation,
                    runtime=host.runtime,
                    identity=item.identity,
                    registered_after=(launch_started_at if launched else None),
                )
            except Exception as exc:
                rollback_errors: list[str] = []
                if launched:
                    try:
                        host.runtime.stop(item.identity)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"container: {rollback_exc}")
                if action == "restart":
                    try:
                        with service.capability_update_guard():
                            host.remove_expectations((item.definition.prefix,))
                            service.reload_control(require_current=False)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"expectation revocation: {rollback_exc}")
                    try:
                        service.update_clients(
                            running,
                            provider_clients=remaining_provider_clients,
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(f"client revocation: {rollback_exc}")
                else:
                    try:
                        host.publish(previous_expectations)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"expectation: {rollback_exc}")
                    try:
                        service.update_clients(
                            running, provider_clients=previous_clients
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(f"clients: {rollback_exc}")
                if rollback_errors:
                    raise CycloError(
                        f"{exc}; provider rollback failed: "
                        + "; ".join(rollback_errors)
                    ) from exc
                raise
            print(f"{verb} provider: {item.definition.prefix}")
    return 0


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
    def restart_handler(restart_args: argparse.Namespace) -> int:
        selected = argparse.Namespace(**vars(args))
        selected.gateway_image = restart_args.image
        selected.store_volume = restart_args.store_volume
        return cmd_gateway_restart(selected, build=restart_args.build)

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
    result = gateway_cli.main([*delegated, *rest])
    if action == "login" and result == 0:
        _reload_runtime_after_gateway_change(args, state_store(args))
    return result


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    configured_providers = ()
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
        runtime = provider_service(args, state_store(args))
        runtime_running = False
        try:
            status = runtime.status()
            runtime_running = status.running
            if not status.running:
                raise CycloError(
                    "provider runtime is not running; run `cyclo runtime start`"
                )
            if not status.current:
                raise CycloError(
                    "provider runtime is stale; run `cyclo runtime restart` "
                    "(add `--build` only if Cyclo reports that the image is stale)"
                )
            print(f"ok  provider runtime: {runtime.container_name} (current)")
        except CycloError as exc:
            failures += 1
            print(f"no  provider runtime: {exc}")
        catalog: dict[str, dict] = {}
        if runtime_running:
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

    run = commands.add_parser(
        "run",
        help="start a team against a writable project workspace",
        description=(
            "Start a team with its definition read-only by default and its "
            "project root writable by default."
        ),
    )
    run.add_argument(
        "team",
        help="team definition repository (read-only by default)",
    )
    run.add_argument(
        "project",
        help="project root directory (writable by default)",
    )
    run.add_argument("--name", help="stable instance name (default: derived from team and project paths)")
    run.add_argument("--image", default=os.environ.get("CYCLO_RUNTIME_IMAGE", DEFAULT_RUNTIME_IMAGE))
    run.add_argument(
        "--team-write",
        action="store_true",
        help="allow the team to modify its own definition repository",
    )
    run.add_argument(
        "--project-read-only",
        action="store_true",
        help="make the project root read-only (default: writable)",
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

    stop = commands.add_parser("stop", help="stop an instance and revoke its proxy capability")
    stop.add_argument("instance")
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
