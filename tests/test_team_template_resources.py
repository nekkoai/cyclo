from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.team import load_team
from cyclo.team.templates import (
    bundled_team_template_names,
    packaged_team_template,
    packaged_team_templates_root,
)


EXPECTED = (
    "adversarial-audit",
    "plan-execute-verify",
    "test-driven-repair",
)


def test_all_example_teams_are_packaged() -> None:
    assert bundled_team_template_names() == EXPECTED
    for name in EXPECTED:
        team = load_team(packaged_team_template(name))
        assert team.agents


def test_packaged_examples_match_source_examples() -> None:
    source = Path(__file__).parents[1] / "template"
    packaged = packaged_team_templates_root()
    for name in EXPECTED:
        source_files = {
            path.relative_to(source / name): path.read_bytes()
            for path in (source / name).rglob("*")
            if path.is_file()
        }
        packaged_files = {
            path.relative_to(packaged / name): path.read_bytes()
            for path in (packaged / name).rglob("*")
            if path.is_file()
        }
        assert packaged_files == source_files


def test_packaged_template_index_matches_source_index() -> None:
    source = Path(__file__).parents[1] / "template" / "README.md"
    packaged = packaged_team_templates_root() / "README.md"

    assert packaged.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("name", ["../escape", ".", "missing"])
def test_packaged_template_name_is_confined(name: str) -> None:
    with pytest.raises(CycloError):
        packaged_team_template(name)
