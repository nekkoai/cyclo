from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.images import Image
from cyclo.team.definition import Team
from cyclo.team.image import TeamImageBuilder


def valid_image(
    reference: str,
    marker: str,
    *,
    labels: tuple[tuple[str, str], ...] = (),
) -> Image:
    return Image(
        reference,
        "sha256:" + marker * 64,
        {
            "Entrypoint": ["/usr/local/bin/cyclo-container-entrypoint"],
            "Healthcheck": {"Test": ["CMD", "true"]},
            "Labels": dict(labels),
            "User": "",
        },
    )


class FakeImages:
    def __init__(self, override: Image | None = None) -> None:
        self.override = override
        self.builds: list[dict[str, object]] = []
        self.inspections: list[str] = []

    def inspect(self, reference: str, *, missing_ok: bool = False) -> Image | None:
        self.inspections.append(reference)
        return self.override

    def build(
        self,
        reference: str,
        *,
        dockerfile: Path,
        context: Path,
        build_args=(),
        labels=(),
    ) -> Image:
        self.builds.append(
            {
                "reference": reference,
                "dockerfile": dockerfile,
                "context": context,
                "build_args": tuple(build_args),
                "labels": tuple(labels),
            }
        )
        marker = "a" if len(self.builds) == 1 else "b"
        return valid_image(reference, marker, labels=tuple(labels))


def team(root: Path) -> Team:
    return Team(
        root=root,
        roster=root / "team",
        roles_dir=root / "roles",
        protocol=None,
        dockerfile=root / "Dockerfile",
        agents=(),
    )


def test_common_image_uses_the_team_component_context_once_per_builder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    components = tmp_path / "components"
    (components / "team").mkdir(parents=True)
    images = FakeImages()
    builder = TeamImageBuilder(images, "installation")  # type: ignore[arg-type]
    monkeypatch.setattr("cyclo.team.image.components_root", lambda: components)
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 1234)
    monkeypatch.setattr("cyclo.team.image.os.getgid", lambda: 5678)

    root = tmp_path / "plain-team"
    root.mkdir()
    dockerfile = root / "Dockerfile"
    dockerfile.write_text("ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n")
    selected = team(root)
    builder.build(selected)
    builder.build(selected)

    assert images.builds[0] == {
        "reference": "cyclo-installation-team:0.2.0",
        "dockerfile": components / "team" / "Dockerfile",
        "context": components,
        "build_args": (("CYCLO_HOST_UID", "1234"), ("CYCLO_HOST_GID", "5678")),
        "labels": (),
    }
    assert len(images.builds) == 3
    assert all(build["dockerfile"] == dockerfile for build in images.builds[1:])


def test_image_override_is_validated_without_building_the_common_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    override = valid_image("custom-team:1", "c")
    images = FakeImages(override)
    builder = TeamImageBuilder(images, "installation")  # type: ignore[arg-type]
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 1234)

    assert builder.build(team(tmp_path / "plain-team"), override="custom-team:1") is override
    assert images.inspections == ["custom-team:1"]
    assert images.builds == []


def test_missing_image_override_has_a_domain_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    images = FakeImages()
    builder = TeamImageBuilder(images, "installation")  # type: ignore[arg-type]
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 1234)

    with pytest.raises(CycloError, match="team runtime image is not built"):
        builder.build(team(tmp_path / "plain-team"), override="missing:1")


def test_derived_team_image_records_the_exact_common_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "custom-team"
    root.mkdir()
    dockerfile = root / "Dockerfile"
    dockerfile.write_text("ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n")
    components = tmp_path / "components"
    (components / "team").mkdir(parents=True)
    images = FakeImages()
    builder = TeamImageBuilder(images, "installation")  # type: ignore[arg-type]
    monkeypatch.setattr("cyclo.team.image.components_root", lambda: components)
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 1234)
    monkeypatch.setattr("cyclo.team.image.os.getgid", lambda: 5678)

    derived = builder.build(team(root))

    base = images.builds[0]
    assert images.builds[1]["dockerfile"] == dockerfile
    assert images.builds[1]["context"] == root
    assert images.builds[1]["build_args"] == (
        ("CYCLO_TEAM_BASE", base["reference"]),
    )
    assert images.builds[1]["labels"] == (("io.cyclo.team-base", "sha256:" + "a" * 64),)
    assert derived.id == "sha256:" + "b" * 64
