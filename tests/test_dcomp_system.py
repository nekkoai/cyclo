from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.dcomp_system import (
    Bind,
    Component,
    Endpoint,
    Link,
    Materializer,
    PublishedPort,
    System,
    Volume,
    component_name,
    provider_endpoint,
)
from cyclo.errors import CycloError


def test_materializes_one_deterministic_dcomp_system(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    system = System(
        "cyclo-123456789abc",
        (
            Component(
                "gateway",
                "sha256:" + "a" * 64,
                outputs=(provider_endpoint(),),
                volumes=(Volume("credentials", "/var/lib/cyclo-gateway"),),
                egress=True,
            ),
            Component(
                "team-core-et",
                "sha256:" + "b" * 64,
                inputs=(provider_endpoint(),),
                binds=(Bind(project, "/workspace/core-et", False),),
                ports=(PublishedPort("127.0.0.1", 0, 4137),),
                egress=True,
            ),
        ),
        (Link("team-core-et", "provider", "gateway", "provider"),),
    )

    materializer = Materializer(state / "dcomp")
    first = materializer.materialize(system)
    second = materializer.materialize(system)

    assert first == second
    assert first.path == state / "dcomp" / "system.dcomp"
    text = first.path.read_text(encoding="utf-8")
    assert "component gateway descriptors/gateway-" in text
    assert "volume gateway credentials /var/lib/cyclo-gateway rw" in text
    assert "publish team-core-et tcp 127.0.0.1 0 4137" in text
    assert "link team-core-et.provider gateway.provider" in text
    assert str(project) in text
    assert len(tuple((state / "dcomp" / "descriptors").iterdir())) == 2


def test_materializer_keeps_only_the_selected_component_descriptors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generated"
    materializer = Materializer(root)
    materializer.materialize(
        System("demo", (Component("first", "first:1"),), ())
    )
    materializer.materialize(
        System("demo", (Component("second", "second:1"),), ())
    )

    descriptors = sorted(path.name for path in (root / "descriptors").iterdir())
    assert len(descriptors) == 1
    assert descriptors[0].startswith("second-")


def test_materializer_recovers_an_empty_unpublished_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generated"
    materializer = Materializer(root)
    materializer.materialize(
        System("demo", (Component("first", "first:1"),), ())
    )
    abandoned = root / "descriptors" / ".first.tmp.1234.abcdef123456"
    abandoned.mkdir()

    materializer.materialize(
        System("demo", (Component("second", "second:1"),), ())
    )

    descriptors = tuple((root / "descriptors").iterdir())
    assert len(descriptors) == 1
    assert descriptors[0].name.startswith("second-")


def test_links_are_interfaces_not_dependencies_and_cycles_are_valid() -> None:
    service = "example.loop.v1.Loop"
    system = System(
        "cycle",
        (
            Component(
                "left",
                "left:1",
                inputs=(Endpoint(service, "input"),),
                outputs=(Endpoint(service, "output"),),
            ),
            Component(
                "right",
                "right:1",
                inputs=(Endpoint(service, "input"),),
                outputs=(Endpoint(service, "output"),),
            ),
        ),
        (
            Link("left", "input", "right", "output"),
            Link("right", "input", "left", "output"),
        ),
    )

    system.validate()


def test_every_input_must_be_linked() -> None:
    system = System(
        "demo",
        (Component("consumer", "consumer:1", inputs=(provider_endpoint(),)),),
        (),
    )

    with pytest.raises(CycloError, match="unlinked DComp input"):
        system.validate()


def test_mount_targets_must_not_overlap(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    component = Component(
        "worker",
        "worker:1",
        binds=(
            Bind(first, "/agentws", True),
            Bind(second, "/agentws/tasks", False),
        ),
    )

    with pytest.raises(CycloError, match="overlapping mount targets"):
        component.validate()


def test_component_name_is_stable_and_bounded() -> None:
    first = component_name("team", "CORE_ET.Jon/" + "x" * 100)
    second = component_name("team", "CORE_ET.Jon/" + "x" * 100)

    assert first == second
    assert len(first) <= 63
    assert first.startswith("team-core-et-jon-")


def test_materializer_rejects_unrepresentable_bind_path(tmp_path: Path) -> None:
    source = tmp_path / "has space"
    source.mkdir()
    system = System(
        "demo",
        (Component("worker", "worker:1", binds=(Bind(source, "/data", True),)),),
        (),
    )

    with pytest.raises(CycloError, match="cannot be represented"):
        Materializer(tmp_path / "state").materialize(system)
