from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from . import __version__
from .authority import validate_project_authority
from .dcomp import DCompComponentStatus, DCompStatus
from .errors import CycloError
from .gateway_admin import GatewayAdmin
from .host import load_host
from .installation import SYSTEM_HOST_CONFIG, local_state_root
from .project import (
    ProjectDefinition,
    ProjectTeam,
    load_project,
    project_context_marker,
    read_project_context,
    render_container_project,
)
from .project_run import (
    RunBinding,
    capture_source_identities,
    load_project_teams,
    project_instance_id,
    project_run_bindings,
    validate_run_options,
    verify_source_identities,
)
from .project_state import decode_instance_project, encode_project_mounts
from .provider_client import list_models
from .runtime import CycloRuntime
from .state import Instance, StateStore, slug
from .team import (
    Team,
    init_team,
    load_team,
    require_team_repository,
    team_generation,
    verify_agentws_runtime,
)
from .team.admin import TaskAdmin, read_task_specification
from .team.compatibility import validate_pi_team_models
from .team.resources import packaged_agentws_runtime
from .team.templates import bundled_team_template_names


DEFAULT_HOST_CONFIG = SYSTEM_HOST_CONFIG
DEFAULT_AGENTWS_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _Progress:
    """Report human-facing CLI work without contaminating command results."""

    def __init__(self, command: str) -> None:
        self.command = command

    def note(self, message: str) -> None:
        print(f"cyclo {self.command}: {message}", file=sys.stderr, flush=True)

    @contextmanager
    def step(self, operation: str) -> Iterator[None]:
        self.note(f"{operation}...")
        try:
            yield
        except Exception:
            self.note(f"{operation}: failed")
            raise
        self.note(f"{operation}: done")


def state_store(args: argparse.Namespace) -> StateStore:
    selected = getattr(args, "state_root", None)
    local = bool(getattr(args, "local", False))
    if local and selected:
        raise CycloError(
            "--local cannot be combined with --state-root or CYCLO_STATE_ROOT"
        )
    if local:
        return StateStore(
            local_state_root(),
            requested_host_config_scope="local",
            shared=False,
            allow_legacy_scope_migration=True,
        )
    root = Path(selected).expanduser().resolve() if selected else None
    return StateStore(
        root,
        requested_host_config_scope="local" if selected else "system",
        shared=False if selected else True,
    )


def host_config(store: StateStore) -> Path:
    if store.host_config_scope == "system":
        return DEFAULT_HOST_CONFIG
    if store.host_config_scope == "local":
        return store.root / "host.conf"
    raise CycloError("Cyclo realm has no host configuration scope")


def cyclo_runtime(
    args: argparse.Namespace,
    store: StateStore | None = None,
) -> CycloRuntime:
    selected = store or state_store(args)
    return CycloRuntime(selected, host_config(selected))


def agentws_root() -> Path:
    root = packaged_agentws_runtime()
    verify_agentws_runtime(root)
    return root


def _looks_like_project_file(value: str | os.PathLike[str]) -> bool:
    path = Path(value).expanduser()
    if path.is_dir():
        return False
    return path.is_file() or path.name == "project.cyclo" or path.suffix == ".cyclo"


def cmd_team_init(args: argparse.Namespace) -> int:
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


def cmd_team_templates(_args: argparse.Namespace) -> int:
    for name in bundled_team_template_names():
        print(name)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    if _looks_like_project_file(args.definition):
        definition = load_project(args.definition)
        teams = load_project_teams(definition)
        agentws_root()
        store = state_store(args)
        validate_project_authority(
            store,
            load_host(host_config(store)),
            definition,
            teams,
            docker_endpoint=store.observed_docker_endpoint,
        )
        print(f"project: {definition.name}")
        print(f"description: {definition.description}")
        print(f"definition: {definition.path}")
        print(f"generation: {definition.definition_sha256}")
        for selected, team in teams:
            print(
                f"team ({selected.mode}): {team.name} {team.root} "
                f"[{len(team.agents)} agents]"
            )
        for mount in definition.mounts:
            print(
                f"mount ({mount.mode}): {mount.name} {mount.path} "
                f"-> {mount.container_path}"
            )
        return 0

    team = load_team(args.definition)
    require_team_repository(team)
    agentws_root()
    print(f"team: {team.name}")
    print(f"repository: {team.root}")
    print(f"roster: {team.roster.name}")
    print(f"agents: {len(team.agents)}")
    print(f"providers: {', '.join(team.providers)}")
    print(f"generation: {team_generation(team)}")
    return 0


def _project_init_path(value: str) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = Path(os.path.abspath(selected))
    if selected.name != "project.cyclo" and selected.suffix != ".cyclo":
        raise CycloError(
            "project definition must be named project.cyclo or end in .cyclo"
        )
    return selected


