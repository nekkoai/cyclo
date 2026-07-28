from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .errors import CycloError
from .docker_engine import (
    DockerContainerState,
    DockerEngine,
    docker_container_state as classify_docker_container,
)
from .installation import (
    LABEL_INSTANCE,
    LABEL_KIND,
    LABEL_SYSTEM,
    TEAM_KIND,
    TEAM_NETWORK_KIND,
    resource_labels,
    team_container_name,
    team_network_name,
)
from .project import (
    CONTAINER_READONLY_ROOT,
    MOUNT_NAME_RE,
    ProjectMount,
)
from .state import DEFAULT_AGENTWS_HOST, LAUNCH_ID_RE, Instance
from .team import Team


CONTAINER_AGENTWS = Path("/agentws")
CONTAINER_TEAM = Path("/team")
CONTAINER_WORKSPACE = Path("/workspace")
CONTAINER_READONLY = CONTAINER_READONLY_ROOT
CONTAINER_PI = Path("/home/cyclo/.pi")
CONTAINER_PROVIDER_ROOT = Path("/run/cyclo/provider")
CONTAINER_PROVIDER_SOCKET = CONTAINER_PROVIDER_ROOT / "component.sock"
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
DOCKER_ENDPOINT_FORMAT = '{{json (index .Endpoints "docker").Host}}'
DOCKER_ENDPOINT_TIMEOUT_SECONDS = 5.0


def docker_container_state(
    info: Mapping[str, object] | None, *, name: str
) -> DockerContainerState:
    """Classify Docker lifecycle separately from operational readiness."""

    if info is None:
        return DockerContainerState.ABSENT
    try:
        return classify_docker_container(info)
    except CycloError as exc:
        raise CycloError(f"{exc}: {name}") from exc


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


def _endpoint_error(detail: str) -> CycloError:
    normalized = " ".join(detail.split())[:512]
    return CycloError(
        "cannot resolve selected Docker endpoint"
        + (f": {normalized}" if normalized else "")
    )


def _unix_socket_from_endpoint(endpoint: str) -> Path | None:
    try:
        parsed = urlsplit(endpoint)
    except ValueError as exc:
        raise _endpoint_error("Docker returned an invalid endpoint URI") from exc
    if not parsed.scheme:
        raise _endpoint_error("Docker returned an endpoint without a URI scheme")
    if parsed.scheme.lower() != "unix":
        return None
    if parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        raise _endpoint_error("Docker returned an invalid Unix endpoint URI")
    try:
        decoded = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise _endpoint_error("Docker returned an invalid Unix endpoint path") from exc
    if (
        not Path(decoded).is_absolute()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded)
    ):
        raise _endpoint_error("Docker returned an invalid Unix endpoint path")
    return Path(decoded)


def selected_docker_endpoint(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve the effective daemon endpoint through Docker's context logic."""

    command = [
        "docker",
        "context",
        "inspect",
        "--format",
        DOCKER_ENDPOINT_FORMAT,
    ]
    try:
        process = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=None if environment is None else dict(environment),
            timeout=DOCKER_ENDPOINT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise _endpoint_error("Docker is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise _endpoint_error("Docker context inspection timed out") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _endpoint_error(str(exc) or type(exc).__name__) from exc
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise _endpoint_error(detail or "Docker context inspection failed")

    lines = (process.stdout or "").splitlines()
    if len(lines) != 1 or lines[0] != lines[0].strip():
        raise _endpoint_error("Docker returned an invalid endpoint response")
    try:
        endpoint = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise _endpoint_error("Docker returned an invalid endpoint response") from exc
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or endpoint != endpoint.strip()
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in endpoint
        )
    ):
        raise _endpoint_error("Docker returned an invalid endpoint response")
    _unix_socket_from_endpoint(endpoint)
    return endpoint


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
    selected = _unix_socket_from_endpoint(
        selected_docker_endpoint(environment)
    )
    if selected is not None:
        candidates.append(selected)
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise _endpoint_error(
                f"Docker socket path is not resolvable: {exc}"
            ) from exc
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
    validate_mount_collection(
        ((team, "team"),),
        ((project, "project"),),
        state_root,
        host_pi_agent_dir,
        trusted_roots,
    )


