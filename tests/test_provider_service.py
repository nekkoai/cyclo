from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.errors import CycloError
from cyclo.host_config import HostConfig
from cyclo.provider_service import (
    PROVIDER_RUNTIME_CONFIG_DIR,
    PROVIDER_RUNTIME_CONFIG_FILE,
    PROVIDER_RUNTIME_PROVIDER_SOCKET_ROOT,
    PROVIDER_RUNTIME_SOCKET_DIR,
    PROVIDER_RUNTIME_STATE,
    ProviderRuntimeConfig,
    ProviderService,
    provider_runtime_config_fingerprint,
    provider_runtime_private_socket_dir,
    provider_runtime_run_command,
)
from cyclo.state import Instance, StateStore


def private_token(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def runtime_config(tmp_path: Path) -> ProviderRuntimeConfig:
    config_dir = tmp_path / "etc" / "cyclo"
    config_dir.mkdir(parents=True)
    host_config = config_dir / "host.conf"
    host_config.write_text("# first\n", encoding="utf-8")
    state = tmp_path / "state"
    runtime_sockets = state / "sockets" / "runtime"
    provider_sockets = state / "sockets" / "providers"
    runtime_sockets.mkdir(parents=True)
    provider_sockets.mkdir(parents=True)
    return ProviderRuntimeConfig(
        image="cyclo-provider-runtime:test",
        container="cyclo-provider-runtime-test",
        gateway_container="cyclo-gateway-test",
        gateway_network="cyclo-gateway-net-test",
        host_config=host_config,
        state_root=state,
        runtime_socket_dir=runtime_sockets,
        provider_socket_root=provider_sockets,
        admin_token_file=private_token(state / "admin.token", "admin"),
        gateway_token_file=private_token(tmp_path / "gateway" / "gateway-token", "gateway"),
    )


def test_runtime_fingerprint_changes_with_host_config_contents(tmp_path: Path) -> None:
    config = runtime_config(tmp_path)
    first = provider_runtime_config_fingerprint(config, "source")
    config.host_config.write_text("provider changed opaque content\n", encoding="utf-8")
    assert provider_runtime_config_fingerprint(config, "source") != first


def test_runtime_fingerprint_changes_when_host_config_is_atomically_replaced(
    tmp_path: Path,
) -> None:
    config = runtime_config(tmp_path)
    first = provider_runtime_config_fingerprint(config, "source")
    replacement = config.host_config.with_name("host.conf.new")
    replacement.write_text("# replacement\n", encoding="utf-8")
    replacement.replace(config.host_config)

    assert provider_runtime_config_fingerprint(config, "source") != first


def test_runtime_run_command_has_readonly_config_and_stable_socket_mounts(
    tmp_path: Path,
) -> None:
    config = runtime_config(tmp_path)
    command = provider_runtime_run_command(
        config,
        source_fingerprint="source",
        config_fingerprint="config",
        gateway_network_id="gateway-network-id",
    )
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]

    assert f"{os.getuid()}:{os.getgid()}" in command
    assert f"type=bind,src={config.host_config.resolve()},dst={PROVIDER_RUNTIME_CONFIG_FILE},readonly" in mounts
    assert not any(f"src={config.host_config.parent}," in mount for mount in mounts)
    assert f"type=bind,src={config.state_root},dst={PROVIDER_RUNTIME_STATE}" in mounts
    private_socket_dir = provider_runtime_private_socket_dir(config)
    assert (
        f"type=bind,src={config.runtime_socket_dir},dst={private_socket_dir}"
        in mounts
    )
    assert not any(f"dst={PROVIDER_RUNTIME_SOCKET_DIR}" in mount for mount in mounts)
    assert (
        f"type=bind,src={config.provider_socket_root},dst={PROVIDER_RUNTIME_PROVIDER_SOCKET_ROOT},readonly"
        in mounts
    )
    assert (
        f"CYCLO_PROVIDER_RUNTIME_SOCKET_ROOT={private_socket_dir}"
        in command
    )
    assert (
        f"CYCLO_PROVIDER_RUNTIME_ADMIN_SOCKET={private_socket_dir / 'admin.sock'}"
        in command
    )
    assert not any("CYCLO_GATEWAY_SOCKET=" in value for value in command)


