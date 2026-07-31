from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from cyclo.dcomp import (
    DCOMP_API_VERSION,
    DCompClient,
    DCompComponentStatus,
    DCompNetworkStatus,
    DCompPublishedPort,
)
from cyclo.errors import CycloError
from cyclo.state import StateStore


VERSION = json.dumps({"version": "0.1.0", "api_version": DCOMP_API_VERSION})


def status_json(*, operational: bool = True, name: str = "cyclo-test") -> str:
    return json.dumps(
        {
            "api_version": DCOMP_API_VERSION,
            "name": name,
            "desired": True,
            "operational": operational,
            "digest": "sha256:composition",
            "operation": "",
            "phase": "",
            "networks": [
                {
                    "key": "link:gateway.provider",
                    "id": "network-id",
                    "internal": True,
                    "problem": "",
                }
            ],
            "components": [
                {
                    "name": "team",
                    "container_id": "container-id",
                    "status": "running",
                    "health": "healthy",
                    "exit_code": 0,
                    "problem": "",
                    "published_ports": [
                        {
                            "protocol": "tcp",
                            "host_ip": "0.0.0.0",
                            "host_port": 49152,
                            "container_port": 4137,
                        }
                    ],
                }
            ],
        }
    )


def install_fake_discovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: str | None = "/opt/dcomp/bin/dcomp",
) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []

    def which(command: str, *, path: str | None = None) -> str | None:
        calls.append((command, path))
        return result

    monkeypatch.setattr("cyclo.dcomp.shutil.which", which)
    return calls


def test_discovers_override_and_pins_state_and_docker_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered = install_fake_discovery(monkeypatch)
    store = StateStore(tmp_path / "cyclo")
    store._docker_endpoint = "unix:///run/user/1000/docker.sock"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object):
        calls.append((list(command), dict(options)))
        stdout = VERSION if command[-2:] == ["version", "--json"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        store,
        environment={
            "PATH": "/custom/bin",
            "CYCLO_DCOMP": "dcomp-dev",
            "DOCKER_CONTEXT": "wrong-context",
        },
    )
    client.up(tmp_path / "system.dcomp")

    assert discovered == [("dcomp-dev", "/custom/bin")]
    assert client.executable == "/opt/dcomp/bin/dcomp"
    assert calls[0][0] == ["/opt/dcomp/bin/dcomp", "version", "--json"]
    assert calls[1][0] == [
        "/opt/dcomp/bin/dcomp",
        "--state-root",
        str(tmp_path / "cyclo" / "dcomp"),
        "up",
        str(tmp_path / "system.dcomp"),
    ]
    for _command, options in calls:
        environment = options["env"]
        assert isinstance(environment, dict)
        assert environment["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
        assert "DOCKER_CONTEXT" not in environment


def test_endpoint_can_be_bound_after_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    environments = []

    def run(command, **options):
        environments.append(options["env"])
        return subprocess.CompletedProcess(command, 0, VERSION, "")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin", "DOCKER_CONTEXT": "ambient"},
    )
    client.bind_docker("unix:///run/docker.sock")
    client.version()

    assert environments[0]["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert "DOCKER_CONTEXT" not in environments[0]


def test_uses_path_without_any_repository_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered = install_fake_discovery(monkeypatch, result=None)

    with pytest.raises(CycloError, match="not installed or not on PATH"):
        DCompClient(
            StateStore(tmp_path / "state"),
            environment={"PATH": "/usr/bin"},
        )

    assert discovered == [("dcomp", "/usr/bin")]


def test_empty_or_missing_override_fails_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch, result=None)
    with pytest.raises(CycloError, match="CYCLO_DCOMP is empty"):
        DCompClient(
            StateStore(tmp_path / "empty"),
            environment={"PATH": "/usr/bin", "CYCLO_DCOMP": ""},
        )

    with pytest.raises(CycloError, match="from CYCLO_DCOMP was not found"):
        DCompClient(
            StateStore(tmp_path / "missing"),
            environment={"PATH": "/usr/bin", "CYCLO_DCOMP": "/missing/dcomp"},
        )


def test_rejects_incompatible_or_malformed_machine_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    replies = iter(
        [
            json.dumps({"version": "9.0.0", "api_version": 2}),
            "dcomp version 0.1.0",
        ]
    )

    def run(command: list[str], **_options: object):
        return subprocess.CompletedProcess(command, 0, next(replies), "")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    with pytest.raises(CycloError, match="incompatible dcomp machine API"):
        client.check(tmp_path / "system.dcomp")
    with pytest.raises(CycloError, match="expected one JSON object"):
        client.check(tmp_path / "system.dcomp")


