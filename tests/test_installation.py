from pathlib import Path

import pytest

from cyclo.component_stack import Gateway, ProviderStack
from cyclo.errors import CycloError
from cyclo.installation import (
    gateway_name,
    installation_id,
    provider_name,
    resource_labels,
    team_container_name,
    team_image_name,
    team_network_name,
)
from cyclo.state import StateStore


def test_state_root_defines_stable_independent_resource_namespace(
    tmp_path: Path,
) -> None:
    first_store = StateStore(tmp_path / "first")
    second_store = StateStore(tmp_path / "second")
    first = first_store.system
    second = second_store.system

    assert first == installation_id(first_store.components_root)
    assert first != second
    assert team_container_name(first, "project-team") == (
        f"cyclo-{first}-team-project-team"
    )
    assert team_network_name(first, "project-team") == (
        f"cyclo-{first}-team-project-team-net"
    )
    assert team_image_name(first, "0.2.0") == f"cyclo-{first}-team:0.2.0"
    assert len(
        {
            gateway_name(first),
            provider_name(first, "team-project-team"),
            team_container_name(first, "project-team"),
        }
    ) == 3


def test_gateway_providers_and_teams_share_one_installation_identity(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    gateway = Gateway(store.components_root)
    providers = ProviderStack(
        store.components_root,
        tmp_path / "host.conf",
        gateway=gateway,
        load_config=False,
    )

    assert store.system == gateway.deployment.system == providers.system


def test_resource_identity_rejects_untrusted_installation_id() -> None:
    with pytest.raises(CycloError, match="invalid Cyclo installation ID"):
        resource_labels("../../other", "team", "instance")
