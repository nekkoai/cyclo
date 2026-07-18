from __future__ import annotations

from cyclo import team_runtime_image


def test_packaged_team_runtime_context_is_owned_outside_gateway() -> None:
    root = team_runtime_image.context_root()

    assert root.parent.name == "cyclo"
    assert root.name == "team_runtime_context"
    assert team_runtime_image.dockerfile_path().parent == root
    assert (root / "entrypoint.sh").is_file()
    assert (root / "package-lock.json").is_file()

    fingerprint = team_runtime_image.source_fingerprint()
    command = team_runtime_image.build_command("cyclo-runtime:test", fingerprint)
    assert command[-1] == str(root)
    assert str(team_runtime_image.dockerfile_path()) in command
    assert len(fingerprint) == 64


def test_team_runtime_image_build_is_independent_from_gateway(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "docker_image_exists",
        lambda _image: False,
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "run_command",
        lambda command: commands.append(command) or 0,
    )

    team_runtime_image.ensure("cyclo-runtime:test")

    assert commands == [
        team_runtime_image.build_command(
            "cyclo-runtime:test", team_runtime_image.source_fingerprint()
        )
    ]
