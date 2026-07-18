from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .errors import CycloError
from .state import DEFAULT_AGENTWS_HOST, Instance
from .team import Team


CYCLO_LABEL = "cyclo.instance"
CONTAINER_AGENTWS = Path("/agentws")
CONTAINER_TEAM = Path("/team")
CONTAINER_WORKSPACE = Path("/workspace")
CONTAINER_PI = Path("/home/cyclo/.pi")
HOST_PSEUDO_FILESYSTEMS = (
    (Path("/proc"), "host process filesystem"),
    (Path("/sys"), "host system filesystem"),
    (Path("/dev"), "host device filesystem"),
    (Path("/run"), "host runtime filesystem"),
)
AGENTWS_RETRY_ENVIRONMENT = (
    "AGENTWS_MAX_JOB_ATTEMPTS",
    "AGENTWS_MAX_CONSECUTIVE_FAILURES",
    "AGENTWS_RETRY_INITIAL_SECONDS",
    "AGENTWS_RETRY_MAX_SECONDS",
)


def overlaps(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def overlaps_lexically(left: Path, right: Path) -> bool:
    """Compare mount names without following a final symlink.

    The resolved comparison protects a trusted target; this second comparison
    also protects the host-owned path name when it is missing or is itself a
    symlink inside an agent-writable tree.
    """

    left = Path(os.path.abspath(left.expanduser()))
    right = Path(os.path.abspath(right.expanduser()))
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def docker_socket_paths(
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return system and rootless Unix Docker sockets worth protecting."""

    env = os.environ if environment is None else environment
    candidates = [
        Path("/var/run/docker.sock"),
        Path(f"/run/user/{os.getuid()}/docker.sock"),
        Path.home() / ".docker" / "run" / "docker.sock",
    ]
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime).expanduser() / "docker.sock")
    configured = env.get("DOCKER_HOST")
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme == "unix" and parsed.path:
            candidates.append(Path(unquote(parsed.path)).expanduser())
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return tuple(result)


def validate_mount_boundaries(
    team: Path,
    project: Path,
    state_root: Path,
    host_pi_agent_dir: Path,
    trusted_roots: Iterable[tuple[Path, str]] = (),
) -> None:
    if overlaps(team, project):
        raise CycloError(f"team and project must be separate filesystem trees: {team} and {project}")
    protected: list[tuple[Path, str]] = [
        (state_root, "Cyclo state"),
        (host_pi_agent_dir, "host Pi credential/configuration directory"),
    ]
    protected.extend(HOST_PSEUDO_FILESYSTEMS)
    protected.extend((path, "Docker socket") for path in docker_socket_paths())
    protected.extend(trusted_roots)
    for source, label in protected:
        for mounted, mounted_label in ((team, "team"), (project, "project")):
            if overlaps(mounted, source) or overlaps_lexically(mounted, source):
                raise CycloError(f"{mounted_label} mount overlaps {label}: {mounted} and {source}")


def mount(source: Path, target: Path, mode: str = "rw") -> str:
    if "," in str(source):
        raise CycloError(f"Docker bind source cannot contain a comma: {source}")
    value = f"type=bind,src={source},dst={target}"
    if mode == "ro":
        value += ",readonly"
    return value


@dataclass(frozen=True)
class ContainerSpec:
    instance: Instance
    team: Team
    project: Path
    runtime_root: Path
    tasks_dir: Path
    jobs_dir: Path
    agents_dir: Path
    pi_root: Path
    port: int
    verbose: bool = False


def container_command(spec: ContainerSpec) -> list[str]:
    instance = spec.instance
    protocol = (
        CONTAINER_TEAM / "AGENTS.md"
        if spec.team.protocol is not None
        else CONTAINER_AGENTWS / "AGENTS.md"
    )
    roster = CONTAINER_TEAM / spec.team.roster.name
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        instance.container_name,
        "--label",
        f"{CYCLO_LABEL}={instance.id}",
        "--label",
        f"cyclo.team={instance.team_name}",
        "--label",
        f"cyclo.generation={instance.generation}",
        "--restart",
        "unless-stopped",
        "--stop-timeout",
        "30",
        "--pids-limit",
        "2048",
        "--security-opt",
        "no-new-privileges",
        # The runtime binds team capabilities to its destination interface.
        # Removing raw-packet authority prevents a root/custom team image from
        # forging traffic for another private-network interface.
        "--cap-drop",
        "NET_RAW",
        "--network",
        instance.network_name,
    ]
    if not instance.offline:
        published = (
            f"{instance.agentws_host}:{spec.port}:4137"
            if spec.port
            else f"{instance.agentws_host}::4137"
        )
        command.extend(["--publish", published])
    command.extend(
        [
        "--workdir",
        str(CONTAINER_WORKSPACE),
        "-e",
        f"CYCLO_HOST_UID={os.getuid()}",
        "-e",
        f"CYCLO_HOST_GID={os.getgid()}",
        "-e",
        f"PI_CODING_AGENT_DIR={CONTAINER_PI / 'agent'}",
        "-e",
        f"CYCLO_AGENTWS_RUNTIME={CONTAINER_AGENTWS}",
        "-e",
        f"CYCLO_VERBOSE={'1' if spec.verbose else '0'}",
        "-e",
        f"AGENTWS_TEAM_ROOT={CONTAINER_TEAM}",
        "-e",
        f"AGENTWS_TEAM_ROSTER={roster}",
        "-e",
        f"AGENTWS_TEAM_PROTOCOL={protocol}",
        "-e",
        f"AGENTWS_TEAM_ROLES_DIR={CONTAINER_TEAM / 'roles'}",
        "-e",
        f"AGENTWS_WORKSPACE={CONTAINER_WORKSPACE}",
        ]
    )
    for name in AGENTWS_RETRY_ENVIRONMENT:
        value = os.environ.get(name)
        if value is not None:
            command.extend(["-e", f"{name}={value}"])
    command.extend(
        [
        "--mount",
        mount(spec.runtime_root, CONTAINER_AGENTWS, "ro"),
        "--mount",
        mount(spec.tasks_dir, CONTAINER_AGENTWS / "tasks"),
        "--mount",
        mount(spec.jobs_dir, CONTAINER_AGENTWS / "jobs"),
        "--mount",
        mount(spec.agents_dir, CONTAINER_AGENTWS / "agents"),
        "--mount",
        # Pi creates lock files and mutable runtime metadata beside its projected
        # configuration. This per-instance tree contains only a provider-runtime
        # capability scoped to provider/model names; host credentials remain
        # inside the gateway.
        mount(spec.pi_root, CONTAINER_PI),
        "--mount",
        mount(spec.team.root, CONTAINER_TEAM, "rw" if instance.team_write else "ro"),
        "--mount",
        mount(spec.project, CONTAINER_WORKSPACE, "ro" if instance.project_read_only else "rw"),
        instance.image,
        "python3",
        str(CONTAINER_AGENTWS / ".cyclo-runtime.py"),
        ]
    )
    return command


class Docker:
    def _run(
        self,
        command: Sequence[str],
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                list(command),
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CycloError("Docker is not installed or not on PATH") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise CycloError(f"Docker command failed ({proc.returncode}): {detail or ' '.join(command)}")
        return proc

    def available(self) -> tuple[bool, str]:
        proc = self._run(["docker", "info", "--format", "{{.ServerVersion}}"], capture=True, check=False)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        return proc.returncode == 0, stdout or stderr

    @staticmethod
    def _resource_id(info: dict[str, object], *, kind: str, name: str) -> str:
        resource_id = info.get("Id")
        if not isinstance(resource_id, str) or not resource_id:
            raise CycloError(f"cannot inspect Docker {kind} {name}: missing resource ID")
        return resource_id

    def _inspect_container(self, name: str) -> dict[str, object] | None:
        proc = self._run(
            ["docker", "container", "inspect", name], capture=True, check=False
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            lowered = detail.lower()
            if "no such object" in lowered or "no such container" in lowered:
                return None
            raise CycloError(
                f"cannot inspect Docker container {name}: {detail or 'unknown Docker error'}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CycloError(f"cannot inspect Docker container: {name}") from exc
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise CycloError(f"cannot inspect Docker container: {name}")
        self._resource_id(data[0], kind="container", name=name)
        return data[0]

    def _inspect_network(self, name: str) -> dict[str, object] | None:
        proc = self._run(
            ["docker", "network", "inspect", name], capture=True, check=False
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            lowered = detail.lower()
            if "no such network" in lowered or f"network {name.lower()} not found" in lowered:
                return None
            raise CycloError(
                f"cannot inspect Docker network {name}: {detail or 'unknown Docker error'}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CycloError(f"cannot inspect Docker network: {name}") from exc
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise CycloError(f"cannot inspect Docker network: {name}")
        self._resource_id(data[0], kind="network", name=name)
        return data[0]

    def _owned_container(
        self, name: str, expected_instance: str
    ) -> dict[str, object] | None:
        info = self._inspect_container(name)
        if info is None:
            return None
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or labels.get(CYCLO_LABEL) != expected_instance:
            raise CycloError(f"refusing to use non-Cyclo container: {name}")
        return info

    def container_running(self, name: str) -> bool:
        info = self._inspect_container(name)
        state = info.get("State") if info is not None else None
        return isinstance(state, dict) and state.get("Running") is True

    def container_label(self, name: str, label: str = CYCLO_LABEL) -> str | None:
        info = self._inspect_container(name)
        if info is None:
            return None
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        value = labels.get(label) if isinstance(labels, dict) else None
        return value if isinstance(value, str) and value else None

    def container_exists(self, name: str) -> bool:
        return self._inspect_container(name) is not None

    def ensure_network(self, name: str, *, offline: bool) -> str:
        info = self._inspect_network(name)
        if info is not None:
            labels = info.get("Labels") or {}
            internal = bool(info.get("Internal"))
            if not isinstance(labels, dict):
                raise CycloError(f"cannot inspect existing Docker network: {name}")
            if labels.get(CYCLO_LABEL) != name:
                raise CycloError(f"Docker network name is already owned outside Cyclo: {name}")
            if internal != offline:
                mode = "offline" if internal else "egress-enabled"
                raise CycloError(f"Docker network {name} already exists in {mode} mode")
            return self._resource_id(info, kind="network", name=name)
        command = ["docker", "network", "create", "--label", f"{CYCLO_LABEL}={name}"]
        if offline:
            command.append("--internal")
        command.append(name)
        self._run(command)
        info = self._inspect_network(name)
        if info is None:
            raise CycloError(f"Docker network disappeared after creation: {name}")
        labels = info.get("Labels") or {}
        if not isinstance(labels, dict) or labels.get(CYCLO_LABEL) != name:
            raise CycloError(f"created Docker network has unexpected ownership: {name}")
        return self._resource_id(info, kind="network", name=name)

    @staticmethod
    def _network_members(info: dict[str, object]) -> dict[str, str]:
        containers = info.get("Containers")
        if containers is None:
            return {}
        if not isinstance(containers, dict):
            raise CycloError("cannot parse Docker network membership")
        result: dict[str, str] = {}
        for container_id, value in containers.items():
            if (
                isinstance(container_id, str)
                and isinstance(value, dict)
                and isinstance(value.get("Name"), str)
            ):
                result[container_id] = value["Name"]
        return result

    def connect_runtime(
        self,
        network_id: str,
        runtime_container_id: str,
        runtime_alias: str,
    ) -> None:
        info = self._inspect_network(network_id)
        if info is None:
            raise CycloError(f"Docker network disappeared before connection: {network_id}")
        labels = info.get("Labels") or {}
        if not isinstance(labels, dict) or not isinstance(labels.get(CYCLO_LABEL), str):
            raise CycloError(f"refusing to connect runtime to non-Cyclo network: {network_id}")
        if runtime_container_id in self._network_members(info):
            return
        self._run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                runtime_alias,
                network_id,
                runtime_container_id,
            ]
        )

    def start(self, spec: ContainerSpec) -> int | None:
        info = self._owned_container(spec.instance.container_name, spec.instance.id)
        if info is not None:
            state = info.get("State")
            if isinstance(state, dict) and state.get("Running") is True:
                raise CycloError(f"Cyclo instance is already running: {spec.instance.id}")
            resource_id = self._resource_id(
                info, kind="container", name=spec.instance.container_name
            )
            self._run(["docker", "rm", resource_id])
        self._run(container_command(spec))
        info = self._owned_container(spec.instance.container_name, spec.instance.id)
        if info is None:
            raise CycloError(
                f"Cyclo container disappeared after start: {spec.instance.container_name}"
            )
        resource_id = self._resource_id(
            info, kind="container", name=spec.instance.container_name
        )
        if spec.instance.offline:
            return None
        return self.published_port(resource_id)

    def published_port(self, container: str) -> int:
        proc = self._run(["docker", "port", container, "4137/tcp"], capture=True)
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            return int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise CycloError(f"unexpected Docker port output for {container}: {line!r}") from exc

    def wait_ready(
        self,
        container: str,
        port: int | None,
        *,
        host: str = DEFAULT_AGENTWS_HOST,
        timeout: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        probe_host = DEFAULT_AGENTWS_HOST if host == "0.0.0.0" else host
        url = f"http://{probe_host}:{port}/" if port is not None else "http://127.0.0.1:4137/"
        while time.monotonic() < deadline:
            if not self.container_running(container):
                logs = self._run(
                    ["docker", "logs", "--tail", "40", container],
                    capture=True,
                    check=False,
                )
                detail = ((logs.stdout or "") + (logs.stderr or "")).strip()
                raise CycloError(
                    f"Cyclo container exited before AgentWS became ready: {detail or container}"
                )
            if port is None:
                probe = self._run(
                    [
                        "docker",
                        "exec",
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        container,
                        "python3",
                        "-c",
                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4137/', timeout=1).read(1)",
                    ],
                    capture=True,
                    check=False,
                )
                if probe.returncode == 0:
                    return
            else:
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            return
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.2)
        location = url if port is not None else f"inside {container} on port 4137"
        raise CycloError(f"timed out waiting for AgentWS {location}")

    def stop_remove(self, container: str, expected_instance: str | None = None) -> None:
        if expected_instance is None:
            raise CycloError("expected Cyclo instance ID is required for container removal")
        info = self._owned_container(container, expected_instance)
        if info is None:
            return
        resource_id = self._resource_id(info, kind="container", name=container)
        state = info.get("State")
        if isinstance(state, dict) and state.get("Running") is True:
            self._run(["docker", "stop", "--timeout", "30", resource_id])
        self._run(["docker", "rm", resource_id])

    def remove_network(self, name: str, runtime_container: str) -> None:
        info = self._inspect_network(name)
        if info is None:
            return
        labels = info.get("Labels") or {}
        if not isinstance(labels, dict):
            raise CycloError(f"cannot inspect Docker network before removal: {name}")
        if labels.get(CYCLO_LABEL) != name:
            raise CycloError(f"refusing to remove non-Cyclo network: {name}")
        network_id = self._resource_id(info, kind="network", name=name)
        for container_id, container_name in self._network_members(info).items():
            if container_name == runtime_container:
                self._run(
                    ["docker", "network", "disconnect", network_id, container_id],
                    check=False,
                )
        self._run(["docker", "network", "rm", network_id])

    def logs(self, container: str, *, follow: bool) -> int:
        command = ["docker", "logs"]
        if follow:
            command.append("--follow")
        command.append(container)
        return self._run(command, check=False).returncode

    def copy_to(self, container: str, source: Path, destination: str) -> None:
        self._run(["docker", "cp", str(source), f"{container}:{destination}"])

    def exec(
        self,
        container: str,
        command: Sequence[str],
        *,
        check: bool = True,
        user: str | None = None,
    ) -> int:
        identity = user or f"{os.getuid()}:{os.getgid()}"
        return self._run(
            ["docker", "exec", "--user", identity, container, *command], check=check
        ).returncode
