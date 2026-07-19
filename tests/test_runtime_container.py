from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.host_config import HostConfig
from cyclo import runtime_container as runtime_module
from cyclo.runtime_container import (
    RUNTIME_CONFIG_FINGERPRINT_LABEL,
    RUNTIME_OWNERSHIP_LABEL,
    RUNTIME_OWNERSHIP_VALUE,
    RUNTIME_RESOURCE_LABEL,
    RUNTIME_SYSTEM_LABEL,
    RuntimeContainer,
)
from cyclo.state import StateStore


def runtime(tmp_path: Path) -> RuntimeContainer:
    return RuntimeContainer(
        StateStore(tmp_path / "state"),
        HostConfig(tmp_path / "host.conf"),
        image="cyclo-provider-runtime:test",
    )


def container_info(
    selected: RuntimeContainer,
    *,
    fingerprint: str = "expected-config",
    running: bool = True,
    network_id: str = "gateway-network-id",
) -> dict[str, object]:
    return {
        "Id": "immutable-container-id",
        "Name": f"/{selected.container_name}",
        "Config": {
            "Labels": {
                RUNTIME_OWNERSHIP_LABEL: RUNTIME_OWNERSHIP_VALUE,
                RUNTIME_SYSTEM_LABEL: runtime_module._system_id(selected.state_root),
                RUNTIME_RESOURCE_LABEL: selected.container_name,
                RUNTIME_CONFIG_FINGERPRINT_LABEL: fingerprint,
            }
        },
        "State": {"Running": running},
        "NetworkSettings": {
            "Networks": {
                selected.gateway_network: {"NetworkID": network_id},
            }
        },
    }


def patch_start_preflight(
    selected: RuntimeContainer,
    monkeypatch: pytest.MonkeyPatch,
    owned_container,
) -> None:
    monkeypatch.setattr(selected, "_prepare_layout", lambda: None)
    monkeypatch.setattr(selected, "_require_current_image", lambda: "source")
    monkeypatch.setattr(selected, "config", lambda: object())
    monkeypatch.setattr(selected, "_owned_container", owned_container)
    monkeypatch.setattr(
        selected.credential_gateway,
        "validate_running",
        lambda: ("gateway-id", "gateway-network-id", 8787),
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_config_fingerprint",
        lambda _config, _source: "expected-config",
    )


def test_owned_container_rejects_a_foreign_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    foreign = container_info(selected)
    foreign["Config"]["Labels"][RUNTIME_OWNERSHIP_LABEL] = "0"  # type: ignore[index]
    monkeypatch.setattr(selected, "_inspect", lambda _kind, _name: foreign)

    with pytest.raises(CycloError, match="owned outside"):
        selected._owned_container()


def test_status_requires_matching_configuration_image_and_gateway_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    info = container_info(selected)
    monkeypatch.setattr(selected, "_owned_container", lambda: info)
    monkeypatch.setattr(selected, "config", lambda: object())
    monkeypatch.setattr(selected, "_image_current", lambda _source: True)
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_source_fingerprint",
        lambda: "source",
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_config_fingerprint",
        lambda _config, _source: "expected-config",
    )

    assert selected.status().current is True

    info["Config"]["Labels"][RUNTIME_CONFIG_FINGERPRINT_LABEL] = "stale"  # type: ignore[index]
    assert selected.status().current is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_status_rejects_nonoperational_container_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    selected = runtime(tmp_path)
    info = container_info(selected)
    info["State"][flag] = True  # type: ignore[index]
    monkeypatch.setattr(selected, "_owned_container", lambda: info)
    monkeypatch.setattr(selected, "config", lambda: object())
    monkeypatch.setattr(selected, "_image_current", lambda _source: True)
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_source_fingerprint",
        lambda: "source",
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_config_fingerprint",
        lambda _config, _source: "expected-config",
    )

    assert selected.status().running is False


