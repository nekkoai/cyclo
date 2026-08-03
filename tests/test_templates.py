from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cyclo.team import load_team, require_team_repository, team_generation


TEMPLATES = {
    "plan-execute-verify": {"planner", "builder", "critic", "verifier"},
    "test-driven-repair": {"planner", "implementer", "judge", "committer"},
    "adversarial-audit": {
        "planner",
        "threat-modeler",
        "inspector",
        "challenger",
        "synthesizer",
    },
}

AVAILABLE_EXAMPLE_MODELS = {
    "openai-codex/gpt-5.3-codex",
    "openai-codex/gpt-5.4",
    "openai-codex/gpt-5.4-mini",
    "openai-codex/gpt-5.5",
}


@pytest.mark.parametrize(("name", "roles"), TEMPLATES.items())
def test_example_team_is_complete_and_copy_ready(
    tmp_path: Path, name: str, roles: set[str]
) -> None:
    source = Path(__file__).parents[1] / "template" / name
    destination = tmp_path / name
    shutil.copytree(source, destination)
    subprocess.run(["git", "init", "-q", str(destination)], check=True)

    team = load_team(destination)
    require_team_repository(team)

    assert team.roster.name == "team"
    assert team.protocol is None
    assert {agent.role for agent in team.agents} == roles
    assert all(agent.engine in {"pi", "pi-interactive"} for agent in team.agents)
    assert {agent.model for agent in team.agents} <= AVAILABLE_EXAMPLE_MODELS
    assert len({agent.model for agent in team.agents}) > 1
    assert (destination / "README.md").is_file()
    assert team.dockerfile == destination / "Dockerfile"
    assert team_generation(team).startswith("unborn-content-")


def test_template_index_names_every_example() -> None:
    root = Path(__file__).parents[1] / "template"
    index = (root / "README.md").read_text(encoding="utf-8")

    assert {path.name for path in root.iterdir() if (path / "team").is_file()} == set(
        TEMPLATES
    )
    for name in TEMPLATES:
        assert f"`{name}`" in index
