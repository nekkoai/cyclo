from __future__ import annotations

import copy
import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.credential_gateway import auth, cli, commands, gateway, safe_model_fields, source


@dataclass(frozen=True)
class Client:
    project_id: str
    name: str
    generation: str


@dataclass(frozen=True)
class Config:
    gateway_image: str
    gateway_container: str
    gateway_network: str
    store_volume: str
    host_models_json: Path
    client_registry_dir: Path
    name: str = "test"


def test_registry_contains_hashes_and_scopes_but_not_capabilities(tmp_path: Path) -> None:
    root = tmp_path / "state"
    clients = [Client("two", "review", "gen-2"), Client("one", "build", "gen-1")]

    tokens = gateway.prepare_client_registry(
        root,
        clients,
        allowed_providers={"one": ("openai-codex",), "two": ("anthropic",)},
        allowed_models={
            "one": ("openai-codex/org/model",),
            "two": ("anthropic/claude-test",),
        },
    )

    assert list(tokens) == ["one", "two"]
    registry_path = gateway.host_client_registry_path(root)
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert [item["client_id"] for item in data["clients"]] == ["one", "two"]
    assert data["clients"][0]["providers"] == ["openai-codex"]
    assert data["clients"][0]["models"] == ["openai-codex/org/model"]
    assert data["clients"][0]["token_sha256"] == hashlib.sha256(
        tokens["one"].encode("utf-8")
    ).hexdigest()
    serialized = registry_path.read_text(encoding="utf-8")
    assert all(token not in serialized for token in tokens.values())
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(registry_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(gateway.client_token_path(root, "one").stat().st_mode) == 0o600


@pytest.mark.parametrize("identifier", ["../escape", "-flag", "", "has/slash"])
def test_client_id_cannot_escape_registry(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(CycloError, match="invalid gateway client id"):
        gateway.client_token_path(tmp_path, identifier)


def test_registry_rejects_duplicate_clients_and_invalid_provider(tmp_path: Path) -> None:
    with pytest.raises(CycloError, match="duplicate gateway client"):
        gateway.prepare_client_registry(
            tmp_path / "duplicate",
            [Client("same", "a", "1"), Client("same", "b", "2")],
            allowed_providers={"same": ("openai",)},
            allowed_models={"same": ("openai/model",)},
        )
    with pytest.raises(CycloError, match="invalid gateway provider scope"):
        gateway.prepare_client_registry(
            tmp_path / "provider",
            [Client("one", "a", "1")],
            allowed_providers={"one": ("../secret",)},
            allowed_models={"one": ("../secret/model",)},
        )
    assert not gateway.client_token_path(tmp_path / "provider", "one").exists()


@pytest.mark.parametrize(
    "models",
    [
        (),
        ("model-without-provider",),
        ("other/model",),
        ("openai/",),
        ("openai/model with space",),
    ],
)
def test_registry_rejects_missing_or_cross_provider_model_scope(
    tmp_path: Path, models: tuple[str, ...]
) -> None:
    with pytest.raises(CycloError, match="invalid gateway model scope"):
        gateway.prepare_client_registry(
            tmp_path / "model",
            [Client("one", "a", "1")],
            allowed_providers={"one": ("openai",)},
            allowed_models={"one": models},
        )


def test_projection_drops_secret_and_extension_fields() -> None:
    catalog = {
        "account": {
            "api": "openai-completions",
            "models": [
                {
                    "id": "model",
                    "name": "Model",
                    "input": ["text", "audio", {"apiKey": "secret"}],
                    "cost": {"input": 1, "output": 2, "apiKey": "secret"},
                    "compat": {
                        "supportsStore": False,
                        "authorization": "secret",
                        "thinkingFormat": "qwen",
                    },
                    "apiKey": "secret",
                    "headers": {"authorization": "secret"},
                    "baseUrl": "https://provider.invalid",
                }
            ],
        }
    }

    projected = auth.projected_models_json(catalog, "http://gateway:8787", "capability")
    provider = projected["providers"]["account"]
    model = provider["models"][0]
    assert provider["apiKey"] == "capability"
    assert provider["baseUrl"] == "http://gateway:8787/p/account"
    assert model["baseUrl"] == "http://gateway:8787/p/account"
    assert model["input"] == ["text"]
    assert model["cost"] == {"input": 1, "output": 2}
    assert model["compat"] == {"supportsStore": False, "thinkingFormat": "qwen"}
    assert not ({"apiKey", "headers", "authorization"} & model.keys())
    assert "secret" not in json.dumps(projected)


def test_python_projection_consumes_the_canonical_safe_field_manifest() -> None:
    policy = safe_model_fields.SAFE_MODEL_FIELDS
    document = json.loads(
        safe_model_fields.MANIFEST_PATH.read_text(encoding="utf-8")
    )

    assert policy.schema_version == document["schemaVersion"] == 1
    assert policy.cost_fields == frozenset(document["costFields"])
    assert policy.input_types == frozenset(document["inputTypes"])
    assert policy.compat_boolean_fields == frozenset(document["compatBooleanFields"])
    assert policy.max_tokens_fields == frozenset(document["maxTokensFields"])
    assert policy.thinking_formats == frozenset(document["thinkingFormats"])
    assert policy.thinking_levels == frozenset(document["thinkingLevels"])
    assert policy.cache_control_formats == frozenset(document["cacheControlFormats"])
    assert auth.SAFE_COST_FIELDS is policy.cost_fields
    assert auth.SAFE_INPUT_TYPES is policy.input_types
    assert auth.SAFE_COMPAT_BOOLEAN_FIELDS is policy.compat_boolean_fields
    assert auth.SAFE_MAX_TOKENS_FIELDS is policy.max_tokens_fields
    assert auth.SAFE_THINKING_FORMATS is policy.thinking_formats
    assert auth.SAFE_THINKING_LEVELS is policy.thinking_levels
    assert auth.SAFE_CACHE_CONTROL_FORMATS is policy.cache_control_formats


def test_safe_field_manifest_schema_fails_closed(tmp_path: Path) -> None:
    valid = json.loads(
        safe_model_fields.MANIFEST_PATH.read_text(encoding="utf-8")
    )
    missing = copy.deepcopy(valid)
    missing.pop("costFields")
    duplicate = copy.deepcopy(valid)
    duplicate["inputTypes"] = ["text", "text"]
    cases = [
        ([], "must be a JSON object"),
        ({**valid, "schemaVersion": 2}, "requires schemaVersion 1"),
        (missing, "invalid keys"),
        ({**valid, "unknownFields": ["unsafe"]}, "invalid keys"),
        ({**valid, "inputTypes": "text"}, "must be a non-empty array"),
        ({**valid, "inputTypes": []}, "must be a non-empty array"),
        ({**valid, "inputTypes": ["text", ""]}, "only non-empty strings"),
        (duplicate, "must not contain duplicates"),
    ]
    manifest = tmp_path / "safe-model-fields.json"
    for document, error in cases:
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(RuntimeError, match=error):
            safe_model_fields.load_safe_model_fields(manifest)

    manifest.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot load safe model fields manifest"):
        safe_model_fields.load_safe_model_fields(manifest)

    with pytest.raises(RuntimeError, match="cannot load safe model fields manifest"):
        safe_model_fields.load_safe_model_fields(tmp_path / "missing.json")


def test_python_projection_uses_every_canonical_allowlist() -> None:
    policy = safe_model_fields.SAFE_MODEL_FIELDS
    cost = {field: index for index, field in enumerate(policy.cost_fields)}
    cost["authorization"] = "secret"
    compat: dict[str, object] = {
        field: True for field in policy.compat_boolean_fields
    }
    compat.update(
        {
            "maxTokensField": next(iter(policy.max_tokens_fields)),
            "thinkingFormat": next(iter(policy.thinking_formats)),
            "cacheControlFormat": next(iter(policy.cache_control_formats)),
            "headers": {"authorization": "secret"},
        }
    )
    thinking_levels = {
        field: f"mapped-{field}" for field in policy.thinking_levels
    }
    thinking_levels["unsafe"] = "secret"

    projected = auth.sanitize_model(
        {
            "id": "model",
            "input": [*policy.input_types, "audio", {"apiKey": "secret"}],
            "cost": cost,
            "compat": compat,
            "thinkingLevelMap": thinking_levels,
            "apiKey": "secret",
            "baseUrl": "https://provider.invalid",
            "headers": {"authorization": "secret"},
        }
    )

    assert projected is not None
    assert set(projected["input"]) == policy.input_types
    assert set(projected["cost"]) == policy.cost_fields
    assert set(projected["compat"]) == {
        *policy.compat_boolean_fields,
        "maxTokensField",
        "thinkingFormat",
        "cacheControlFormat",
    }
    assert set(projected["thinkingLevelMap"]) == policy.thinking_levels
    assert "secret" not in json.dumps(projected)

    enum_fields = {
        "maxTokensField": policy.max_tokens_fields,
        "thinkingFormat": policy.thinking_formats,
        "cacheControlFormat": policy.cache_control_formats,
    }
    for compat_field, allowed_values in enum_fields.items():
        for allowed_value in allowed_values:
            projected = auth.sanitize_model(
                {"id": "model", "compat": {compat_field: allowed_value}}
            )
            assert projected == {
                "id": "model",
                "compat": {compat_field: allowed_value},
            }


def test_gateway_run_command_mounts_only_gateway_owned_inputs(tmp_path: Path) -> None:
    models = tmp_path / "models.json"
    models.write_text("{}\n", encoding="utf-8")
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "clients.json").write_text("{}\n", encoding="utf-8")
    config = Config(
        gateway_image="cyclo-gateway:test",
        gateway_container="cyclo-gateway-test",
        gateway_network="cyclo-gateway-net-test",
        store_volume="cyclo-gateway-store-test",
        host_models_json=models,
        client_registry_dir=registry,
    )

    command = gateway.gateway_run_command(config, "admin-capability")

    assert "CYCLO_GATEWAY_TOKEN=admin-capability" in command
    assert f"CYCLO_GATEWAY_CLIENTS_JSON={gateway.GATEWAY_CLIENT_REGISTRY_PATH}" in command
    assert all("multiagent" not in part.lower() for part in command)
    assert "127.0.0.1::8787" in command
    assert "no-new-privileges" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in command
    assert "/tmp:rw,noexec,nosuid,nodev,size=64m" in command
    assert command[command.index("--label") + 1] == gateway.GATEWAY_LABEL
    assert gateway._gateway_resource_label(config.gateway_container) in command
    assert command[-1] == "cyclo-gateway:test"

    by_id = gateway.gateway_run_command(
        config, "admin-capability", network_identifier="verified-network-id"
    )
    assert by_id[by_id.index("--network") + 1] == "verified-network-id"


def test_gateway_network_creation_sets_ownership_label(monkeypatch) -> None:
    commands: list[list[str]] = []
    inspections = iter(
        [
            None,
            {
                "Id": "created-network-id",
                "Labels": {
                    gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                    gateway.GATEWAY_RESOURCE_LABEL: "cyclo-gateway-net-test",
                },
            },
        ]
    )
    monkeypatch.setattr(
        gateway, "_inspect_gateway_network", lambda _name: next(inspections)
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "run_command",
        lambda command: commands.append(command) or 0,
    )

    assert gateway.ensure_network("cyclo-gateway-net-test") == "created-network-id"

    assert commands == [
        [
            "docker",
            "network",
            "create",
            "--label",
            gateway.GATEWAY_LABEL,
            "--label",
            gateway._gateway_resource_label("cyclo-gateway-net-test"),
            "cyclo-gateway-net-test",
        ]
    ]


def test_gateway_network_is_part_of_configuration_fingerprint(tmp_path: Path) -> None:
    common = {
        "gateway_image": "cyclo-gateway:test",
        "gateway_container": "cyclo-gateway-test",
        "store_volume": "cyclo-gateway-store-test",
        "host_models_json": tmp_path / "models.json",
        "client_registry_dir": tmp_path / "registry",
    }
    first = Config(gateway_network="cyclo-gateway-net-one", **common)
    second = Config(gateway_network="cyclo-gateway-net-two", **common)

    assert gateway.gateway_config_fingerprint(
        first, "admin-capability"
    ) != gateway.gateway_config_fingerprint(second, "admin-capability")


def test_gateway_network_refuses_foreign_reuse_and_removal(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_network",
        lambda _name: {"Id": "foreign-network-id", "Labels": {}},
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "run_command",
        lambda command: commands.append(command) or 0,
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: commands.append(command) or 0,
    )

    with pytest.raises(CycloError, match="network blocks Cyclo gateway"):
        gateway.ensure_network("cyclo-gateway-net-test")
    with pytest.raises(CycloError, match="network blocks Cyclo gateway"):
        gateway.remove_network("cyclo-gateway-net-test")
    assert not gateway.remove_network("cyclo-gateway-net-test", best_effort=True)
    assert commands == []


def test_gateway_rejects_generic_label_for_a_different_resource(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_network",
        lambda _name: {
            "Id": "other-network-id",
            "Labels": {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                gateway.GATEWAY_RESOURCE_LABEL: "cyclo-gateway-net-other",
            },
        },
    )

    with pytest.raises(CycloError, match="outside this Cyclo gateway"):
        gateway.ensure_network("cyclo-gateway-net-test")


def test_legacy_gateway_label_has_actionable_migration_error(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_network",
        lambda _name: {
            "Id": "legacy-network-id",
            "Labels": {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE
            },
        },
    )

    with pytest.raises(CycloError, match="one-time migration.*remove that network"):
        gateway.ensure_network("cyclo-gateway-net-test")


def test_network_missing_detection_does_not_hide_docker_context_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="docker context production not found\n"
        ),
    )

    with pytest.raises(CycloError, match="cannot inspect Docker network"):
        gateway._inspect_gateway_network("cyclo-gateway-net-test")


