from __future__ import annotations

from pathlib import Path

import pytest

from cyclo.cli import main
from cyclo.errors import CycloError
from cyclo.host_config import HostConfig


def provider_source(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return path


def test_missing_and_empty_host_config_mean_gateway_only(tmp_path: Path) -> None:
    config = HostConfig(tmp_path / "etc" / "host.conf")
    assert config.load() == ()

    config.path.parent.mkdir(parents=True)
    config.path.write_text("\n# gateway only\n", encoding="utf-8")
    assert config.load() == ()


def test_relative_provider_paths_are_based_on_host_conf_and_arguments_are_opaque(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    etc = tmp_path / "etc" / "cyclo"
    source = provider_source(tmp_path / "providers" / "pool")
    etc.mkdir(parents=True)
    config_path = etc / "host.conf"
    config_path.write_text(
        "provider codex-pool ../../providers/pool "
        "codex-a/gpt-5 codex-b/gpt-5 policy=round-robin\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "unrelated-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    providers = HostConfig(config_path).load()

    assert len(providers) == 1
    assert providers[0].prefix == "codex-pool"
    assert providers[0].path == source.resolve()
    assert providers[0].arguments == (
        "codex-a/gpt-5",
        "codex-b/gpt-5",
        "policy=round-robin",
    )
    assert providers[0].inputs == ("codex-a/gpt-5", "codex-b/gpt-5")
    assert providers[0].parameters == (("policy", "round-robin"),)
    assert providers[0].line == 1


def test_absolute_provider_path_is_canonicalized(tmp_path: Path) -> None:
    source = provider_source(tmp_path / "provider")
    alias = tmp_path / "provider-link"
    alias.symlink_to(source, target_is_directory=True)
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        f"provider pass {alias} codex-work/gpt-5\n", encoding="utf-8"
    )

    assert HostConfig(config_path).load()[0].path == source.resolve()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("component foo ./provider", "unknown host directive"),
        ("provider only", "expected provider <prefix> <path>"),
        ("provider UPPER ./provider codex/gpt-5", "invalid provider prefix"),
        ("provider gateway ./provider codex/gpt-5", "invalid provider prefix"),
        ("provider pass ~/provider codex/gpt-5", "must not use '~'"),
    ],
)
def test_invalid_provider_lines_report_file_and_line(
    tmp_path: Path, line: str, message: str
) -> None:
    config_path = tmp_path / "host.conf"
    config_path.write_text(f"# comment\n{line}\n", encoding="utf-8")

    with pytest.raises(CycloError, match=message) as stopped:
        HostConfig(config_path).load()

    assert f"{config_path}:2:" in str(stopped.value)


def test_duplicate_prefix_is_rejected(tmp_path: Path) -> None:
    source = provider_source(tmp_path / "provider")
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        f"provider fusion {source} codex/gpt-5\n"
        f"provider fusion {source} codex/gpt-5 second=yes\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="duplicate provider prefix"):
        HostConfig(config_path).load()


@pytest.mark.parametrize("make_source", ["missing", "file", "no-dockerfile"])
def test_provider_path_must_be_a_build_context(
    tmp_path: Path, make_source: str
) -> None:
    source = tmp_path / "provider"
    if make_source == "file":
        source.write_text("not a directory\n", encoding="utf-8")
    elif make_source == "no-dockerfile":
        source.mkdir()
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        f"provider pass {source} codex/gpt-5\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match="not found|not a directory|no Dockerfile"):
        HostConfig(config_path).load()


def test_doctor_checks_the_selected_host_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        "provider broken ./missing codex/gpt-5\n", encoding="utf-8"
    )

    class FakeDocker:
        def available(self):
            return True, "test daemon"

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--host-config", str(config_path), "doctor"]) == 1
    output = capsys.readouterr().out
    assert "no  host provider configuration:" in output
    assert f"{config_path}:1:" in output


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        ("", "requires at least one input model"),
        ("mode=fast", "requires at least one input model"),
        ("not-a-model", "expected provider/model"),
        ("UPPER/model", "expected provider/model"),
        ("codex/", "expected provider/model"),
        ("codex/model mode=fast other/model", "appears after a parameter"),
        ("codex/model Mode=fast", "invalid provider parameter key"),
        ("codex/model _mode=fast", "invalid provider parameter key"),
        ("codex/model mode=fast mode=slow", "duplicate provider parameter"),
    ],
)
def test_provider_inputs_and_parameters_are_strict(
    tmp_path: Path, tail: str, message: str
) -> None:
    source = provider_source(tmp_path / "provider")
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        f"provider fusion {source}{' ' if tail else ''}{tail}\n", encoding="utf-8"
    )

    with pytest.raises(CycloError, match=message) as stopped:
        HostConfig(config_path).load()

    assert f"{config_path}:1:" in str(stopped.value)


def test_parameter_values_are_opaque_and_may_be_empty(tmp_path: Path) -> None:
    source = provider_source(tmp_path / "provider")
    config_path = tmp_path / "host.conf"
    config_path.write_text(
        f"provider fusion {source} codex/model empty= expression=a=b=c\n",
        encoding="utf-8",
    )

    provider = HostConfig(config_path).load()[0]

    assert provider.arguments == ("codex/model", "empty=", "expression=a=b=c")
    assert provider.inputs == ("codex/model",)
    assert provider.parameters == (("empty", ""), ("expression", "a=b=c"))