def test_runtime_run_command_resolves_host_config_symlink_before_binding(
    tmp_path: Path,
) -> None:
    config = runtime_config(tmp_path)
    target = config.host_config.with_name("real-host.conf")
    config.host_config.replace(target)
    config.host_config.symlink_to(target)

    command = provider_runtime_run_command(
        config,
        source_fingerprint="source",
        config_fingerprint="config",
        gateway_network_id="gateway-network-id",
    )
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]

    assert f"type=bind,src={target.resolve()},dst={PROVIDER_RUNTIME_CONFIG_FILE},readonly" in mounts
    assert not any(f"src={config.host_config}," in mount for mount in mounts)


def test_runtime_run_command_omits_missing_host_config_mount(
    tmp_path: Path,
) -> None:
    config = replace(runtime_config(tmp_path), host_config=None)
    command = provider_runtime_run_command(
        config,
        source_fingerprint="source",
        config_fingerprint="config",
        gateway_network_id="gateway-network-id",
    )
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]

    assert not any(str(PROVIDER_RUNTIME_CONFIG_FILE) in mount for mount in mounts)
    assert f"CYCLO_HOST_CONFIG={PROVIDER_RUNTIME_CONFIG_FILE}" in command


def provider_source(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "Dockerfile").write_text(
        'FROM scratch\nENTRYPOINT ["/provider"]\n', encoding="utf-8"
    )
    return path


def active_instance() -> Instance:
    return Instance(
        id="team-one",
        team_name="one",
        team_path="/team",
        project_path="/project",
        generation="generation",
        providers=["top"],
        models=["top/result"],
        container_name="cyclo-team-one",
        network_name="cyclo-team-one-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=True,
    )


def bind_team_addresses(
    service: ProviderService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_team_local_addresses",
        lambda instances: {
            instance.id: ("172.30.0.2",)
            for instance in instances
        },
    )


def test_team_local_addresses_are_taken_from_runtime_network_interfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    monkeypatch.setattr(
        service,
        "_owned_container",
        lambda: {
            "NetworkSettings": {
                "Networks": {
                    active_instance().network_name: {
                        "IPAddress": "172.30.0.2",
                        "GlobalIPv6Address": "2001:db8::2",
                    }
                }
            }
        },
    )

    assert service._team_local_addresses((active_instance(),)) == {
        "team-one": ("172.30.0.2", "2001:db8::2")
    }


def test_client_update_publishes_virtual_runtime_and_concrete_gateway_scopes(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    leaf = provider_source(tmp_path / "leaf-source")
    top = provider_source(tmp_path / "top-source")
    host_path = config_dir / "host.conf"
    host_path.write_text(
        f"provider leaf {leaf} account/model\n"
        f"provider top {top} leaf/output\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    provider_record = {
        "client_id": "host-provider-leaf-test",
        "kind": "provider",
        "provider_prefix": "leaf",
        "team_id": "provider:leaf",
        "binding_generation": "provider-generation",
        "token_sha256": "a" * 64,
        "providers": ["account"],
        "models": ["account/model"],
        "enabled": True,
        "revoked": False,
        "expires_at": None,
    }

    tokens = service.update_clients(
        (active_instance(),),
        provider_clients=(provider_record,),
        apply_runtime=False,
    )

    runtime_path = store.provider_runtime_root / "clients.json"
    gateway_path = store.gateway_registry / "runs" / "gateway" / "client-registry" / "clients.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))["clients"]
    gateway = json.loads(gateway_path.read_text(encoding="utf-8"))["clients"]
    team_runtime = next(record for record in runtime if record["kind"] == "team")

    assert team_runtime["models"] == ["top/result"]
    assert team_runtime["local_addresses"] == []
    assert provider_record in runtime
    assert len(gateway) == 1
    assert gateway[0]["client_id"] == "team-one"
    assert gateway[0]["models"] == ["account/model"]
    assert gateway[0]["providers"] == ["account"]
    assert "local_addresses" not in gateway[0]
    assert gateway[0]["token_sha256"] == hashlib.sha256(
        tokens["team-one"].encode("utf-8")
    ).hexdigest()
    assert runtime_path.stat().st_mode & 0o777 == 0o600
    assert gateway_path.stat().st_mode & 0o777 == 0o644
    assert gateway_path.parent.stat().st_mode & 0o777 == 0o755


def test_client_registry_mode_failure_cannot_publish_new_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "registry" / "clients.json"
    path.parent.mkdir()
    path.write_text('{"old": true}\n', encoding="utf-8")

    def fail_mode(_descriptor: int, _mode: int) -> None:
        raise OSError("injected mode failure")

    monkeypatch.setattr("cyclo.provider_service.os.fchmod", fail_mode)

    with pytest.raises(CycloError, match="cannot publish client registry"):
        ProviderService._write_client_registry(path, ())

    assert path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(path.parent.glob(".clients.json.tmp.*")) == []


def registry_clients(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))["clients"]


