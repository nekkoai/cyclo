from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .dashboard import dashboard_host_is_loopback
from .docker import ContainerSpec, Docker, overlaps, overlaps_lexically
from .errors import CycloError
from .instance_lifecycle import (
    active_instances,
    attach_active_networks,
    rotate_client_tokens,
    stop_remove_instance_container,
    token_rotation_failure,
)
from .project import (
    ProjectDefinition,
    ProjectMount,
    ProjectTeam,
    render_project_manifest,
)
from .project_state import decode_instance_project, encode_project_mounts
from .provider_service import ProviderService
from .runtime_container import (
    provider_runtime_container_name,
    provider_runtime_health_url,
)
from .state import Instance, StateStore, instance_id, validate_instance_id
from .team import (
    Team,
    load_team,
    require_team_repository,
    resolve_directory,
    team_generation,
)
from .team_runtime_image import ensure as ensure_team_runtime_image


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
    project: Path,
    *,
    identifier: str | None = None,
    team_write: bool | None = None,
    definition: ProjectDefinition | None = None,
) -> Instance:
    identifier = identifier or instance_id(team.root, project, args.name)
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
        team_write=args.team_write if team_write is None else team_write,
        offline=args.offline,
        agentws_host=args.host,
        active=True,
        project_name=definition.name if definition is not None else project.name,
        project_file=(str(definition.path.resolve()) if definition else ""),
        project_description=definition.description if definition else "",
        project_generation=definition.definition_sha256 if definition else "",
        project_mounts=(
            encode_project_mounts(definition.mounts) if definition else []
        ),
        launch_id="" if args.dry_run else secrets.token_hex(16),
    )


@dataclass(frozen=True)
class SourceIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class RunBinding:
    team: Team
    project_root: Path
    instance: Instance
    manifest: str
    project_mounts: tuple[ProjectMount, ...] = ()
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


def _binding_mount_sources(binding: RunBinding) -> tuple[tuple[Path, str, str], ...]:
    sources = [(binding.team.root, "team", f"team {binding.team.name!r}")]
    if binding.project_mounts:
        sources.extend(
            (mount.path, "project", f"mount {mount.name!r}")
            for mount in binding.project_mounts
        )
    else:
        sources.append((binding.project_root, "project", "project root"))
    return tuple(sources)


