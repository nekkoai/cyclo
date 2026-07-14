from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.gateway import CredentialGateway
from cyclo.state import Instance, StateStore
from cyclo.team import load_team


def configured_instance(identifier: str, active: bool, provider: str) -> Instance:
    return Instance(
        id=identifier,
        team_name="team",
        team_path="/tmp/team",
        project_path="/tmp/project",
        generation="abc",
        providers=[provider],
        models=[f"{provider}/model"],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=active,
    )


def test_client_snapshot_includes_every_active_instance(tmp_path: Path) -> None:
    adapter = CredentialGateway(StateStore(tmp_path / "state"))
    clients, provider_scopes, model_scopes = adapter._clients(
        [
            configured_instance("one", True, "openai-codex"),
            configured_instance("two", True, "anthropic"),
            configured_instance("old", False, "xai"),
        ]
    )

    assert [client.project_id for client in clients] == ["one", "two"]
    assert provider_scopes == {"one": ("openai-codex",), "two": ("anthropic",)}
    assert model_scopes == {
        "one": ("openai-codex/model",),
        "two": ("anthropic/model",),
    }


def test_catalog_validation_is_per_provider_and_model(team_repo: Path) -> None:
    team = load_team(team_repo)
    valid = {
        "openai-codex": {"models": [{"id": "gpt-test"}]},
        "anthropic": {"models": [{"id": "claude-test"}]},
    }
    CredentialGateway._validate_models(team, valid)

    with pytest.raises(CycloError, match="not in the gateway catalog"):
        CredentialGateway._validate_models(
            team,
            {
                "openai-codex": {"models": [{"id": "different"}]},
                "anthropic": {"models": [{"id": "claude-test"}]},
            },
        )


@pytest.mark.parametrize("bad_models", [None, [], "not-a-list", [{}]])
def test_catalog_validation_rejects_missing_or_malformed_models(
    team_repo: Path, bad_models: object
) -> None:
    team = load_team(team_repo)
    catalog = {
        "openai-codex": {"models": bad_models},
        "anthropic": {"models": [{"id": "claude-test"}]},
    }

    with pytest.raises(CycloError, match="model catalogue|not in the gateway catalog"):
        CredentialGateway._validate_models(team, catalog)


def test_catalog_validation_rejects_numeric_model_id(team_repo: Path) -> None:
    team = load_team(team_repo)
    catalog = {
        "openai-codex": {"models": [{"id": 123}]},
        "anthropic": {"models": [{"id": "claude-test"}]},
    }

    with pytest.raises(CycloError, match="not in the gateway catalog"):
        CredentialGateway._validate_models(team, catalog)


def test_projection_defaults_come_from_first_roster_agent(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    (team_repo / "team").write_text(
        "reviewer-1 reviewer pi-interactive anthropic/claude-test\n"
        "planner-1 planner pi openai-codex/gpt-test\n",
        encoding="utf-8",
    )
    team = load_team(team_repo)
    project_settings = project_repo / ".pi" / "settings.json"
    project_settings.parent.mkdir()
    project_settings.write_text(
        json.dumps(
            {
                "defaultProvider": "openai-codex",
                "defaultModel": "host-controlled-model",
            }
        ),
        encoding="utf-8",
    )

    store = StateStore(tmp_path / "state")
    instance = Instance(
        id="alpha",
        team_name=team.name,
        team_path=str(team.root),
        project_path=str(project_repo),
        generation="generation",
        providers=list(team.providers),
        models=[agent.model for agent in team.agents],
        container_name="cyclo-alpha",
        network_name="cyclo-alpha-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=True,
    )
    catalog = {
        "anthropic": {"models": [{"id": "claude-test"}]},
        "openai-codex": {"models": [{"id": "gpt-test"}]},
    }

    class FakeGateway:
        @staticmethod
        def gateway_container_name(_registry: Path) -> str:
            return "test-gateway"

        @staticmethod
        def gateway_network_name(_registry: Path) -> str:
            return "test-gateway-network"

        @staticmethod
        def published_port(container: str) -> int:
            assert container == "test-gateway"
            return 8787

        @staticmethod
        def fetch_provider_catalog(port: int, token: str) -> dict[str, dict]:
            assert (port, token) == (8787, "scoped-token")
            return catalog

    class FakeAuth:
        PI_PACKAGES = ("package:test",)

        @staticmethod
        def gateway_base_url(container: str) -> str:
            assert container == "test-gateway"
            return "http://test-gateway:8787"

        @staticmethod
        def projected_models_json(
            projected_catalog: dict[str, dict], base_url: str, token: str
        ) -> dict[str, object]:
            assert projected_catalog == catalog
            return {"base_url": base_url, "token": token}

    adapter = CredentialGateway.__new__(CredentialGateway)
    adapter.store = store
    adapter.gateway = FakeGateway()
    adapter.auth = FakeAuth()
    adapter.reconcile = lambda _instances, *, build=False: {"alpha": "scoped-token"}

    adapter.prepare_instance(instance, team, project_repo, [instance])

    agent_dir = store.pi_root("alpha") / "agent"
    settings = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    assert settings["defaultProvider"] == "anthropic"
    assert settings["defaultModel"] == "claude-test"
    assert settings["packages"] == ["package:test"]
    assert models == {
        "base_url": "http://test-gateway:8787",
        "token": "scoped-token",
    }
