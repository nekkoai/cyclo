from __future__ import annotations

import subprocess
from pathlib import Path

from cyclo import team_runtime_image


def test_source_files_prunes_installed_dependencies(tmp_path: Path) -> None:
    source = tmp_path / "team-runtime" / "src"
    source.mkdir(parents=True)
    (source / "owned.mjs").write_text("export {};\n", encoding="utf-8")
    installed = tmp_path / "team-runtime" / "node_modules" / "package"
    installed.mkdir(parents=True)
    (installed / "ignored.mjs").write_text("throw new Error();\n", encoding="utf-8")
    (installed / "broken").symlink_to(tmp_path / "missing")

    assert team_runtime_image.source_files(tmp_path) == (
        Path("team-runtime/src/owned.mjs"),
    )


def test_packaged_team_image_uses_common_component_context() -> None:
    root = team_runtime_image.context_root()

    assert root.parent.name == "components"
    assert root.name == "team-runtime"
    assert team_runtime_image.dockerfile_path().parent == root
    assert team_runtime_image.build_context_root() == root.parent
    assert (root / "entrypoint.sh").is_file()
    assert (root / "package-lock.json").is_file()
    assert (root.parent / "pi-provider" / "src" / "extension.mjs").is_file()
    assert "npm:pi-lens" in team_runtime_image.PI_PACKAGES
    assert team_runtime_image.PI_PACKAGES[-1] == "/opt/cyclo/pi-provider"
    files = {path.as_posix() for path in team_runtime_image.source_files()}
    assert "team-runtime/Dockerfile" in files
    assert "protocol/component/src/transport.mjs" in files
    assert "protocol/provider/src/protocol.mjs" in files
    assert "pi-provider/src/extension.mjs" in files
    assert all(not path.startswith("gateway/") for path in files)
    assert all("node_modules" not in path for path in files)

    dockerfile = team_runtime_image.dockerfile_path().read_text(encoding="utf-8")
    assert "COPY --from=agent-tools /opt/cyclo /opt/cyclo" in dockerfile
    assert "/opt/cyclo/pi-provider/node_modules/@earendil-works/pi-ai" in dockerfile

    fingerprint = team_runtime_image.source_fingerprint()
    command = team_runtime_image.build_command("cyclo-runtime:test", fingerprint)
    assert command[-1] == str(root.parent)
    assert str(team_runtime_image.dockerfile_path()) in command
    assert len(fingerprint) == 64


def test_team_runtime_image_build_is_independent_from_gateway(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "inspect",
        lambda _kind, _image: None,
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "call",
        lambda command, **_kwargs: commands.append(["docker", *command])
        or subprocess.CompletedProcess(command, 0),
    )

    team_runtime_image.ensure("cyclo-runtime:test")

    assert commands == [
        team_runtime_image.build_command(
            "cyclo-runtime:test", team_runtime_image.source_fingerprint()
        )
    ]


def test_team_runtime_image_is_reused_only_at_exact_fingerprint(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    fingerprint = team_runtime_image.source_fingerprint()
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "inspect",
        lambda _kind, _image: {
            "Config": {
                "Labels": {
                    team_runtime_image.SOURCE_FINGERPRINT_LABEL: fingerprint
                }
            }
        },
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "call",
        lambda command, **_kwargs: commands.append(command),
    )

    team_runtime_image.ensure("cyclo-runtime:test")

    assert commands == []