@pytest.mark.parametrize(
    ("control", "path"),
    [
        ("reload_control", "/_cyclo/v1/control/reload"),
        ("refresh_catalog_control", "/_cyclo/v1/control/refresh-catalog"),
    ],
)
def test_runtime_controls_use_authenticated_private_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    path: str,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    captured: dict[str, object] = {}

    def unix_http_request(
        socket_path,
        method,
        request_path,
        *,
        token,
        body,
        timeout,
    ):
        captured.update(
            socket_path=socket_path,
            method=method,
            path=request_path,
            token=token,
            body=body,
        )
        captured["timeout"] = timeout
        return 204, b""

    def runtime_port(*, require_current: bool) -> int:
        assert require_current is True
        return 4321

    monkeypatch.setattr(service, "_runtime_port", runtime_port)
    monkeypatch.setattr(service, "admin_token", lambda: "private-admin")
    monkeypatch.setattr(
        "cyclo.provider_service._unix_http_request", unix_http_request
    )

    getattr(service, control)(attempts=1, timeout=0.25)

    assert captured["socket_path"] == service.admin_socket_file
    assert captured["method"] == "POST"
    assert captured["path"] == path
    assert captured["body"] == b""
    assert captured["token"] == "private-admin"
    assert captured["timeout"] == 0.25


def test_catalog_can_explicitly_refresh_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    events: list[object] = []
    monkeypatch.setattr(
        service, "refresh_catalog_control", lambda: events.append("refresh")
    )
    monkeypatch.setattr(
        service,
        "_request",
        lambda path, token: events.append((path, token)) or {"account": {}},
    )

    assert service.catalog("team-token", refresh=True) == {"account": {}}
    assert events == ["refresh", ("/providers", "team-token")]


def test_unacknowledged_catalog_refresh_keeps_the_previous_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    attempts: list[tuple[str, str, float]] = []

    def fail_refresh(
        path: str,
        operation: str,
        *,
        require_current: bool,
        timeout: float,
    ) -> None:
        assert require_current is True
        attempts.append((path, operation, timeout))
        raise CycloError("injected unavailable gateway")

    monkeypatch.setattr(service, "_control_request_once", fail_refresh)
    monkeypatch.setattr(
        service,
        "stop",
        lambda: pytest.fail("catalog refresh failure must not stop the runtime"),
    )
    monkeypatch.setattr("cyclo.provider_service.time.sleep", lambda _delay: None)

    with pytest.raises(CycloError, match="catalog refresh could not be acknowledged"):
        service.refresh_catalog_control()

    assert len(attempts) == 3
    assert all(
        path.endswith("/refresh-catalog")
        for path, _operation, _timeout in attempts
    )


def test_client_update_acknowledges_both_runtime_phases_around_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    bind_team_addresses(service, monkeypatch)
    runtime_path = store.provider_runtime_root / "clients.json"
    gateway_path = (
        store.gateway_registry
        / "runs"
        / "gateway"
        / "client-registry"
        / "clients.json"
    )
    events: list[str] = []
    original_write = service._write_client_registry

    def record_write(path, records, *, public_hashes=False):
        if path == runtime_path:
            events.append("runtime")
        elif path == gateway_path:
            events.append("gateway")
        else:
            raise AssertionError(f"unexpected registry path: {path}")
        return original_write(path, records, public_hashes=public_hashes)

    monkeypatch.setattr(service, "_write_client_registry", record_write)
    monkeypatch.setattr(
        service,
        "reload_control",
        lambda *, require_current=True: events.append(
            "reload-current" if require_current else "reload-stale-safe"
        ),
    )

    service.update_clients((active_instance(),))

    assert events == [
        "runtime",
        "reload-stale-safe",
        "gateway",
        "runtime",
        "reload-current",
    ]


