from __future__ import annotations

import os
import runpy
import shutil
import signal
import subprocess
import time
from pathlib import Path

from cyclo.agentws_bundle import packaged_agentws_template


RETRYABLE_AGENT_EXIT = 75


def test_agent_prompt_treats_workspace_as_an_internal_project_root() -> None:
    agent_script = packaged_agentws_template() / "tools" / "agent"
    build_initial_prompt = runpy.run_path(str(agent_script))["build_initial_prompt"]

    prompt = build_initial_prompt(
        "builder-1",
        "builder",
        Path("/agentws"),
        Path("/workspace"),
        Path("/team/AGENTS.md"),
        Path("/team/roles/builder.md"),
        "Protocol.",
        "Role.",
    )

    normalized = " ".join(prompt.split())
    assert "Project root (current working directory): /workspace" in normalized
    assert "Interpret relative paths in user tasks from" in normalized
    assert "container mount name is an internal runtime detail" in normalized
    assert "do not require the task author to name it" in normalized
    assert "Agent workspace:" not in normalized


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def copy_runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "agentws"
    shutil.copytree(packaged_agentws_template(), runtime)
    subprocess.run([str(runtime / "bin" / "job-init")], check=True, capture_output=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return runtime, workspace


def create_planner_job(runtime: Path, tmp_path: Path, task_id: str = "retry") -> Path:
    spec = tmp_path / f"{task_id}.md"
    spec.write_text("# Retry safety test\n", encoding="utf-8")
    subprocess.run(
        [str(runtime / "bin" / "task-create"), task_id, str(spec)],
        check=True,
        capture_output=True,
        text=True,
    )
    return runtime / "jobs" / f"{task_id}-plan"


def agent_environment(
    tmp_path: Path,
    workspace: Path,
    fake_pi: str,
    *,
    max_attempts: int = 2,
) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    write_executable(fake_bin / "pi", fake_pi)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment.get('PATH', '')}",
            "AGENTWS_WORKSPACE": str(workspace),
            "AGENTWS_MAX_JOB_ATTEMPTS": str(max_attempts),
            "FAKE_PI_COUNT": str(tmp_path / "pi-count"),
            "FAKE_PI_STARTED": str(tmp_path / "pi-started"),
        }
    )
    return environment


