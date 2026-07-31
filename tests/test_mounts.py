from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.mounts import (
    validate_mount_authority,
    validate_strict_source_root_separation,
    validate_team_mount_separation,
)


def validate(
    tmp_path: Path,
    *,
    teams: tuple[tuple[Path, str], ...] = (),
    mounts: tuple[tuple[Path, str], ...] = (),
    trusted: tuple[tuple[Path, str], ...] = (),
) -> None:
    validate_mount_authority(
        teams,
        mounts,
        state_root=tmp_path / "state",
        docker_endpoint=f"unix://{tmp_path / 'docker.sock'}",
        trusted_roots=trusted,
    )


def test_disjoint_team_and_mount_are_allowed(tmp_path: Path) -> None:
    team = tmp_path / "team"
    project = tmp_path / "project"
    team.mkdir()
    project.mkdir()

    validate(
        tmp_path,
        teams=((team, "team"),),
        mounts=((project, "mount"),),
    )


def test_team_and_project_trees_must_not_overlap(tmp_path: Path) -> None:
    team = tmp_path / "team"
    project = team / "project"
    project.mkdir(parents=True)

    with pytest.raises(CycloError, match="separate filesystem trees"):
        validate(
            tmp_path,
            teams=((team, "team"),),
            mounts=((project, "mount"),),
        )


@pytest.mark.parametrize(
    ("selected", "label"),
    (
        ("state/inside", "Cyclo state"),
        ("docker.sock", "Docker socket"),
    ),
)
def test_host_authority_cannot_be_mounted(
    tmp_path: Path,
    selected: str,
    label: str,
) -> None:
    path = tmp_path / selected
    path.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(CycloError, match=label):
        validate(tmp_path, mounts=((path, "mount"),))


def test_installed_controller_cannot_be_mounted(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    controller.mkdir()

    with pytest.raises(CycloError, match="installed controller"):
        validate(
            tmp_path,
            mounts=((controller, "mount"),),
            trusted=((controller, "installed controller"),),
        )


def test_lexical_ancestor_of_trusted_path_is_rejected(tmp_path: Path) -> None:
    writable = tmp_path / "writable"
    trusted = writable / "bin" / "dcomp"

    with pytest.raises(CycloError, match="DComp executable"):
        validate(
            tmp_path,
            mounts=((writable, "mount"),),
            trusted=((trusted, "DComp executable"),),
        )


def test_cross_project_team_and_mount_alias_is_rejected(tmp_path: Path) -> None:
    team = tmp_path / "teams" / "review"
    mount = tmp_path / "teams"

    with pytest.raises(CycloError, match="separate filesystem trees"):
        validate_team_mount_separation(
            ((team, "team from project A"),),
            ((mount, "mount from project B"),),
        )


def test_cross_project_nested_roots_are_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    child = parent / "core-et"

    with pytest.raises(CycloError, match="must not contain one another"):
        validate_strict_source_root_separation(
            (
                (parent, "mount from project A"),
                (child, "mount from project B"),
            )
        )


def test_cross_project_exact_root_reuse_is_allowed(tmp_path: Path) -> None:
    shared = tmp_path / "shared"

    validate_strict_source_root_separation(
        (
            (shared, "mount from project A"),
            (shared, "mount from project B"),
        )
    )