def test_unacknowledged_bridge_reload_stops_runtime_before_gateway_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    bind_team_addresses(service, monkeypatch)
    service.update_clients((active_instance(),), apply_runtime=False)
    runtime_path = store.provider_runtime_root / "clients.json"
    gateway_path = (
        store.gateway_registry
        / "runs"
        / "gateway"
        / "client-registry"
        / "clients.json"
    )
    old_gateway = gateway_path.read_bytes()
    attempts: list[tuple[str, str, float]] = []
    stopped: list[bool] = []

    def fail_reload(
        path: str,
        operation: str,
        *,
        require_current: bool,
        timeout: float,
    ) -> None:
        assert require_current is False
        attempts.append((path, operation, timeout))
        raise CycloError("injected missing acknowledgement")

    monkeypatch.setattr(service, "_control_request_once", fail_reload)
    monkeypatch.setattr(service, "stop", lambda: stopped.append(True) or True)
    monkeypatch.setattr("cyclo.provider_service.time.sleep", lambda _delay: None)
    changed = replace(active_instance(), generation="next-generation")

    with pytest.raises(CycloError, match="provider runtime was stopped"):
        service.update_clients((changed,))

    assert attempts == [
        (
            "/_cyclo/v1/control/reload",
            "capability activation/revocation",
            2.0,
        )
    ] * 3
    assert stopped == [True]
    assert registry_clients(runtime_path) == []
    assert gateway_path.read_bytes() == old_gateway


def test_interrupted_bridge_reload_emergency_stops_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    bind_team_addresses(service, monkeypatch)
    service.update_clients((active_instance(),), apply_runtime=False)
    stopped: list[bool] = []

    def interrupt_reload(
        _path: str,
        _operation: str,
        *,
        require_current: bool,
        timeout: float,
    ) -> None:
        assert require_current is False
        assert timeout == 2.0
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_control_request_once", interrupt_reload)
    monkeypatch.setattr(service, "stop", lambda: stopped.append(True) or True)

    with pytest.raises(KeyboardInterrupt):
        service.update_clients(
            (replace(active_instance(), generation="next-generation"),)
        )

    assert stopped == [True]


def test_final_capability_activation_requires_a_current_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    bind_team_addresses(service, monkeypatch)
    service.update_clients((active_instance(),), apply_runtime=False)
    freshness_checks: list[bool] = []
    requests = 0
    stopped: list[bool] = []

    def runtime_port(*, require_current: bool) -> int:
        freshness_checks.append(require_current)
        if freshness_checks == [False, True, True]:
            raise CycloError("runtime became stale")
        return 4321

    def unix_http_request(
        _socket_path,
        _method,
        _path,
        *,
        token,
        body,
        timeout,
    ):
        nonlocal requests
        assert token == "private-admin"
        assert body == b""
        assert timeout == 2.0
        requests += 1
        return 204, b""

    monkeypatch.setattr(service, "_runtime_port", runtime_port)
    monkeypatch.setattr(service, "admin_token", lambda: "private-admin")
    monkeypatch.setattr(
        "cyclo.provider_service._unix_http_request", unix_http_request
    )
    monkeypatch.setattr(service, "stop", lambda: stopped.append(True) or True)
    monkeypatch.setattr("cyclo.provider_service.time.sleep", lambda _delay: None)

    with pytest.raises(CycloError, match="provider runtime was stopped"):
        service.update_clients(
            (replace(active_instance(), generation="next-generation"),)
        )

    assert freshness_checks == [False, True, True]
    assert requests == 2
    assert stopped == [True]


