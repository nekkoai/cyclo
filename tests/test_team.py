from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cyclo.agentws_bundle import packaged_agentws_root
from cyclo.errors import CycloError
from cyclo.team import load_team, require_team_repository, team_generation, verify_agentws_abi


def test_loads_plain_agentws_roster(team_repo: Path) -> None:
    team = load_team(team_repo)

    assert team.name == "review-team"
    assert [agent.name for agent in team.agents] == ["planner-1", "reviewer-1"]
    assert team.agents[0].model == "openai-codex/gpt-test"
    assert team.providers == ("anthropic", "openai-codex")
    require_team_repository(team)


def test_requires_explicit_proxy_model(team_repo: Path) -> None:
    (team_repo / "team").write_text("planner-1 planner pi\n", encoding="utf-8")

    with pytest.raises(CycloError, match="provider/model"):
        load_team(team_repo)


@pytest.mark.parametrize(
    "provider",
    ["OpenAI", "with.dot", "constructor", "gateway", "prototype"],
)
def test_rejects_provider_names_the_gateway_cannot_route(
    team_repo: Path, provider: str
) -> None:
    (team_repo / "team").write_text(
        f"planner-1 planner pi {provider}/model\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match="invalid proxy model"):
        load_team(team_repo)


@pytest.mark.parametrize("provider", ["_legacy", "-legacy"])
def test_preserves_legacy_direct_provider_names(
    team_repo: Path, provider: str
) -> None:
    (team_repo / "team").write_text(
        f"planner-1 planner pi {provider}/model\n", encoding="utf-8"
    )

    team = load_team(team_repo)
    assert team.providers == (provider,)
    assert team.agents[0].model == f"{provider}/model"


def test_rejects_non_pi_engine(team_repo: Path) -> None:
    (team_repo / "team").write_text(
        "planner-1 planner codex openai-codex/gpt-test\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match="supports pi"):
        load_team(team_repo)


def test_rejects_missing_role_file(team_repo: Path) -> None:
    (team_repo / "team").write_text(
        "builder-1 implementer pi openai-codex/gpt-test\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match="role file not found"):
        load_team(team_repo)


@pytest.mark.parametrize("name", [".", ".."])
def test_rejects_path_segment_agent_names(team_repo: Path, name: str) -> None:
    (team_repo / "team").write_text(
        f"{name} planner pi openai-codex/gpt-test\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match="invalid agent name"):
        load_team(team_repo)


def test_rejects_role_symlink_escaping_repository(team_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    role = team_repo / "roles" / "planner.md"
    role.unlink()
    role.symlink_to(outside)

    with pytest.raises(CycloError, match="role.*symlink"):
        load_team(team_repo)


def test_rejects_unreferenced_role_symlink(team_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (team_repo / "roles" / "unused.md").symlink_to(outside)

    with pytest.raises(CycloError, match="role.*symlink"):
        load_team(team_repo)


def test_generation_rechecks_role_after_team_load(
    team_repo: Path, tmp_path: Path
) -> None:
    team = load_team(team_repo)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    role = team_repo / "roles" / "reviewer.md"
    role.unlink()
    role.symlink_to(outside)

    with pytest.raises(CycloError, match="role.*symlink"):
        team_generation(team)


def test_generation_never_follows_replaced_roles_directory(
    team_repo: Path, tmp_path: Path
) -> None:
    team = load_team(team_repo)
    original_roles = team_repo / "roles-original"
    (team_repo / "roles").rename(original_roles)
    outside_roles = tmp_path / "outside-roles"
    outside_roles.mkdir()
    (outside_roles / "planner.md").write_text("private host text\n", encoding="utf-8")
    (team_repo / "roles").symlink_to(outside_roles, target_is_directory=True)

    with pytest.raises(CycloError, match="roles directory.*symlink"):
        team_generation(team)


def test_requires_planner_task_entry_role(team_repo: Path) -> None:
    (team_repo / "team").write_text(
        "reviewer-1 reviewer pi anthropic/claude-test\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="planner.*task entry role"):
        load_team(team_repo)


def test_generation_tracks_uncommitted_team_content(team_repo: Path) -> None:
    before = team_generation(load_team(team_repo))
    (team_repo / "roles" / "planner.md").write_text("Plan differently.\n", encoding="utf-8")
    after = team_generation(load_team(team_repo))

    assert before.startswith("unborn-content-")
    assert after.startswith("unborn-content-")
    assert before != after


def test_generation_does_not_execute_repository_fsmonitor(
    team_repo: Path, tmp_path: Path
) -> None:
    hook = tmp_path / "malicious-fsmonitor"
    marker = Path(f"{hook}.executed")
    hook.write_text(
        "#!/bin/sh\n"
        ': > "$0.executed"\n'
        "printf '\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(team_repo), "config", "core.fsmonitor", str(hook)],
        check=True,
    )

    generation = team_generation(load_team(team_repo))

    assert generation.startswith("unborn-content-")
    assert not marker.exists()


def test_packaged_agentws_has_external_team_abi() -> None:
    verify_agentws_abi(packaged_agentws_root())