def validate_mount_collection(
    teams: Iterable[tuple[Path, str]],
    projects: Iterable[tuple[Path, str]],
    state_root: Path,
    host_pi_agent_dir: Path,
    trusted_roots: Iterable[tuple[Path, str]] = (),
) -> None:
    """Validate a complete team/mounted-directory set before Docker sees it."""

    mounted = [*teams, *projects]
    for (left, left_label), (right, right_label) in combinations(mounted, 2):
        if overlaps(left, right) or overlaps_lexically(left, right):
            raise CycloError(
                f"{left_label} and {right_label} must be separate filesystem "
                f"trees: {left} and {right}"
            )
    protected: list[tuple[Path, str]] = [
        (state_root, "Cyclo state"),
        (host_pi_agent_dir, "host Pi credential/configuration directory"),
    ]
    protected.extend(HOST_PSEUDO_FILESYSTEMS)
    protected.extend((path, "Docker socket") for path in docker_socket_paths())
    protected.extend(trusted_roots)
    for source, label in protected:
        for mounted_path, mounted_label in mounted:
            if overlaps(mounted_path, source) or overlaps_lexically(
                mounted_path, source
            ):
                raise CycloError(
                    f"{mounted_label} mount overlaps {label}: "
                    f"{mounted_path} and {source}"
                )


def mount(source: Path, target: Path, mode: str = "rw") -> str:
    if "," in str(source):
        raise CycloError(f"Docker bind source cannot contain a comma: {source}")
    if mode not in {"ro", "rw"}:
        raise CycloError(f"invalid Docker bind mode: {mode!r}")
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
    provider_socket_dir: Path
    system: str
    port: int
    verbose: bool = False
    project_mounts: tuple[ProjectMount, ...] = ()
    workspace_layout: Path | None = None
    readonly_layout: Path | None = None


def validate_container_spec(spec: ContainerSpec) -> None:
    if spec.instance.container_name != team_container_name(
        spec.system, spec.instance.id
    ) or spec.instance.network_name != team_network_name(
        spec.system, spec.instance.id
    ):
        raise CycloError(
            "Cyclo team resources do not match the selected installation"
        )
    if not LAUNCH_ID_RE.fullmatch(spec.instance.launch_id):
        raise CycloError(
            f"invalid launch identity for Cyclo instance: {spec.instance.id}"
        )
    provider_socket_dir = spec.provider_socket_dir
    if (
        not provider_socket_dir.is_absolute()
        or provider_socket_dir.is_symlink()
        or not provider_socket_dir.is_dir()
    ):
        raise CycloError(
            f"invalid Cyclo provider socket directory: {provider_socket_dir}"
        )


