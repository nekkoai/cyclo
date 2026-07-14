from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from . import docker as runner_docker
from . import source as runner_source
from ..errors import CycloError


DEFAULT_GATEWAY_IMAGE = "cyclo-gateway:local"
DEFAULT_STORE_VOLUME = "cyclo-gateway-store"
GATEWAY_PORT = 8787
GATEWAY_STORE_PATH = "/var/lib/cyclo-gateway"
GATEWAY_MODELS_PATH = "/run/pi/models.json"
GATEWAY_CLIENT_REGISTRY_DIR = "/run/cyclo-gateway"
GATEWAY_CLIENT_REGISTRY_PATH = f"{GATEWAY_CLIENT_REGISTRY_DIR}/clients.json"
CLIENT_REGISTRY_VERSION = 1
GATEWAY_OWNERSHIP_LABEL = "cyclo.gateway"
GATEWAY_OWNERSHIP_VALUE = "1"
GATEWAY_LABEL = f"{GATEWAY_OWNERSHIP_LABEL}={GATEWAY_OWNERSHIP_VALUE}"
GATEWAY_RESOURCE_LABEL = "cyclo.gateway-resource"
GATEWAY_RUN_LABEL = "cyclo.gateway-run"
SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"
GATEWAY_CONFIG_FINGERPRINT_LABEL = "cyclo.gateway-config-fingerprint"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROVIDER_RE = CLIENT_ID_RE

# One gateway per system (keyed by its registry dir), reused by every project in
# that system instead of a container per run. The store volume was already
# shared, so this just collapses the redundant per-project containers/networks.
def _system_id(registry_dir) -> str:
    return hashlib.sha256(str(Path(registry_dir).resolve()).encode("utf-8")).hexdigest()[:12]


class GatewayConfig(Protocol):
    gateway_image: str
    gateway_container: str
    gateway_network: str
    store_volume: str
    host_models_json: Path
    client_registry_dir: Path
    name: str


class ClientProject(Protocol):
    project_id: str
    name: str
    generation: str


def gateway_container_name(registry_dir) -> str:
    return f"cyclo-gateway-{_system_id(registry_dir)}"


def gateway_network_name(registry_dir) -> str:
    return f"cyclo-gateway-net-{_system_id(registry_dir)}"


def host_client_registry_dir(registry_dir: Path) -> Path:
    return Path(registry_dir) / "runs" / "gateway" / "client-registry"


def host_client_registry_path(registry_dir: Path) -> Path:
    return host_client_registry_dir(registry_dir) / "clients.json"


def client_token_dir(registry_dir: Path) -> Path:
    return Path(registry_dir) / "runs" / "gateway" / "client-tokens"


def _validate_client_id(project_id: str) -> None:
    if not CLIENT_ID_RE.fullmatch(project_id):
        raise CycloError(f"invalid gateway client id: {project_id!r}")


def client_token_path(registry_dir: Path, project_id: str) -> Path:
    _validate_client_id(project_id)
    return client_token_dir(registry_dir) / f"{project_id}.token"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private_atomic(path: Path, text: str) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def stable_client_token(registry_dir: Path, project_id: str) -> str:
    """Return the stable opaque capability token for one runtime project."""
    path = client_token_path(registry_dir, project_id)
    _private_dir(path.parent)
    for _attempt in range(100):
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        except OSError as exc:
            raise CycloError(f"cannot read gateway client token {path}: {exc}") from exc
        if token:
            os.chmod(path, 0o600)
            return token
        if path.exists():
            raise CycloError(f"gateway client token is empty: {path}")
        from . import auth as runner_auth

        token = runner_auth.make_proxy_token()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            time.sleep(0.01)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
        return token
    raise CycloError(f"timed out creating gateway client token: {path}")