def test_parses_typed_machine_status_and_accepts_degraded_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **_options: object):
        commands.append(list(command))
        if command[-2:] == ["version", "--json"]:
            return subprocess.CompletedProcess(command, 0, VERSION, "")
        return subprocess.CompletedProcess(
            command,
            1,
            status_json(operational=False),
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    status = client.status("cyclo-test")

    assert not status.operational
    assert status.networks == (
        DCompNetworkStatus(
            key="link:gateway.provider",
            id="network-id",
            internal=True,
            problem="",
        ),
    )
    assert status.components == (
        DCompComponentStatus(
            name="team",
            container_id="container-id",
            status="running",
            health="healthy",
            exit_code=0,
            problem="",
            published_ports=(
                DCompPublishedPort(
                    protocol="tcp",
                    host_ip="0.0.0.0",
                    host_port=49152,
                    container_port=4137,
                ),
            ),
        ),
    )
    assert status.component("team") == status.components[0]
    assert status.component("missing") is None
    assert commands[1] == [
        "/opt/dcomp/bin/dcomp",
        "--state-root",
        str(tmp_path / "state" / "dcomp"),
        "status",
        "--json",
        "cyclo-test",
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"api_version": 2}, "incompatible dcomp status API"),
        ({"name": "other"}, "requested 'cyclo-test', received 'other'"),
        ({"components": {}}, "components must be an array"),
        ({"desired": 1}, "desired must be a boolean"),
    ],
)
def test_status_fails_closed_on_invalid_machine_data(
    change: dict[str, object],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    payload = json.loads(status_json())
    payload.update(change)
    replies = iter((VERSION, json.dumps(payload)))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(
            command, 0, next(replies), ""
        ),
    )
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    with pytest.raises(CycloError, match=message):
        client.status("cyclo-test")


def test_status_requires_exit_code_to_match_operational_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    replies = iter((VERSION, status_json(operational=True)))
    returncodes = iter((0, 1))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(
            command, next(returncodes), next(replies), ""
        ),
    )
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    with pytest.raises(CycloError, match="exit status disagree"):
        client.status("cyclo-test")


def test_all_control_methods_use_argv_and_check_api_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **_options: object):
        commands.append(list(command))
        stdout = VERSION if command[-2:] == ["version", "--json"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    definition = tmp_path / "system with spaces.dcomp"
    client.check(definition)
    client.up(definition)
    client.restart("cyclo-test")
    client.restart("cyclo-test", "gateway", "team")
    client.down("cyclo-test")
    client.resume("cyclo-test")
    client.abort("cyclo-test")

    prefix = [
        "/opt/dcomp/bin/dcomp",
        "--state-root",
        str(tmp_path / "state" / "dcomp"),
    ]
    assert commands == [
        ["/opt/dcomp/bin/dcomp", "version", "--json"],
        [*prefix, "check", str(definition)],
        [*prefix, "up", str(definition)],
        [*prefix, "restart", "cyclo-test"],
        [*prefix, "restart", "cyclo-test", "gateway", "team"],
        [*prefix, "down", "cyclo-test"],
        [*prefix, "resume", "cyclo-test"],
        [*prefix, "abort", "cyclo-test"],
    ]


def test_logs_stream_to_requested_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    calls: list[tuple[list[str], object]] = []
    output = io.StringIO()

    def run(command: list[str], **options: object):
        calls.append((list(command), options["stdout"]))
        stdout = VERSION if command[-2:] == ["version", "--json"] else None
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    client.logs("cyclo-test", "team", follow=True, output=output)

    assert calls[-1] == (
        [
            "/opt/dcomp/bin/dcomp",
            "--state-root",
            str(tmp_path / "state" / "dcomp"),
            "logs",
            "--follow",
            "cyclo-test",
            "team",
        ],
        output,
    )


def test_status_command_error_does_not_masquerade_as_degraded_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)
    calls = 0

    def run(command: list[str], **_options: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, VERSION, "")
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "cannot inspect Docker daemon",
        )

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    with pytest.raises(CycloError, match="cannot inspect Docker daemon"):
        client.status("cyclo-test")


@pytest.mark.parametrize("failure", ("exit", "missing", "os-error", "invalid"))
def test_subprocess_failures_are_cyclo_errors(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_discovery(monkeypatch)

    def run(command: list[str], **_options: object):
        if failure == "exit":
            return subprocess.CompletedProcess(
                command,
                2,
                "",
                "bad configuration\nwith context",
            )
        if failure == "missing":
            raise FileNotFoundError(command[0])
        if failure == "os-error":
            raise OSError("cannot execute")
        raise ValueError("embedded null byte")

    monkeypatch.setattr(subprocess, "run", run)
    client = DCompClient(
        StateStore(tmp_path / "state"),
        environment={"PATH": "/bin"},
    )
    with pytest.raises(CycloError, match="dcomp version"):
        client.version()