def test_client_update_gateway_failure_leaves_revoking_runtime_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    service.update_clients((active_instance(),), apply_runtime=False)
    runtime_path = store.provider_runtime_root / "clients.json"
    gateway_path = store.gateway_registry / "runs" / "gateway" / "client-registry" / "clients.json"
    old_gateway = gateway_path.read_bytes()
    original_write = service._write_client_registry
    calls = 0

    def fail_gateway(path, records, *, public_hashes=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CycloError("injected gateway publication failure")
        return original_write(path, records, public_hashes=public_hashes)

    monkeypatch.setattr(service, "_write_client_registry", fail_gateway)
    changed = replace(active_instance(), generation="next-generation")

    with pytest.raises(CycloError, match="injected gateway"):
        service.update_clients((changed,), apply_runtime=False)

    assert registry_clients(runtime_path) == []
    assert gateway_path.read_bytes() == old_gateway


def test_client_update_runtime_final_failure_never_exposes_new_runtime_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    service.update_clients((active_instance(),), apply_runtime=False)
    runtime_path = store.provider_runtime_root / "clients.json"
    gateway_path = store.gateway_registry / "runs" / "gateway" / "client-registry" / "clients.json"
    original_write = service._write_client_registry
    calls = 0

    def fail_runtime_final(path, records, *, public_hashes=False):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise CycloError("injected runtime final publication failure")
        return original_write(path, records, public_hashes=public_hashes)

    monkeypatch.setattr(service, "_write_client_registry", fail_runtime_final)
    changed = replace(active_instance(), generation="next-generation")

    with pytest.raises(CycloError, match="injected runtime final"):
        service.update_clients((changed,), apply_runtime=False)

    assert registry_clients(runtime_path) == []
    assert registry_clients(gateway_path)[0]["binding_generation"] == "next-generation"

    monkeypatch.setattr(service, "_write_client_registry", original_write)
    service.update_clients((changed,), apply_runtime=False)
    assert registry_clients(runtime_path)[0]["binding_generation"] == "next-generation"


def test_remove_provider_clients_never_rewrites_gateway_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_path = tmp_path / "host.conf"
    host_path.write_text("", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    service = ProviderService(store, HostConfig(host_path))
    provider = {
        "client_id": "host-provider-test",
        "kind": "provider",
        "provider_prefix": "test",
        "team_id": "provider:test",
        "binding_generation": "generation",
        "token_sha256": "a" * 64,
        "providers": ["account"],
        "models": ["account/model"],
        "enabled": True,
        "revoked": False,
        "expires_at": None,
    }
    service.update_clients(
        (active_instance(),),
        provider_clients=(provider,),
        apply_runtime=False,
    )
    gateway_path = store.gateway_registry / "runs" / "gateway" / "client-registry" / "clients.json"
    gateway_before = gateway_path.read_bytes()
    reloads = 0

    def reload_control(*, require_current: bool = True) -> None:
        nonlocal reloads
        assert require_current is False
        reloads += 1

    monkeypatch.setattr(service, "reload_control", reload_control)

    service.remove_provider_clients(("test",))

    assert reloads == 1
    assert gateway_path.read_bytes() == gateway_before
    assert all(
        record.get("kind") != "provider"
        for record in registry_clients(store.provider_runtime_root / "clients.json")
    )


class FakeComponentRuntime:
    def __init__(self, *, running: bool = True, logs: str = "provider log") -> None:
        self.running = running
        self.logs = logs

    def container_running(self, _identity) -> bool:
        return self.running

    def status(self, _identity):
        return SimpleNamespace(
            container_running=self.running,
            container_exists=True,
        )

    def logs_tail(self, _identity, *, lines: int) -> str:
        assert lines == 40
        return self.logs


def test_wait_provider_requires_component_generation_and_fresh_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    catalogs = iter(
        (
            {
                "local": {
                    "kind": "component",
                    "generation": "expected",
                    "registered_at": "2026-01-01T00:00:00.000Z",
                }
            },
            {
                "local": {
                    "kind": "component",
                    "generation": "expected",
                    "registered_at": "2026-01-01T00:00:02.000Z",
                }
            },
        )
    )
    monkeypatch.setattr(service, "catalog", lambda: next(catalogs))
    monkeypatch.setattr("cyclo.provider_service.time.sleep", lambda _delay: None)
    marker = service._registered_timestamp("2026-01-01T00:00:01.000Z")
    assert marker is not None

    service.wait_provider(
        "local",
        "expected",
        runtime=FakeComponentRuntime(),
        identity=SimpleNamespace(),
        registered_after=marker,
        timeout=1,
    )


def test_wait_provider_rejects_concrete_collision_with_status_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )
    monkeypatch.setattr(
        service,
        "catalog",
        lambda: {"local": {"api": "openai", "models": []}},
    )
    moments = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr("cyclo.provider_service.time.monotonic", lambda: next(moments))
    monkeypatch.setattr("cyclo.provider_service.time.sleep", lambda _delay: None)

    with pytest.raises(CycloError, match="not a provider component") as failure:
        service.wait_provider(
            "local",
            "expected",
            runtime=FakeComponentRuntime(logs="collision detail"),
            identity=SimpleNamespace(),
            timeout=0.5,
        )

    assert "container=running" in str(failure.value)
    assert "collision detail" in str(failure.value)


def test_wait_provider_reports_immediate_container_exit(
    tmp_path: Path,
) -> None:
    service = ProviderService(
        StateStore(tmp_path / "state"), HostConfig(tmp_path / "host.conf")
    )

    with pytest.raises(CycloError, match="exited before registration") as failure:
        service.wait_provider(
            "local",
            "expected",
            runtime=FakeComponentRuntime(running=False, logs="bad arguments"),
            identity=SimpleNamespace(),
            timeout=1,
        )

    assert "container=stopped" in str(failure.value)
    assert "bad arguments" in str(failure.value)


def test_catalog_does_not_create_missing_admin_capability(tmp_path: Path) -> None:
    host_path = tmp_path / "host.conf"
    service = ProviderService(StateStore(tmp_path / "state"), HostConfig(host_path))

    with pytest.raises(CycloError, match="cannot read provider-runtime token"):
        service.catalog()

    assert not service.admin_token_file.exists()
