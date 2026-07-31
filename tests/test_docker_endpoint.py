from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from cyclo.docker_endpoint import (
    local_docker_endpoint,
    selected_docker_endpoint,
    unix_socket_from_endpoint,
)
from cyclo.errors import CycloError


def completed(stdout: str, *, status: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(("docker",), status, stdout, stderr)


def test_selected_endpoint_is_resolved_by_docker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return completed(json.dumps("unix:///run/user/1000/docker.sock") + "\n")

    monkeypatch.setattr("cyclo.docker_endpoint.subprocess.run", run)

    assert selected_docker_endpoint({"PATH": "/usr/bin"}) == (
        "unix:///run/user/1000/docker.sock"
    )
    assert calls[0][0] == [
        "docker",
        "context",
        "inspect",
        "--format",
        '{{json (index .Endpoints "docker").Host}}',
    ]


@pytest.mark.parametrize(
    "response",
    (
        "",
        "not-json\n",
        "null\n",
        '""\n',
        '"relative"\n',
        '"unix://host/path"\n',
        '"unix:///tmp/socket?query=yes"\n',
        '"unix:///tmp/socket"\\n"extra"\n',
    ),
)
def test_selected_endpoint_rejects_malformed_context_output(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    monkeypatch.setattr(
        "cyclo.docker_endpoint.subprocess.run",
        lambda *_args, **_kwargs: completed(response),
    )

    with pytest.raises(CycloError, match="cannot resolve selected Docker endpoint"):
        selected_docker_endpoint()


def test_selected_endpoint_reports_context_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cyclo.docker_endpoint.subprocess.run",
        lambda *_args, **_kwargs: completed("", status=1, stderr="no daemon"),
    )

    with pytest.raises(CycloError, match="no daemon"):
        selected_docker_endpoint()


def test_local_endpoint_rejects_remote_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cyclo.docker_endpoint.selected_docker_endpoint",
        lambda _environment=None: "tcp://127.0.0.1:2375",
    )

    with pytest.raises(CycloError, match="only a local Docker Unix socket"):
        local_docker_endpoint()


def test_local_endpoint_requires_and_canonicalizes_a_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    monkeypatch.setattr(
        "cyclo.docker_endpoint.selected_docker_endpoint",
        lambda _environment=None: f"unix://{path}",
    )
    try:
        assert local_docker_endpoint() == f"unix://{path.resolve()}"
    finally:
        listener.close()


def test_unix_endpoint_path_is_percent_decoded(tmp_path: Path) -> None:
    assert unix_socket_from_endpoint("unix:///tmp/a%20b.sock") == Path(
        "/tmp/a b.sock"
    )