def prepare_client_registry(
    registry_dir: Path,
    projects: Iterable[ClientProject],
    *,
    allowed_providers: Mapping[str, Iterable[str]] | None = None,
    allowed_models: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, str]:
    """Issue project tokens and atomically publish a hash-only gateway registry.

    Every client must have explicit provider-account and provider/model scopes.
    Raw tokens remain in private per-project files and are never written here.
    """
    tokens: dict[str, str] = {}
    clients: list[dict[str, object]] = []
    for project in sorted(projects, key=lambda item: item.project_id):
        _validate_client_id(project.project_id)
        if project.project_id in tokens:
            raise CycloError(f"duplicate gateway client id: {project.project_id}")
        providers = tuple(
            dict.fromkeys((allowed_providers or {}).get(project.project_id, ()))
        )
        if not providers or any(
            not isinstance(provider, str)
            or not PROVIDER_RE.fullmatch(provider)
            for provider in providers
        ):
            raise CycloError(
                f"invalid gateway provider scope for project {project.project_id}"
            )
        models = tuple(
            dict.fromkeys((allowed_models or {}).get(project.project_id, ()))
        )
        valid_models = True
        for model in models:
            if not isinstance(model, str):
                valid_models = False
                break
            provider, separator, model_id = model.partition("/")
            if (
                not separator
                or not PROVIDER_RE.fullmatch(provider)
                or not model_id
                or provider not in providers
                or any(
                    character.isspace()
                    or ord(character) < 0x20
                    or ord(character) == 0x7F
                    for character in model_id
                )
            ):
                valid_models = False
                break
        if not models or not valid_models:
            raise CycloError(
                f"invalid gateway model scope for project {project.project_id}"
            )
        token = stable_client_token(registry_dir, project.project_id)
        tokens[project.project_id] = token
        clients.append(
            {
                "client_id": project.project_id,
                "team_id": project.name,
                "binding_generation": getattr(project, "generation", "") or None,
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "providers": list(providers),
                "models": list(models),
                "expires_at": None,
                "enabled": True,
                "revoked": False,
            }
        )
    data = {"version": CLIENT_REGISTRY_VERSION, "clients": clients}
    _write_private_atomic(
        host_client_registry_path(registry_dir),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )
    return tokens


def shared_token(registry_dir) -> str:
    """Stable token every project in this system presents to its shared gateway.
    Persisted in the registry dir so each run projects the same token the running
    gateway already accepts."""
    path = Path(registry_dir) / "gateway-token"
    from . import auth as runner_auth

    _private_dir(path.parent)
    for _attempt in range(100):
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            existing = ""
        except OSError as exc:
            raise CycloError(f"cannot read gateway admin token {path}: {exc}") from exc
        if existing:
            os.chmod(path, 0o600)
            return existing
        if path.exists():
            raise CycloError(f"gateway admin token is empty: {path}")
        token = runner_auth.make_proxy_token()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            time.sleep(0.01)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
        return token
    raise CycloError(f"timed out creating shared gateway token: {path}")


def gateway_image_fingerprint() -> str:
    return runner_source.source_fingerprint(runner_source.gateway_context_root())


def gateway_build_command(image: str, fingerprint: str) -> list[str]:
    return [
        "docker",
        "build",
        "-t",
        image,
        "--label",
        f"{SOURCE_FINGERPRINT_LABEL}={fingerprint}",
        "-f",
        str(runner_source.gateway_dockerfile_path()),
        str(runner_source.gateway_context_root()),
    ]


def _gateway_resource_label(name: str) -> str:
    return f"{GATEWAY_RESOURCE_LABEL}={name}"


def gateway_run_command(
    config: GatewayConfig,
    token: str,
    *,
    network_identifier: str | None = None,
) -> list[str]:
    command = [
        "docker",
        "run",
        "--detach",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        "256",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--name",
        config.gateway_container,
        "--label",
        GATEWAY_LABEL,
        "--label",
        _gateway_resource_label(config.gateway_container),
        "--label",
        f"{GATEWAY_RUN_LABEL}={config.name}",
        "--label",
        f"{GATEWAY_CONFIG_FINGERPRINT_LABEL}={gateway_config_fingerprint(config, token)}",
        "--network",
        network_identifier or config.gateway_network,
        "--publish",
        f"127.0.0.1::{GATEWAY_PORT}",
        "-e",
        f"CYCLO_GATEWAY_TOKEN={token}",
        "--mount",
        f"type=volume,src={config.store_volume},dst={GATEWAY_STORE_PATH}",
    ]
    client_registry_dir = getattr(config, "client_registry_dir", None)
    if client_registry_dir is not None and Path(client_registry_dir).is_dir():
        command.extend(
            [
                "-e",
                f"CYCLO_GATEWAY_CLIENTS_JSON={GATEWAY_CLIENT_REGISTRY_PATH}",
                "--mount",
                "type=bind,"
                f"src={client_registry_dir},"
                f"dst={GATEWAY_CLIENT_REGISTRY_DIR},readonly",
            ]
        )
    if config.host_models_json.exists():
        command.extend(["--mount", f"type=bind,src={config.host_models_json},dst={GATEWAY_MODELS_PATH},readonly"])
    command.append(config.gateway_image)
    return command


