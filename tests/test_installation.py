from pathlib import Path

from cyclo.installation import installation_id, realm_id


def test_state_root_defines_stable_independent_system_namespace(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    assert installation_id(first) == installation_id(first)
    assert installation_id(first) != installation_id(second)
    assert len(installation_id(first)) == 12
    assert realm_id(first) == installation_id(first)
