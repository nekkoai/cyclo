from __future__ import annotations

from .docker import Docker
from .errors import CycloError
from .provider_service import ProviderService
from .state import Instance, StateStore


def active_instances(
    store: StateStore,
    docker: Docker,
    *,
    candidate: Instance | None = None,
    stale: list[Instance] | None = None,
) -> list[Instance]:
    """Return active containers and persist instances that became stale."""

    result: list[Instance] = []
    for instance in store.list():
        if candidate is not None and instance.id == candidate.id:
            result.append(candidate)
        elif not instance.active:
            continue
        elif docker.container_lifecycle_active(instance.container_name):
            result.append(instance)
        else:
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
    """Attach the already-running Cyclo runtime to active team networks."""

    if not instances:
        return
    status = runtime.status()
    if not status.running or not status.container_id:
        raise CycloError("Cyclo runtime is not running; run `cyclo runtime start`")
    for instance in instances:
        network_id = docker.ensure_network(
            instance.network_name, offline=instance.offline
        )
        docker.connect_runtime(
            network_id, status.container_id, runtime.container_name
        )


def rotate_client_tokens(
    runtime: ProviderService,
    identifiers: list[str],
) -> list[str]:
    """Best-effort token-file rotation after capability publication."""

    errors: list[str] = []
    for identifier in dict.fromkeys(identifiers):
        try:
            runtime.rotate_client_token(identifier)
        except Exception as exc:
            errors.append(f"{identifier}: {exc}")
    return errors


def token_rotation_failure(errors: list[str]) -> CycloError:
    return CycloError(
        "client registries were published, but obsolete local capability files "
        "could not be rotated: " + "; ".join(errors)
    )


def stop_remove_instance_container(
    docker: Docker,
    instance: Instance,
    *,
    expected_launch_id: str | None = None,
) -> None:
    """Remove one launch-pinned team container."""

    if expected_launch_id is None:
        docker.stop_remove(instance.container_name, instance.id)
    else:
        docker.stop_remove(
            instance.container_name,
            instance.id,
            expected_launch=expected_launch_id,
        )


def stop_instance(
    store: StateStore,
    docker: Docker,
    runtime: ProviderService,
    identifier: str,
    *,
    expected_launch_id: str | None = None,
) -> None:
    """Revoke an instance capability before removing its Docker resources."""

    with store.locked():
        # Capability publication is fleet-wide. Refuse every mutation until the
        # complete persisted inventory has been parsed and validated.
        inventory = store.list()
        instance = next(
            (item for item in inventory if item.id == identifier),
            None,
        )
        if instance is None:
            raise CycloError(f"Cyclo instance not found: {identifier}")
        if expected_launch_id is not None:
            if instance.launch_id != expected_launch_id:
                raise CycloError(
                    f"instance {identifier!r} was replaced during project rollback"
                )
            if docker.container_exists(instance.container_name):
                actual_launch = docker.container_label(
                    instance.container_name, "cyclo.launch"
                )
                if actual_launch != expected_launch_id:
                    raise CycloError(
                        f"container for instance {identifier!r} was replaced "
                        "during project rollback"
                    )
        instance.active = False
        instance.port = None
        store.save(instance)

        stale: list[Instance] = []
        revoke_error: Exception | None = None
        repair_error: Exception | None = None
        try:
            remaining = active_instances(store, docker, stale=stale)
        except Exception as exc:
            revoke_error = exc
        else:
            try:
                attach_active_networks(docker, runtime, remaining)
            except Exception as exc:
                repair_error = exc
            try:
                runtime.update_clients(remaining)
            except Exception as exc:
                revoke_error = exc

        rotation_errors = rotate_client_tokens(
            runtime, [instance.id, *[item.id for item in stale]]
        )
        cleanup_error: Exception | None = None
        try:
            stop_remove_instance_container(
                docker,
                instance,
                expected_launch_id=expected_launch_id,
            )
            docker.remove_network(instance.network_name, runtime.container_name)
        except Exception as exc:
            cleanup_error = exc

        if revoke_error is not None:
            raise CycloError(
                "instance stopped in metadata but proxy capability revocation "
                f"failed: {revoke_error}"
            ) from revoke_error
        if cleanup_error is not None:
            raise CycloError(
                f"proxy capability was revoked but Docker cleanup failed: {cleanup_error}"
            ) from cleanup_error
        if repair_error is not None:
            raise CycloError(
                "capability was revoked and the instance stopped, but active "
                f"network repair failed: {repair_error}"
            ) from repair_error
        if rotation_errors:
            raise token_rotation_failure(rotation_errors)