def test_gateway_network_removal_targets_verified_resource_id(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_network",
        lambda _name: {
            "Id": "owned-network-id",
            "Labels": {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                gateway.GATEWAY_RESOURCE_LABEL: "cyclo-gateway-net-test",
            },
        },
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: commands.append(command) or 0,
    )

    assert gateway.remove_network("cyclo-gateway-net-test")
    assert commands == [["docker", "network", "rm", "owned-network-id"]]


def test_gateway_container_refuses_foreign_reuse_and_removal(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_container",
        lambda _name: {
            "Id": "foreign-container-id",
            "Config": {"Labels": {}},
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: commands.append(command) or 0,
    )
    config = Config(
        gateway_image="cyclo-gateway:test",
        gateway_container="cyclo-gateway-test",
        gateway_network="cyclo-gateway-net-test",
        store_volume="cyclo-gateway-store-test",
        host_models_json=tmp_path / "models.json",
        client_registry_dir=tmp_path / "registry",
    )

    with pytest.raises(CycloError, match="container name is already owned outside"):
        gateway.start_gateway(config, "admin-capability")
    with pytest.raises(CycloError, match="container name is already owned outside"):
        gateway.stop_gateway_container("cyclo-gateway-test")
    assert not gateway.stop_gateway_container(
        "cyclo-gateway-test", best_effort=True
    )
    assert commands == []


def test_gateway_container_removal_targets_verified_resource_id(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_container",
        lambda _name: {
            "Id": "owned-container-id",
            "Config": {
                "Labels": {
                    gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                    gateway.GATEWAY_RESOURCE_LABEL: "cyclo-gateway-test",
                }
            },
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: commands.append(command) or 0,
    )

    assert gateway.stop_gateway_container("cyclo-gateway-test")
    assert commands == [
        ["docker", "stop", "--timeout", "10", "owned-container-id"],
        ["docker", "rm", "owned-container-id"],
    ]


def _store_gateway_info(
    resource_id: str,
    name: str,
    volume: str,
    *,
    labels: dict[str, str] | None = None,
    destination: str = gateway.GATEWAY_STORE_PATH,
) -> dict[str, object]:
    return {
        "Id": resource_id,
        "Name": f"/{name}",
        "Config": {
            "Labels": labels
            if labels is not None
            else {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                gateway.GATEWAY_RESOURCE_LABEL: name,
            }
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": volume,
                "Destination": destination,
            }
        ],
    }


def test_destroy_store_stops_every_verified_gateway_by_immutable_id(
    monkeypatch,
) -> None:
    volume = "cyclo-store"
    containers = {
        "gateway-one-id": _store_gateway_info(
            "gateway-one-id", "cyclo-gateway-state-one", volume
        ),
        "gateway-two-id": _store_gateway_info(
            "gateway-two-id", "cyclo-gateway-state-two", volume
        ),
    }
    docker_commands: list[list[str]] = []
    removed_volumes: list[str] = []
    monkeypatch.setattr(
        gateway, "_inspect_gateway_volume", lambda _name: {"Name": volume}
    )
    monkeypatch.setattr(
        gateway, "_container_ids_using_volume", lambda _name: list(containers)
    )
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_container",
        lambda identifier: containers.get(identifier),
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: docker_commands.append(command) or 0,
    )
    monkeypatch.setattr(
        gateway, "_remove_store_volume", lambda name: removed_volumes.append(name)
    )

    assert gateway.destroy_store_volume(volume)

    assert docker_commands == [
        ["docker", "stop", "--timeout", "10", "gateway-one-id"],
        ["docker", "rm", "gateway-one-id"],
        ["docker", "stop", "--timeout", "10", "gateway-two-id"],
        ["docker", "rm", "gateway-two-id"],
    ]
    assert removed_volumes == [volume]
    assert all(
        "cyclo-gateway-state" not in part
        for command in docker_commands
        for part in command
    )


