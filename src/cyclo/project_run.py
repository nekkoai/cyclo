from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Collection

from . import __version__
from .errors import CycloError
from .project import (
    ProjectDefinition,
    ProjectTeam,
    render_container_project,
)
from .project_state import decode_instance_project, encode_project_mounts
from .state import Instance, StateStore, validate_instance_id
from .team import (
    Team,
    load_team,
    require_team_repository,
    team_generation,
)


def project_instance_id(project: ProjectDefinition, team: ProjectTeam) -> str:
    readable = f"{project.name}-{team.name.lower()}"
    if len(readable) <= 64:
        return validate_instance_id(readable)
    digest = hashlib.sha256(
        f"{project.path}\0{team.path}".encode("utf-8")
    ).hexdigest()[:8]
    return validate_instance_id(f"{readable[:55].rstrip('-._')}-{digest}")


def new_instance(
    args: argparse.Namespace,
    team: Team,
    *,
    identifier: str,
    image: str,
    image_override: str,
    team_write: bool,
    definition: ProjectDefinition,
    project_config: str,
) -> Instance:
    return Instance(
        id=identifier,
        team_name=team.name,
        team_path=str(team.root),
        generation=team_generation(team),
        models=sorted({agent.model for agent in team.agents}),
        image=image,
        image_override=image_override,
        team_write=team_write,
        offline=args.offline,
        verbose=args.verbose,
        agentws_host=args.host,
        intent="running",
        requested_port=args.port,
        team_roster=team.roster.name,
        team_protocol=team.protocol is not None,
        pi_default_provider=team.agents[0].provider,
        pi_default_model=team.agents[0].model_id,
        project_name=definition.name,
        project_file=str(definition.path.resolve()),
        project_description=definition.description,
        project_generation=definition.definition_sha256,
        project_config=project_config,
        project_mounts=encode_project_mounts(definition.mounts),
        runtime_version=__version__,
    )


@dataclass(frozen=True)
class SourceIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class RunBinding:
    team: Team
    instance: Instance
    source_identities: tuple[SourceIdentity, ...] = ()


def _source_identity(path: Path) -> SourceIdentity:
    try:
        metadata = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CycloError(f"cannot inspect mount source {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise CycloError(f"mount source is no longer a canonical directory: {path}")
    return SourceIdentity(path, metadata.st_dev, metadata.st_ino)


def capture_source_identities(paths: tuple[Path, ...]) -> tuple[SourceIdentity, ...]:
    return tuple(_source_identity(path) for path in paths)


def verify_source_identities(binding: RunBinding) -> None:
    for expected in binding.source_identities:
        try:
            actual = _source_identity(expected.path)
        except CycloError as exc:
            raise CycloError(
                f"mount source changed after validation: {expected.path}: {exc}"
            ) from exc
        if actual != expected:
            raise CycloError(
                f"mount source changed after validation: {expected.path}"
            )


def _stored_path(value: object, label: str, instance_id: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CycloError(f"invalid {label} path in instance state: {instance_id}")
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        raise CycloError(f"non-absolute {label} path in instance state: {instance_id}")
    try:
        canonical = Path(os.path.abspath(selected))
        resolved = canonical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CycloError(
            f"cannot resolve {label} path in instance state {instance_id}: {exc}"
        ) from exc
    if resolved != canonical or not resolved.is_dir():
        raise CycloError(
            f"{label} path in instance state is not a canonical directory: "
            f"{instance_id}"
        )
    return resolved


def instance_mount_sources(
    instance: Instance,
) -> tuple[tuple[Path, str, str], ...]:
    result = [
        (
            _stored_path(instance.team_path, "team", instance.id),
            "team",
            f"team of running instance {instance.id!r}",
        )
    ]
    project = decode_instance_project(instance).require_valid()
    result.extend(
        (
            _stored_path(str(mount.path), f"mount {mount.name!r}", instance.id),
            "project",
            f"mount {mount.name!r} of running instance {instance.id!r}",
        )
        for mount in project.mounts
    )
    return tuple(result)


def load_project_teams(
    definition: ProjectDefinition,
    *,
    instance_ids: Collection[str] | None = None,
) -> tuple[tuple[ProjectTeam, Team], ...]:
    selected_ids = set(instance_ids) if instance_ids is not None else None
    result = []
    for selected in definition.teams:
        if (
            selected_ids is not None
            and project_instance_id(definition, selected) not in selected_ids
        ):
            continue
        team = load_team(selected.path)
        require_team_repository(team)
        result.append((selected, team))
    if selected_ids is not None:
        loaded_ids = {
            project_instance_id(definition, selected)
            for selected, _team in result
        }
        missing = sorted(selected_ids - loaded_ids)
        if missing:
            raise CycloError(
                "project definition no longer provides recorded instance(s): "
                + ", ".join(missing)
            )
    return tuple(result)


def validate_run_options(args: argparse.Namespace, *, team_count: int) -> None:
    if args.port < 0 or args.port > 65535:
        raise CycloError("port must be 0 or an integer from 1 to 65535")
    if args.offline and args.port:
        raise CycloError(
            "--port cannot be used with --offline because no host UI is published"
        )
    try:
        ipaddress.IPv4Address(args.host)
    except ipaddress.AddressValueError as exc:
        raise CycloError("--host must be a literal IPv4 address") from exc
    if team_count > 1 and args.port:
        raise CycloError("--port is ambiguous for a project with multiple teams")
    if team_count > 1 and args.foreground:
        raise CycloError(
            "--foreground is ambiguous for a project with multiple teams; "
            "use `cyclo logs -f INSTANCE`"
        )


def project_run_bindings(
    args: argparse.Namespace,
    definition: ProjectDefinition,
    configured_teams: tuple[tuple[ProjectTeam, Team], ...],
    *,
    base_image: str,
) -> tuple[RunBinding, ...]:
    result: list[RunBinding] = []
    identifiers: set[str] = set()
    for selected, team in configured_teams:
        identifier = project_instance_id(definition, selected)
        if identifier in identifiers:
            raise CycloError(
                f"project teams produce duplicate instance ID {identifier!r}"
        )
        identifiers.add(identifier)
        project_config = render_container_project(
            definition,
            team=selected,
        )
        image_override = args.image or ""
        image = image_override or base_image
        instance = new_instance(
            args,
            team,
            identifier=identifier,
            image=image,
            image_override=image_override,
            team_write=selected.writable,
            definition=definition,
            project_config=project_config,
        )
        result.append(
            RunBinding(
                team=team,
                instance=instance,
                source_identities=capture_source_identities(
                    (team.root, *(mount.path for mount in definition.mounts))
                ),
            )
        )
    return tuple(result)