def cmd_project_init(args: argparse.Namespace) -> int:
    destination = _project_init_path(args.definition)
    name = args.name or slug(
        (
            destination.parent.name
            if destination.name == "project.cyclo"
            else destination.stem
        ),
        64,
    )
    description = args.description or f"Cyclo project {name}."
    lines = [f"name {name}", f"description {description}"]
    if args.context_file:
        context = read_project_context(args.context_file)
        marker = project_context_marker(context)
        lines.extend(("", f"context <<{marker}", context, marker))
    lines.append("")
    for path, mode in args.team:
        _access_mode(mode, "team")
        lines.append(f"team {path} {mode}")
    lines.append("")
    for mount_name, path, mode in args.mount:
        _access_mode(mode, "mount")
        lines.append(f"mount {mount_name} {path} {mode}")
    content = "\n".join(lines) + "\n"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.init-{os.getpid()}-{secrets.token_hex(8)}"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o644,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            initialized = load_project(temporary)
            load_project_teams(initialized)
        except CycloError as exc:
            raise CycloError(
                str(exc).replace(str(temporary), str(destination))
            ) from exc
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise CycloError(
                f"refusing to overwrite project definition: {destination}"
            ) from exc
        except OSError as exc:
            raise CycloError(
                f"cannot create project definition {destination}: {exc}"
            ) from exc
        try:
            parent = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise CycloError(
                f"cannot synchronize project definition {destination}: {exc}"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    print(f"initialized Cyclo project: {destination}")
    return 0


def _access_mode(value: str, label: str) -> None:
    if value not in {"ro", "rw"}:
        raise CycloError(
            f"invalid {label} access mode {value!r}; expected ro or rw"
        )


def _catalogue(runtime: CycloRuntime, status: DCompStatus) -> dict[str, object]:
    catalogue = list_models(runtime.outer_port(status))
    models = catalogue.get("models")
    if not isinstance(models, list):
        raise CycloError("provider system returned an invalid model catalogue")
    return catalogue


def _catalogue_ids(catalogue: Mapping[str, object]) -> tuple[str, ...]:
    raw = catalogue.get("models")
    if not isinstance(raw, list):
        raise CycloError("provider system returned an invalid model catalogue")
    identifiers = sorted(
        {
            item["id"]
            for item in raw
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"]
        }
    )
    return tuple(identifiers)


def _prepare_run_bindings(
    args: argparse.Namespace,
    runtime: CycloRuntime,
    definition: ProjectDefinition,
    teams: tuple[tuple[ProjectTeam, Team], ...],
) -> tuple[RunBinding, ...]:
    images = {
        team.root: runtime.build_team(team, override=args.image or "").id
        for _selected, team in teams
    }
    # project_run_bindings performs the common project/instance derivation and
    # captures the mount identities. Each team's immutable image is selected
    # afterwards because projects may mix derived team images.
    placeholder = next(iter(images.values()))
    bindings = project_run_bindings(
        args,
        definition,
        teams,
        base_image=placeholder,
    )
    return tuple(
        replace(binding, instance=replace(binding.instance, image=images[binding.team.root]))
        for binding in bindings
    )


def _ensure_new_instances(
    bindings: Iterable[RunBinding],
    current: Sequence[Instance],
) -> None:
    existing = {instance.id for instance in current}
    collisions = sorted(
        binding.instance.id for binding in bindings if binding.instance.id in existing
    )
    if collisions:
        raise CycloError(
            "Cyclo instance already exists: "
            + ", ".join(collisions)
            + "; use `cyclo start`, `cyclo refresh`, or `cyclo forget`"
        )


def _announce_instance(
    runtime: CycloRuntime,
    instance: Instance,
    status: DCompStatus,
) -> None:
    print(f"{instance.id}: running")
    port = runtime.team_port(instance, status)
    if port is None:
        print("  AgentWS dashboard: disabled (--offline)")
    elif instance.agentws_host == "0.0.0.0":
        print(f"  AgentWS dashboard: port {port} (bound on all IPv4 interfaces)")
    else:
        print(f"  AgentWS dashboard: http://{instance.agentws_host}:{port}/")


def cmd_run(args: argparse.Namespace) -> int:
    definition = load_project(args.project)
    teams = load_project_teams(definition)
    validate_run_options(args, team_count=len(teams))
    agentws_root()
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("run")
    runtime.validate_project_mounts(definition, teams)
    with store.locked():
        current = store.list()
        expected_ids = {
            project_instance_id(definition, selected) for selected, _team in teams
        }
        existing = expected_ids.intersection(instance.id for instance in current)
        if existing:
            raise CycloError(
                "Cyclo instance already exists: "
                + ", ".join(sorted(existing))
                + "; use `cyclo start`, `cyclo refresh`, or `cyclo forget`"
            )

        # The provider system is independent of teams. Bring it to its declared
        # state first so model compatibility can be checked before intent is
        # committed.
        with progress.step("prepare and verify provider system"):
            baseline = runtime.apply(current)
            catalogue = _catalogue(runtime, baseline.status)
            for _selected, team in teams:
                validate_pi_team_models(team, catalogue)

        with progress.step(f"build {len(teams)} team image(s)"):
            bindings = _prepare_run_bindings(args, runtime, definition, teams)
        _ensure_new_instances(bindings, current)
        for binding in bindings:
            verify_source_identities(binding)
        runtime.validate_instances(
            (
                *(
                    instance
                    for instance in current
                    if instance.intent == "running"
                ),
                *(binding.instance for binding in bindings),
            )
        )
        with progress.step(f"start and verify {len(bindings)} team instance(s)"):
            store.save_many(binding.instance for binding in bindings)
            applied = runtime.apply(store.list())
            runtime.require_instances_ready(
                (binding.instance for binding in bindings),
                applied.status,
            )

    for binding in bindings:
        _announce_instance(runtime, binding.instance, applied.status)
    if args.foreground:
        if len(bindings) != 1:
            raise CycloError(
                "--foreground requires a project with exactly one team"
            )
        runtime.dcomp.logs(
            runtime.name,
            runtime.component_for_instance(bindings[0].instance.id),
            follow=True,
            output=sys.stdout,
        )
    return 0


def _refresh_instance(
    runtime: CycloRuntime,
    instance: Instance,
) -> tuple[Instance, Team]:
    definition = load_project(instance.project_file)
    configured = load_project_teams(definition)
    runtime.validate_project_mounts(definition, configured)
    candidates = [
        (selected, team)
        for selected, team in configured
        if project_instance_id(definition, selected) == instance.id
    ]
    if len(candidates) != 1:
        raise CycloError(
            f"project definition no longer selects instance {instance.id!r}"
        )
    selected, team = candidates[0]
    image = runtime.build_team(
        team,
        override=instance.image_override,
    )
    identities = capture_source_identities(
        (team.root, *(mount.path for mount in definition.mounts))
    )
    refreshed = replace(
        instance,
        team_name=team.name,
        team_path=str(team.root),
        generation=team_generation(team),
        models=sorted({agent.model for agent in team.agents}),
        image=image.id,
        team_write=selected.writable,
        team_roster=team.roster.name,
        team_protocol=team.protocol is not None,
        pi_default_provider=team.agents[0].provider,
        pi_default_model=team.agents[0].model_id,
        project_name=definition.name,
        project_file=str(definition.path.resolve()),
        project_description=definition.description,
        project_generation=definition.definition_sha256,
        project_config=render_container_project(definition, team=selected),
        project_mounts=encode_project_mounts(definition.mounts),
        runtime_version=__version__,
    )
    verify_source_identities(RunBinding(team, refreshed, identities))
    return refreshed, team


def cmd_refresh(args: argparse.Namespace) -> int:
    agentws_root()
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("refresh")
    with store.locked():
        current = store.list()
        running = tuple(
            instance for instance in current if instance.intent == "running"
        )
        progress.note(f"found {len(running)} running instance(s)")
        replacements = {}
        for instance in running:
            with progress.step(f"rebuild team image for {instance.id}"):
                replacements[instance.id] = _refresh_instance(runtime, instance)
        refreshed = [
            replacements[instance.id][0]
            if instance.id in replacements
            else instance
            for instance in current
        ]
        runtime.validate_instances(
            instance for instance in refreshed if instance.intent == "running"
        )
        # Refresh must not first compile stale team mounts or pruned team
        # images. Reconcile only the provider system, obtain its catalogue,
        # then publish and apply the complete replacement inventory.
        with progress.step("rebuild and verify provider system"):
            baseline = runtime.apply((), rebuild_host=True)
            catalogue = _catalogue(runtime, baseline.status)
            for _refreshed, team in replacements.values():
                validate_pi_team_models(team, catalogue)
        operation = f"apply and verify {len(running)} running instance(s)"
        with progress.step(operation):
            store.save_many(
                instance for instance in refreshed if instance.intent == "running"
            )
            applied = runtime.apply(store.list())
            runtime.require_instances_ready(
                (instance for instance in refreshed if instance.intent == "running"),
                applied.status,
            )
    print(
        f"refreshed {sum(item.intent == 'running' for item in refreshed)} "
        f"running instance(s); system operational={str(applied.status.operational).lower()}"
    )
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("start")
    with store.locked():
        instance = store.load(args.instance)
        if instance.intent == "running":
            raise CycloError(f"Cyclo instance is already desired running: {instance.id}")
        instance.intent = "running"
        with progress.step(f"start and verify instance {instance.id}"):
            store.save(instance)
            applied = runtime.apply(store.list())
            runtime.require_instances_ready((instance,), applied.status)
    _announce_instance(runtime, instance, applied.status)
    return 0


def _stop_targets(
    target: str,
    store: StateStore,
) -> tuple[Instance, ...]:
    if _looks_like_project_file(target):
        definition = load_project(target)
        identifiers = {
            project_instance_id(definition, selected) for selected in definition.teams
        }
        if not identifiers:
            raise CycloError("project has no team instances")
        return tuple(store.load(identifier) for identifier in sorted(identifiers))
    return (store.load(target),)


def cmd_stop(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("stop")
    with store.locked():
        targets = _stop_targets(args.target, store)
        changed = []
        for instance in targets:
            if instance.intent == "running":
                instance.intent = "stopped"
                changed.append(instance)
        with progress.step("apply stopped instance state"):
            store.save_many(changed)
            runtime.apply(store.list())
    changed_ids = {instance.id for instance in changed}
    for instance in changed:
        print(f"{instance.id}: stopped")
    for instance in targets:
        if instance.id not in changed_ids:
            print(f"{instance.id}: already stopped")
    return 0


def cmd_shutdown(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("shutdown")
    with store.locked(bind_host_config=False):
        with progress.step("remove all realm runtime components"):
            runtime.shutdown()
    print(f"shut down Cyclo realm: {store.root}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    if args.confirm != args.instance:
        raise CycloError("confirmation must exactly match the instance ID")
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    with store.locked():
        instance = store.load(args.instance)
        if instance.intent != "stopped":
            raise CycloError(
                f"refusing to forget running Cyclo instance: {instance.id}"
            )
        # First make the DComp system prove this stopped instance is absent.
        runtime.apply(store.list())
        store.remove(instance.id)
    print(f"forgot Cyclo instance: {instance.id}")
    return 0


def _component_condition(component: DCompComponentStatus | None) -> tuple[str, str]:
    if component is None:
        return "absent", "component is not in the applied DComp system"
    if component.status == "running" and component.health == "healthy":
        return "ready", ""
    detail = component.problem or (
        f"container={component.status}, health={component.health}"
    )
    return "not-ready", detail


def _running_instance_condition(
    runtime: CycloRuntime,
    status: DCompStatus,
    component: DCompComponentStatus | None,
    *,
    check_provider: bool = True,
) -> tuple[str, str]:
    if status.operation:
        phase = f" ({status.phase})" if status.phase else ""
        return (
            "not-ready",
            f"DComp operation {status.operation!r} is pending{phase}",
        )
    if not check_provider:
        return _component_condition(component)
    provider = status.component(runtime.host.outer_component)
    provider_condition, provider_detail = _component_condition(provider)
    if provider_condition != "ready":
        return (
            "not-ready",
            f"provider {runtime.host.outer_component!r}: "
            f"{provider_detail or provider_condition}",
        )
    return _component_condition(component)


def _safe_status(runtime: CycloRuntime) -> DCompStatus:
    return runtime.status()


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    text = [[str(value) for value in row] for row in rows]
    widths = [
        max((len(row[index]) for row in text), default=len(headers[index]))
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    for row in text:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def cmd_ps(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    instances, errors = store.list_report()
    try:
        status = _safe_status(runtime)
    except CycloError as exc:
        status = None
        errors.append(f"DComp status unavailable: {exc}")
    try:
        runtime.host
    except CycloError as exc:
        provider_check = False
        errors.append(f"host configuration unavailable: {exc}")
    else:
        provider_check = True
    rows = []
    for instance in instances:
        component = (
            status.component(runtime.component_for_instance(instance.id))
            if status is not None
            else None
        )
        if status is None:
            condition, _detail = "unknown", "DComp status unavailable"
        elif instance.intent == "running":
            condition, _detail = _running_instance_condition(
                runtime,
                status,
                component,
                check_provider=provider_check,
            )
        else:
            condition, _detail = _component_condition(component)
        port = "-"
        if (
            status is not None
            and instance.intent == "running"
            and condition == "ready"
        ):
            selected = runtime.team_port(instance, status)
            port = str(selected) if selected is not None else "offline"
        rows.append(
            (
                instance.id,
                instance.team_name,
                instance.project_name,
                instance.intent,
                condition,
                port,
            )
        )
    _print_table(
        ("INSTANCE", "TEAM", "PROJECT", "DESIRED", "STATUS", "PORT"),
        rows,
    )
    for error in errors:
        print(f"warning: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    instance = store.load(args.instance)
    try:
        status = runtime.status()
    except CycloError as exc:
        status = None
        component = None
        condition, problem = "unknown", f"DComp status unavailable: {exc}"
    else:
        component = status.component(runtime.component_for_instance(instance.id))
        try:
            runtime.host
        except CycloError as exc:
            provider_check = False
            provider_problem = f"host configuration unavailable: {exc}"
        else:
            provider_check = True
            provider_problem = ""
        if instance.intent == "running":
            condition, problem = _running_instance_condition(
                runtime,
                status,
                component,
                check_provider=provider_check,
            )
        else:
            condition, problem = _component_condition(component)
        if provider_problem:
            problem = "; ".join(
                detail for detail in (problem, provider_problem) if detail
            )
    project = decode_instance_project(instance).require_valid()
    report = {
        "id": instance.id,
        "team": instance.team_name,
        "team_repository": instance.team_path,
        "project": project.dashboard_value(),
        "desired": instance.intent,
        "status": condition,
        "problem": problem,
        "component": runtime.component_for_instance(instance.id),
        "container_id": component.container_id if component else "",
        "image": instance.image,
        "models": instance.models,
        "offline": instance.offline,
        "team_write": instance.team_write,
        "agentws_state": str(store.queue_root(instance.id)),
        "created_at": instance.created_at,
        "updated_at": instance.updated_at,
    }
    if status is not None and component and condition == "ready":
        report["agentws_port"] = runtime.team_port(instance, status)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    store.load(args.instance)
    runtime.dcomp.logs(
        runtime.name,
        runtime.component_for_instance(args.instance),
        follow=args.follow,
        output=sys.stdout,
    )
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    store = state_store(args)
    store.load(args.instance)
    print(store.queue_root(args.instance))
    return 0


def _validate_task_id(value: str) -> None:
    if not _TASK_ID_RE.fullmatch(value):
        raise CycloError(
            "task ID must start with a letter or number and contain only "
            "letters, numbers, dot, underscore, and hyphen"
        )


def _validate_job_id(value: str) -> None:
    if not _TASK_ID_RE.fullmatch(value):
        raise CycloError(
            "job ID must start with a letter or number and contain only "
            "letters, numbers, dot, underscore, and hyphen"
        )


def _validate_role(value: str) -> None:
    if not _TASK_ID_RE.fullmatch(value):
        raise CycloError(
            "role must start with a letter or number and contain only "
            "letters, numbers, dot, underscore, and hyphen"
        )


def _run_task_tool(
    args: argparse.Namespace,
    tool: str,
    arguments: Sequence[str] = (),
    *,
    specification: bytes | None = None,
) -> tuple[int, Instance]:
    store = state_store(args)
    with store.locked():
        instance = store.load(args.instance)
        result = TaskAdmin(store, instance).run(
            tool,
            arguments,
            specification=specification,
        )
    return result, instance


def cmd_task_list(args: argparse.Namespace) -> int:
    result, _instance = _run_task_tool(args, "task-list")
    return result


def cmd_task_show(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    result, _instance = _run_task_tool(args, "task-show", (args.task_id,))
    return result


def cmd_task_run(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    requested_spec = Path(args.spec).expanduser()
    specification = read_task_specification(requested_spec)
    result, instance = _run_task_tool(
        args,
        "task-create",
        (args.task_id,),
        specification=specification,
    )
    if result == 0:
        for line in _task_project_summary(instance):
            print(line)
    return result


def cmd_task_add_job(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    _validate_job_id(args.job_id)
    _validate_role(args.role)
    requested_spec = Path(args.spec).expanduser()
    specification = read_task_specification(requested_spec)
    result, _instance = _run_task_tool(
        args,
        "job-create",
        (
            args.job_id,
            "--role",
            args.role,
            "--task-id",
            args.task_id,
        ),
        specification=specification,
    )
    return result


def cmd_task_comment(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    result, _instance = _run_task_tool(
        args,
        "task-comment",
        (args.task_id, " ".join(args.message)),
    )
    return result


def cmd_task_state(args: argparse.Namespace) -> int:
    _validate_task_id(args.task_id)
    command = [args.task_id, args.task_state]
    if args.message:
        command.extend(("-m", args.message))
    result, _instance = _run_task_tool(args, "task-state", command)
    return result


def _task_project_summary(instance: Instance) -> tuple[str, ...]:
    project = decode_instance_project(instance).require_valid()
    writable = [
        f"  {mount.name}: {mount.container_path}"
        for mount in project.mounts
        if mount.writable
    ]
    readonly = [
        f"  {mount.name}: {mount.container_path}"
        for mount in project.mounts
        if mount.read_only
    ]
    return (
        f"project: {project.name}",
        "writable workspace mounts:",
        *(writable or ("  none",)),
        "read-only mounts:",
        *(readonly or ("  none",)),
    )


def cmd_models(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("models")
    with store.locked():
        with progress.step("prepare provider system and read catalogue"):
            applied = runtime.apply(store.list())
            identifiers = _catalogue_ids(_catalogue(runtime, applied.status))
    if not identifiers:
        raise CycloError(
            "provider system returned no models; run `cyclo gateway providers` "
            "and log in"
        )
    for identifier in identifiers:
        print(identifier)
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    progress = _Progress("usage")
    with store.locked():
        with progress.step("read gateway usage"):
            report = GatewayAdmin(runtime).usage()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _component_rows(
    names: Iterable[str],
    status: DCompStatus,
) -> list[tuple[str, str, str, str]]:
    rows = []
    for name in names:
        component = status.component(name)
        condition, detail = _component_condition(component)
        rows.append(
            (
                name,
                condition,
                component.health if component else "-",
                detail or "-",
            )
        )
    return rows


def _known_component_names(
    runtime: CycloRuntime,
    instances: Iterable[Instance],
    status: DCompStatus,
) -> tuple[str, ...]:
    names = {
        "gateway",
        *(
            runtime.component_for_instance(instance.id)
            for instance in instances
            if instance.intent == "running"
        ),
        *(component.name for component in status.components),
    }
    return tuple(sorted(names))


def cmd_component(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    action = args.component_action
    if action in {"list", "status"}:
        status = runtime.status()
        configured = _known_component_names(runtime, store.list(), status)
        names = (
            [args.name]
            if action == "status" and args.name
            else configured
        )
        if args.name and args.name not in configured:
            raise CycloError(f"DComp component not found: {args.name}")
        _print_table(
            ("COMPONENT", "STATUS", "HEALTH", "DETAIL"),
            _component_rows(names, status),
        )
        return 0
    if action == "logs":
        runtime.dcomp.logs(
            runtime.name,
            args.name,
            follow=args.follow,
            output=sys.stdout,
        )
        return 0
    if action == "restart":
        with store.locked():
            status = runtime.status()
            if status.component(args.name) is None:
                raise CycloError(f"DComp component not found: {args.name}")
            runtime.dcomp.restart(runtime.name, args.name)
        print(f"{args.name}: restarted")
        return 0
    raise CycloError(f"unsupported component action: {action}")


def cmd_providers(args: argparse.Namespace) -> int:
    store = state_store(args)
    action = args.providers_action
    if action == "check":
        host = load_host(host_config(store))
        names = [provider.name for provider in host.providers]
        print(f"host configuration: {host.path}")
        print(f"provider components: {len(names)}")
        for name in names:
            print(name)
        return 0
    runtime = cyclo_runtime(args, store)
    names = [provider.name for provider in runtime.host.providers]
    if action == "status":
        status = runtime.status()
        _print_table(
            ("PROVIDER", "STATUS", "HEALTH", "DETAIL"),
            _component_rows(names, status),
        )
        return 0
    if action == "restart":
        if not names:
            print("no configured provider components")
            return 0
        with store.locked():
            runtime.apply(store.list())
            runtime.dcomp.restart(runtime.name, *names)
        print(f"restarted {len(names)} provider component(s)")
        return 0
    raise CycloError(f"unsupported providers action: {action}")


def _gateway_login_arguments(args: argparse.Namespace) -> list[str]:
    result = [args.provider]
    if args.account:
        result.extend(("--as", args.account))
    if args.api_key_env:
        result.extend(("--api-key-env", args.api_key_env))
    elif args.api_key_stdin:
        result.append("--api-key-stdin")
    return result


def cmd_gateway(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    admin = GatewayAdmin(runtime)
    action = args.gateway_action
    if action == "providers":
        with store.locked():
            print(admin.providers())
        return 0
    if action == "login":
        with store.locked():
            admin.login(_gateway_login_arguments(args))
        return 0
    if action == "logout":
        with store.locked():
            admin.logout(args.account)
        return 0
    if action == "rename":
        with store.locked():
            admin.rename(args.account, args.new_account)
        return 0
    if action == "status":
        status = runtime.status()
        _print_table(
            ("COMPONENT", "STATUS", "HEALTH", "DETAIL"),
            _component_rows(("gateway",), status),
        )
        try:
            volume = admin.credential_volume()
        except CycloError as exc:
            print(f"credential store: unavailable ({exc})")
            return 1
        print(f"credential store: {volume}")
        return 0
    if action == "restart":
        with store.locked():
            admin.restart()
        print("gateway: restarted")
        return 0
    if action == "build":
        with store.locked():
            applied = runtime.rebuild_gateway(store.list())
        condition, detail = _component_condition(applied.status.component("gateway"))
        print(f"gateway: {condition}" + (f" ({detail})" if detail else ""))
        return 0
    if action == "destroy-store":
        with store.locked():
            volume = admin.destroy_store(args.confirm)
        print(f"deleted gateway credential store: {volume}")
        return 0
    raise CycloError(f"unsupported gateway action: {action}")


def cmd_repair(args: argparse.Namespace) -> int:
    store = state_store(args)
    runtime = cyclo_runtime(args, store)
    with store.locked():
        applied = runtime.apply(store.list())
    print(
        f"{runtime.name}: "
        f"{'operational' if applied.status.operational else 'not operational'}"
    )
    return 0 if applied.status.operational else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    store = state_store(args)
    failures: list[str] = []
    instances, state_errors = store.list_report()
    failures.extend(state_errors)
    try:
        runtime = cyclo_runtime(args, store)
        version = runtime.dcomp.version()
        print(f"ok  dcomp {version.version} (API {version.api_version})")
        status = runtime.status()
    except CycloError as exc:
        failures.append(str(exc))
        status = None
        runtime = None

    if runtime is not None and status is not None:
        if not status.desired:
            failures.append("DComp system has not been applied")
        try:
            providers = tuple(
                provider.name for provider in runtime.host.providers
            )
            host_components = tuple(
                component.name for component in runtime.host.components
            )
            host_available = True
        except CycloError as exc:
            providers = ()
            host_components = ()
            host_available = False
            failures.append(f"host configuration unavailable: {exc}")
        expected = {
            "gateway",
            *providers,
            *host_components,
            *(
                runtime.component_for_instance(instance.id)
                for instance in instances
                if instance.intent == "running"
            ),
            *(component.name for component in status.components),
        }
        for name in sorted(expected):
            condition, detail = _component_condition(status.component(name))
            if condition == "ready":
                print(f"ok  component {name}")
            else:
                failures.append(f"component {name}: {detail or condition}")
        unexpected_teams = {
            runtime.component_for_instance(instance.id)
            for instance in instances
            if instance.intent == "stopped"
        }.intersection(component.name for component in status.components)
        failures.extend(
            f"stopped instance component is still present: {name}"
            for name in sorted(unexpected_teams)
        )
        if status.operational and host_available:
            try:
                count = len(_catalogue_ids(_catalogue(runtime, status)))
            except CycloError as exc:
                failures.append(f"model catalogue: {exc}")
            else:
                print(f"ok  model catalogue: {count} model(s)")

    for failure in failures:
        print(f"no  {failure}")
    return 1 if failures else 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import (
        DashboardSnapshot,
        dashboard_host_is_loopback,
        make_dashboard_server,
        packaged_dashboard_assets,
    )

    store = state_store(args)
    try:
        runtime = cyclo_runtime(args, store)
    except CycloError:
        runtime = None
    snapshot = DashboardSnapshot(store, runtime=runtime)
    server = make_dashboard_server(
        snapshot,
        host=args.host,
        port=args.port,
        static_assets=packaged_dashboard_assets(),
    )
    host, port = server.server_address[:2]
    if dashboard_host_is_loopback(host):
        print(f"Cyclo dashboard: http://{host}:{port}/", flush=True)
    else:
        scope = "all IPv4 interfaces" if host == "0.0.0.0" else f"interface {host}"
        print(
            f"Cyclo dashboard: bound on {scope}, port {port}",
            flush=True,
        )
    if not dashboard_host_is_loopback(host):
        print(
            "WARNING: dashboard is unauthenticated and exposed on a "
            "non-loopback address.",
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


def add_common_options(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--state-root",
        default=os.environ.get("CYCLO_STATE_ROOT"),
        help=(
            "private Cyclo realm directory using STATE_ROOT/host.conf"
        ),
    )
    selection.add_argument(
        "--local",
        action="store_true",
        help=(
            "use the current user's private XDG realm and its host.conf "
            "instead of the shared host realm"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclo",
        description="Run Git-defined agent teams through composable model providers",
        epilog=(
            "Everyday:  validate, run, start, stop, shutdown, ps, inspect, "
            "logs, task\n"
            "Authoring: team, project\n"
            "System:    models, usage, component, gateway, providers, doctor"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"cyclo {__version__}")
    add_common_options(parser)
    commands = parser.add_subparsers(required=True)

    team = commands.add_parser("team", help="create and inspect team repositories")
    team_commands = team.add_subparsers(dest="team_action", required=True)
    team_init = team_commands.add_parser("init", help="create a team repository")
    team_init.add_argument("team", help="new team-repository directory")
    team_init.add_argument("--model", required=True, help="initial provider/model")
    team_init.add_argument(
        "--template",
        choices=bundled_team_template_names(),
        help="bundled team template",
    )
    team_init.add_argument(
        "--no-git",
        action="store_true",
        help="do not initialize a Git repository",
    )
    team_init.set_defaults(func=cmd_team_init)
    templates = team_commands.add_parser(
        "templates",
        help="list bundled team templates",
    )
    templates.set_defaults(func=cmd_team_templates)

    project = commands.add_parser("project", help="create project definitions")
    project_commands = project.add_subparsers(dest="project_action", required=True)
    project_init = project_commands.add_parser(
        "init",
        help="create a project.cyclo from teams and mounts",
    )
    project_init.add_argument("definition", help="new project.cyclo path")
    project_init.add_argument("--name", help="project name")
    project_init.add_argument("--description", help="project description")
    project_init.add_argument(
        "--context",
        dest="context_file",
        metavar="FILE",
        help="embed project layout and source guidance",
    )
    project_init.add_argument(
        "--team",
        action="append",
        nargs=2,
        required=True,
        metavar=("PATH", "MODE"),
        help="team repository and ro/rw access; repeatable",
    )
    project_init.add_argument(
        "--mount",
        action="append",
        nargs=3,
        required=True,
        metavar=("NAME", "PATH", "MODE"),
        help="named mount and ro/rw access; repeatable",
    )
    project_init.set_defaults(func=cmd_project_init)

    validate = commands.add_parser("validate", help="validate a team or project")
    validate.add_argument("definition")
    validate.set_defaults(func=cmd_validate)

    run = commands.add_parser("run", help="run every team in a project")
    run.add_argument("project", help="project.cyclo")
    run.add_argument(
        "--image",
        default=os.environ.get("CYCLO_TEAM_IMAGE"),
        help="operator-supplied team image; bypass team Dockerfiles",
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help="block direct external network access while retaining provider links",
    )
    run.add_argument(
        "--host",
        default=DEFAULT_AGENTWS_HOST,
        help="AgentWS dashboard literal IPv4 bind address",
    )
    run.add_argument(
        "--port",
        type=int,
        default=0,
        help="AgentWS dashboard port; 0 asks Docker for a free port",
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        help="mirror rendered agent transcripts into component logs",
    )
    run.add_argument(
        "--foreground",
        action="store_true",
        help="follow logs after starting a single-team project",
    )
    run.set_defaults(func=cmd_run)

    refresh = commands.add_parser(
        "refresh",
        help="rebuild images and refresh every running instance",
    )
    refresh.set_defaults(func=cmd_refresh)
    start = commands.add_parser("start", help="start a stopped instance")
    start.add_argument("instance")
    start.set_defaults(func=cmd_start)
    stop = commands.add_parser("stop", help="stop an instance or project")
    stop.add_argument("target", help="instance ID or project.cyclo")
    stop.set_defaults(func=cmd_stop)
    shutdown = commands.add_parser(
        "shutdown",
        help="remove every runtime component in the selected realm",
    )
    shutdown.set_defaults(func=cmd_shutdown)
    forget = commands.add_parser(
        "forget",
        help="delete a stopped instance and its durable AgentWS state",
    )
    forget.add_argument("instance")
    forget.add_argument("--confirm", required=True, metavar="INSTANCE")
    forget.set_defaults(func=cmd_forget)
    ps = commands.add_parser("ps", help="list team instances")
    ps.set_defaults(func=cmd_ps)
    inspect = commands.add_parser("inspect", help="show one instance")
    inspect.add_argument("instance")
    inspect.set_defaults(func=cmd_inspect)
    logs = commands.add_parser("logs", help="show team component logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("instance")
    logs.set_defaults(func=cmd_logs)
    path = commands.add_parser("path", help="print AgentWS state path")
    path.add_argument("instance")
    path.set_defaults(func=cmd_path)

    task = commands.add_parser("task", help="inspect and control tasks")
    task_commands = task.add_subparsers(dest="task_action", required=True)
    task_list = task_commands.add_parser("list", help="list tasks")
    task_list.add_argument("instance")
    task_list.set_defaults(func=cmd_task_list)
    task_show = task_commands.add_parser("show", help="show a task")
    task_show.add_argument("instance")
    task_show.add_argument("task_id")
    task_show.set_defaults(func=cmd_task_show)
    task_run = task_commands.add_parser("run", help="create and start a task")
    task_run.add_argument("instance")
    task_run.add_argument("task_id")
    task_run.add_argument("spec")
    task_run.set_defaults(func=cmd_task_run)
    task_add_job = task_commands.add_parser(
        "add-job",
        help="add a role-routed job to an existing task",
    )
    task_add_job.add_argument("instance")
    task_add_job.add_argument("task_id")
    task_add_job.add_argument("job_id")
    task_add_job.add_argument("role")
    task_add_job.add_argument("spec")
    task_add_job.set_defaults(func=cmd_task_add_job)
    task_comment = task_commands.add_parser("comment", help="append a task comment")
    task_comment.add_argument("instance")
    task_comment.add_argument("task_id")
    task_comment.add_argument("message", nargs="+")
    task_comment.set_defaults(func=cmd_task_comment)
    for action, state in (("complete", "done"), ("reopen", "open")):
        selected = task_commands.add_parser(action, help=f"{action} a task")
        selected.add_argument("instance")
        selected.add_argument("task_id")
        selected.add_argument("-m", "--message")
        selected.set_defaults(func=cmd_task_state, task_state=state)

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the read-only fleet dashboard",
    )
    dashboard.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.set_defaults(func=cmd_dashboard)
    models = commands.add_parser("models", help="list available provider models")
    models.epilog = (
        "Before login, use `cyclo gateway providers` to list login providers."
    )
    models.set_defaults(func=cmd_models)
    usage = commands.add_parser("usage", help="show gateway usage")
    usage.set_defaults(func=cmd_usage)

    component = commands.add_parser(
        "component",
        help="inspect and control DComp components",
    )
    component_commands = component.add_subparsers(
        dest="component_action",
        required=True,
    )
    component_list = component_commands.add_parser("list", help="list components")
    component_list.set_defaults(func=cmd_component)
    component_status = component_commands.add_parser(
        "status",
        help="show all components or one named component",
    )
    component_status.add_argument("name", nargs="?")
    component_status.set_defaults(func=cmd_component)
    component_logs = component_commands.add_parser("logs", help="show component logs")
    component_logs.add_argument("-f", "--follow", action="store_true")
    component_logs.add_argument("name")
    component_logs.set_defaults(func=cmd_component)
    component_restart = component_commands.add_parser(
        "restart",
        help="restart one component",
    )
    component_restart.add_argument("name")
    component_restart.set_defaults(func=cmd_component)

    providers = commands.add_parser(
        "providers",
        help="inspect Provider components declared in host.conf",
    )
    provider_commands = providers.add_subparsers(
        dest="providers_action",
        required=True,
    )
    for action, help_text in (
        ("check", "validate host.conf and provider descriptors"),
        ("status", "show provider component status"),
        ("restart", "restart configured provider components"),
    ):
        selected = provider_commands.add_parser(action, help=help_text)
        selected.set_defaults(func=cmd_providers)

    gateway = commands.add_parser(
        "gateway",
        help="manage gateway credentials and service",
    )
    gateway_commands = gateway.add_subparsers(
        dest="gateway_action",
        required=True,
    )
    gateway_providers = gateway_commands.add_parser(
        "providers",
        help="list providers available for login",
    )
    gateway_providers.set_defaults(func=cmd_gateway)
    login = gateway_commands.add_parser(
        "login",
        help="store credentials for a provider account",
    )
    login.add_argument("provider")
    login.add_argument("--as", dest="account", help="catalogue account prefix")
    key = login.add_mutually_exclusive_group()
    key.add_argument(
        "--api-key-env",
        metavar="NAME",
        help="read API key from host environment variable NAME",
    )
    key.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read API key from standard input",
    )
    login.set_defaults(func=cmd_gateway)
    logout = gateway_commands.add_parser(
        "logout",
        help="remove one locally stored gateway account",
    )
    logout.add_argument("account")
    logout.set_defaults(func=cmd_gateway)
    rename = gateway_commands.add_parser(
        "rename",
        help="change the public name of a stored gateway account",
    )
    rename.add_argument("account", metavar="OLD_ACCOUNT")
    rename.add_argument("new_account", metavar="NEW_ACCOUNT")
    rename.set_defaults(func=cmd_gateway)
    for action, help_text in (
        ("status", "show gateway status"),
        ("restart", "restart the gateway component"),
        ("build", "force a gateway image build and apply it"),
    ):
        selected = gateway_commands.add_parser(action, help=help_text)
        selected.set_defaults(func=cmd_gateway)
    destroy = gateway_commands.add_parser(
        "destroy-store",
        help="irreversibly delete credentials and usage history",
    )
    destroy.add_argument("--confirm", required=True, metavar="VOLUME")
    destroy.set_defaults(func=cmd_gateway)

    repair = commands.add_parser(
        "repair",
        help="apply persisted host and instance intent",
    )
    repair.set_defaults(func=cmd_repair)
    doctor = commands.add_parser(
        "doctor",
        help="check the installed system without changing it",
    )
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    global_options: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            remaining.extend(argv[index:])
            break
        if argument == "--state-root":
            global_options.append(argument)
            index += 1
            if index >= len(argv):
                break
            global_options.append(argv[index])
        elif argument.startswith("--state-root="):
            global_options.append(argument)
        elif argument == "--local":
            global_options.append(argument)
        else:
            remaining.append(argument)
        index += 1
    return [*global_options, *remaining]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    selected = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_global_options(selected))
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