@pytest.mark.parametrize(
    ("labels", "destination"),
    [
        ({}, gateway.GATEWAY_STORE_PATH),
        (
            {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                gateway.GATEWAY_RESOURCE_LABEL: "different-container",
            },
            gateway.GATEWAY_STORE_PATH,
        ),
        (
            {
                gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
                gateway.GATEWAY_RESOURCE_LABEL: "cyclo-gateway-state-two",
            },
            "/unexpected",
        ),
    ],
)
def test_destroy_store_preflights_every_user_before_mutating(
    monkeypatch, labels: dict[str, str], destination: str
) -> None:
    volume = "cyclo-store"
    containers = {
        "owned-id": _store_gateway_info(
            "owned-id", "cyclo-gateway-state-one", volume
        ),
        "blocked-id": _store_gateway_info(
            "blocked-id",
            "cyclo-gateway-state-two",
            volume,
            labels=labels,
            destination=destination,
        ),
    }
    docker_commands: list[list[str]] = []
    removed_volumes: list[str] = []
    monkeypatch.setattr(
        gateway, "_inspect_gateway_volume", lambda _name: {"Name": volume}
    )
    monkeypatch.setattr(
        gateway, "_container_ids_using_volume", lambda _name: list(containers)
    )
    monkeypatch.setattr(
        gateway,
        "_inspect_gateway_container",
        lambda identifier: containers.get(identifier),
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: docker_commands.append(command) or 0,
    )
    monkeypatch.setattr(
        gateway, "_remove_store_volume", lambda name: removed_volumes.append(name)
    )

    with pytest.raises(CycloError, match="unverified Docker container"):
        gateway.destroy_store_volume(volume)

    assert docker_commands == []
    assert removed_volumes == []


