from __future__ import annotations

from .docker import Docker
from .errors import CycloError
from .state import Instance, StateStore


def active_instances(
    store: StateStore,
    docker: Docker,
    *,
    candidate: Instance | None = None,
    stale: list[Instance] | None = None,
) -> list[Instance]:
    """Return active team containers and persist records that became stale."""

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
            instance.port = None
            store.save(instance)
            if stale is not None:
                stale.append(instance)
    if candidate is not None and all(item.id != candidate.id for item in result):
        result.append(candidate)
    return result


def stop_remove_instance_container(
    docker: Docker,
    instance: Instance,
    *,
    expected_launch_id: str | None = None,
) -> None:
    """Remove exactly one launch-pinned team container."""

    docker.stop_remove(
        instance.container_name,
        instance.id,
        expected_launch=expected_launch_id,
    )


def stop_instance(
    store: StateStore,
    docker: Docker,
    identifier: str,
    *,
    expected_launch_id: str | None = None,
) -> None:
    """Stop one team without changing the independent provider stack."""

    with store.locked():
        inventory = store.list()
        instance = next((item for item in inventory if item.id == identifier), None)
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

        # Persist the stopped intent first. If Docker cleanup fails, a later
        # `cyclo repair` can finish removing the owned container and network.
        instance.active = False
        instance.port = None
        store.save(instance)

        try:
            stop_remove_instance_container(
                docker,
                instance,
                expected_launch_id=expected_launch_id,
            )
            docker.remove_network(instance.network_name)
        except Exception as exc:
            raise CycloError(
                f"instance stopped in metadata but Docker cleanup failed: {exc}"
            ) from exc