def test_start_reuses_a_current_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    info = container_info(selected)
    events: list[str] = []
    monkeypatch.setattr(selected, "_prepare_layout", lambda: events.append("layout"))
    monkeypatch.setattr(selected, "_require_current_image", lambda: "source")
    monkeypatch.setattr(selected, "config", lambda: object())
    monkeypatch.setattr(selected, "_owned_container", lambda: info)
    monkeypatch.setattr(
        selected.credential_gateway,
        "validate_running",
        lambda: ("gateway-id", "gateway-network-id", 8787),
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_config_fingerprint",
        lambda _config, _source: "expected-config",
    )
    monkeypatch.setattr(
        selected,
        "_run",
        lambda _command, **_kwargs: pytest.fail(
            "a current runtime must not be recreated"
        ),
    )
    monkeypatch.setattr(
        selected,
        "wait_healthy",
        lambda: pytest.fail("a retained runtime is already healthy"),
    )

    selected.start()

    assert events == ["layout"]


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_start_never_reuses_a_nonoperational_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    selected = runtime(tmp_path)
    info = container_info(selected)
    info["State"][flag] = True  # type: ignore[index]
    patch_start_preflight(selected, monkeypatch, lambda: info)
    monkeypatch.setattr(
        selected,
        "_run",
        lambda _command, **_kwargs: pytest.fail(
            "a nonoperational runtime must not be reused"
        ),
    )
    monkeypatch.setattr(
        selected,
        "wait_healthy",
        lambda: pytest.fail("a nonoperational runtime must not report healthy"),
    )

    with pytest.raises(CycloError, match="not operational.*runtime restart"):
        selected.start()


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_replace_replaces_a_nonoperational_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    selected = runtime(tmp_path)
    current = container_info(selected)
    current["State"][flag] = True  # type: ignore[index]
    started = container_info(selected)
    inspections = iter((current, started))
    events: list[object] = []
    patch_start_preflight(selected, monkeypatch, lambda: next(inspections))
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_run_command",
        lambda _config, **_kwargs: ["docker", "run", "runtime"],
    )
    monkeypatch.setattr(
        selected,
        "_remove_container",
        lambda: events.append("removed") or True,
    )
    monkeypatch.setattr(
        selected,
        "_run",
        lambda command, **_kwargs: events.append(list(command)),
    )
    monkeypatch.setattr(selected, "wait_healthy", lambda: events.append("healthy"))

    selected.start(replace=True)

    assert events == ["removed", ["docker", "run", "runtime"], "healthy"]


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_new_nonoperational_container_is_removed_before_start_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    selected = runtime(tmp_path)
    started = container_info(selected)
    started["State"][flag] = True  # type: ignore[index]
    inspections = iter((None, started))
    events: list[object] = []
    patch_start_preflight(selected, monkeypatch, lambda: next(inspections))
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_run_command",
        lambda _config, **_kwargs: ["docker", "run", "runtime"],
    )
    monkeypatch.setattr(
        selected,
        "_run",
        lambda command, **_kwargs: events.append(list(command)),
    )
    monkeypatch.setattr(
        selected,
        "_remove_container",
        lambda: events.append("removed") or True,
    )
    monkeypatch.setattr(
        selected,
        "wait_healthy",
        lambda: pytest.fail("structurally nonoperational container must not be probed"),
    )

    with pytest.raises(CycloError, match="did not start in an operational state"):
        selected.start()

    assert events == [["docker", "run", "runtime"], "removed"]


def test_removal_revalidates_ownership_and_mutates_by_immutable_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    info = container_info(selected)
    ownership_checks = 0
    commands: list[list[str]] = []

    def owned_container():
        nonlocal ownership_checks
        ownership_checks += 1
        return info

    monkeypatch.setattr(selected, "_owned_container", owned_container)
    monkeypatch.setattr(
        selected,
        "_inspect",
        lambda kind, name: info
        if (kind, name) == ("container", "immutable-container-id")
        else pytest.fail(f"unexpected inspection: {kind} {name}"),
    )
    monkeypatch.setattr(
        selected,
        "_run",
        lambda command, **_kwargs: commands.append(list(command)),
    )

    assert selected._remove_container() is True
    assert ownership_checks == 2
    assert commands == [
        ["docker", "stop", "--timeout", "10", "immutable-container-id"],
        ["docker", "rm", "immutable-container-id"],
    ]


def test_failed_health_check_rolls_back_the_new_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    started = container_info(selected)
    inspections = iter((None, started))
    events: list[object] = []
    monkeypatch.setattr(selected, "_prepare_layout", lambda: None)
    monkeypatch.setattr(selected, "_require_current_image", lambda: "source")
    monkeypatch.setattr(selected, "config", lambda: object())
    monkeypatch.setattr(selected, "_owned_container", lambda: next(inspections))
    monkeypatch.setattr(
        selected.credential_gateway,
        "validate_running",
        lambda: ("gateway-id", "gateway-network-id", 8787),
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_config_fingerprint",
        lambda _config, _source: "expected-config",
    )
    monkeypatch.setattr(
        runtime_module,
        "provider_runtime_run_command",
        lambda _config, **_kwargs: ["docker", "run", "runtime"],
    )
    monkeypatch.setattr(
        selected,
        "_run",
        lambda command, **_kwargs: events.append(list(command)),
    )
    monkeypatch.setattr(
        selected,
        "wait_healthy",
        lambda: (_ for _ in ()).throw(CycloError("unhealthy")),
    )
    monkeypatch.setattr(
        selected,
        "_remove_container",
        lambda: events.append("removed") or True,
    )

    with pytest.raises(CycloError, match="unhealthy"):
        selected.start()

    assert events == [["docker", "run", "runtime"], "removed"]


class _HealthResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body[:size] if size >= 0 else self.body


def test_probe_operational_requires_exact_gateway_and_runtime_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    requests: list[tuple[str, float]] = []
    responses = iter(
        (_HealthResponse(200, b"ok\n"), _HealthResponse(200, b"ok\n"))
    )
    monkeypatch.setattr(
        selected.credential_gateway,
        "validate_running",
        lambda: ("gateway-id", "network-id", 4101),
    )
    monkeypatch.setattr(selected, "require_running", lambda: 4102)

    def open_health(url: str, *, timeout: float):
        requests.append((url, timeout))
        return next(responses)

    monkeypatch.setattr(runtime_module.gateway_runtime, "_open_loopback", open_health)

    selected.probe_operational(timeout=0.75)

    assert requests == [
        ("http://127.0.0.1:4101/health", 0.75),
        ("http://127.0.0.1:4102/health", 0.75),
    ]


@pytest.mark.parametrize(
    ("status", "body"),
    [(503, b"ok\n"), (200, b"ok"), (200, b"ok\nextra")],
)
def test_probe_healthy_rejects_every_nonexact_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    selected = runtime(tmp_path)
    monkeypatch.setattr(selected, "require_running", lambda: 4102)
    monkeypatch.setattr(
        runtime_module.gateway_runtime,
        "_open_loopback",
        lambda _url, *, timeout: _HealthResponse(status, body),
    )

    with pytest.raises(CycloError, match="unexpected response"):
        selected.probe_healthy(timeout=0.5)