def test_destroy_store_ignores_volume_filter_false_positive(monkeypatch) -> None:
    volume = "cyclo-store"
    false_positive = _store_gateway_info(
        "other-id", "cyclo-gateway-other", "cyclo-store-similar"
    )
    docker_commands: list[list[str]] = []
    removed_volumes: list[str] = []
    monkeypatch.setattr(
        gateway, "_inspect_gateway_volume", lambda _name: {"Name": volume}
    )
    monkeypatch.setattr(gateway, "_container_ids_using_volume", lambda _name: ["other-id"])
    monkeypatch.setattr(
        gateway, "_inspect_gateway_container", lambda _identifier: false_positive
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: docker_commands.append(command) or 0,
    )
    monkeypatch.setattr(
        gateway, "_remove_store_volume", lambda name: removed_volumes.append(name)
    )

    assert gateway.destroy_store_volume(volume)
    assert docker_commands == []
    assert removed_volumes == [volume]


def test_destroy_store_is_idempotent_when_volume_is_absent(monkeypatch) -> None:
    listed = False

    def unexpected_list(_volume: str) -> list[str]:
        nonlocal listed
        listed = True
        return []

    monkeypatch.setattr(gateway, "_inspect_gateway_volume", lambda _name: None)
    monkeypatch.setattr(gateway, "_container_ids_using_volume", unexpected_list)

    assert not gateway.destroy_store_volume("missing-store")
    assert listed is False