def _stored_path(value: object, label: str, instance_id: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CycloError(f"invalid {label} path in instance state: {instance_id}")
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        raise CycloError(f"non-absolute {label} path in instance state: {instance_id}")
    try:
        return selected.resolve()
    except (OSError, RuntimeError) as exc:
        raise CycloError(
            f"cannot resolve {label} path in instance state {instance_id}: {exc}"
        ) from exc


def _stored_mount_sources(instance: Instance) -> tuple[tuple[Path, str, str], ...]:
    result = [
        (
            _stored_path(instance.team_path, "team", instance.id),
            "team",
            f"team of running instance {instance.id!r}",
        )
    ]
    project = decode_instance_project(instance).require_valid()
    if project.configured:
        result.extend(
            (
                _stored_path(str(mount.path), f"mount {mount.name!r}", instance.id),
                "project",
                f"mount {mount.name!r} of running instance {instance.id!r}",
            )
            for mount in project.mounts
        )
    else:
        assert project.path is not None
        result.append(
            (
                _stored_path(str(project.path), "project", instance.id),
                "project",
                f"project of running instance {instance.id!r}",
            )
        )
    return tuple(result)


def validate_running_mount_boundaries(
    binding: RunBinding, store: StateStore, docker: Docker
) -> None:
    selected_sources = _binding_mount_sources(binding)
    for running in store.list():
        if not docker.container_lifecycle_active(running.container_name):
            continue
        active_sources = _stored_mount_sources(running)
        for selected_path, selected_kind, selected_label in selected_sources:
            for active_path, active_kind, active_label in active_sources:
                if selected_path == active_path:
                    if selected_kind == active_kind:
                        continue
                    raise CycloError(
                        f"{selected_label} reuses {active_label} as a different "
                        f"mount kind: {selected_path}"
                    )
                if overlaps(selected_path, active_path) or overlaps_lexically(
                    selected_path, active_path
                ):
                    raise CycloError(
                        f"{selected_label} overlaps {active_label}: "
                        f"{selected_path} and {active_path}"
                    )


def load_project_teams(
    definition: ProjectDefinition,
) -> tuple[tuple[ProjectTeam, Team], ...]:
    result = []
    for selected in definition.teams:
        team = load_team(selected.path)
        require_team_repository(team)
        result.append((selected, team))
    return tuple(result)


def legacy_project_manifest(team: Team, project: Path, *, team_write: bool) -> str:
    team_mode = "read-write" if team_write else "read-only"
    return (
        "# Cyclo project\n\n"
        f"Name: {project.name}\n"
        "Description: Direct TEAM PROJECT compatibility run.\n\n"
        "The current working directory is /workspace.\n\n"
        "## Writable workspace mounts\n\n"
        "- /workspace (read-write)\n\n"
        "## Read-only mounts\n\n"
        "- none\n\n"
        "## Team definition\n\n"
        f"- /team ({team_mode}; {team.name})\n"
    )


def validate_run_options(
    args: argparse.Namespace, *, project_file: bool, team_count: int
) -> None:
    if args.port < 0 or args.port > 65535:
        raise CycloError("port must be 0 or an integer from 1 to 65535")
    if args.offline and args.port:
        raise CycloError(
            "--port cannot be used with --offline because no host UI is published"
        )
    dashboard_host_is_loopback(args.host)
    if project_file:
        forbidden = [
            option
            for enabled, option in (
                (args.name, "--name"),
                (args.team_write, "--team-write"),
            )
            if enabled
        ]
        if forbidden:
            raise CycloError(
                f"{' '.join(forbidden)} cannot be used with project.cyclo; "
                "declare team and mount modes in the project file"
            )
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
        instance = new_instance(
            args,
            team,
            definition.path.parent,
            identifier=identifier,
            team_write=selected.writable,
            definition=definition,
        )
        result.append(
            RunBinding(
                team=team,
                project_root=definition.path.parent,
                instance=instance,
                manifest=render_project_manifest(definition, team=selected),
                project_mounts=definition.mounts,
                source_identities=capture_source_identities(
                    (team.root, *(mount.path for mount in definition.mounts))
                ),
            )
        )
    return tuple(result)


def legacy_run_binding(args: argparse.Namespace) -> RunBinding:
    team = load_team(args.definition)
    require_team_repository(team)
    project = resolve_directory(args.project, "project")
    instance = new_instance(args, team, project)
    return RunBinding(
        team=team,
        project_root=project,
        instance=instance,
        manifest=legacy_project_manifest(
            team, project, team_write=instance.team_write
        ),
        source_identities=capture_source_identities((team.root, project)),
    )


def container_spec(
    binding: RunBinding, store: StateStore, args: argparse.Namespace
) -> ContainerSpec:
    instance = binding.instance
    named = bool(binding.project_mounts)
    return ContainerSpec(
        instance=instance,
        team=binding.team,
        project=binding.project_root,
        runtime_root=store.runtime_root(instance.id),
        tasks_dir=store.tasks_dir(instance.id),
        jobs_dir=store.jobs_dir(instance.id),
        agents_dir=store.agents_dir(instance.id),
        pi_root=store.pi_root(instance.id),
        port=args.port,
        provider_runtime_health_url=provider_runtime_health_url(
            provider_runtime_container_name(store.provider_runtime_root)
        ),
        verbose=args.verbose,
        project_mounts=binding.project_mounts,
        workspace_layout=store.workspace_root(instance.id) if named else None,
        readonly_layout=store.readonly_root(instance.id) if named else None,
    )


def binding_matches(previous: Instance, binding: RunBinding) -> bool:
    if Path(previous.team_path).resolve() != binding.team.root:
        return False
    if not binding.instance.project_file:
        return not previous.project_file and (
            Path(previous.project_path).resolve() == binding.project_root
        )
    return bool(previous.project_file) and (
        Path(previous.project_file).resolve()
        == Path(binding.instance.project_file).resolve()
    )


def preflight_binding(binding: RunBinding, store: StateStore, docker: Docker) -> None:
    instance = binding.instance
    verify_source_identities(binding)
    state = docker.container_lifecycle_state(instance.container_name)
    if state.lifecycle_active:
        raise CycloError(
            f"Cyclo instance is already active ({state.value}): {instance.id}"
        )
    if store.metadata_path(instance.id).is_file():
        previous = store.load(instance.id)
        if not binding_matches(previous, binding):
            raise CycloError(
                f"instance name {instance.id!r} is already bound to a "
                "different team or project"
            )
    validate_running_mount_boundaries(binding, store, docker)


def start_binding(
    args: argparse.Namespace,
    binding: RunBinding,
    source: Path,
    store: StateStore,
    runtime: ProviderService,
    docker: Docker,
    *,
    build: bool,
) -> None:
    instance = binding.instance
    spec = container_spec(binding, store, args)
    with store.locked():
        preflight_binding(binding, store, docker)
        store.materialize_agentws(
            instance.id,
            source / "template",
            Path(__file__).with_name("container_runtime.py"),
            project_manifest=binding.manifest,
        )
        if binding.project_mounts:
            store.materialize_workspace_layout(
                instance.id,
                [mount.name for mount in binding.project_mounts if mount.writable],
            )
            store.materialize_readonly_layout(
                instance.id,
                [mount.name for mount in binding.project_mounts if mount.read_only],
            )
        stale: list[Instance] = []
        try:
            store.save(instance)
            running = active_instances(store, docker, candidate=instance, stale=stale)
            ensure_team_runtime_image(instance.image, build=build)
            runtime.rotate_client_token(instance.id)
            attach_active_networks(docker, runtime, running)
            runtime.prepare_instance(instance, binding.team, running)
            rotation_errors = rotate_client_tokens(
                runtime, [item.id for item in stale]
            )
            if rotation_errors:
                raise token_rotation_failure(rotation_errors)
            verify_source_identities(binding)
            validate_running_mount_boundaries(binding, store, docker)
            instance.port = docker.start(spec)
            docker.wait_ready(
                instance.container_name,
                instance.port,
                host=instance.agentws_host,
            )
            store.save(instance)
        except BaseException as start_error:
            instance.active = False
            instance.port = None
            cleanup_errors: list[str] = []
            try:
                store.save(instance)
            except Exception as cleanup_error:
                cleanup_errors.append(f"inactive state rollback failed: {cleanup_error}")
            try:
                stale = []
                remaining = active_instances(store, docker, stale=stale)
                network_error: Exception | None = None
                try:
                    attach_active_networks(docker, runtime, remaining)
                except Exception as exc:
                    network_error = exc
                runtime.update_clients(remaining)
                rotation_errors = rotate_client_tokens(
                    runtime, [instance.id, *[item.id for item in stale]]
                )
                if network_error is not None:
                    raise CycloError(
                        "active network repair failed after capability "
                        f"revocation: {network_error}"
                    ) from network_error
                if rotation_errors:
                    cleanup_errors.append(str(token_rotation_failure(rotation_errors)))
            except Exception as cleanup_error:
                cleanup_errors.append(
                    f"runtime capability rollback failed: {cleanup_error}"
                )
            try:
                stop_remove_instance_container(
                    docker,
                    instance,
                    expected_launch_id=instance.launch_id or None,
                )
            except Exception as cleanup_error:
                cleanup_errors.append(f"container rollback failed: {cleanup_error}")
            try:
                docker.remove_network(instance.network_name, runtime.container_name)
            except Exception as cleanup_error:
                cleanup_errors.append(f"network rollback failed: {cleanup_error}")
            if cleanup_errors:
                reason = str(start_error) or type(start_error).__name__
                raise CycloError(
                    f"Cyclo instance {instance.id!r} failed to start ({reason}); "
                    "rollback incomplete: " + "; ".join(cleanup_errors)
                ) from start_error
            raise
