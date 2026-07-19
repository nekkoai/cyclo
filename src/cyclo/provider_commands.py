from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from .docker import Docker
from .errors import CycloError
from .host_config import HostConfig, ProviderDefinition
from .host_providers import HostProviders, provider_definition_spec
from .instance_lifecycle import active_instances
from .provider_runtime import ProviderRuntime
from .provider_service import ProviderService
from .state import StateStore


def _selected_definitions(
    args: argparse.Namespace,
    definitions: tuple[ProviderDefinition, ...],
    host_config: HostConfig,
) -> tuple[ProviderDefinition, ...]:
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
            f"{host_config.path}"
        )
    return selected


def run_provider_command(
    args: argparse.Namespace,
    store: StateStore,
    host_config: HostConfig,
    service_factory: Callable[[], ProviderService],
) -> int:
    """Execute one explicit host-provider lifecycle command."""

    action = args.provider_action
    if action == "status":
        definitions = host_config.load()
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
        service = service_factory()
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

        definitions = host_config.load()
        selected_definitions = _selected_definitions(
            args, definitions, host_config
        )
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