def test_destroy_store_revalidates_immutable_id_before_mutation(monkeypatch) -> None:
    volume = "cyclo-store"
    first = _store_gateway_info("gateway-id", "cyclo-gateway-state", volume)
    changed = _store_gateway_info("gateway-id", "cyclo-gateway-state", "other-store")
    inspections = iter([first, changed])
    docker_commands: list[list[str]] = []
    monkeypatch.setattr(
        gateway, "_inspect_gateway_volume", lambda _name: {"Name": volume}
    )
    monkeypatch.setattr(gateway, "_container_ids_using_volume", lambda _name: ["gateway-id"])
    monkeypatch.setattr(
        gateway, "_inspect_gateway_container", lambda _identifier: next(inspections)
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "docker_call_ignore_missing",
        lambda command: docker_commands.append(command) or 0,
    )

    with pytest.raises(CycloError, match="changed during credential destruction"):
        gateway.destroy_store_volume(volume)

    assert docker_commands == []


def test_destroy_store_volume_removal_is_fail_closed_and_never_forced(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def in_use(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error response from daemon: volume is in use",
        )

    monkeypatch.setattr(gateway.subprocess, "run", in_use)

    with pytest.raises(CycloError, match="volume is in use"):
        gateway._remove_store_volume("cyclo-store")

    assert commands == [["docker", "volume", "rm", "cyclo-store"]]
    assert "--force" not in commands[0]


def test_destroy_store_enumerates_running_and_stopped_volume_users(monkeypatch) -> None:
    commands: list[list[str]] = []

    def list_users(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="first-id\nsecond-id\nfirst-id\n",
            stderr="",
        )

    monkeypatch.setattr(gateway.subprocess, "run", list_users)

    assert gateway._container_ids_using_volume("cyclo-store") == [
        "first-id",
        "second-id",
    ]
    assert commands == [
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "volume=cyclo-store",
        ]
    ]


