from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.provider_runtime import (
    CONTAINER_RUNTIME_SOCKET,
    CONTAINER_PROVIDER_SOCKET,
    CONTAINER_PROVIDER_TOKEN,
    CONTAINER_UPSTREAM_TOKEN,
    PROVIDER_CPU_LIMIT,
    PROVIDER_CONFIG_FINGERPRINT_LABEL,
    PROVIDER_MEMORY_LIMIT,
    PROVIDER_OWNERSHIP_LABEL,
    PROVIDER_OWNERSHIP_VALUE,
    PROVIDER_PREFIX_LABEL,
    PROVIDER_RESOURCE_LABEL,
    PROVIDER_SOURCE_FINGERPRINT_LABEL,
    PROVIDER_SYSTEM_LABEL,
    ProviderRuntime,
    ProviderSpec,
    provider_build_command,
    provider_config_fingerprint,
    provider_generation,
    provider_identity,
    provider_run_command,
    provider_source_fingerprint,
)


def source_tree(path: Path) -> Path:
    path.mkdir()
    (path / "Dockerfile").write_text(
        'FROM scratch\nENTRYPOINT ["/provider"]\n', encoding="utf-8"
    )
    (path / "provider").write_text("implementation one\n", encoding="utf-8")
    return path


def capability_files(path: Path) -> tuple[Path, Path]:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    upstream = path / "upstream.token"
    provider = path / "provider.token"
    upstream.write_text("upstream-capability\n", encoding="utf-8")
    provider.write_text("provider-capability\n", encoding="utf-8")
    os.chmod(upstream, 0o444)
    os.chmod(provider, 0o444)
    return upstream, provider


def spec(tmp_path: Path, prefix: str = "fusion") -> ProviderSpec:
    source = source_tree(tmp_path / f"source-{prefix}")
    upstream, provider = capability_files(tmp_path / f"tokens-{prefix}")
    runtime_socket_dir = tmp_path / f"runtime-socket-{prefix}"
    provider_socket_dir = tmp_path / f"provider-socket-{prefix}"
    runtime_socket_dir.mkdir(mode=0o777)
    provider_socket_dir.mkdir(mode=0o777)
    os.chmod(runtime_socket_dir, 0o777)
    os.chmod(provider_socket_dir, 0o777)
    return ProviderSpec(
        identity=provider_identity(tmp_path / "state", prefix),
        source=source,
        arguments=("account/model", "mode=balanced"),
        runtime_socket_dir=runtime_socket_dir,
        provider_socket_dir=provider_socket_dir,
        upstream_token_file=upstream,
        provider_token_file=provider,
    )


def labels(identity, resource: str, **extra: str) -> dict[str, str]:
    return {
        PROVIDER_OWNERSHIP_LABEL: PROVIDER_OWNERSHIP_VALUE,
        PROVIDER_SYSTEM_LABEL: identity.system_id,
        PROVIDER_PREFIX_LABEL: identity.prefix,
        PROVIDER_RESOURCE_LABEL: resource,
        **extra,
    }


def image_info(provider: ProviderSpec, fingerprint: str) -> dict[str, object]:
    return {
        "Id": "image-id",
        "Config": {
            "Labels": labels(
                provider.identity,
                provider.identity.image,
                **{PROVIDER_SOURCE_FINGERPRINT_LABEL: fingerprint},
            ),
            "Entrypoint": ["/provider"],
        },
    }


def container_info(
    provider: ProviderSpec,
    config_fingerprint: str,
    *,
    identifier: str = "container-id",
) -> dict[str, object]:
    return {
        "Id": identifier,
        "Name": f"/{provider.identity.container}",
        "Config": {
            "Labels": labels(
                provider.identity,
                provider.identity.container,
                **{PROVIDER_CONFIG_FINGERPRINT_LABEL: config_fingerprint},
            )
        },
        "State": {"Running": True},
        "HostConfig": {"NetworkMode": "none"},
    }


def network_info(provider: ProviderSpec, members=None) -> dict[str, object]:
    name = ProviderRuntime.legacy_network_name(provider.identity)
    return {
        "Id": "network-id",
        "Name": name,
        "Labels": labels(provider.identity, name),
        "Internal": True,
        "Containers": {} if members is None else members,
    }


def test_source_fingerprint_hashes_files_modes_and_symlink_text_without_traversal(
    tmp_path: Path,
) -> None:
    source = source_tree(tmp_path / "provider")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_text("first outside value\n", encoding="utf-8")
    (source / "external").symlink_to(target)
    (source / "linked-directory").symlink_to(outside, target_is_directory=True)
    git = source / ".git"
    git.mkdir()
    (git / "index").write_text("ignored one\n", encoding="utf-8")

    first = provider_source_fingerprint(source)
    target.write_text("changed outside value\n", encoding="utf-8")
    (git / "index").write_text("ignored two\n", encoding="utf-8")
    assert provider_source_fingerprint(source) == first

    (source / "external").unlink()
    (source / "external").symlink_to(outside / "another-name")
    second = provider_source_fingerprint(source)
    assert second != first

    executable = source / "provider"
    os.chmod(executable, 0o755)
    assert provider_source_fingerprint(source) != second


