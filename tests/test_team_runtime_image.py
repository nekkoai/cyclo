from __future__ import annotations

import subprocess
from pathlib import Path

from cyclo import team_runtime_image


IMAGE_ID = "sha256:" + "a" * 64


def image_info(*, image_id: str = IMAGE_ID, base_image: str | None = None) -> dict[str, object]:
    labels = (
        {team_runtime_image.BASE_IMAGE_LABEL: base_image}
        if base_image is not None
        else {}
    )
    return {
        "Id": image_id,
        "Config": {
            "Entrypoint": list(team_runtime_image.TEAM_RUNTIME_ENTRYPOINT),
            "Labels": labels,
        },
    }


def test_packaged_team_image_uses_docker_build_context() -> None:
    root = team_runtime_image.context_root()

    assert root.parent.name == "components"
    assert root.name == "team-runtime"
    assert team_runtime_image.dockerfile_path().parent == root
    assert team_runtime_image.build_context_root() == root.parent
    assert (root / "entrypoint.sh").is_file()
    assert (root / "package-lock.json").is_file()
    assert (root.parent / "pi-provider" / "src" / "extension.mjs").is_file()
    assert (root / "Dockerfile.dockerignore").is_file()
    assert "npm:pi-lens" in team_runtime_image.PI_PACKAGES
    assert team_runtime_image.PI_PACKAGES[-1] == "/opt/cyclo/pi-provider"

    dockerfile = team_runtime_image.dockerfile_path().read_text(encoding="utf-8")
    assert "COPY --from=agent-tools /opt/cyclo /opt/cyclo" in dockerfile
    assert "/opt/cyclo/pi-provider/node_modules/@earendil-works/pi-ai" in dockerfile

    command = team_runtime_image.build_command()
    assert command[-1] == str(root.parent)
    assert str(team_runtime_image.dockerfile_path()) in command
    assert "--label" not in command


def test_team_runtime_image_always_delegates_freshness_to_docker(monkeypatch) -> None:
    commands: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "build_image",
        lambda image, command, _validate, **_kwargs: commands.append((image, command))
        or IMAGE_ID,
    )

    assert team_runtime_image.ensure("cyclo-runtime:test") == IMAGE_ID
    assert commands == [
        ("cyclo-runtime:test", team_runtime_image.build_command())
    ]


def test_derived_team_image_tracks_exact_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "team"
    root.mkdir()
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(
        "ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n",
        encoding="utf-8",
    )
    base = "sha256:" + "b" * 64

    def inspect(_kind, reference, **_kwargs):
        if str(reference).startswith("cyclo-team-base-pin:"):
            return image_info(image_id=base)
        return image_info(base_image=base)

    monkeypatch.setattr(team_runtime_image.docker_runner, "inspect", inspect)
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "call",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    builds: list[tuple[list[str], object]] = []

    def build_image(_image, command, _validate, **kwargs):
        builds.append((command, kwargs["before_promote"]))
        kwargs["before_promote"]()
        return IMAGE_ID

    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "build_image",
        build_image,
    )

    assert (
        team_runtime_image.ensure_derived(
            "cyclo-derived:test",
            root,
            base,
        )
        == IMAGE_ID
    )
    assert len(builds) == 1
    command, _before_promote = builds[0]
    assert any(
        value.startswith("CYCLO_TEAM_BASE=cyclo-team-base-pin:")
        for value in command
    )
    assert f"{team_runtime_image.BASE_IMAGE_LABEL}={base}" in command
    assert command[command.index("--file") + 1] == str(dockerfile)
    assert command[-1] == str(root)
