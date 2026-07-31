from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from .errors import CycloError


DOCKER_ENDPOINT_FORMAT = '{{json (index .Endpoints "docker").Host}}'
DOCKER_ENDPOINT_TIMEOUT_SECONDS = 5.0


def _endpoint_error(detail: str) -> CycloError:
    normalized = " ".join(detail.split())[:512]
    return CycloError(
        "cannot resolve selected Docker endpoint"
        + (f": {normalized}" if normalized else "")
    )


def unix_socket_from_endpoint(endpoint: str) -> Path | None:
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
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in decoded
        )
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
    unix_socket_from_endpoint(endpoint)
    return endpoint


def local_docker_endpoint(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return the canonical local Unix daemon selected for this invocation."""

    endpoint = selected_docker_endpoint(environment)
    socket_path = unix_socket_from_endpoint(endpoint)
    if socket_path is None:
        raise _endpoint_error(
            "Cyclo supports only a local Docker Unix socket"
        )
    try:
        resolved = socket_path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise _endpoint_error(f"Docker socket is not resolvable: {exc}") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise _endpoint_error("Docker Unix endpoint is not a socket")
    return f"unix://{resolved}"
