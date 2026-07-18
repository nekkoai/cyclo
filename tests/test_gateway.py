from __future__ import annotations

from pathlib import Path

from cyclo.gateway import CredentialGateway
from cyclo.state import StateStore


def test_gateway_config_remains_credential_boundary_only(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state")
    adapter = CredentialGateway(store, gateway_image="gateway:test")
    monkeypatch.setattr(adapter.auth, "resolve_host_pi_agent_dir", lambda _home: tmp_path / "pi")

    config = adapter.config()

    assert config.gateway_image == "gateway:test"
    assert config.store_volume == "cyclo-gateway-store"
    assert config.host_models_json == tmp_path / "pi" / "models.json"
    assert config.client_registry_dir == (
        store.gateway_registry / "runs" / "gateway" / "client-registry"
    )
    assert not hasattr(config, "host_config")
    assert not hasattr(config, "provider_socket_root")


def test_gateway_restart_preserves_existing_client_registry(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state")
    adapter = CredentialGateway(store)
    calls: list[tuple] = []
    monkeypatch.setattr(adapter.gateway, "shared_token", lambda _root: "admin")
    monkeypatch.setattr(
        adapter.gateway,
        "start_gateway",
        lambda config, token, **options: calls.append(
            (config, token, options)
        )
        or {"account": {"models": []}},
    )
    monkeypatch.setattr(adapter, "_set_restart_policy", lambda: "gateway-id")
    monkeypatch.setattr(
        adapter.gateway,
        "prepare_client_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("restart must not rewrite clients")
        ),
    )

    catalog = adapter.restart(build=True)

    assert catalog == {"account": {"models": []}}
    assert calls[0][1:] == (
        "admin",
        {"build": True, "force_restart": True},
    )


def test_gateway_catalog_is_query_only(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state")
    store.gateway_registry.mkdir(parents=True)
    (store.gateway_registry / "gateway-token").write_text(
        "admin\n", encoding="utf-8"
    )
    adapter = CredentialGateway(store)
    monkeypatch.setattr(adapter.gateway, "published_port", lambda _name: 4242)
    monkeypatch.setattr(
        adapter.gateway,
        "fetch_provider_catalog",
        lambda port, token: {"seen": (port, token)},
    )
    monkeypatch.setattr(
        adapter.gateway,
        "start_gateway",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("catalog must not start gateway")
        ),
    )

    assert adapter.catalog() == {"seen": (4242, "admin")}
