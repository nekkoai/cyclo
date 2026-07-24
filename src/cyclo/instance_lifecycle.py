from __future__ import annotations

from .docker import Docker, DockerContainerState
from .errors import CycloError
from .state import LAUNCH_ID_RE, Instance, StateStore


def instance_lifecycle_label(
    instance: Instance,
    state: DockerContainerState | None,
) -> str:
    """Combine durable intent with the exact observable Docker lifecycle."""

    if state is None:
        return "unknown"
    if state is DockerContainerState.RUNNING:
        return "running" if instance.active else "orphan"
    if state in {
        DockerContainerState.PAUSED,
        DockerContainerState.RESTARTING,
    }:
        return state.value
    return "stale" if instance.active else "stopped"


def active_instances(
    store: StateStore,
    docker: Docker,
    *,
    stale: list[Instance] | None = None,
) -> list[Instance]:
    """Return active team containers and persist records that became stale."""

    result: list[Instance] = []
    for instance in store.list():
        if not instance.active:
            continue
        if docker.container_lifecycle_active(instance, system=store.system):
            result.append(instance)
        else:
            instance.active = False
            instance.port = None
            store.save(instance)
            if stale is not None:
                stale.append(instance)
    return result


def stop_remove_instance_container(
    docker: Docker,
    instance: Instance,
    *,
    system: str,
) -> bool:
    """Remove exactly one launch-pinned team container."""

    return docker.stop_remove(
        instance.container_name,
        instance.id,
        expected_system=system,
        expected_launch=instance.launch_id,
    )


def stop_instance(
    store: StateStore,
    docker: Docker,
    expected: Instance,
) -> None:
    """Stop one team without changing the independent provider components."""

    with store.locked():
        stop_instance_locked(store, docker, expected)


def stop_instance_locked(
    store: StateStore,
    docker: Docker,
    expected: Instance,
) -> None:
    """Stop one launch while the caller holds the installation control lock."""

    if not LAUNCH_ID_RE.fullmatch(expected.launch_id):
        raise CycloError(
            f"invalid launch identity for Cyclo instance: {expected.id}"
        )
    inventory = store.list()
    instance = next((item for item in inventory if item.id == expected.id), None)
    if instance is None:
        raise CycloError(f"Cyclo instance not found: {expected.id}")
    if instance.launch_id != expected.launch_id:
        raise CycloError(
            f"instance {expected.id!r} was replaced before it could be stopped"
        )

    # Persist the stopped intent first. If Docker cleanup fails, a later
    # `cyclo repair` can finish removing the owned container and network.
    instance.active = False
    instance.port = None
    store.save(instance)

    try:
        stop_remove_instance_container(
            docker,
            instance,
            system=store.system,
        )
        docker.remove_network(
            instance.network_name, instance.id, system=store.system
        )
    except Exception as exc:
        raise CycloError(
            f"instance stopped in metadata but Docker cleanup failed: {exc}"
        ) from exc
