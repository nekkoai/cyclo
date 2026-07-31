from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.dcomp_system import PROVIDER_SERVICE
from cyclo.errors import CycloError
from cyclo.host import load_host


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
    assert (host.outer_component, host.outer_output) == ("gateway", "provider")


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
