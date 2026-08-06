from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.images import Images


def test_inspect_parses_one_immutable_image(monkeypatch: pytest.MonkeyPatch) -> None:
    document = [
        {
            "Id": "sha256:" + "a" * 64,
            "Config": {"Healthcheck": {"Test": ["CMD", "true"]}},
        }
    ]
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(document), ""
        ),
    )

    image = Images(endpoint="unix:///run/docker.sock").inspect("example:1")

    assert image is not None
    assert image.id == "sha256:" + "a" * 64
    assert image.has_healthcheck


def test_missing_image_is_not_an_error_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "missing"),
    )

    assert Images().inspect("missing:1", missing_ok=True) is None


def test_healthcheck_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    document = [{"Id": "sha256:" + "a" * 64, "Config": {}}]
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(document), ""
        ),
    )

    with pytest.raises(CycloError, match="HEALTHCHECK"):
        Images._validate(
            Images().inspect("example:1"),  # type: ignore[arg-type]
            require_healthcheck=True,
        )


def test_bound_endpoint_overrides_ambient_docker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_CONTEXT", "wrong")

    environment = Images(endpoint="unix:///run/docker.sock").environment()

    assert environment["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert "DOCKER_CONTEXT" not in environment


def test_command_bounds_failure_diagnostic_and_keeps_its_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "discarded-start\n" + "x" * 20_000 + "\nuseful-end"
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", output
        ),
    )

    with pytest.raises(CycloError) as captured:
        Images().command(["build", "."])

    message = str(captured.value)
    assert len(message) < 17_000
    assert "earlier Docker output omitted" in message
    assert "discarded-start" not in message
    assert message.endswith("useful-end")
