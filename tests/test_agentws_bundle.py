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

from cyclo.agentws_bundle import (
    packaged_agentws_root,
    packaged_agentws_template,
    packaged_default_roles,
    packaged_default_team,
)
from cyclo.state import StateStore
from cyclo.team import init_team, load_team, verify_agentws_abi


def test_packaged_agentws_is_the_complete_owned_template() -> None:
    root = packaged_agentws_root()
    template = packaged_agentws_template()

    assert template == root / "template"
    assert packaged_default_team() == template / "default.team"
    assert packaged_default_roles() == template / "roles"
    verify_agentws_abi(root)


def test_packaged_agentws_preserves_executable_modes() -> None:
    template = packaged_agentws_template()
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
    tools = packaged_agentws_template() / "tools"

    for name in ("agent", "agent-pi-interactive", "run_agentws"):
        source = (tools / name).read_text(encoding="utf-8")
        assert "CYCLO_PROVIDER_RUNTIME" not in source
        assert "provider_runtime" not in source
        assert "X-Cyclo-Runtime" not in source


def test_packaged_agentws_materializes_as_an_executable_runtime(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    runtime_script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"

    runtime = store.materialize_agentws(
        "standalone",
        packaged_agentws_template(),
        runtime_script,
    )

    assert (runtime / "tools" / "run_agentws").stat().st_mode & stat.S_IXUSR
    assert (runtime / "bin" / "task-create").stat().st_mode & stat.S_IXUSR
    assert (runtime / "tools" / "agentws-public" / "index.html").is_file()


@pytest.mark.parametrize("agent_id", ["..", ".hidden", "-hidden", "_hidden"])
def test_queue_names_must_start_alphanumeric(
    tmp_path: Path, agent_id: str
) -> None:
    runtime = Path(
        shutil.copytree(packaged_agentws_template(), tmp_path / "runtime")
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
        str(packaged_agentws_template() / "tools" / "agentws")
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


def test_packaged_agentws_viewer_is_observation_only(tmp_path: Path) -> None:
    runtime = packaged_agentws_template()
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
