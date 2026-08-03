from __future__ import annotations

import json
import runpy
import shutil
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cyclo.resources import components_root
from cyclo.team.resources import (
    packaged_agentws_runtime,
    packaged_default_roles,
    packaged_default_team,
)
from cyclo.team import init_team, load_team, verify_agentws_runtime


def test_packaged_agentws_is_the_complete_owned_template() -> None:
    runtime = packaged_agentws_runtime()

    assert runtime == components_root() / "team" / "agentws"
    assert packaged_default_team() == runtime / "default.team"
    assert packaged_default_roles() == runtime / "roles"
    verify_agentws_runtime(runtime)


def test_packaged_agentws_preserves_executable_modes() -> None:
    template = packaged_agentws_runtime()
    executables = [
        *sorted((template / "bin").iterdir()),
        template / "tools" / "agent",
        template / "tools" / "agent-pi-interactive",
        template / "tools" / "agentws",
        template / "tools" / "run_agentws",
    ]

    assert executables
    for path in executables:
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR, path

    for path in (
        template / "AGENTS.md",
        template / "default.team",
        template / "tools" / "agentws-public" / "app.js",
    ):
        assert not path.stat().st_mode & stat.S_IXUSR, path


def test_packaged_agentws_has_no_provider_runtime_policy() -> None:
    tools = packaged_agentws_runtime() / "tools"
    protocol = (packaged_agentws_runtime() / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    for name in ("agent", "agent-pi-interactive", "run_agentws"):
        source = (tools / name).read_text(encoding="utf-8")
        assert "CYCLO_PROVIDER_RUNTIME" not in source
        assert "provider_runtime" not in source
        assert "X-Cyclo-Runtime" not in source

    assert "Read `/agentws/project.cyclo`" in protocol
    assert "PROJECT.md" not in protocol
    assert "CYCLO_PROJECT_MANIFEST" not in protocol


def test_team_image_bakes_the_owned_agentws_runtime() -> None:
    dockerfile = (
        Path(__file__).parents[1]
        / "src"
        / "cyclo"
        / "components"
        / "team"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "COPY --chown=${CYCLO_HOST_UID}:${CYCLO_HOST_GID} team/agentws/ /agentws/" in dockerfile
    assert "COPY team/runtime.py /usr/local/bin/cyclo-team-runtime" in dockerfile


@pytest.mark.parametrize("agent_id", ["..", ".hidden", "-hidden", "_hidden"])
def test_queue_names_must_start_alphanumeric(
    tmp_path: Path, agent_id: str
) -> None:
    runtime = Path(
        shutil.copytree(packaged_agentws_runtime(), tmp_path / "runtime")
    )

    result = subprocess.run(
        [str(runtime / "bin" / "agent-new"), agent_id, "planner"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid agent ID" in result.stderr
    assert not (runtime / "name").exists()


def test_viewer_team_validation_rejects_hidden_agent_names() -> None:
    namespace = runpy.run_path(
        str(packaged_agentws_runtime() / "tools" / "agentws")
    )

    with pytest.raises(ValueError, match="must start with an alphanumeric"):
        namespace["_validate_team_role"](
            ".hidden", "agent name", Path("team.json")
        )


def test_packaged_agentws_initializes_a_team_without_a_checkout(tmp_path: Path) -> None:
    destination = tmp_path / "team"

    init_team(
        destination,
        "openai-codex/test-model",
        initialize_git=False,
    )

    team = load_team(destination)
    assert len(team.agents) == 5
    assert {agent.model for agent in team.agents} == {"openai-codex/test-model"}
    assert not (destination / "AGENTS.md").exists()
    assert (destination / "roles" / "planner.md").is_file()
    assert (destination / "Dockerfile").read_text(encoding="utf-8") == (
        "ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n"
    )


def test_packaged_agentws_viewer_is_observation_only(tmp_path: Path) -> None:
    runtime = packaged_agentws_runtime()
    namespace = runpy.run_path(str(runtime / "tools" / "agentws"))
    queue = tmp_path / "queue"
    for directory in ("tasks", "jobs", "agents"):
        (queue / directory).mkdir(parents=True)

    config = namespace["Config"](
        root=queue,
        host="127.0.0.1",
        port=0,
        pin_root=True,
    )
    namespace["ViewerHandler"].config = config
    server = namespace["bind_server"](config)
    assert server is not None
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{url}/") as response:
            html = response.read()
            assert b'class="brand-name">cyclo<' in html
            assert b"view-chat" not in html
        with urllib.request.urlopen(f"{url}/api/snapshot") as response:
            snapshot = json.loads(response.read())
            assert snapshot["root"] == str(queue)
            assert snapshot["metrics"]["tasksTotal"] == 0
            assert "capabilities" not in snapshot

        request = urllib.request.Request(
            f"{url}/api/snapshot",
            data=b"{}",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request)
        assert rejected.value.code == 405
        assert json.loads(rejected.value.read())["error"] == "AgentWS viewer is observation-only"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
