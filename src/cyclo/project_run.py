from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping

from .dashboard import dashboard_host_is_loopback
from .docker import (
    ContainerSpec,
    Docker,
    DockerContainerState,
    overlaps,
    overlaps_lexically,
)
from .errors import CycloError
from .instance_lifecycle import stop_remove_instance_container
from .installation import (
    derived_team_image_name,
    team_container_name,
    team_network_name,
)
from .project import (
    ProjectDefinition,
    ProjectMount,
    ProjectTeam,
    render_container_project,
)
from .pi_runtime import model_incompatibility
from .project_state import decode_instance_project, encode_project_mounts
from .state import Instance, StateStore, validate_instance_id
from .team import (
    Team,
    load_team,
    require_team_repository,
    team_generation,
)
from .team_runtime_image import PI_PACKAGES


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
    identifier: str,
    system: str,
    image: str,
    image_override: str,
    team_write: bool,
    definition: ProjectDefinition,
) -> Instance:
    return Instance(
        id=identifier,
        team_name=team.name,
        team_path=str(team.root),
        project_path=str(project),
        generation=team_generation(team),
        providers=list(team.providers),
        models=sorted({agent.model for agent in team.agents}),
        container_name=team_container_name(system, identifier),
        network_name=team_network_name(system, identifier),
        image=image,
        image_override=image_override,
        team_write=team_write,
        offline=args.offline,
        verbose=args.verbose,
        agentws_host=args.host,
        intent="running",
        requested_port=args.port,
        port=None,
        project_name=definition.name,
        project_file=str(definition.path.resolve()),
        project_description=definition.description,
        project_generation=definition.definition_sha256,
        project_mounts=encode_project_mounts(definition.mounts),
        launch_id=secrets.token_hex(16),
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
    project_config: str
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
        if not docker.container_lifecycle_active(running, system=store.system):
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
    dashboard_host_is_loopback(args.host)
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
    system: str,
    base_image: str,
    version: str,
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
        image_override = args.image or ""
        image = (
            image_override
            or (
                derived_team_image_name(system, version, team.root, team.name)
                if team.dockerfile is not None
                else base_image
            )
        )
        instance = new_instance(
            args,
            team,
            definition.path.parent,
            identifier=identifier,
            system=system,
            image=image,
            image_override=image_override,
            team_write=selected.writable,
            definition=definition,
        )
        result.append(
            RunBinding(
                team=team,
                project_root=definition.path.parent,
                instance=instance,
                project_config=render_container_project(
                    definition,
                    team=selected,
                ),
                project_mounts=definition.mounts,
                source_identities=capture_source_identities(
                    (team.root, *(mount.path for mount in definition.mounts))
                ),
            )
        )
    return tuple(result)


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
        provider_socket_dir=Path(instance.provider_socket_path).parent,
        system=store.system,
        port=args.port,
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


def preflight_binding(
    binding: RunBinding, store: StateStore, docker: Docker
) -> Instance | None:
    instance = binding.instance
    verify_source_identities(binding)
    state = docker.previous_launch_lifecycle_state(
        instance, system=store.system
    )
    if state.lifecycle_active:
        raise CycloError(
            f"Cyclo instance is already active ({state.value}): {instance.id}"
        )
    previous = None
    if store.metadata_path(instance.id).is_file():
        previous = store.load(instance.id)
        if previous.intent == "deleting":
            raise CycloError(
                f"Cyclo instance is being deleted: {instance.id}; "
                "run cyclo repair before reusing the name"
            )
        if not binding_matches(previous, binding):
            raise CycloError(
                f"instance name {instance.id!r} is already bound to a "
                "different team or project"
            )
    elif state is not DockerContainerState.ABSENT:
        raise CycloError(
            f"Cyclo container exists without instance state: "
            f"{instance.container_name}"
        )
    validate_running_mount_boundaries(binding, store, docker)
    return previous


def validate_pi_team_models(
    team: Team,
    catalogue: Mapping[str, object],
) -> None:
    raw_models = catalogue.get("models")
    if not isinstance(raw_models, list):
        raise CycloError("provider system returned an invalid model catalogue")
    models = {
        model.get("id"): model
        for model in raw_models
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    }
    for agent in team.agents:
        model = models.get(agent.model)
        if model is None:
            raise CycloError(
                f"agent {agent.name} requests unavailable provider model "
                f"{agent.model!r}"
            )
        incompatibility = model_incompatibility(model)
        if incompatibility:
            raise CycloError(
                f"agent {agent.name} requests provider model {agent.model!r} "
                f"that is incompatible with the {agent.engine} runtime: "
                f"{incompatibility}"
            )


def materialize_pi_settings(
    store: StateStore,
    instance: Instance,
    team: Team,
) -> Path:
    """Publish only Pi's local defaults; authority comes from the socket mount."""

    target = store.pi_root(instance.id)
    temporary = store.new_tree(target)
    try:
        agent_dir = temporary / "agent"
        agent_dir.mkdir(mode=0o700)
        first = team.agents[0]
        settings = {
            "defaultProvider": first.provider,
            "defaultModel": first.model_id,
            "defaultThinkingLevel": "xhigh",
            "packages": list(PI_PACKAGES),
        }
        settings_path = agent_dir / "settings.json"
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(settings_path, 0o600)
        store.replace_tree(temporary, target)
    except Exception:
        store._remove_tree(temporary)
        raise
    return target


def configure_team_network(
    docker: Docker,
    instance: Instance,
    *,
    system: str,
) -> None:
    """Prepare the Docker network boundary for one team launch."""

    if instance.offline:
        # Offline teams use Docker's network namespace isolation directly.
        # Remove an empty network left by an older launch, but never create
        # or attach a bridge for this launch.
        docker.remove_network(instance.network_name, instance.id, system=system)
    else:
        docker.ensure_network(instance.network_name, instance.id, system=system)


def start_binding_locked(
    args: argparse.Namespace,
    binding: RunBinding,
    source: Path,
    store: StateStore,
    docker: Docker,
) -> None:
    """Start one team while the caller holds the installation control lock."""

    instance = binding.instance
    spec = container_spec(binding, store, args)
    previous = preflight_binding(binding, store, docker)
    launch_persisted = False
    try:
        # The desired launch is durable before Docker can create or start it.
        # A killed controller therefore leaves either the previous stopped
        # launch or a complete running intent that `cyclo repair` can finish.
        if not store.instance_dir(instance.id).exists():
            store.save(instance)
            launch_persisted = True
        elif previous is not None:
            docker.remove_inactive_launch(
                previous.container_name,
                previous.id,
                expected_system=store.system,
                expected_launch=previous.launch_id,
            )
        store.materialize_agentws(
            instance.id,
            source / "template",
            Path(__file__).with_name("container_runtime.py"),
            project_config=binding.project_config,
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
        materialize_pi_settings(store, instance, binding.team)
        if not launch_persisted:
            assert previous is not None
            store.save(instance)
            launch_persisted = True
        configure_team_network(docker, instance, system=store.system)
        verify_source_identities(binding)
        validate_running_mount_boundaries(binding, store, docker)
        instance.port = docker.start(spec)
        docker.wait_ready(
            instance,
            instance.port,
            system=store.system,
            host=instance.agentws_host,
        )
        store.save(instance)
    except BaseException as start_error:
        if not launch_persisted:
            raise
        # Failure does not rewrite operator intent. Remove whatever part of the
        # exact launch became visible; a later run/repair retries the durable
        # running intent.
        cleanup_errors: list[str] = []
        try:
            stop_remove_instance_container(
                docker,
                instance,
                system=store.system,
            )
        except Exception as cleanup_error:
            cleanup_errors.append(f"container rollback failed: {cleanup_error}")
        try:
            docker.remove_network(
                instance.network_name, instance.id, system=store.system
            )
        except Exception as cleanup_error:
            cleanup_errors.append(f"network rollback failed: {cleanup_error}")
        if cleanup_errors:
            reason = str(start_error) or type(start_error).__name__
            raise CycloError(
                f"Cyclo instance {instance.id!r} failed to start ({reason}); "
                "launch cleanup incomplete: " + "; ".join(cleanup_errors)
            ) from start_error
        raise