def container_create_arguments(spec: ContainerSpec) -> list[str]:
    instance = spec.instance
    provider_socket_dir = spec.provider_socket_dir
    provider_socket = provider_socket_dir / "component.sock"
    host_uid = os.getuid()
    host_gid = os.getgid()
    extra_groups = ":".join(
        str(group)
        for group in sorted(set(os.getgroups()))
        if group != host_gid
    )
    if instance.provider_socket_path != str(provider_socket):
        raise CycloError(
            "Cyclo instance provider socket does not match its launch configuration"
        )
    if spec.project_mounts and (
        spec.workspace_layout is None or spec.readonly_layout is None
    ):
        raise CycloError(
            "named mounts require workspace and read-only layout roots"
        )
    if not spec.project_mounts and (
        spec.workspace_layout is not None or spec.readonly_layout is not None
    ):
        raise CycloError("named layout roots require configured mounts")
    roster = CONTAINER_TEAM / spec.team.roster.name
    command: list[str] = []
    for key, value in resource_labels(spec.system, TEAM_KIND, instance.id).items():
        command.extend(["--label", f"{key}={value}"])
    command.extend(
        [
            "--label",
            f"cyclo.team={instance.team_name}",
            "--label",
            f"cyclo.generation={instance.generation}",
        ]
    )
    command.extend(["--label", f"cyclo.launch={instance.launch_id}"])
    command.extend(
        [
            "--restart",
            "unless-stopped",
            "--stop-timeout",
            "30",
            "--pids-limit",
            "2048",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "NET_RAW",
            "--network",
            "none" if instance.offline else instance.network_name,
        ]
    )
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
            f"CYCLO_HOST_UID={host_uid}",
            "-e",
            f"CYCLO_HOST_GID={host_gid}",
            "-e",
            f"CYCLO_EXTRA_GROUPS={extra_groups}",
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
            f"AGENTWS_SYSTEM_PROTOCOL={CONTAINER_AGENTWS / 'AGENTS.md'}",
            "-e",
            f"AGENTWS_TEAM_ROLES_DIR={CONTAINER_TEAM / 'roles'}",
            "-e",
            f"AGENTWS_WORKSPACE={CONTAINER_WORKSPACE}",
            "-e",
            f"CYCLO_PROVIDER_SOCKET={CONTAINER_PROVIDER_SOCKET}",
        ]
    )
    if spec.team.protocol is not None:
        command.extend(
            ["-e", f"AGENTWS_TEAM_PROTOCOL={CONTAINER_TEAM / 'AGENTS.md'}"]
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
            # Pi creates lock files and mutable runtime metadata beside its
            # settings. The provider interface is a separate read-only mount;
            # no provider credentials or bearer tokens enter this tree.
            mount(spec.pi_root, CONTAINER_PI),
            "--mount",
            # A read-only bind still permits connecting to the Unix socket, but
            # prevents the team from creating or replacing socket-directory
            # entries on the host.
            mount(provider_socket_dir, CONTAINER_PROVIDER_ROOT, "ro"),
            "--mount",
            mount(
                spec.team.root,
                CONTAINER_TEAM,
                "rw" if instance.team_write else "ro",
            ),
        ]
    )
    if spec.project_mounts:
        assert spec.workspace_layout is not None
        assert spec.readonly_layout is not None
        command.extend(
            [
                "--mount",
                mount(spec.workspace_layout, CONTAINER_WORKSPACE, "ro"),
                "--mount",
                mount(spec.readonly_layout, CONTAINER_READONLY, "ro"),
            ]
        )
        for project_mount in spec.project_mounts:
            expected_parent = (
                CONTAINER_WORKSPACE
                if project_mount.writable
                else CONTAINER_READONLY
            )
            if (
                project_mount.name in {".", ".."}
                or not MOUNT_NAME_RE.fullmatch(project_mount.name)
                or project_mount.container_path.parent != expected_parent
            ):
                raise CycloError(
                    f"invalid named mount target: {project_mount.name!r}"
                )
            command.extend(
                [
                    "--mount",
                    mount(
                        project_mount.path,
                        project_mount.container_path,
                        project_mount.mode,
                    ),
                ]
            )
    else:
        command.extend(
            [
                "--mount",
                mount(
                    spec.project,
                    CONTAINER_WORKSPACE,
                    "rw",
                ),
            ]
        )
    command.extend(
        [
            instance.image,
            "python3",
            str(CONTAINER_AGENTWS / ".cyclo-runtime.py"),
        ]
    )
    return command


def container_command(spec: ContainerSpec) -> list[str]:
    """Return the exact Docker create command used for this team launch."""

    return [
        "docker",
        "create",
        "--name",
        spec.instance.container_name,
        *container_create_arguments(spec),
    ]


