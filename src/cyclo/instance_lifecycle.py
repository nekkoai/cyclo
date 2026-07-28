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
    if instance.intent == "deleting":
        return "deleting"
    if instance.intent == "stopped":
        return (
            "stopped"
            if state is DockerContainerState.ABSENT
            else "orphan"
        )
    if state is DockerContainerState.RUNNING:
        return "running"
    if state in {DockerContainerState.PAUSED, DockerContainerState.RESTARTING}:
        return state.value
    return "stale"


def intended_running_instances(store: StateStore) -> list[Instance]:
    """Return durable running intent without consulting or changing Docker."""

    return [instance for instance in store.list() if instance.intent == "running"]


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
    instance = store.load(expected.id)
    if instance.launch_id != expected.launch_id:
        raise CycloError(
            f"instance {expected.id!r} was replaced before it could be stopped"
        )
    if instance.intent == "deleting":
        raise CycloError(
            f"Cyclo instance is being deleted: {instance.id}; run cyclo repair"
        )

    # Persist the stopped intent first. If Docker cleanup fails, a later
    # `cyclo repair` can finish removing the owned container and network.
    instance.intent = "stopped"
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
