from __future__ import annotations

import subprocess
from pathlib import Path

from cyclo import team_runtime_image


IMAGE_ID = "sha256:" + "a" * 64


def image_info(
    fingerprint: str,
    *,
    base_image: str | None = None,
) -> dict[str, object]:
    labels = {team_runtime_image.SOURCE_FINGERPRINT_LABEL: fingerprint}
    if base_image is not None:
        labels[team_runtime_image.BASE_IMAGE_LABEL] = base_image
    return {
        "Id": IMAGE_ID,
        "Config": {
            "Entrypoint": list(team_runtime_image.TEAM_RUNTIME_ENTRYPOINT),
            "Labels": labels,
        },
    }


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
    command = team_runtime_image.build_command(fingerprint)
    assert command[-1] == str(root.parent)
    assert str(team_runtime_image.dockerfile_path()) in command
    assert len(fingerprint) == 64


def test_team_runtime_image_build_is_independent_from_gateway(
    monkeypatch,
) -> None:
    commands: list[tuple[str, list[str], str]] = []
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "inspect",
        lambda _kind, _image: None,
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "build_image",
        lambda image, command, _validate, **_kwargs: commands.append(
            (image, command, team_runtime_image.source_fingerprint())
        )
        or IMAGE_ID,
    )

    assert team_runtime_image.ensure("cyclo-runtime:test") == IMAGE_ID

    assert commands == [
        (
            "cyclo-runtime:test",
            team_runtime_image.build_command(team_runtime_image.source_fingerprint()),
            team_runtime_image.source_fingerprint(),
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
        lambda _kind, _image: image_info(fingerprint),
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "call",
        lambda command, **_kwargs: commands.append(command),
    )

    assert team_runtime_image.ensure("cyclo-runtime:test") == IMAGE_ID

    assert commands == []


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
    (root / "packages.txt").write_text("verilator\n", encoding="utf-8")
    base = "sha256:" + "b" * 64
    def inspect(_kind, reference, **_kwargs):
        if str(reference).startswith("cyclo-team-base-pin:"):
            pinned = image_info("base")
            pinned["Id"] = base
            return pinned
        return image_info("derived", base_image=base)

    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "inspect",
        inspect,
    )
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "call",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    builds: list[list[str]] = []
    monkeypatch.setattr(
        team_runtime_image.docker_runner,
        "build_image",
        lambda _image, command, _validate, **_kwargs: builds.append(command)
        or IMAGE_ID,
    )

    assert (
        team_runtime_image.ensure_derived(
            "cyclo-derived:test",
            root,
            base,
        )
        == IMAGE_ID
    )
    assert builds == []

    assert (
        team_runtime_image.ensure_derived(
            "cyclo-derived:test",
            root,
            base,
            build=True,
        )
        == IMAGE_ID
    )
    assert len(builds) == 1
    command = builds[0]
    assert any(
        value.startswith("CYCLO_TEAM_BASE=cyclo-team-base-pin:")
        for value in command
    )
    assert f"{team_runtime_image.BASE_IMAGE_LABEL}={base}" in command
    assert command[command.index("--file") + 1] == str(dockerfile)
    assert command[-1] == str(root)