def test_destroy_store_volume_removal_tolerates_concurrent_absence(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error response from daemon: get cyclo-store: no such volume",
        ),
    )

    gateway._remove_store_volume("cyclo-store")


def test_destroy_store_cli_requires_exact_confirmation(monkeypatch, capsys) -> None:
    destroyed: list[str] = []
    monkeypatch.setattr(
        gateway,
        "destroy_store_volume",
        lambda volume: destroyed.append(volume) or True,
    )

    assert (
        cli.main(
            [
                "destroy-store",
                "--image",
                "cyclo-gateway:test",
                "--store-volume",
                "cyclo-store",
                "--confirm",
                "cyclo-store",
            ]
        )
        == 0
    )
    assert destroyed == ["cyclo-store"]
    assert "destroyed gateway store volume: cyclo-store" in capsys.readouterr().out

    assert (
        cli.main(
            [
                "destroy-store",
                "--store-volume",
                "cyclo-store",
                "--confirm",
                "different-store",
            ]
        )
        == 1
    )
    assert destroyed == ["cyclo-store"]
    assert "must exactly match" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["login", "status", "destroy-store"])
def test_gateway_store_help_discloses_every_irreversibly_deleted_kind(
    command: str, capsys
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([command, "--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "credentials, subscriptions, and retained usage history" in help_text
    assert "irreversibly" in help_text


def test_destroy_store_confirmation_remains_fail_closed(monkeypatch) -> None:
    destroyed: list[str] = []
    monkeypatch.setattr(
        gateway,
        "destroy_store_volume",
        lambda volume: destroyed.append(volume) or True,
    )

    assert cli.main(["destroy-store", "--confirm", "wrong-volume"]) == 1
    assert destroyed == []


def test_gateway_restarts_on_wrong_network_and_runs_by_verified_network_id(
    tmp_path: Path, monkeypatch
) -> None:
    config = Config(
        gateway_image="cyclo-gateway:test",
        gateway_container="cyclo-gateway-test",
        gateway_network="cyclo-gateway-net-test",
        store_volume="cyclo-gateway-store-test",
        host_models_json=tmp_path / "models.json",
        client_registry_dir=tmp_path / "registry",
    )
    labels = {
        gateway.GATEWAY_OWNERSHIP_LABEL: gateway.GATEWAY_OWNERSHIP_VALUE,
        gateway.GATEWAY_RESOURCE_LABEL: config.gateway_container,
        gateway.GATEWAY_CONFIG_FINGERPRINT_LABEL: "expected-fingerprint",
    }
    old = {
        "Id": "old-container-id",
        "Config": {"Labels": labels},
        "State": {"Running": True},
        "NetworkSettings": {
            "Networks": {"wrong": {"NetworkID": "wrong-network-id"}}
        },
    }
    replacement = {
        "Id": "replacement-container-id",
        "Config": {"Labels": labels},
        "State": {"Running": True},
        "NetworkSettings": {
            "Networks": {"right": {"NetworkID": "verified-network-id"}}
        },
    }
    inspections = iter([old, replacement])
    commands: list[list[str]] = []
    stopped: list[str] = []
    monkeypatch.setattr(
        gateway, "_owned_gateway_container", lambda _name: next(inspections)
    )
    monkeypatch.setattr(gateway, "ensure_gateway_image", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway, "ensure_network", lambda _name: "verified-network-id")
    monkeypatch.setattr(
        gateway, "gateway_config_fingerprint", lambda *_args: "expected-fingerprint"
    )
    monkeypatch.setattr(
        gateway,
        "stop_gateway_container",
        lambda name: stopped.append(name) or True,
    )
    monkeypatch.setattr(
        gateway.runner_docker,
        "run_command_capture",
        lambda command: commands.append(command) or (0, "replacement-container-id"),
    )
    monkeypatch.setattr(gateway, "_published_port", lambda *_args, **_kwargs: 49152)
    monkeypatch.setattr(gateway, "wait_healthy", lambda _port: None)
    monkeypatch.setattr(
        gateway,
        "fetch_provider_catalog",
        lambda port, token: {"provider": {"port": port, "token": token}},
    )

    catalog = gateway.start_gateway(config, "admin-capability")

    assert stopped == [config.gateway_container]
    assert commands[0][commands[0].index("--network") + 1] == "verified-network-id"
    assert catalog == {
        "provider": {"port": 49152, "token": "admin-capability"}
    }


def test_login_env_forwards_name_not_secret() -> None:
    name = cli.login_env_var(
        "openai",
        api_key=None,
        api_key_env=None,
        api_key_stdin=False,
        environ={"OPENAI_API_KEY": "top-secret"},
    )
    command = cli.login_command(
        "cyclo-gateway:test",
        "cyclo-store",
        "openai",
        api_key_env=name,
    )
    assert "OPENAI_API_KEY" in command
    assert "top-secret" not in command
    assert "no-new-privileges" in command
    assert "--read-only" in command


def test_oauth_login_is_interactive_hardened_and_uses_a_writable_store() -> None:
    command = cli.login_command(
        "cyclo-gateway:test",
        "cyclo-store",
        "openai-codex",
    )

    assert "-i" in command
    assert "-t" in command
    assert "--network" not in command
    assert "--env" not in command
    assert "-e" not in command
    assert "no-new-privileges" in command
    assert "--read-only" in command
    mount = f"type=volume,src=cyclo-store,dst={gateway.GATEWAY_STORE_PATH}"
    assert mount in command
    assert f"{mount},readonly" not in command


def test_status_container_is_hardened_and_credential_volume_is_read_only() -> None:
    command = cli.status_command("cyclo-gateway:test", "cyclo-store")

    assert "no-new-privileges" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in command
    assert command[command.index("--network") + 1] == "none"
    assert (
        f"type=volume,src=cyclo-store,dst={gateway.GATEWAY_STORE_PATH},readonly"
        in command
    )


def test_packaged_contexts_are_the_only_build_inputs() -> None:
    runtime_root = source.runtime_context_root()
    gateway_root = source.gateway_context_root()
    assert source.dockerfile_path().parent == runtime_root
    assert source.gateway_dockerfile_path().parent == gateway_root
    assert (gateway_root / "package-lock.json").is_file()
    assert (gateway_root / "oauth-ui.mjs").is_file()
    assert (gateway_root / "pi-registry.mjs").is_file()
    assert (gateway_root / "safe-model-fields.json").is_file()
    assert (gateway_root / "safe-model-fields.mjs").is_file()
    assert (gateway_root / "model-metadata.mjs").is_file()
    assert (gateway_root / "supported-providers.mjs").is_file()
    assert (gateway_root / "server.mjs").is_file()
    assert (runtime_root / "entrypoint.sh").is_file()

    fingerprint = source.source_fingerprint()
    command = commands.docker_build_command("cyclo-runtime:test", fingerprint)
    assert command[-1] == str(runtime_root)
    assert str(source.dockerfile_path()) in command
    assert len(fingerprint) == 64


def test_gateway_javascript_uses_only_cyclo_runtime_names() -> None:
    context = source.gateway_context_root()
    server = (context / "server.mjs").read_text(encoding="utf-8")
    login = (context / "login.mjs").read_text(encoding="utf-8")
    oauth_ui = (context / "oauth-ui.mjs").read_text(encoding="utf-8")
    providers = (context / "providers.mjs").read_text(encoding="utf-8")
    supported_providers = (context / "supported-providers.mjs").read_text(
        encoding="utf-8"
    )
    registry = (context / "pi-registry.mjs").read_text(encoding="utf-8")
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    package = json.loads((context / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((context / "package-lock.json").read_text(encoding="utf-8"))
    assert "CYCLO_GATEWAY_TOKEN" in server
    assert "MULTIAGENT_GATEWAY" not in server
    assert 'from "./pi-registry.mjs"' in server
    assert 'from "./model-metadata.mjs"' in server
    assert "SAFE_COMPAT_BOOLEAN_FIELDS" not in server
    assert 'from "./pi-registry.mjs"' in login
    assert 'from "./oauth-ui.mjs"' in login
    assert "createOAuthLoginCallbacks" in login
    assert "store.mjs" not in oauth_ui
    assert 'from "./pi-registry.mjs"' in providers
    assert 'import("./pi-registry.mjs")' in supported_providers
    assert "store.mjs" not in supported_providers
    assert 'from "@earendil-works/pi-ai/providers/all"' in registry
    assert 'from "@earendil-works/pi-ai"' not in server
    assert 'from "@earendil-works/pi-ai"' not in login
    assert "checkBuiltinRegistry();" in providers
    assert "checkBuiltinRegistry();" in supported_providers
    assert "pi-registry.mjs" in dockerfile
    assert "safe-model-fields.json" in dockerfile
    assert "safe-model-fields.mjs" in dockerfile
    assert "model-metadata.mjs" in dockerfile
    assert "oauth-ui.mjs" in dockerfile
    assert "supported-providers.mjs" in dockerfile
    assert package["name"] == "cyclo-gateway"
    assert package["dependencies"] == {"@earendil-works/pi-ai": "0.80.6"}
    assert lock["packages"][""]["name"] == "cyclo-gateway"
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"]["node_modules/@earendil-works/pi-ai"]["version"] == "0.80.6"
