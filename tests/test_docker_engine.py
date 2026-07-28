from __future__ import annotations

import json
import subprocess

import pytest

from cyclo.docker_engine import (
    DockerContainerState,
    DockerEngine,
    VerifiedContainer,
)
from cyclo.errors import CycloError


CONTAINER_ID = "a" * 64
OTHER_ID = "b" * 64
NAME = "cyclo-test-component"
LABELS = {"io.cyclo.test": "1"}


def _info(
    *,
    identifier: str = CONTAINER_ID,
    name: str = NAME,
    state: str = "created",
) -> dict[str, object]:
    return {
        "Id": identifier,
        "Name": f"/{name}",
        "Config": {"Labels": LABELS},
        "State": {
            "Running": state == "running",
            "Status": state,
        },
    }


def _verify(info: object) -> None:
    assert isinstance(info, dict)
    assert info["Name"] == f"/{NAME}"
    assert info["Config"] == {"Labels": LABELS}


def test_create_inspects_before_start_and_starts_the_exact_id() -> None:
    class Engine(DockerEngine):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def call(self, arguments, **_options):
            command = list(arguments)
            self.commands.append(command)
            if command[0] == "create":
                return subprocess.CompletedProcess(
                    command, 0, f"{CONTAINER_ID}\n", ""
                )
            if command[:2] == ["container", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps([_info()]), ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

    engine = Engine()
    container, _created = engine.create_container(
        NAME,
        ["--label", "io.cyclo.test=1", "example:latest"],
        verify=_verify,
    )
    engine.start_container(container)

    assert engine.commands == [
        [
            "create",
            "--name",
            NAME,
            "--label",
            "io.cyclo.test=1",
            "example:latest",
        ],
        ["container", "inspect", "--", CONTAINER_ID],
        ["start", CONTAINER_ID],
    ]


def test_create_refuses_an_exact_id_inspection_returning_another_id() -> None:
    class Engine(DockerEngine):
        def call(self, arguments, **_options):
            command = list(arguments)
            if command[0] == "create":
                return subprocess.CompletedProcess(
                    command, 0, f"{CONTAINER_ID}\n", ""
                )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([_info(identifier=OTHER_ID)]),
                "",
            )

    with pytest.raises(CycloError, match="different container"):
        Engine().create_container(NAME, ["example:latest"], verify=_verify)


def test_remove_reinspects_and_mutates_only_the_verified_id() -> None:
    class Engine(DockerEngine):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def call(self, arguments, **_options):
            command = list(arguments)
            self.commands.append(command)
            if command[:2] == ["container", "inspect"]:
                assert command[-1] == CONTAINER_ID
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps([_info(state="running")]),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

    engine = Engine()
    selected = VerifiedContainer(
        CONTAINER_ID,
        DockerContainerState.STOPPED,
        _info(),
    )

    assert engine.remove_container(
        selected,
        verify=_verify,
        timeout=10,
        remove_volumes=True,
    )
    assert engine.commands == [
        ["container", "inspect", "--", CONTAINER_ID],
        ["stop", "--timeout", "10", CONTAINER_ID],
        ["rm", "--volumes", CONTAINER_ID],
    ]
