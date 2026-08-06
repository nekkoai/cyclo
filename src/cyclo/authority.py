from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .host import Host
from .mounts import validate_mount_authority
from .project import ProjectDefinition, ProjectTeam
from .resources import package_root
from .state import StateStore
from .team import Team


def trusted_host_roots(
    host: Host,
    *,
    dcomp_executable: str | None = None,
) -> tuple[tuple[Path, str], ...]:
    trusted: list[tuple[Path, str]] = [
        (package_root(), "installed Cyclo controller"),
        (host.path, "Cyclo host configuration"),
        *(
            (
                provider.context,
                f"source of provider component {provider.name!r}",
            )
            for provider in host.providers
        ),
    ]
    if dcomp_executable is not None:
        trusted.append((Path(dcomp_executable), "DComp executable"))
    return tuple(trusted)


def validate_project_authority(
    store: StateStore,
    host: Host,
    definition: ProjectDefinition,
    teams: Iterable[tuple[ProjectTeam, Team]],
    *,
    docker_endpoint: str,
    dcomp_executable: str | None = None,
) -> None:
    validate_mount_authority(
        (
            (team.root, f"team {selected.name!r}")
            for selected, team in teams
        ),
        (
            (mount.path, f"mount {mount.name!r}")
            for mount in definition.mounts
        ),
        state_root=store.root,
        docker_endpoint=docker_endpoint,
        trusted_roots=trusted_host_roots(
            host,
            dcomp_executable=dcomp_executable,
        ),
    )
