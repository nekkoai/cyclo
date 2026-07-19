from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.host_config import ProviderDefinition, provider_configuration_sha256
from cyclo.host_providers import (
    RUNTIME_PROVIDER_SOCKET_ROOT,
    HostProviders,
    provider_client_id,
    provider_definition_spec,
    provider_socket_id,
)
from cyclo.provider_runtime import CONTAINER_PROVIDER_SOCKET, ProviderStatus


def provider_source(path: Path, implementation: str = "provider one\n") -> Path:
    path.mkdir(parents=True)
    (path / "Dockerfile").write_text(
        'FROM scratch\nENTRYPOINT ["/provider"]\n', encoding="utf-8"
    )
    (path / "provider").write_text(implementation, encoding="utf-8")
    return path


def definition(
    prefix: str,
    source: Path,
    *inputs: str,
    parameters: tuple[tuple[str, str], ...] = (),
    line: int = 1,
) -> ProviderDefinition:
    arguments = (*inputs, *(f"{key}={value}" for key, value in parameters))
    return ProviderDefinition(
        prefix=prefix,
        path=source,
        arguments=tuple(arguments),
        inputs=tuple(inputs),
        parameters=parameters,
        line=line,
        configuration_sha256=provider_configuration_sha256(
            prefix,
            str(source),
            tuple(arguments),
        ),
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def status_for(item) -> ProviderStatus:
    return ProviderStatus(
        identity=item.identity,
        source_fingerprint=item.source_fingerprint,
        generation=item.generation,
        config_fingerprint="config-fingerprint",
        image_built=True,
        container_restarted=True,
        container_id="provider-container-id",
    )


def test_definition_spec_is_read_only_and_preserves_arguments(tmp_path: Path) -> None:
    source = provider_source(tmp_path / "source")
    state = tmp_path / "missing-runtime-state"
    selected = definition(
        "fusion",
        source,
        "account/model",
        parameters=(("mode", "balanced"),),
    )

    spec = provider_definition_spec(state, selected)

    assert not state.exists()
    assert spec.identity.prefix == "fusion"
    assert spec.source == source
    assert spec.arguments == ("account/model", "mode=balanced")
    assert spec.runtime_socket_dir == (
        state / "sockets" / "runtime" / provider_socket_id("fusion")
    )
    assert spec.provider_socket_dir == (
        state / "sockets" / "providers" / provider_socket_id("fusion")
    )


def test_prepare_reuses_ingress_capability_and_sets_private_file_modes(
    tmp_path: Path,
) -> None:
    source = provider_source(tmp_path / "source")
    host = HostProviders(tmp_path / "runtime-state")
    selected = definition(
        "fusion",
        source,
        "codex-one/gpt-5",
        parameters=(("mode", "balanced"),),
    )

    first = host.prepare((selected,))[0]
    first_token = first.ingress_token_file.read_text(encoding="utf-8")
    second = host.prepare((selected,))[0]
    other_source = provider_source(tmp_path / "other-source")
    other = host.prepare(
        (selected, definition("review", other_source, "codex-one/gpt-5"))
    )[1]

    assert first.generation == second.generation
    assert first.identity == second.identity
    assert second.ingress_token_file.read_text(encoding="utf-8") == first_token
    assert first_token.strip()
    assert mode(second.ingress_token_file) == 0o444
    assert mode(second.ingress_token_file.parent) == 0o700
    assert second.upstream_token_file.read_text(encoding="utf-8").strip()
    assert mode(second.upstream_token_file) == 0o444
    assert second.socket_id == provider_socket_id("fusion")
    assert mode(host.runtime_socket_dir / second.socket_id) == 0o777
    assert host.spec(second).runtime_socket_dir != host.spec(other).runtime_socket_dir
    assert mode(host.spec(other).runtime_socket_dir) == 0o777
    assert mode(second.provider_socket_dir) == 0o777
    assert mode(host.provider_sockets_dir) == 0o755
    assert mode(host.runtime_socket_dir) == 0o700


def test_explicit_provider_relaunch_rotates_both_local_capabilities(
    tmp_path: Path,
) -> None:
    source = provider_source(tmp_path / "source")
    host = HostProviders(tmp_path / "runtime-state")
    item = host.prepare((definition("fusion", source, "codex/gpt-5"),))[0]
    before = (
        item.ingress_token_file.read_text(encoding="utf-8"),
        item.upstream_token_file.read_text(encoding="utf-8"),
    )

    host.rotate_capabilities(item)

    after = (
        item.ingress_token_file.read_text(encoding="utf-8"),
        item.upstream_token_file.read_text(encoding="utf-8"),
    )
    assert after[0] != before[0]
    assert after[1] != before[1]
    assert mode(item.ingress_token_file) == 0o444
    assert mode(item.upstream_token_file) == 0o444


def test_provider_client_is_stable_and_scoped_only_to_declared_inputs(
    tmp_path: Path,
) -> None:
    source = provider_source(tmp_path / "source")
    host = HostProviders(tmp_path / "runtime-state")
    item = host.prepare(
        (
            definition(
                "pool",
                source,
                "codex-a/gpt-5",
                "codex-a/gpt-5-mini",
                "anthropic/claude-sonnet",
            ),
        )
    )[0]
    assert item.client.project_id == provider_client_id("pool")
    assert item.client.kind == "provider"
    assert item.client.provider_prefix == "pool"
    assert item.client.generation == item.generation

    first_record = host.client_record(item)
    repeated_record = host.client_record(host.prepare((item.definition,))[0])
    assert repeated_record == first_record
    assert first_record == {
        "binding_generation": item.generation,
        "client_id": item.client.project_id,
        "enabled": True,
        "expires_at": None,
        "kind": "provider",
        "models": [
            "codex-a/gpt-5",
            "codex-a/gpt-5-mini",
            "anthropic/claude-sonnet",
        ],
        "provider_prefix": "pool",
        "providers": ["codex-a", "anthropic"],
        "revoked": False,
        "team_id": "provider:pool",
        "token_sha256": hashlib.sha256(
            item.upstream_token_file.read_text(encoding="utf-8").strip().encode("utf-8")
        ).hexdigest(),
    }

    runtime_spec = host.spec(item)
    assert runtime_spec.arguments == item.definition.arguments
    assert runtime_spec.upstream_token_file == item.upstream_token_file
    assert runtime_spec.runtime_socket_dir == host.runtime_socket_dir / item.socket_id
    assert runtime_spec.provider_socket_dir == item.provider_socket_dir
    assert item.upstream_token_file.read_text(encoding="utf-8").strip()
    assert mode(item.upstream_token_file) == 0o444
    assert mode(item.upstream_token_file.parent) == 0o700


def test_expected_registry_contains_only_hashes_and_verified_route_metadata(
    tmp_path: Path,
) -> None:
    host = HostProviders(tmp_path / "runtime-state")
    item = host.prepare(
        (
            definition(
                "pass",
                provider_source(tmp_path / "source"),
                "codex/gpt-5",
            ),
        )
    )[0]
    host.publish(())

    empty = json.loads(host.registry_path.read_text(encoding="utf-8"))
    assert empty == {"providers": [], "version": 1}
    assert mode(host.registry_dir) == 0o700
    assert mode(host.registry_path) == 0o644

    expected = host.expectation(item, status_for(item))
    ingress = item.ingress_token_file.read_text(encoding="utf-8").strip()
    assert expected == {
        "prefix": "pass",
        "generation": item.generation,
        "configuration_sha256": item.definition.configuration_sha256,
        "token_sha256": hashlib.sha256(ingress.encode("utf-8")).hexdigest(),
        "inputs": ["codex/gpt-5"],
        "socket_path": str(
            RUNTIME_PROVIDER_SOCKET_ROOT
            / item.socket_id
            / CONTAINER_PROVIDER_SOCKET.name
        ),
    }
    assert ingress not in json.dumps(expected)

    host.publish((expected,))
    assert json.loads(host.registry_path.read_text(encoding="utf-8")) == {
        "providers": [expected],
        "version": 1,
    }
    assert mode(host.registry_dir) == 0o700
    assert mode(host.registry_path) == 0o644
def test_expectation_rejects_runtime_identity_or_generation_drift(
    tmp_path: Path,
) -> None:
    host = HostProviders(tmp_path / "runtime-state")
    item = host.prepare(
        (
            definition(
                "pass",
                provider_source(tmp_path / "source"),
                "codex/gpt-5",
            ),
        )
    )[0]
    valid = status_for(item)
    drifted = ProviderStatus(
        identity=valid.identity,
        source_fingerprint=valid.source_fingerprint,
        generation="different-generation",
        config_fingerprint=valid.config_fingerprint,
        image_built=valid.image_built,
        container_restarted=valid.container_restarted,
        container_id=valid.container_id,
    )

    with pytest.raises(CycloError, match="identity changed"):
        host.expectation(item, drifted)


@pytest.mark.parametrize(
    "definitions",
    [
        ("fusion", ("fusion/output",)),
        ("fusion", ("pool/output",)),
    ],
)
def test_prepare_rejects_self_and_forward_references(
    tmp_path: Path,
    definitions: tuple[str, tuple[str, ...]],
) -> None:
    source = provider_source(tmp_path / "source")
    prefix, inputs = definitions
    configured = [definition(prefix, source, *inputs)]
    if prefix != inputs[0].partition("/")[0]:
        configured.append(definition("pool", source, "codex/gpt-5", line=2))

    with pytest.raises(CycloError, match="forward or self reference"):
        HostProviders(tmp_path / "runtime-state").prepare(configured)


def test_prepare_accepts_an_earlier_provider_as_an_input(tmp_path: Path) -> None:
    source = provider_source(tmp_path / "source")
    prepared = HostProviders(tmp_path / "runtime-state").prepare(
        (
            definition("pool", source, "codex/gpt-5"),
            definition("fusion", source, "pool/balanced", line=2),
        )
    )

    assert [item.definition.prefix for item in prepared] == ["pool", "fusion"]


def test_prepare_rejects_symlinked_socket_ancestor(
    tmp_path: Path,
) -> None:
    host = HostProviders(tmp_path / "runtime-state")
    source = provider_source(tmp_path / "source")
    outside = tmp_path / "outside-sockets"
    outside.mkdir()
    host.provider_sockets_dir.parent.mkdir(parents=True)
    host.provider_sockets_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CycloError, match="symlinked provider state directory"):
        host.prepare((definition("pass", source, "codex/gpt-5"),))
    assert list(outside.iterdir()) == []


def test_prepare_rejects_symlinked_provider_secret_entry(
    tmp_path: Path,
) -> None:
    host = HostProviders(tmp_path / "runtime-state")
    source = provider_source(tmp_path / "source")
    host.secrets_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (host.secrets_dir / "pass").symlink_to(target, target_is_directory=True)
    with pytest.raises(CycloError, match="symlinked provider state directory"):
        host.prepare((definition("pass", source, "codex/gpt-5"),))


def test_prepare_rejects_a_symlinked_secret_ancestor(
    tmp_path: Path,
) -> None:
    host = HostProviders(tmp_path / "runtime-state")
    source = provider_source(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.mkdir()
    host.secrets_dir.parent.mkdir(parents=True)
    host.secrets_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CycloError, match="symlinked provider state directory"):
        host.prepare((definition("pass", source, "codex/gpt-5"),))
    assert list(outside.iterdir()) == []