def gateway_config_fingerprint(config: GatewayConfig, token: str) -> str:
    try:
        models_hash = hashlib.sha256(config.host_models_json.read_bytes()).hexdigest()
    except OSError:
        models_hash = ""
    client_registry = getattr(config, "client_registry_dir", None)
    client_registry_path = (
        Path(client_registry) / "clients.json" if client_registry is not None else None
    )
    try:
        client_registry_hash = hashlib.sha256(client_registry_path.read_bytes()).hexdigest()
    except (OSError, AttributeError):
        client_registry_hash = ""
    data = {
        "image": config.gateway_image,
        "source": gateway_image_fingerprint(),
        "gateway_network": config.gateway_network,
        "store_volume": config.store_volume,
        "host_models_json": str(config.host_models_json) if config.host_models_json.exists() else "",
        "host_models_hash": models_hash,
        "client_registry_hash": client_registry_hash,
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _inspect_docker_resource(
    command: list[str],
    *,
    kind: str,
    name: str,
    missing_markers: tuple[str, ...],
) -> dict[str, object] | None:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        lowered = detail.lower()
        if any(marker in lowered for marker in missing_markers):
            return None
        raise CycloError(
            f"cannot inspect Docker {kind} {name}: {detail or 'unknown Docker error'}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CycloError(f"cannot inspect existing Docker {kind}: {name}") from exc
    if (
        not isinstance(data, list)
        or len(data) != 1
        or not isinstance(data[0], dict)
    ):
        raise CycloError(f"cannot inspect existing Docker {kind}: {name}")
    return data[0]


def _inspect_gateway_container(name: str) -> dict[str, object] | None:
    return _inspect_docker_resource(
        ["docker", "container", "inspect", name],
        kind="container",
        name=name,
        missing_markers=("no such container", "no such object"),
    )


def _inspect_gateway_network(name: str) -> dict[str, object] | None:
    return _inspect_docker_resource(
        ["docker", "network", "inspect", name],
        kind="network",
        name=name,
        missing_markers=("no such network", f"network {name.lower()} not found"),
    )


def _resource_id(info: Mapping[str, object], *, kind: str, name: str) -> str:
    resource_id = info.get("Id")
    if not isinstance(resource_id, str) or not resource_id:
        raise CycloError(f"cannot inspect existing Docker {kind}: {name}")
    return resource_id


def _owned_gateway_container(name: str) -> dict[str, object] | None:
    info = _inspect_gateway_container(name)
    if info is None:
        return None
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if (
        not isinstance(labels, Mapping)
        or labels.get(GATEWAY_OWNERSHIP_LABEL) != GATEWAY_OWNERSHIP_VALUE
        or labels.get(GATEWAY_RESOURCE_LABEL) != name
    ):
        if (
            isinstance(labels, Mapping)
            and labels.get(GATEWAY_OWNERSHIP_LABEL) == GATEWAY_OWNERSHIP_VALUE
            and not labels.get(GATEWAY_RESOURCE_LABEL)
        ):
            raise CycloError(
                f"legacy Cyclo gateway container needs one-time migration: {name}; "
                "stop Cyclo, remove that container, and retry"
            )
        raise CycloError(
            f"Docker container name is already owned outside this Cyclo gateway: {name}"
        )
    _resource_id(info, kind="container", name=name)
    return info


def _owned_gateway_network(name: str) -> dict[str, object] | None:
    info = _inspect_gateway_network(name)
    if info is None:
        return None
    labels = info.get("Labels")
    if (
        not isinstance(labels, Mapping)
        or labels.get(GATEWAY_OWNERSHIP_LABEL) != GATEWAY_OWNERSHIP_VALUE
        or labels.get(GATEWAY_RESOURCE_LABEL) != name
    ):
        if not labels:
            raise CycloError(
                f"unlabelled Docker network blocks Cyclo gateway startup: {name}; "
                "if it came from an older Cyclo release, stop Cyclo, remove that "
                "network, and retry"
            )
        if (
            isinstance(labels, Mapping)
            and labels.get(GATEWAY_OWNERSHIP_LABEL) == GATEWAY_OWNERSHIP_VALUE
            and not labels.get(GATEWAY_RESOURCE_LABEL)
        ):
            raise CycloError(
                f"legacy Cyclo gateway network needs one-time migration: {name}; "
                "stop Cyclo, remove that network, and retry"
            )
        raise CycloError(
            f"Docker network name is already owned outside this Cyclo gateway: {name}"
        )
    _resource_id(info, kind="network", name=name)
    return info


def _container_state(
    info: Mapping[str, object], *, name: str
) -> tuple[bool, Mapping[str, object]]:
    state = info.get("State")
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(state, Mapping) or not isinstance(labels, Mapping):
        raise CycloError(f"cannot inspect existing Docker container: {name}")
    return state.get("Running") is True, labels


def network_exists(name: str) -> bool:
    return _inspect_gateway_network(name) is not None


def ensure_network(name: str) -> str:
    info = _owned_gateway_network(name)
    if info is not None:
        return _resource_id(info, kind="network", name=name)
    rc = runner_docker.run_command(
        [
            "docker",
            "network",
            "create",
            "--label",
            GATEWAY_LABEL,
            "--label",
            _gateway_resource_label(name),
            name,
        ]
    )
    if rc != 0:
        raise CycloError(f"failed to create docker network: {name}")
    info = _owned_gateway_network(name)
    if info is None:
        raise CycloError(f"gateway network disappeared after creation: {name}")
    return _resource_id(info, kind="network", name=name)


def remove_network(name: str, *, best_effort: bool = False) -> bool:
    try:
        info = _owned_gateway_network(name)
    except CycloError:
        if best_effort:
            return False
        raise
    if info is None:
        return True
    resource_id = _resource_id(info, kind="network", name=name)
    try:
        remove_rc = runner_docker.docker_call_ignore_missing(
            ["docker", "network", "rm", resource_id]
        )
    except CycloError:
        if best_effort:
            return False
        raise
    if remove_rc != 0 and not best_effort:
        raise CycloError(f"failed to remove gateway network: {name}")
    return remove_rc == 0


def gateway_image_current(image: str, fingerprint: str) -> bool:
    return (
        runner_docker.docker_image_exists(image)
        and runner_docker.docker_image_label(image, SOURCE_FINGERPRINT_LABEL) == fingerprint
    )


def ensure_gateway_image(image: str, build: bool = False) -> None:
    fingerprint = gateway_image_fingerprint()
    if build or not gateway_image_current(image, fingerprint):
        rc = runner_docker.run_command(gateway_build_command(image, fingerprint))
        if rc != 0:
            raise CycloError("failed to build gateway image")


def _published_port(info: Mapping[str, object], *, name: str) -> int:
    resource_id = _resource_id(info, kind="container", name=name)
    rc, out = runner_docker.run_command_capture(
        ["docker", "port", resource_id, f"{GATEWAY_PORT}/tcp"]
    )
    if rc != 0 or not out:
        raise CycloError(f"could not read published port for gateway container {name}")
    # Output like "127.0.0.1:49153"; take the last field's port.
    last = out.splitlines()[-1].strip()
    try:
        return int(last.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise CycloError(f"unexpected docker port output: {out!r}") from exc


def published_port(container: str) -> int:
    info = _owned_gateway_container(container)
    if info is None:
        raise CycloError(f"gateway container is not running: {container}")
    return _published_port(info, name=container)


def gateway_container_id(container: str) -> str:
    info = _owned_gateway_container(container)
    if info is None:
        raise CycloError(f"gateway container is not running: {container}")
    return _resource_id(info, kind="container", name=container)


def _container_uses_network(
    info: Mapping[str, object], *, network_id: str
) -> bool:
    settings = info.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, Mapping) else None
    if not isinstance(networks, Mapping):
        return False
    return any(
        isinstance(network, Mapping) and network.get("NetworkID") == network_id
        for network in networks.values()
    )


def wait_healthy(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise CycloError("gateway container did not become healthy")


def fetch_provider_catalog(port: int, token: str) -> dict[str, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/providers",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CycloError(f"failed to read gateway provider catalog: {exc}") from exc
    if not isinstance(data, dict):
        raise CycloError("gateway provider catalog was not a JSON object")
    return data


def fetch_usage(registry_dir: Path) -> dict[str, object]:
    container = gateway_container_name(registry_dir)
    info = _owned_gateway_container(container)
    if info is None:
        raise CycloError(f"gateway container is not running: {container}")
    running, _labels = _container_state(info, name=container)
    if not running:
        raise CycloError(f"gateway container is not running: {container}")
    token = shared_token(registry_dir)
    port = _published_port(info, name=container)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CycloError(f"failed to read gateway usage: {exc}") from exc
    if not isinstance(data, dict):
        raise CycloError("gateway usage response was not a JSON object")
    return data


def start_gateway(config: GatewayConfig, token: str, build: bool = False) -> dict[str, dict]:
    """Ensure the one shared gateway is up (start it if not) and return its
    catalog. Idempotent: a second project reuses the already-running gateway
    rather than launching another container."""
    container_info = _owned_gateway_container(config.gateway_container)
    ensure_gateway_image(config.gateway_image, build=build)
    network_id = ensure_network(config.gateway_network)
    expected_fingerprint = gateway_config_fingerprint(config, token)
    if container_info is None:
        running = False
        current_fingerprint = ""
    else:
        running, labels = _container_state(
            container_info, name=config.gateway_container
        )
        current_fingerprint = str(labels.get(GATEWAY_CONFIG_FINGERPRINT_LABEL) or "")
    network_current = container_info is not None and _container_uses_network(
        container_info, network_id=network_id
    )
    if build or not running or not network_current or current_fingerprint != expected_fingerprint:
        stop_gateway_container(config.gateway_container)
        rc, _ = runner_docker.run_command_capture(
            gateway_run_command(config, token, network_identifier=network_id)
        )
        if rc != 0:
            raise CycloError("failed to start gateway container")
        container_info = _owned_gateway_container(config.gateway_container)
        if container_info is None:
            raise CycloError("gateway container disappeared after start")
        if not _container_uses_network(container_info, network_id=network_id):
            raise CycloError("gateway container started on the wrong Docker network")
    assert container_info is not None
    port = _published_port(container_info, name=config.gateway_container)
    wait_healthy(port)
    catalog = fetch_provider_catalog(port, token)
    if not catalog:
        raise CycloError(
            "gateway has no provisioned providers; run "
            "`cyclo gateway login <provider>` (or `--api-key`) first"
        )
    return catalog


def stop_gateway_container(container: str, *, best_effort: bool = False) -> bool:
    try:
        info = _owned_gateway_container(container)
    except CycloError:
        if best_effort:
            return False
        raise
    if info is None:
        return True
    resource_id = _resource_id(info, kind="container", name=container)
    try:
        stop_rc = runner_docker.docker_call_ignore_missing(
            ["docker", "stop", "--timeout", "10", resource_id]
        )
    except CycloError:
        if best_effort:
            return False
        raise
    if stop_rc != 0 and not best_effort:
        raise CycloError(f"failed to stop gateway container: {container}")

    try:
        remove_rc = runner_docker.docker_call_ignore_missing(["docker", "rm", resource_id])
    except CycloError:
        if best_effort:
            return False
        raise
    if remove_rc != 0 and not best_effort:
        raise CycloError(f"failed to remove gateway container: {container}")
    return stop_rc == 0 and remove_rc == 0


def stop_shared_gateway(registry_dir) -> None:
    """Best-effort shutdown for the system gateway and network.

    Reconciliation uses strict container removal through ``start_gateway``;
    system shutdown remains tolerant so the credential store volume persists
    and a cleanup failure does not mask an earlier lifecycle error.
    """
    stop_gateway_container(gateway_container_name(registry_dir), best_effort=True)
    remove_network(gateway_network_name(registry_dir), best_effort=True)
