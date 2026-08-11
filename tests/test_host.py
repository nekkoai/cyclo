from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.dcomp_system import PROVIDER_SERVICE
from cyclo.errors import CycloError
from cyclo.host import load_host
from cyclo.resources import components_root


def provider(root: Path, name: str, *, input_name: str = "upstream") -> Path:
    source = root / name
    source.mkdir()
    (source / "component.dcomp").write_text(
        "\n".join(
            (
                f"docker {name}:dev",
                f"input {PROVIDER_SERVICE} {input_name}",
                f"output {PROVIDER_SERVICE} provider",
                "",
            )
        ),
        encoding="utf-8",
    )
    (source / "Dockerfile").write_text(
        "FROM scratch\nHEALTHCHECK CMD true\n",
        encoding="utf-8",
    )
    return source


def test_empty_host_uses_gateway_as_outer_provider(tmp_path: Path) -> None:
    host = load_host(tmp_path / "host.conf")

    assert host.providers == ()
    assert host.components == ()
    assert (host.outer_component, host.outer_output) == ("gateway", "provider")


def test_openai_component_is_enabled_independently_of_provider_chain(
    tmp_path: Path,
) -> None:
    source = provider(tmp_path, "trace")
    config = tmp_path / "host.conf"
    config.write_text(
        "\n".join(
            (
                "component openai port=18080 bind=0.0.0.0",
                f"provider trace {source} upstream=gateway.provider",
                "",
            )
        ),
        encoding="utf-8",
    )

    host = load_host(config)

    assert [(item.name, item.bind, item.port) for item in host.components] == [
        ("openai", "0.0.0.0", 18080)
    ]
    assert [item.name for item in host.providers] == ["trace"]
    assert host.outer_component == "trace"


def test_openai_component_defaults_to_loopback_port_8080(tmp_path: Path) -> None:
    config = tmp_path / "host.conf"
    config.write_text("component openai\n", encoding="utf-8")

    host = load_host(config)

    assert (host.components[0].bind, host.components[0].port) == (
        "127.0.0.1",
        8080,
    )


@pytest.mark.parametrize("port", ("0", "65536", "not-a-port"))
def test_openai_component_rejects_invalid_port(
    tmp_path: Path,
    port: str,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text(f"component openai port={port}\n", encoding="utf-8")

    with pytest.raises(CycloError, match="between 1 and 65535"):
        load_host(config)


@pytest.mark.parametrize("bind", ("", "localhost", "::1", "300.1.1.1"))
def test_openai_component_rejects_non_ipv4_bind(
    tmp_path: Path,
    bind: str,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text(f"component openai bind={bind}\n", encoding="utf-8")

    with pytest.raises(CycloError, match="literal IPv4 address"):
        load_host(config)


@pytest.mark.parametrize(
    "settings",
    (
        "bind=127.0.0.1 bind=0.0.0.0",
        "port=8080 port=18080",
        "host=0.0.0.0",
    ),
)
def test_openai_component_rejects_duplicate_or_unknown_settings(
    tmp_path: Path,
    settings: str,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text(f"component openai {settings}\n", encoding="utf-8")

    with pytest.raises(CycloError, match="invalid or duplicate"):
        load_host(config)


def test_host_rejects_duplicate_or_unknown_components(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.conf"
    duplicate.write_text(
        "component openai\ncomponent openai\n",
        encoding="utf-8",
    )
    unknown = tmp_path / "unknown.conf"
    unknown.write_text("component other\n", encoding="utf-8")

    with pytest.raises(CycloError, match="duplicate component"):
        load_host(duplicate)
    with pytest.raises(CycloError, match="expected component openai"):
        load_host(unknown)


def test_bindings_name_exact_component_interfaces_and_may_cycle(
    tmp_path: Path,
) -> None:
    left = provider(tmp_path, "left")
    right = provider(tmp_path, "right")
    config = tmp_path / "host.conf"
    config.write_text(
        "\n".join(
            (
                f"provider left {left} upstream=right.provider -- left",
                f"provider right {right} upstream=left.provider -- right",
                "",
            )
        ),
        encoding="utf-8",
    )

    host = load_host(config)

    assert host.providers[0].bindings[0].component == "right"
    assert host.providers[1].bindings[0].component == "left"
    assert host.outer_component == "right"


def test_binding_requires_explicit_output_name(tmp_path: Path) -> None:
    source = provider(tmp_path, "trace")
    config = tmp_path / "host.conf"
    config.write_text(
        f"provider trace {source} upstream=gateway\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="INPUT=COMPONENT.OUTPUT"):
        load_host(config)


def test_bundled_pooler_resolves_to_the_packaged_provider(
    tmp_path: Path,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text(
        "provider pool pooler upstream=gateway.provider "
        "-- account-a account-b\n",
        encoding="utf-8",
    )

    host = load_host(config)

    pool = host.providers[0]
    assert pool.name == "pool"
    assert pool.source == components_root() / "pooler"
    assert pool.context == components_root()
    assert pool.arguments == ("account-a", "account-b")
    assert [
        (binding.input, binding.component, binding.output)
        for binding in pool.bindings
    ] == [("upstream", "gateway", "provider")]
    assert pool.provider_output == "provider"


def test_bundled_pooler_supports_exact_model_arguments(tmp_path: Path) -> None:
    config = tmp_path / "host.conf"
    config.write_text(
        "provider quota pooler upstream=gateway.provider -- "
        "account-a/model account-b/model model=balanced\n",
        encoding="utf-8",
    )

    host = load_host(config)

    assert host.providers[0].arguments == (
        "account-a/model",
        "account-b/model",
        "model=balanced",
    )
    assert host.outer_component == "quota"


def test_bundled_provider_rejects_context_override(
    tmp_path: Path,
) -> None:
    overridden = tmp_path / "overridden.conf"
    overridden.write_text(
        "provider pool pooler context=. upstream=gateway.provider "
        "-- account-a account-b\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="do not accept context=PATH"):
        load_host(overridden)


def test_descriptor_must_expose_one_provider_output(tmp_path: Path) -> None:
    source = tmp_path / "broken"
    source.mkdir()
    (source / "component.dcomp").write_text(
        "docker broken:dev\noutput example.v1.Other other\n",
        encoding="utf-8",
    )
    config = tmp_path / "host.conf"
    config.write_text(f"provider broken {source}\n", encoding="utf-8")

    with pytest.raises(CycloError, match="exactly one"):
        load_host(config)


def test_host_configuration_must_be_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "real.conf"
    target.write_text("", encoding="utf-8")
    linked = tmp_path / "host.conf"
    linked.symlink_to(target)

    with pytest.raises(CycloError, match="cannot read host configuration"):
        load_host(linked)


def test_component_descriptor_must_not_be_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "provider"
    source.mkdir()
    target = tmp_path / "descriptor"
    target.write_text(
        f"docker provider:dev\noutput {PROVIDER_SERVICE} provider\n",
        encoding="utf-8",
    )
    (source / "component.dcomp").symlink_to(target)
    config = tmp_path / "host.conf"
    config.write_text(f"provider provider {source}\n", encoding="utf-8")

    with pytest.raises(CycloError, match="cannot read DComp descriptor"):
        load_host(config)


def test_host_configuration_rejects_control_characters(tmp_path: Path) -> None:
    config = tmp_path / "host.conf"
    config.write_bytes(b"# bad\x00line\n")

    with pytest.raises(CycloError, match="control character"):
        load_host(config)
