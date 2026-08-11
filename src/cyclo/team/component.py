from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..dcomp_system import (
    Bind,
    Component,
    PublishedPort,
    component_name,
    provider_endpoint,
)
from ..errors import CycloError
from ..project_state import decode_instance_project
from ..state import Instance, StateStore


TEAM_DASHBOARD_PORT = 4137
AGENTWS_ROOT = "/agentws"
TEAM_ROOT = "/team"
PI_ROOT = "/home/cyclo/.pi"
PI_SETTINGS_TEMPLATE = "/opt/cyclo/pi-settings.json"


@dataclass(frozen=True)
class InstanceFiles:
    project_config: Path
    pi_settings: Path


def team_component_name(identifier: str) -> str:
    return component_name("team", identifier)


def materialize_instance(store: StateStore, instance: Instance) -> InstanceFiles:
    """Create only the mutable and instance-specific files a team consumes."""

    store.ensure()
    instance_root = store.instance_dir(instance.id)
    try:
        store.prepare_directory(instance_root, "Cyclo instance directory")
        for path in (
            store.queue_root(instance.id),
            store.tasks_dir(instance.id),
            store.jobs_dir(instance.id),
            store.agents_dir(instance.id),
            store.pi_root(instance.id),
        ):
            store.prepare_directory(path, "AgentWS state directory")
    except OSError as exc:
        raise CycloError(
            f"cannot prepare team state for {instance.id}: {exc}"
        ) from exc

    config = (
        instance_root
        / "project-config"
        / instance.project_generation
        / "project.cyclo"
    )
    store.prepare_directory(config.parent.parent, "project configuration directory")
    store.prepare_directory(config.parent, "project generation directory")
    _immutable_file(
        config,
        instance.project_config.rstrip().encode("utf-8") + b"\n",
        mode=0o444,
    )
    return InstanceFiles(
        project_config=config,
        pi_settings=_materialize_pi_settings(store, instance),
    )


def make_team_component(store: StateStore, instance: Instance) -> Component:
    """Compile one persisted team instance into its DComp component."""

    if not instance.image.startswith("sha256:"):
        raise CycloError(
            f"instance {instance.id!r} is not bound to an immutable team image"
        )
    project = decode_instance_project(instance).require_valid()
    files = materialize_instance(store, instance)
    team_root = Path(instance.team_path).resolve(strict=True)
    roster = Path(instance.team_roster)
    if roster.name != instance.team_roster:
        raise CycloError(f"invalid team roster for instance {instance.id!r}")
    binds = [
        Bind(store.tasks_dir(instance.id), f"{AGENTWS_ROOT}/tasks", False),
        Bind(store.jobs_dir(instance.id), f"{AGENTWS_ROOT}/jobs", False),
        Bind(store.agents_dir(instance.id), f"{AGENTWS_ROOT}/agents", False),
        Bind(files.project_config, f"{AGENTWS_ROOT}/project.cyclo", True),
        Bind(files.pi_settings, PI_SETTINGS_TEMPLATE, True),
        Bind(store.pi_root(instance.id), PI_ROOT, False),
        Bind(team_root, TEAM_ROOT, not instance.team_write),
    ]
    binds.extend(
        Bind(mount.path, str(mount.container_path), mount.read_only)
        for mount in project.mounts
    )
    arguments = [
        "python3",
        "/usr/local/bin/cyclo-team-runtime",
        "--roster",
        f"{TEAM_ROOT}/{instance.team_roster}",
        "--generation",
        instance.generation,
        "--project-generation",
        instance.project_generation,
    ]
    if instance.team_protocol:
        arguments.extend(("--team-protocol", f"{TEAM_ROOT}/AGENTS.md"))
    if instance.verbose:
        arguments.append("--verbose")
    ports = (
        ()
        if instance.offline
        else (
            PublishedPort(
                instance.agentws_host,
                instance.requested_port,
                TEAM_DASHBOARD_PORT,
            ),
        )
    )
    return Component(
        team_component_name(instance.id),
        instance.image,
        inputs=(provider_endpoint(),),
        binds=tuple(binds),
        arguments=tuple(arguments),
        ports=ports,
        egress=not instance.offline,
    )


def _materialize_pi_settings(store: StateStore, instance: Instance) -> Path:
    content = (
        json.dumps(
            {
                "defaultProvider": instance.pi_default_provider,
                "defaultModel": instance.pi_default_model,
                "defaultThinkingLevel": "xhigh",
                "packages": [
                    "npm:pi-web-access",
                    "npm:pi-lens",
                    "npm:pi-simplify",
                    (
                        "/opt/cyclo-agent-tools/lib/node_modules/"
                        "@earendil-works/pi-coding-agent/node_modules/"
                        "pi-safe-compact"
                    ),
                    "/opt/cyclo/team/pi",
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = (
        store.instance_dir(instance.id)
        / "runtime-config"
        / digest
        / "pi-settings.json"
    )
    store.prepare_directory(path.parent.parent, "team runtime configuration directory")
    store.prepare_directory(path.parent, "team runtime generation directory")
    _immutable_file(path, content, mode=0o444)
    return path


def _immutable_file(path: Path, content: bytes, *, mode: int) -> None:
    try:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise CycloError(f"invalid immutable runtime file: {path}")
            if path.read_bytes() != content:
                raise CycloError(
                    f"immutable runtime file has unexpected content: {path}"
                )
            return
        temporary = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except CycloError:
        raise
    except OSError as exc:
        raise CycloError(f"cannot materialize runtime file {path}: {exc}") from exc