def test_identity_is_installation_scoped_readable_and_collision_safe(
    tmp_path: Path,
) -> None:
    first = provider_identity(tmp_path / "state", "a" * 80)
    repeated = provider_identity(tmp_path / "state", "a" * 80)
    other_prefix = provider_identity(tmp_path / "state", "a" * 79 + "b")
    other_state = provider_identity(tmp_path / "other-state", "a" * 80)

    assert first == repeated
    assert first != other_prefix
    assert first != other_state
    assert first.container.startswith("cyclo-provider-")
    assert first.image == f"{first.container}:local"


def test_generation_excludes_deployment_capabilities_and_config_does_not(
    tmp_path: Path,
) -> None:
    provider = spec(tmp_path)
    source_fingerprint = provider_source_fingerprint(provider.source)
    generation = provider_generation(
        provider.identity, provider.arguments, source_fingerprint
    )
    config = provider_config_fingerprint(provider, source_fingerprint)

    os.chmod(provider.provider_token_file, 0o600)
    provider.provider_token_file.write_text("rotated\n", encoding="utf-8")
    os.chmod(provider.provider_token_file, 0o444)

    assert (
        provider_generation(provider.identity, provider.arguments, source_fingerprint)
        == generation
    )
    assert provider_config_fingerprint(provider, source_fingerprint) != config
    assert (
        provider_generation(
            provider.identity,
            (*provider.arguments, "new=yes"),
            source_fingerprint,
        )
        != generation
    )


def test_capability_projection_requires_0444_files_in_0700_directory(
    tmp_path: Path,
) -> None:
    provider = spec(tmp_path)
    fingerprint = provider_source_fingerprint(provider.source)

    os.chmod(provider.provider_token_file, 0o600)
    with pytest.raises(CycloError, match="mode 0444"):
        provider_config_fingerprint(provider, fingerprint)
    os.chmod(provider.provider_token_file, 0o444)

    os.chmod(provider.provider_token_file.parent, 0o755)
    with pytest.raises(CycloError, match="mode 0700"):
        provider_config_fingerprint(provider, fingerprint)


def test_build_and_run_commands_use_uds_without_network_and_limit_resources(
    tmp_path: Path,
) -> None:
    provider = spec(tmp_path)
    source_fingerprint = provider_source_fingerprint(provider.source)
    generation = provider_generation(
        provider.identity, provider.arguments, source_fingerprint
    )
    config_fingerprint = provider_config_fingerprint(provider, source_fingerprint)

    build = provider_build_command(provider, source_fingerprint)
    assert build[:4] == ["docker", "build", "-t", provider.identity.image]
    assert build[-1] == str(provider.source.resolve())
    assert build[build.index("-f") + 1] == str(
        provider.source.resolve() / "Dockerfile"
    )
    assert f"{PROVIDER_SOURCE_FINGERPRINT_LABEL}={source_fingerprint}" in build

    command = provider_run_command(
        provider,
        generation=generation,
        config_fingerprint=config_fingerprint,
    )
    rendered = " ".join(command)
    assert command[:3] == ["docker", "run", "--detach"]
    assert command[command.index("--network") + 1] == "none"
    assert "--network-alias" not in command
    assert "--publish" not in command
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert "/tmp:rw,noexec,nosuid,nodev,size=64m" in command
    assert f"CYCLO_PROVIDER_GENERATION={generation}" in command
    assert f"CYCLO_PROVIDER_RUNTIME_SOCKET={CONTAINER_RUNTIME_SOCKET}" in command
    assert f"CYCLO_PROVIDER_SOCKET={CONTAINER_PROVIDER_SOCKET}" in command
    assert "CYCLO_GATEWAY_URL" not in rendered
    assert "CYCLO_PROVIDER_PORT" not in rendered
    assert f"CYCLO_UPSTREAM_TOKEN_FILE={CONTAINER_UPSTREAM_TOKEN}" in command
    assert f"CYCLO_PROVIDER_TOKEN_FILE={CONTAINER_PROVIDER_TOKEN}" in command
    assert "upstream-capability" not in rendered
    assert "provider-capability" not in rendered
    assert command[command.index("--memory") + 1] == PROVIDER_MEMORY_LIMIT
    assert command[command.index("--memory-swap") + 1] == PROVIDER_MEMORY_LIMIT
    assert command[command.index("--cpus") + 1] == PROVIDER_CPU_LIMIT
    assert command[command.index("--ulimit") + 1] == "nofile=1024:1024"
    assert command[-len(provider.arguments) :] == list(provider.arguments)
    mounts = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount"
    ]
    assert len(mounts) == 4
    assert any(
        f"src={provider.provider_socket_dir}" in mount
        and not mount.endswith(",readonly")
        for mount in mounts
    )
    assert sum(mount.endswith(",readonly") for mount in mounts) == 3