def wait_for(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def wait_for_process_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    stat_path = Path(f"/proc/{pid}/stat")
    while time.monotonic() < deadline:
        try:
            fields = stat_path.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return
        if len(fields) > 2 and fields[2] == "Z":
            return
        time.sleep(0.02)
    raise AssertionError(f"engine descendant {pid} is still alive")


def stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        return process.communicate(timeout=5)


def test_failed_agent_requeues_only_until_job_attempt_budget(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path)
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
count=0
[ ! -f "$FAKE_PI_COUNT" ] || count=$(cat "$FAKE_PI_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_PI_COUNT"
exit 23
""",
    )
    command = [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"]

    first = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert first.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert (job / ".agent-attempts").read_text(encoding="utf-8").strip() == "1"
    assert "released for bounded retry (1/2)" in (job / "log.md").read_text(
        encoding="utf-8"
    )

    second = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert second.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert (job / ".agent-attempts").read_text(encoding="utf-8").strip() == "2"
    assert "retry safety budget exhausted (2/2)" in (job / "log.md").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "pi-count").read_text(encoding="utf-8").strip() == "2"


def test_signal_shutdown_releases_job_without_consuming_attempt(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "interrupted")
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
: > "$FAKE_PI_STARTED"
exec sleep 30
""",
    )
    process = subprocess.Popen(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_for(tmp_path / "pi-started")
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            stop_process_group(process)

    assert process.returncode in (1, 128 + signal.SIGTERM)
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / ".agent-attempts").exists()
    assert "without consuming a retry attempt" in (job / "log.md").read_text(
        encoding="utf-8"
    )


def test_crashed_engine_cannot_hold_output_pipe_or_leave_descendant(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "descendant")
    child_file = tmp_path / "pi-child"
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
sleep 30 &
child=$!
printf '%s\n' "$child" > "$FAKE_PI_CHILD"
exit 23
""",
    )
    environment["FAKE_PI_CHILD"] = str(child_file)

    started = time.monotonic()
    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert time.monotonic() - started < 7
    assert result.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    wait_for_process_exit(int(child_file.read_text(encoding="utf-8").strip()))


def test_prelaunch_failure_still_settles_claimed_job(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "prelaunch")
    agent_dir = runtime / "agents" / "planner-1"
    agent_dir.mkdir()
    (agent_dir / "role").write_text("planner\n", encoding="utf-8")
    (agent_dir / "prompt.md").mkdir()
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 0\n",
    )

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / "lock").exists()
    assert (job / ".agent-attempts").read_text(encoding="utf-8").strip() == "1"


def test_transition_failure_fails_closed_and_returns_fatal_status(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "transition")
    write_executable(runtime / "bin" / "job-release", "#!/bin/sh\nexit 99\n")
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 23\n",
    )

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 70
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert not (job / "lock").exists()
    assert "Failed by agent safety fallback" in (job / "log.md").read_text(
        encoding="utf-8"
    )


def test_interactive_agent_cleans_descendants_and_settles_job(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "interactive-descendant")
    child_file = tmp_path / "interactive-pi-child"
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
sleep 30 &
child=$!
printf '%s\n' "$child" > "$FAKE_PI_CHILD"
exit 23
""",
    )
    environment["FAKE_PI_CHILD"] = str(child_file)

    result = subprocess.run(
        [
            str(runtime / "tools" / "agent-pi-interactive"),
            "--headless",
            "planner",
            "planner-1",
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    wait_for_process_exit(int(child_file.read_text(encoding="utf-8").strip()))


def supervisor_environment(
    tmp_path: Path,
    runtime: Path,
    workspace: Path,
    *,
    max_failures: int,
    initial_delay: int = 1,
    maximum_delay: int = 2,
) -> dict[str, str]:
    team_root = tmp_path / "team"
    roles = team_root / "roles"
    roles.mkdir(parents=True)
    (roles / "planner.md").write_text("Plan.\n", encoding="utf-8")
    protocol = team_root / "AGENTS.md"
    protocol.write_text("Use the queue.\n", encoding="utf-8")
    roster = team_root / "team"
    roster.write_text("planner-1 planner pi test/model\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTWS_TEAM_ROOT": str(team_root),
            "AGENTWS_TEAM_ROSTER": str(roster),
            "AGENTWS_TEAM_PROTOCOL": str(protocol),
            "AGENTWS_TEAM_ROLES_DIR": str(roles),
            "AGENTWS_WORKSPACE": str(workspace),
            "AGENTWS_MAX_JOB_ATTEMPTS": "2",
            "AGENTWS_MAX_CONSECUTIVE_FAILURES": str(max_failures),
            "AGENTWS_RETRY_INITIAL_SECONDS": str(initial_delay),
            "AGENTWS_RETRY_MAX_SECONDS": str(maximum_delay),
            "FAKE_AGENT_COUNT": str(tmp_path / "agent-count"),
            "FAKE_AGENT_FIFTH": str(tmp_path / "agent-fifth"),
        }
    )
    return environment


def start_supervisor(
    runtime: Path, environment: dict[str, str]
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(runtime / "tools" / "run_agentws")],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_supervisor_uses_capped_backoff_then_suspends(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    write_executable(
        runtime / "tools" / "agent",
        """#!/bin/sh
set -eu
count=0
[ ! -f "$FAKE_AGENT_COUNT" ] || count=$(cat "$FAKE_AGENT_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_AGENT_COUNT"
exit 9
""",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=3
    )
    suspended = runtime / "agents" / ".team-runs" / "planner-1.suspended"
    process = start_supervisor(runtime, environment)
    try:
        wait_for(suspended)
        stdout, stderr = stop_process_group(process)
    finally:
        if process.poll() is None:
            stop_process_group(process)

    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "3"
    assert "retry 1/3 in 1s" in stderr
    assert "retry 2/3 in 2s" in stderr
    assert "suspended after 3 consecutive failures" in stderr
    assert "started planner-1" in stdout


def test_supervisor_suspends_immediately_on_fatal_agent_status(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    write_executable(
        runtime / "tools" / "agent",
        "#!/bin/sh\nprintf '1\\n' > \"$FAKE_AGENT_COUNT\"\nexit 70\n",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=5
    )
    suspended = runtime / "agents" / ".team-runs" / "planner-1.suspended"
    process = start_supervisor(runtime, environment)
    try:
        wait_for(suspended)
        _stdout, stderr = stop_process_group(process)
    finally:
        if process.poll() is None:
            stop_process_group(process)

    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "1"
    assert "suspended immediately after fatal safety status 70" in stderr


def test_successful_agent_exit_resets_consecutive_failure_count(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    write_executable(
        runtime / "tools" / "agent",
        """#!/bin/sh
set -eu
count=0
[ ! -f "$FAKE_AGENT_COUNT" ] || count=$(cat "$FAKE_AGENT_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_AGENT_COUNT"
case "$count" in
    1|3) exit 9 ;;
    2|4) exit 0 ;;
    *) : > "$FAKE_AGENT_FIFTH"; exec sleep 30 ;;
esac
""",
    )
    environment = supervisor_environment(
        tmp_path,
        runtime,
        workspace,
        max_failures=2,
        maximum_delay=1,
    )
    suspended = runtime / "agents" / ".team-runs" / "planner-1.suspended"
    process = start_supervisor(runtime, environment)
    try:
        wait_for(tmp_path / "agent-fifth")
        assert not suspended.exists()
    finally:
        stop_process_group(process)

    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "5"
