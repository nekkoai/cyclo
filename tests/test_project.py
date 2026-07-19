from __future__ import annotations

import re
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.project import (
    MAX_PROJECT_FILE_BYTES,
    ProjectTeam,
    load_project,
    render_project_manifest,
)


def project_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    config_dir = tmp_path / "experiment"
    team = tmp_path / "teams" / "jon-rtl"
    source = tmp_path / "sources" / "core-et"
    docs = tmp_path / "references" / "specifications"
    for path in (config_dir, team, source, docs):
        path.mkdir(parents=True)
    return config_dir, team, source, docs


def valid_text() -> str:
    return (
        "name core-et-uart\n"
        "description Design and verify a UART IP for OpenHW CORE-V.\n"
        "team ../teams/jon-rtl ro\n"
        "mount core-et ../sources/core-et rw\n"
        "mount specifications ../references/specifications ro\n"
    )


def test_loads_strict_project_definition_relative_to_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir, team, source, docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(valid_text(), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    project = load_project(config)

    assert project.path == config.absolute()
    assert project.name == "core-et-uart"
    assert project.description == "Design and verify a UART IP for OpenHW CORE-V."
    assert len(project.definition_sha256) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", project.definition_sha256)
    assert project.teams == (
        ProjectTeam(path=team.resolve(), mode="ro", line=3),
    )
    assert project.teams[0].name == "jon-rtl"
    assert project.teams[0].read_only
    assert not project.teams[0].writable
    assert [mount.name for mount in project.mounts] == [
        "core-et",
        "specifications",
    ]
    assert [mount.path for mount in project.mounts] == [
        source.resolve(),
        docs.resolve(),
    ]
    assert project.mounts[0].container_path == Path("/workspace/core-et")
    assert project.mounts[0].writable
    assert project.mounts[1].read_only
    assert project.mounts[1].container_path == Path("/readonly/specifications")


def test_description_consumes_the_line_and_whole_line_comments_are_the_only_comments(
    tmp_path: Path,
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(
        "# leading comment\n"
        "name core-et-uart\n"
        "description UART, DMA, and interrupts # this is description text\n"
        "team ../teams/jon-rtl ro\n"
        "mount core-et ../sources/core-et rw\n",
        encoding="utf-8",
    )

    assert load_project(config).description == (
        "UART, DMA, and interrupts # this is description text"
    )


def test_definition_digest_is_semantic_and_deterministic(tmp_path: Path) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    first = config_dir / "first.cyclo"
    second = config_dir / "second.cyclo"
    first.write_text(valid_text(), encoding="utf-8")
    second.write_bytes(
        (
            "# ignored\r\n"
            "name   core-et-uart\r\n"
            "description   Design and verify a UART IP for OpenHW CORE-V.\r\n"
            "team   ../teams/jon-rtl   ro\r\n"
            "mount core-et ../sources/core-et rw\r\n"
            "mount specifications ../references/specifications ro\r\n"
        ).encode("utf-8")
    )

    assert load_project(first).definition_sha256 == load_project(
        second
    ).definition_sha256

    second.write_text(valid_text().replace("core-et rw", "core-et ro"), encoding="utf-8")
    assert load_project(first).definition_sha256 != load_project(
        second
    ).definition_sha256


def test_agent_manifest_has_container_paths_but_no_host_paths(tmp_path: Path) -> None:
    config_dir, team_path, source, docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(valid_text(), encoding="utf-8")
    project = load_project(config)

    generic = render_project_manifest(project)
    selected = render_project_manifest(project, team=project.teams[0])

    assert "Name: core-et-uart" in selected
    assert "Description: Design and verify" in selected
    assert "## Writable workspace mounts" in selected
    assert "/workspace/core-et (read-write)" in selected
    assert "## Read-only mounts" in selected
    assert "/readonly/specifications (read-only)" in selected
    assert "/team (read-only; jon-rtl)" in selected
    assert "jon-rtl (read-only)" in generic
    for host_path in (config, team_path, source, docs):
        assert str(host_path.resolve()) not in generic
        assert str(host_path.resolve()) not in selected


def test_manifest_rejects_a_team_from_another_definition(tmp_path: Path) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(valid_text(), encoding="utf-8")
    project = load_project(config)
    foreign = ProjectTeam(path=tmp_path / "foreign", mode="ro", line=1)

    with pytest.raises(ValueError, match="not part"):
        render_project_manifest(project, team=foreign)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "missing required name"),
        ("description words\n", "missing required name"),
        ("name demo\n", "missing required description"),
        ("name demo\nname again\n", "duplicate name"),
        (
            "name demo\ndescription one\ndescription two\n",
            "duplicate description",
        ),
        ("name UPPER\n", "invalid project name"),
        ("name demo extra\n", "expected name"),
        ("name demo\ndescription\n", "expected description"),
        (
            "name demo\ndescription words\nteam ../teams/jon-rtl\n",
            "expected team",
        ),
        (
            "name demo\ndescription words\nteam ../teams/jon-rtl execute\n",
            "invalid access mode",
        ),
        (
            "name demo\ndescription words\nteam ../teams/jon-rtl ro\n"
            "mount source ../sources/core-et\n",
            "expected mount",
        ),
        (
            "name demo\ndescription words\nteam ../teams/jon-rtl ro\n"
            "mount source ../sources/core-et write\n",
            "invalid access mode",
        ),
        ("name demo\ndescription words\nwat value\n", "unknown project directive"),
        ("name demo\ndescription words\nmcp docs socket\n", "MCP servers are not supported"),
    ],
)
def test_rejects_malformed_directives_with_source_line(
    tmp_path: Path, content: str, message: str
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(CycloError, match=message) as stopped:
        load_project(config)

    assert str(config) in str(stopped.value)


def test_requires_at_least_one_team_and_mount(tmp_path: Path) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text("name demo\ndescription words\n", encoding="utf-8")
    with pytest.raises(CycloError, match="no teams"):
        load_project(config)

    config.write_text(
        "name demo\ndescription words\nteam ../teams/jon-rtl ro\n",
        encoding="utf-8",
    )
    with pytest.raises(CycloError, match="no mounts"):
        load_project(config)


@pytest.mark.parametrize(
    ("directive", "message"),
    [
        ("team ~/jon-rtl ro", "invalid path"),
        ("team '../teams/jon-rtl' ro", "invalid path"),
        ("team ..\\teams\\jon-rtl ro", "invalid path"),
        ("mount source ../sources/core,et rw", "invalid path"),
        ("mount source ../missing rw", "path not found"),
    ],
)
def test_rejects_unsafe_or_missing_path_tokens(
    tmp_path: Path, directive: str, message: str
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\ndescription words\n"
        f"{directive}\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match=message):
        load_project(config)


def test_rejects_file_paths_but_allows_resolved_parent_whitespace(tmp_path: Path) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("data", encoding="utf-8")
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\ndescription words\n"
        "team ../not-a-directory ro\n",
        encoding="utf-8",
    )
    with pytest.raises(CycloError, match="not a directory"):
        load_project(config)

    spaced = tmp_path / "path with spaces"
    spaced.mkdir()
    alias = tmp_path / "space-alias"
    alias.symlink_to(spaced, target_is_directory=True)
    config.write_text(
        "name demo\ndescription words\n"
        "team ../teams/jon-rtl ro\n"
        "mount spaced ../space-alias ro\n",
        encoding="utf-8",
    )
    assert load_project(config).mounts[0].path == spaced.resolve()

    comma = tmp_path / "path,with-comma"
    comma.mkdir()
    comma_alias = tmp_path / "comma-alias"
    comma_alias.symlink_to(comma, target_is_directory=True)
    config.write_text(
        "name demo\ndescription words\n"
        "team ../teams/jon-rtl ro\n"
        "mount comma ../comma-alias ro\n",
        encoding="utf-8",
    )
    with pytest.raises(CycloError, match="resolved path cannot contain a comma"):
        load_project(config)


def test_rejects_duplicate_team_and_mount_identities(tmp_path: Path) -> None:
    config_dir, team, source, _docs = project_tree(tmp_path)
    other_team = tmp_path / "other" / team.name
    other_team.mkdir(parents=True)
    other_source = tmp_path / "other-source"
    other_source.mkdir()
    config = config_dir / "project.cyclo"

    cases = [
        (
            "team ../teams/jon-rtl ro\nteam ../teams/jon-rtl ro\n"
            "mount source ../sources/core-et rw\n",
            "duplicate team path",
        ),
        (
            f"team ../teams/jon-rtl ro\nteam {other_team} ro\n"
            "mount source ../sources/core-et rw\n",
            "duplicate team name",
        ),
        (
            "team ../teams/jon-rtl ro\n"
            "mount source ../sources/core-et rw\n"
            f"mount source {other_source} ro\n",
            "duplicate mount name",
        ),
        (
            "team ../teams/jon-rtl ro\n"
            "mount source ../sources/core-et rw\n"
            "mount alias ../sources/core-et ro\n",
            "duplicate mount path",
        ),
    ]
    for body, message in cases:
        config.write_text(
            "name demo\ndescription words\n" + body,
            encoding="utf-8",
        )
        with pytest.raises(CycloError, match=message):
            load_project(config)

    assert source.is_dir()


def test_rejects_overlapping_team_and_mount_trees(tmp_path: Path) -> None:
    config_dir, team, _source, _docs = project_tree(tmp_path)
    nested = team / "nested-project"
    nested.mkdir()
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\ndescription words\n"
        "team ../teams/jon-rtl ro\n"
        "mount nested ../teams/jon-rtl/nested-project rw\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="overlaps team") as stopped:
        load_project(config)

    assert ":4:" in str(stopped.value)


@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_project_definition_may_be_inside_a_project_mount(
    tmp_path: Path, mode: str
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\ndescription words\n"
        "team ../teams/jon-rtl ro\n"
        f"mount definition . {mode}\n",
        encoding="utf-8",
    )

    assert load_project(config).mounts[0].mode == mode


def test_project_definition_may_be_inside_a_writable_team(tmp_path: Path) -> None:
    config_dir, _team, source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\ndescription words\n"
        "team . rw\n"
        f"mount source {source} rw\n",
        encoding="utf-8",
    )

    project = load_project(config)
    assert project.teams[0].path == config_dir.resolve()
    assert project.teams[0].writable


def test_project_file_must_be_regular_bounded_utf8_without_controls(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.cyclo"
    with pytest.raises(CycloError, match="not found"):
        load_project(missing)

    directory = tmp_path / "directory.cyclo"
    directory.mkdir()
    with pytest.raises(CycloError, match="not a regular file"):
        load_project(directory)

    target = tmp_path / "target.cyclo"
    target.write_text("name demo\n", encoding="utf-8")
    alias = tmp_path / "alias.cyclo"
    alias.symlink_to(target)
    with pytest.raises(CycloError, match="must not be a symlink"):
        load_project(alias)

    invalid = tmp_path / "invalid.cyclo"
    invalid.write_bytes(b"name \xff\n")
    with pytest.raises(CycloError, match="not valid UTF-8"):
        load_project(invalid)

    controls = tmp_path / "controls.cyclo"
    controls.write_bytes(b"name\tdemo\n")
    with pytest.raises(CycloError, match="control character"):
        load_project(controls)

    oversized = tmp_path / "oversized.cyclo"
    oversized.write_bytes(b"#" * (MAX_PROJECT_FILE_BYTES + 1))
    with pytest.raises(CycloError, match="exceeds"):
        load_project(oversized)