class Docker(DockerEngine):
    """Team runtime operations built on Cyclo's shared Docker boundary."""

    def _owned_container(
        self, name: str, expected_instance: str, expected_system: str
    ) -> dict[str, object] | None:
        if name != team_container_name(expected_system, expected_instance):
            raise CycloError(
                "Cyclo container name does not match the selected installation "
                f"and instance: {name}"
            )
        info = self.inspect("container", name)
        if info is None:
            return None
        self._verify_owned_container(
            info,
            name=name,
            expected_instance=expected_instance,
            expected_system=expected_system,
        )
        return info

    def _verify_owned_container(
        self,
        info: Mapping[str, object],
        *,
        name: str,
        expected_instance: str,
        expected_system: str,
        expected_launch: str | None = None,
    ) -> None:
        labels = self.labels(info)
        raw_name = info.get("Name")
        actual_name = (
            raw_name[1:]
            if isinstance(raw_name, str) and raw_name.startswith("/")
            else raw_name
        )
        if (
            actual_name != name
            or labels.get(LABEL_SYSTEM) != expected_system
            or labels.get(LABEL_KIND) != TEAM_KIND
            or labels.get(LABEL_INSTANCE) != expected_instance
        ):
            raise CycloError(f"refusing to use non-Cyclo container: {name}")
        if (
            expected_launch is not None
            and labels.get("cyclo.launch") != expected_launch
        ):
            raise CycloError(
                f"Cyclo container launch identity changed: {name}"
            )
        self.container_id(info)

    def _launch_verifier(
        self,
        container: str,
        expected_instance: str,
        *,
        expected_system: str,
        expected_launch: str,
    ) -> Callable[[Mapping[str, object]], None]:
        if not LAUNCH_ID_RE.fullmatch(expected_launch):
            raise CycloError(
                f"invalid launch identity for Cyclo instance: {expected_instance}"
            )

        def verify(info: Mapping[str, object]) -> None:
            self._verify_owned_container(
                info,
                name=container,
                expected_instance=expected_instance,
                expected_system=expected_system,
                expected_launch=expected_launch,
            )

        return verify

    def _current_container(
        self, instance: Instance, system: str
    ) -> dict[str, object] | None:
        return self._container_for_launch(
            instance.container_name,
            instance.id,
            expected_system=system,
            expected_launch=instance.launch_id,
        )

    def _container_for_launch(
        self,
        container: str,
        expected_instance: str,
        *,
        expected_system: str,
        expected_launch: str,
    ) -> dict[str, object] | None:
        info = self._owned_container(
            container,
            expected_instance,
            expected_system,
        )
        if info is None:
            return info
        self._launch_verifier(
            container,
            expected_instance,
            expected_system=expected_system,
            expected_launch=expected_launch,
        )(info)
        return info

    def _required_current_container_id(
        self, instance: Instance, system: str
    ) -> str:
        info = self._current_container(instance, system)
        if info is None:
            raise CycloError(f"Cyclo container not found: {instance.container_name}")
        return self.container_id(info)

    def container_running(self, instance: Instance, *, system: str) -> bool:
        return self.container_lifecycle_state(instance, system=system).operational

    def container_lifecycle_state(
        self, instance: Instance, *, system: str
    ) -> DockerContainerState:
        info = self._current_container(instance, system)
        return docker_container_state(info, name=instance.container_name)

    def previous_launch_lifecycle_state(
        self, instance: Instance, *, system: str
    ) -> DockerContainerState:
        """Inspect ownership during startup without adopting a previous launch."""

        info = self._owned_container(instance.container_name, instance.id, system)
        return docker_container_state(info, name=instance.container_name)

    def container_lifecycle_active(
        self, instance: Instance, *, system: str
    ) -> bool:
        return self.container_lifecycle_state(
            instance, system=system
        ).lifecycle_active

    def current_published_port(
        self,
        instance: Instance,
        *,
        system: str,
    ) -> int:
        """Return AgentWS's port for the exact launch recorded by Cyclo."""

        container_id = self._required_current_container_id(instance, system)
        return self.published_port(container_id)

    def ensure_network(
        self,
        name: str,
        expected_instance: str,
        *,
        system: str,
    ) -> str:
        if name != team_network_name(system, expected_instance):
            raise CycloError(
                "Cyclo network name does not match the selected installation"
            )
        info = self.inspect("network", name)
        if info is not None:
            labels = info.get("Labels") or {}
            internal = bool(info.get("Internal"))
            if not isinstance(labels, dict):
                raise CycloError(f"cannot inspect existing Docker network: {name}")
            current = name == team_network_name(system, expected_instance) and all(
                (
                    labels.get(LABEL_SYSTEM) == system,
                    labels.get(LABEL_KIND) == TEAM_NETWORK_KIND,
                    labels.get(LABEL_INSTANCE) == expected_instance,
                )
            )
            if not current:
                raise CycloError(f"Docker network name is already owned outside Cyclo: {name}")
            if internal:
                raise CycloError(
                    f"Docker network {name} already exists in internal mode"
                )
            return self.resource_id(info)
        command = ["network", "create"]
        for key, value in resource_labels(
            system, TEAM_NETWORK_KIND, expected_instance
        ).items():
            command.extend(["--label", f"{key}={value}"])
        command.append(name)
        self.call(command, capture=False)
        info = self.inspect("network", name)
        if info is None:
            raise CycloError(f"Docker network disappeared after creation: {name}")
        labels = info.get("Labels") or {}
        if not isinstance(labels, dict) or not all(
            (
                labels.get(LABEL_SYSTEM) == system,
                labels.get(LABEL_KIND) == TEAM_NETWORK_KIND,
                labels.get(LABEL_INSTANCE) == expected_instance,
            )
        ):
            raise CycloError(f"created Docker network has unexpected ownership: {name}")
        return self.resource_id(info)

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

    def start(self, spec: ContainerSpec) -> int | None:
        validate_container_spec(spec)
        create_arguments = container_create_arguments(spec)
        instance = spec.instance
        verifier = self._launch_verifier(
            instance.container_name,
            instance.id,
            expected_system=spec.system,
            expected_launch=instance.launch_id,
        )
        previous = self.inspect_container(
            instance.container_name,
            verify=verifier,
        )
        if previous is not None:
            if previous.state.lifecycle_active:
                raise CycloError(
                    f"Cyclo instance is already active "
                    f"({previous.state.value}): {instance.id}"
                )
            self.remove_container(
                previous,
                verify=verifier,
                timeout=30,
                reject_active=True,
            )

        created = None
        try:
            created, _result = self.create_container(
                instance.container_name,
                create_arguments,
                verify=verifier,
            )
            self.start_container(created)
        except BaseException as cause:
            if created is None:
                # Docker may have created an object without returning a
                # verified immutable ID.  Never clean it up through the
                # reusable name; locked lifecycle reconciliation can inspect
                # its launch labels later.
                raise
            try:
                failed = self.inspect_container(
                    created.id,
                    verify=verifier,
                )
                if failed is not None:
                    self.remove_container(
                        failed,
                        verify=verifier,
                        timeout=30,
                        force=True,
                    )
            except Exception as cleanup:
                primary = str(cause) or cause.__class__.__name__
                raise CycloError(
                    f"{primary}; launch cleanup failed: {cleanup}"
                ) from cause
            raise

        assert created is not None
        if instance.offline:
            return None
        return self.published_port(created.id)

    def remove_inactive_launch(
        self,
        container: str,
        expected_instance: str,
        *,
        expected_system: str,
        expected_launch: str,
    ) -> bool:
        """Remove one exact stopped/dead launch without stopping an active one."""

        info = self._container_for_launch(
            container,
            expected_instance,
            expected_system=expected_system,
            expected_launch=expected_launch,
        )
        if info is None:
            return False
        verifier = self._launch_verifier(
            container,
            expected_instance,
            expected_system=expected_system,
            expected_launch=expected_launch,
        )
        verified = self.verify_container(info, verify=verifier)
        return self.remove_container(
            verified,
            verify=verifier,
            timeout=30,
            reject_active=True,
        )

    def published_port(self, container: str) -> int:
        proc = self.call(["port", container, "4137/tcp"])
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            return int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise CycloError(f"unexpected Docker port output for {container}: {line!r}") from exc

    def wait_ready(
        self,
        instance: Instance,
        port: int | None,
        *,
        system: str,
        host: str = DEFAULT_AGENTWS_HOST,
        timeout: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        probe_host = DEFAULT_AGENTWS_HOST if host == "0.0.0.0" else host
        url = f"http://{probe_host}:{port}/" if port is not None else "http://127.0.0.1:4137/"
        while time.monotonic() < deadline:
            info = self._current_container(instance, system)
            state = docker_container_state(info, name=instance.container_name)
            if not state.operational:
                if info is None:
                    raise CycloError(
                        "Cyclo container disappeared before AgentWS became "
                        f"ready: {instance.container_name}"
                    )
                container_id = self.container_id(info)
                logs = self.call(
                    ["logs", "--tail", "40", container_id],
                    check=False,
                )
                detail = ((logs.stdout or "") + (logs.stderr or "")).strip()
                raise CycloError(
                    "Cyclo container exited before AgentWS became ready: "
                    f"{detail or instance.container_name}"
                )
            assert info is not None
            container_id = self.container_id(info)
            if port is None:
                probe = self.call(
                    [
                        "exec",
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        container_id,
                        "python3",
                        "-c",
                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4137/', timeout=1).read(1)",
                    ],
                    check=False,
                )
                if probe.returncode == 0:
                    return
            else:
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            final = self._current_container(instance, system)
                            if docker_container_state(
                                final, name=instance.container_name
                            ).operational:
                                return
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.2)
        location = (
            url
            if port is not None
            else f"inside {instance.container_name} on port 4137"
        )
        raise CycloError(f"timed out waiting for AgentWS {location}")

    def stop_remove(
        self,
        container: str,
        expected_instance: str,
        *,
        expected_system: str,
        expected_launch: str,
    ) -> bool:
        info = self._container_for_launch(
            container,
            expected_instance,
            expected_system=expected_system,
            expected_launch=expected_launch,
        )
        if info is None:
            return False
        verifier = self._launch_verifier(
            container,
            expected_instance,
            expected_system=expected_system,
            expected_launch=expected_launch,
        )
        verified = self.verify_container(info, verify=verifier)
        return self.remove_container(
            verified,
            verify=verifier,
            timeout=30,
        )

    def remove_network(
        self, name: str, expected_instance: str, *, system: str
    ) -> None:
        info = self.inspect("network", name)
        if info is None:
            return
        labels = info.get("Labels") or {}
        if not isinstance(labels, dict):
            raise CycloError(f"cannot inspect Docker network before removal: {name}")
        current = name == team_network_name(system, expected_instance) and all(
            (
                labels.get(LABEL_SYSTEM) == system,
                labels.get(LABEL_KIND) == TEAM_NETWORK_KIND,
                labels.get(LABEL_INSTANCE) == expected_instance,
            )
        )
        if not current:
            raise CycloError(f"refusing to remove non-Cyclo network: {name}")
        network_id = self.resource_id(info)
        members = sorted(self._network_members(info).values())
        if members:
            raise CycloError(
                f"refusing to remove Cyclo network {name} while containers "
                "remain attached: "
                + ", ".join(members)
            )
        self.call(["network", "rm", network_id], capture=False)

    def logs(self, instance: Instance, *, system: str, follow: bool) -> int:
        container_id = self._required_current_container_id(instance, system)
        command = ["logs"]
        if follow:
            command.append("--follow")
        command.append(container_id)
        return self.call(command, capture=False, check=False).returncode

    def copy_to(
        self,
        instance: Instance,
        source: Path,
        destination: str,
        *,
        system: str,
    ) -> None:
        container_id = self._required_current_container_id(instance, system)
        with tempfile.TemporaryDirectory(prefix="cyclo-task-copy-") as temporary:
            staged = Path(temporary) / "spec.md"
            shutil.copyfile(source, staged)
            staged.chmod(0o600)
            self.call(
                [
                    "cp",
                    "--archive",
                    str(staged),
                    f"{container_id}:{destination}",
                ],
                capture=False,
            )

    def exec(
        self,
        instance: Instance,
        command: Sequence[str],
        *,
        system: str,
        check: bool = True,
        user: str | None = None,
    ) -> int:
        container_id = self._required_current_container_id(instance, system)
        identity = user or f"{os.getuid()}:{os.getgid()}"
        return self.call(
            ["exec", "--user", identity, container_id, *command],
            capture=False,
            check=check,
        ).returncode