def test_existing_image_requires_owned_labels_and_oci_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = spec(tmp_path)
    runtime = ProviderRuntime(tmp_path / "state")
    fingerprint = provider_source_fingerprint(provider.source)
    info = image_info(provider, fingerprint)
    info["Config"]["Entrypoint"] = None  # type: ignore[index]
    monkeypatch.setattr(runtime, "_inspect_image", lambda _name: info)

    with pytest.raises(CycloError, match="must define OCI ENTRYPOINT"):
        runtime.require_current_image(provider)

    info["Config"]["Entrypoint"] = ["/provider"]  # type: ignore[index]
    info["Config"]["Labels"][PROVIDER_SYSTEM_LABEL] = "foreign"  # type: ignore[index]
    with pytest.raises(CycloError, match="owned outside"):
        runtime.require_current_image(provider)


def test_socket_directories_must_be_real_and_world_accessible(tmp_path: Path) -> None:
    provider = spec(tmp_path)
    fingerprint = provider_source_fingerprint(provider.source)

    os.chmod(provider.provider_socket_dir, 0o755)
    with pytest.raises(CycloError, match="mode 0777"):
        provider_config_fingerprint(provider, fingerprint)

    provider.provider_socket_dir.rmdir()
    outside = tmp_path / "outside-socket-dir"
    outside.mkdir(mode=0o777)
    provider.provider_socket_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CycloError, match="symlinked"):
        provider_config_fingerprint(provider, fingerprint)


def test_clearing_a_hostile_socket_symlink_never_follows_it(tmp_path: Path) -> None:
    provider = spec(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("must survive\n", encoding="utf-8")
    socket_path = provider.provider_socket_dir / CONTAINER_PROVIDER_SOCKET.name
    socket_path.symlink_to(outside)

    ProviderRuntime._clear_provider_socket(provider)

    assert not socket_path.exists()
    assert outside.read_text(encoding="utf-8") == "must survive\n"


def test_container_reuse_requires_network_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = spec(tmp_path)
    runtime = ProviderRuntime(tmp_path / "state")
    source_fingerprint = provider_source_fingerprint(provider.source)
    fingerprint = provider_config_fingerprint(provider, source_fingerprint)
    info = container_info(provider, fingerprint)
    monkeypatch.setattr(
        runtime,
        "_inspect_image",
        lambda _name: image_info(provider, source_fingerprint),
    )
    monkeypatch.setattr(runtime, "_inspect_container", lambda _name: info)

    runtime.require_startable(provider)

    info["HostConfig"]["NetworkMode"] = "bridge"  # type: ignore[index]
    with pytest.raises(CycloError, match="stale or stopped"):
        runtime.require_startable(provider)


def test_status_evaluates_current_source_and_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = spec(tmp_path)
    runtime = ProviderRuntime(tmp_path / "state")
    source_fingerprint = provider_source_fingerprint(provider.source)
    config_fingerprint = provider_config_fingerprint(provider, source_fingerprint)
    monkeypatch.setattr(
        runtime,
        "_inspect_image",
        lambda _name: image_info(provider, source_fingerprint),
    )
    monkeypatch.setattr(
        runtime,
        "_inspect_container",
        lambda _name: container_info(provider, config_fingerprint),
    )

    current = runtime.status(provider.identity, provider)
    changed_arguments = runtime.status(
        provider.identity,
        replace(provider, arguments=(*provider.arguments, "changed=yes")),
    )
    (provider.source / "provider").write_text(
        "implementation two\n", encoding="utf-8"
    )
    changed_source = runtime.status(provider.identity, provider)

    assert current.image_current
    assert current.configuration_current
    assert changed_arguments.image_current
    assert not changed_arguments.configuration_current
    assert not changed_source.image_current
    assert not changed_source.configuration_current


def test_container_removal_reinspects_immutable_id_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = spec(tmp_path)
    runtime = ProviderRuntime(tmp_path / "state")
    info = container_info(provider, "config")
    inspected: list[str] = []
    commands: list[list[str]] = []

    def inspect(identifier: str):
        inspected.append(identifier)
        return info

    monkeypatch.setattr(runtime, "_inspect_container", inspect)
    monkeypatch.setattr(
        runtime,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert runtime._remove_container(provider.identity)
    assert inspected == [provider.identity.container, "container-id"]
    assert commands == [
        ["docker", "stop", "--timeout", "10", "container-id"],
        ["docker", "rm", "container-id"],
    ]
