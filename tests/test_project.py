from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.project import (
    MAX_PROJECT_FILE_BYTES,
    ProjectDefinition,
    ProjectTeam,
    load_project,
    read_project_context,
    render_container_project,
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


def load_rendered_container_project(
    tmp_path: Path,
    rendered: str,
    source: ProjectDefinition,
) -> ProjectDefinition:
    """Parse normalized container paths after mapping them to real test paths."""

    selected = source.teams[0]
    translated = rendered.replace(
        f"team /team {selected.mode}",
        f"team {selected.path} {selected.mode}",
    )
    for mount in source.mounts:
        translated = translated.replace(
            f"mount {mount.name} {mount.container_path} {mount.mode}",
            f"mount {mount.name} {mount.path} {mount.mode}",
        )
    path = tmp_path / "rendered-container-project.cyclo"
    path.write_text(translated, encoding="utf-8")
    return load_project(path)


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


def test_context_block_is_literal_hashed_and_rendered(tmp_path: Path) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    context = (
        "`core-et` is the implementation tree.\n"
        "\n"
        "  Preserve this indentation.\n"
        "# This is project guidance, not a comment.\n"
        "mount example /not/a/directive rw\n"
        "CYCLO_CONTEXT\n"
        "CYCLO_CONTEXT_2"
    )
    config.write_text(
        valid_text().replace(
            "team ../teams/jon-rtl ro\n",
            f"context <<PROJECT_CONTEXT\n\n{context}\n\nPROJECT_CONTEXT\n"
            "team ../teams/jon-rtl ro\n",
        ),
        encoding="utf-8",
    )

    project = load_project(config)
    rendered = render_container_project(project, team=project.teams[0])

    assert project.context == context
    assert rendered.count(context) == 1
    assert "context <<CYCLO_CONTEXT_3\n" in rendered
    assert load_rendered_container_project(
        tmp_path, rendered, project
    ).context == context

    original_hash = project.definition_sha256
    equivalent = config_dir / "equivalent.cyclo"
    equivalent.write_bytes(
        config.read_text(encoding="utf-8")
        .replace("PROJECT_CONTEXT", "OTHER_MARKER")
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    assert load_project(equivalent).definition_sha256 == original_hash

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "implementation tree", "RTL implementation tree"
        ),
        encoding="utf-8",
    )
    assert load_project(config).definition_sha256 != original_hash


def test_container_project_rejects_generated_output_over_size_limit(
    tmp_path: Path,
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(valid_text(), encoding="utf-8")
    project = load_project(config)
    oversized = replace(project, context="x" * MAX_PROJECT_FILE_BYTES)

    with pytest.raises(
        CycloError,
        match="container project configuration exceeds",
    ):
        render_container_project(oversized, team=oversized.teams[0])


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("context words\n", "expected context"),
        ("context <<bad-marker\n", "expected context"),
        ("context <<END\nEND\n", "must not be empty"),
        ("context <<END\nwords\n", "unterminated context block"),
        (
            "context <<ONE\nfirst\nONE\ncontext <<TWO\nsecond\nTWO\n",
            "duplicate context",
        ),
    ],
)
def test_rejects_malformed_context_blocks(
    tmp_path: Path, block: str, message: str
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(
        "name demo\n"
        "description words\n"
        f"{block}"
        "team ../teams/jon-rtl ro\n"
        "mount source ../sources/core-et rw\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match=message) as stopped:
        load_project(config)

    assert str(config) in str(stopped.value)


def test_container_project_is_valid_normalized_and_has_no_host_paths(
    tmp_path: Path,
) -> None:
    config_dir, team_path, source, docs = project_tree(tmp_path)
    audit_team = tmp_path / "teams" / "rtl-auditor"
    firmware = tmp_path / "sources" / "firmware"
    audit_team.mkdir()
    firmware.mkdir()
    config = config_dir / "project.cyclo"
    config.write_text(
        valid_text().replace(
            "team ../teams/jon-rtl ro\n",
            "team ../teams/jon-rtl ro\n"
            "team ../teams/rtl-auditor rw\n",
        )
        + "mount firmware ../sources/firmware rw\n",
        encoding="utf-8",
    )
    project = load_project(config)

    rendered = render_container_project(project, team=project.teams[0])
    audit_rendered = render_container_project(project, team=project.teams[1])
    reparsed = load_rendered_container_project(tmp_path, rendered, project)

    assert "name core-et-uart\n" in rendered
    assert (
        "description Design and verify a UART IP for OpenHW CORE-V.\n"
        in rendered
    )
    assert "team /team ro\n" in rendered
    assert rendered.count("\nteam ") == 1
    assert "team /team rw\n" in audit_rendered
    assert audit_rendered.count("\nteam ") == 1
    assert "mount core-et /workspace/core-et rw\n" in rendered
    assert "mount firmware /workspace/firmware rw\n" in rendered
    assert (
        "mount specifications /readonly/specifications ro\n"
        in rendered
    )
    assert reparsed.name == project.name
    assert reparsed.description == project.description
    assert reparsed.context == project.context
    assert [(team.mode, team.path) for team in reparsed.teams] == [
        ("ro", team_path.resolve())
    ]
    assert [
        (mount.name, mount.mode, mount.path) for mount in reparsed.mounts
    ] == [
        ("core-et", "rw", source.resolve()),
        ("specifications", "ro", docs.resolve()),
        ("firmware", "rw", firmware.resolve()),
    ]
    for host_path in (
        config,
        team_path,
        audit_team,
        source,
        docs,
        firmware,
    ):
        assert str(host_path.resolve()) not in rendered
        assert str(host_path.resolve()) not in audit_rendered


def test_container_project_rejects_a_team_from_another_definition(
    tmp_path: Path,
) -> None:
    config_dir, _team, _source, _docs = project_tree(tmp_path)
    config = config_dir / "project.cyclo"
    config.write_text(valid_text(), encoding="utf-8")
    project = load_project(config)
    foreign = ProjectTeam(path=tmp_path / "foreign", mode="ro", line=1)

    with pytest.raises(ValueError, match="not part"):
        render_container_project(project, team=foreign)


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


def test_project_context_file_uses_the_same_safe_read_boundary(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context.md"
    context.write_bytes(b"Source layout.\r\n")
    assert read_project_context(context) == "Source layout."

    alias = tmp_path / "context-alias.md"
    alias.symlink_to(context)
    with pytest.raises(CycloError, match="must not be a symlink"):
        read_project_context(alias)

    fifo = tmp_path / "context.fifo"
    os.mkfifo(fifo)
    with pytest.raises(CycloError, match="not a regular file"):
        read_project_context(fifo)
