from __future__ import annotations

from pathlib import Path

from cyclo.cli import main as cyclo_main
from cyclo.state import StateStore
from cyclo.credential_gateway import cli, gateway, source


def test_supported_providers_command_is_hardened_and_credential_free() -> None:
    command = cli.providers_command("cyclo-gateway:test")

    assert command == [
        "docker",
        "run",
        "--rm",
        *cli.GATEWAY_CONTAINER_HARDENING,
        "--network",
        "none",
        "cyclo-gateway:test",
        "supported-providers.mjs",
    ]
    assert "--mount" not in command
    assert "--env" not in command
    assert "-e" not in command
    assert gateway.GATEWAY_STORE_PATH not in " ".join(command)


def test_gateway_providers_runs_before_login_without_credentials(
    monkeypatch,
) -> None:
    ensured: list[tuple[str, bool]] = []
    commands: list[list[str]] = []
    for variable in cli.PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        gateway,
        "ensure_gateway_image",
        lambda image, *, build=False: ensured.append((image, build)),
    )
    monkeypatch.setattr(
        cli.docker,
        "run_command",
        lambda command: commands.append(command) or 0,
    )

    assert cli.main(["providers", "--image", "cyclo-gateway:test", "--build"]) == 0
    assert ensured == [("cyclo-gateway:test", True)]
    assert commands == [cli.providers_command("cyclo-gateway:test")]


def test_outer_gateway_providers_does_not_forward_the_store_volume(monkeypatch) -> None:
    delegated: list[list[str]] = []
    monkeypatch.setattr(
        "cyclo.cli.gateway_cli.main",
        lambda arguments: delegated.append(arguments) or 0,
    )

    assert (
        cyclo_main(
            [
                "--gateway-image",
                "cyclo-gateway:test",
                "--store-volume",
                "must-not-be-mounted",
                "gateway",
                "providers",
                "--build",
            ]
        )
        == 0
    )
    assert delegated == [
        ["providers", "--image", "cyclo-gateway:test", "--build"]
    ]


def test_models_empty_catalog_points_to_provider_discovery_then_login(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")

    class FakeDocker:
        pass

    class FakeProxy:
        def catalog(self, instances, *, build=False):
            assert instances == []
            assert not build
            return {}

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: FakeProxy())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda _store, _docker, *, stale: [],
    )

    assert cyclo_main(["models"]) == 1
    error = capsys.readouterr().err
    assert "gateway returned no models" in error
    assert "cyclo gateway providers" in error
    assert "cyclo gateway login PROVIDER" in error
    assert error.index("cyclo gateway providers") < error.index(
        "cyclo gateway login PROVIDER"
    )


def test_supported_provider_entrypoint_is_packaged() -> None:
    entrypoint = source.gateway_context_root() / "supported-providers.mjs"
    assert entrypoint.is_file()
    assert "store.mjs" not in entrypoint.read_text(encoding="utf-8")
