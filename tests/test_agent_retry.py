from __future__ import annotations

import os
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

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
    assert "Workspace namespace (current working directory): /workspace" in normalized
    assert "Interpret task paths using the Cyclo project manifest" in normalized
    assert "Writable named projects are under `/workspace/<name>`" in normalized
    assert "read-only inputs are under `/readonly/<name>`" in normalized
    assert "Agent workspace:" not in normalized


def test_agent_prompt_always_includes_project_mounts_beside_custom_team_protocol() -> None:
    agent_script = packaged_agentws_template() / "tools" / "agent"
    build_initial_prompt = runpy.run_path(str(agent_script))["build_initial_prompt"]
    manifest = (
        "# Cyclo project\n\n"
        "Name: uart\n"
        "- /workspace/core (read-write)\n"
        "- /readonly/specifications (read-only)\n"
    )

    prompt = build_initial_prompt(
        "designer-1",
        "designer",
        Path("/agentws"),
        Path("/workspace"),
        Path("/team/AGENTS.md"),
        Path("/team/roles/designer.md"),
        "Custom team protocol that does not mention mounts.",
        "Design RTL.",
        project_file=Path("/agentws/PROJECT.md"),
        project_text=manifest,
    )

    assert "Cyclo project manifest: /agentws/PROJECT.md" in prompt
    assert "/workspace/core (read-write)" in prompt
    assert "/readonly/specifications (read-only)" in prompt
    assert "Custom team protocol that does not mention mounts." in prompt
    assert prompt.count(manifest.strip()) == 1


def test_interactive_agent_prompt_includes_configured_project_manifest(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    create_planner_job(runtime, tmp_path, "interactive-project-context")
    manifest = tmp_path / "project-manifest.md"
    manifest.write_text(
        "# Interactive project\n\n- /workspace/core (read-write)\n",
        encoding="utf-8",
    )
    environment = agent_environment(tmp_path, workspace, "#!/bin/sh\nexit 23\n")
    environment["CYCLO_PROJECT_MANIFEST"] = str(manifest)

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

    assert result.returncode == RETRYABLE_AGENT_EXIT, result.stderr
    prompt = (runtime / "agents" / "planner-1" / "prompt.md").read_text(
        encoding="utf-8"
    )
    assert f"Cyclo project manifest: {manifest}" in prompt
    assert "/workspace/core (read-write)" in prompt


def test_python_agent_worker_rejects_hidden_agent_id(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    environment = agent_environment(tmp_path, workspace, "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", ".hidden"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must start with an alphanumeric" in result.stderr
    assert not (runtime / "agents" / ".hidden").exists()


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


def create_role_job(
    runtime: Path,
    tmp_path: Path,
    *,
    task_id: str,
    job_id: str,
    role: str,
) -> Path:
    create_planner_job(runtime, tmp_path, task_id)
    spec = tmp_path / f"{job_id}.md"
    spec.write_text(f"# Work for {role}\n", encoding="utf-8")
    subprocess.run(
        [
            str(runtime / "bin" / "job-create"),
            job_id,
            "-r",
            role,
            "-t",
            task_id,
            str(spec),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return runtime / "jobs" / job_id


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
    assert sorted(path.name for path in (runtime / "jobs").iterdir()) == [
        "retry-plan"
    ]


def test_stored_retry_exhaustion_fails_job_without_launching_engine(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "stored-planner")
    (job / ".agent-attempts").write_text("1\n", encoding="utf-8")
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\n: > \"$FAKE_PI_STARTED\"\nexit 99\n",
        max_attempts=1,
    )

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert not (tmp_path / "pi-started").exists()
    assert sorted(path.name for path in (runtime / "jobs").iterdir()) == [
        "stored-planner-plan"
    ]


def test_terminal_engine_crash_notifies_planner_before_failing_job(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="uart",
        job_id="uart-implementation",
        role="implementer",
    )
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 23\n",
        max_attempts=1,
    )
    jobs_before = sorted(path.name for path in (runtime / "jobs").iterdir())

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "implementer", "impl-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == RETRYABLE_AGENT_EXIT
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    jobs_after = {path.name for path in (runtime / "jobs").iterdir()}
    created = jobs_after - set(jobs_before)
    assert len(created) == 1
    notification = runtime / "jobs" / created.pop()
    assert (notification / "role").read_text(encoding="utf-8").strip() == "planner"
    assert (notification / "task-id").read_text(encoding="utf-8").strip() == "uart"
    spec = (notification / "spec.md").read_text(encoding="utf-8")
    assert "# Notify Planner: uart-implementation" in spec
    assert "## Source Job\nuart-implementation" in spec
    assert "did not finish the source job" in spec
    source_log = (job / "log.md").read_text(encoding="utf-8")
    assert "process exited with status 23" in source_log
    assert "retry safety budget exhausted (1/1)" in source_log


def test_existing_engine_failure_notification_is_reused_after_crash_boundary(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="crash-boundary",
        job_id="crash-boundary-work",
        role="implementer",
    )
    agent_module = runpy.run_path(str(runtime / "tools" / "agent"))
    (job / ".agent-attempts").write_text("7\n", encoding="utf-8")
    subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            job.name,
            "--agent-id",
            "impl-before-crash",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert agent_module["ensure_automatic_failure_notification"](
        runtime,
        job,
        job.name,
    )
    notification_id = agent_module["automatic_failure_notification_id"](
        "crash-boundary",
        job.name,
    )
    notification = runtime / "jobs" / notification_id
    original_spec = (notification / "spec.md").read_text(encoding="utf-8")
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert "source may still be claimed briefly" in original_spec
    subprocess.run(
        [str(runtime / "bin" / "job-reset-orphans")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\n: > \"$FAKE_PI_STARTED\"\nexit 99\n",
        max_attempts=7,
    )
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "implementer", "impl-after-crash"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == RETRYABLE_AGENT_EXIT, result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert not (tmp_path / "pi-started").exists()
    assert {path.name for path in (runtime / "jobs").iterdir()} == jobs_before
    assert (notification / "spec.md").read_text(encoding="utf-8") == original_spec


def test_mismatched_engine_failure_notification_collision_is_container_fatal(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="notice-collision",
        job_id="notice-collision-work",
        role="implementer",
    )
    agent_module = runpy.run_path(str(runtime / "tools" / "agent"))
    notification_id = agent_module["automatic_failure_notification_id"](
        "notice-collision",
        job.name,
    )
    foreign_spec = tmp_path / "foreign-notification.md"
    canonical_spec = agent_module["automatic_failure_notification_spec"](
        "notice-collision",
        job.name,
        "implementer",
    )
    foreign_spec.write_text(
        canonical_spec.replace("bounded", "unbounded", 1),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(runtime / "bin" / "job-create"),
            notification_id,
            "-r",
            "planner",
            "-t",
            "notice-collision",
            str(foreign_spec),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 23\n",
        max_attempts=1,
    )
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "implementer", "impl-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 70
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "lock").is_dir()
    assert "planner notification collision" in result.stderr
    assert {path.name for path in (runtime / "jobs").iterdir()} == jobs_before


def test_notification_metadata_checks_reject_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    runtime, _workspace = copy_runtime(tmp_path)
    agent_module = runpy.run_path(str(runtime / "tools" / "agent"))
    fifo = tmp_path / "metadata-fifo"
    os.mkfifo(fifo)

    assert agent_module["_read_regular_text"](fifo, 1024) is None
    assert agent_module["_is_regular_file"](fifo) is False


def test_planner_notification_creation_failure_leaves_source_claimed(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="notice-create-failure",
        job_id="notice-create-failure-work",
        role="implementer",
    )
    write_executable(runtime / "bin" / "job-create", "#!/bin/sh\nexit 99\n")
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 23\n",
        max_attempts=1,
    )
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "implementer", "impl-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 70
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "lock").is_dir()
    assert "planner notification creation failed" in result.stderr
    assert {path.name for path in (runtime / "jobs").iterdir()} == jobs_before


def test_public_nonplanner_job_fail_remains_terminal_when_engine_exits(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="public-fail",
        job_id="public-fail-work",
        role="implementer",
    )
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
"$FAKE_JOB_FAIL" "$FAKE_JOB_ID" --agent-id impl-1 -m "private direct failure detail"
exit 23
""",
        max_attempts=3,
    )
    environment.update(
        {
            "FAKE_JOB_FAIL": str(runtime / "bin" / "job-fail"),
            "FAKE_JOB_ID": job.name,
        }
    )
    jobs_before = sorted(path.name for path in (runtime / "jobs").iterdir())

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "implementer", "impl-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert "private direct failure detail" in (job / "log.md").read_text(
        encoding="utf-8"
    )
    assert sorted(path.name for path in (runtime / "jobs").iterdir()) == jobs_before


def test_public_planner_job_fail_remains_terminal_when_engine_exits(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "public-planner-fail")
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
"$FAKE_JOB_FAIL" "$FAKE_JOB_ID" --agent-id planner-1 -m "planner direct failure"
exit 23
""",
        max_attempts=3,
    )
    environment.update(
        {
            "FAKE_JOB_FAIL": str(runtime / "bin" / "job-fail"),
            "FAKE_JOB_ID": job.name,
        }
    )
    jobs_before = sorted(path.name for path in (runtime / "jobs").iterdir())

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 0
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert sorted(path.name for path in (runtime / "jobs").iterdir()) == jobs_before


@pytest.mark.parametrize(
    ("role", "agent_name", "task_id", "job_id"),
    [
        (
            "implementer",
            "impl-1",
            "release-fallback",
            "release-fallback-work",
        ),
        (
            "planner",
            "planner-1",
            "planner-release-fallback",
            "planner-release-fallback-plan",
        ),
    ],
)
def test_job_release_failure_fails_owned_job_closed(
    tmp_path: Path,
    role: str,
    agent_name: str,
    task_id: str,
    job_id: str,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    if role == "planner":
        job = create_planner_job(runtime, tmp_path, task_id)
        assert job.name == job_id
    else:
        job = create_role_job(
            runtime,
            tmp_path,
            task_id=task_id,
            job_id=job_id,
            role=role,
        )
    write_executable(runtime / "bin" / "job-release", "#!/bin/sh\nexit 99\n")
    environment = agent_environment(
        tmp_path,
        workspace,
        "#!/bin/sh\nexit 23\n",
        max_attempts=3,
    )
    jobs_before = sorted(path.name for path in (runtime / "jobs").iterdir())

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", role, agent_name],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 70
    assert (job / "status").read_text(encoding="utf-8").strip() == "failed"
    assert sorted(path.name for path in (runtime / "jobs").iterdir()) == jobs_before


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


def test_detached_engine_descendant_is_reaped_before_local_retry(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "descendant")
    child_file = tmp_path / "pi-child"
    environment = agent_environment(
        tmp_path,
        workspace,
        r"""#!/usr/bin/env python3
import os
import subprocess

child = subprocess.Popen(
    ["sleep", "30"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(os.environ["FAKE_PI_CHILD"], "w", encoding="utf-8") as stream:
    stream.write(f"{child.pid}\n")
os._exit(23)
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
    assert result.returncode == RETRYABLE_AGENT_EXIT, result.stderr
    child_pid = int(child_file.read_text(encoding="utf-8").strip())
    wait_for_process_exit(child_pid)
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / "lock").exists()


def test_exited_engine_cli_still_cleans_its_process_group(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "same-group-descendant")
    child_file = tmp_path / "same-group-pi-child"
    environment = agent_environment(
        tmp_path,
        workspace,
        """#!/bin/sh
set -eu
sleep 30 </dev/null >/dev/null 2>&1 &
child=$!
printf '%s\n' "$child" > "$FAKE_PI_CHILD"
exit 23
""",
    )
    environment["FAKE_PI_CHILD"] = str(child_file)

    result = subprocess.run(
        [str(runtime / "tools" / "agent"), "--pi", "planner", "planner-1"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == RETRYABLE_AGENT_EXIT, result.stderr
    child_pid = int(child_file.read_text(encoding="utf-8").strip())
    wait_for_process_exit(child_pid)
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / "lock").exists()


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


def test_fallback_rechecks_ownership_before_failing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _workspace = copy_runtime(tmp_path)
    job = create_role_job(
        runtime,
        tmp_path,
        task_id="fallback-owner-race",
        job_id="fallback-owner-race-work",
        role="implementer",
    )
    subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            job.name,
            "--agent-id",
            "impl-1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    agent_module = runpy.run_path(str(runtime / "tools" / "agent"))

    def lose_ownership(*_args, **_kwargs) -> bool:
        with agent_module["job_control_lock"](job):
            (job / "agent-id").write_text("impl-2\n", encoding="utf-8")
        return False

    function_globals = agent_module["transition_or_fail_closed"].__globals__
    monkeypatch.setitem(function_globals, "run_job_transition", lose_ownership)

    settled, used_fallback = agent_module["transition_or_fail_closed"](
        runtime,
        job,
        job.name,
        "impl-1",
        "job-release",
        "simulated transition failure",
    )

    assert not settled
    assert used_fallback
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "impl-2"
    assert (job / "lock").is_dir()


def test_interactive_reaps_detached_descendant_before_local_retry(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    job = create_planner_job(runtime, tmp_path, "interactive-descendant")
    child_file = tmp_path / "interactive-pi-child"
    environment = agent_environment(
        tmp_path,
        workspace,
        r"""#!/usr/bin/env python3
import os
import subprocess

child = subprocess.Popen(
    ["sleep", "30"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(os.environ["FAKE_PI_CHILD"], "w", encoding="utf-8") as stream:
    stream.write(f"{child.pid}\n")
os._exit(23)
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

    assert result.returncode == RETRYABLE_AGENT_EXIT, result.stderr
    child_pid = int(child_file.read_text(encoding="utf-8").strip())
    wait_for_process_exit(child_pid)
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / "lock").exists()


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


def test_supervisor_validates_the_complete_roster_before_starting_any_agent(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    started = tmp_path / "agent-started"
    write_executable(
        runtime / "tools" / "agent",
        "#!/bin/sh\n: > \"$FAKE_AGENT_STARTED\"\nexec sleep 30\n",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=3
    )
    environment["FAKE_AGENT_STARTED"] = str(started)
    roster = Path(environment["AGENTWS_TEAM_ROSTER"])
    roster.write_text(
        "planner-1 planner pi test/model\n"
        "planner-1 planner pi test/other\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(runtime / "tools" / "run_agentws")],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "duplicate agent name 'planner-1'" in result.stderr
    assert not started.exists()
    assert not (runtime / "agents" / ".team-runs" / "supervisor.ready").exists()


def test_supervisor_refuses_unremovable_stale_suspension_state(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    started = tmp_path / "agent-started"
    write_executable(
        runtime / "tools" / "agent",
        "#!/bin/sh\n: > \"$FAKE_AGENT_STARTED\"\nexec sleep 30\n",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=3
    )
    environment["FAKE_AGENT_STARTED"] = str(started)
    stale = runtime / "agents" / ".team-runs" / "planner-1.suspended"
    stale.mkdir(parents=True)

    result = subprocess.run(
        [str(runtime / "tools" / "run_agentws")],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "cannot clear stale agent suspension markers" in result.stderr
    assert not started.exists()
    assert not (runtime / "agents" / ".team-runs" / "supervisor.ready").exists()


def test_supervisor_refreshes_readiness_and_removes_it_on_shutdown(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    write_executable(runtime / "tools" / "agent", "#!/bin/sh\nexec sleep 30\n")
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=3
    )
    ready = runtime / "agents" / ".team-runs" / "supervisor.ready"
    process = start_supervisor(runtime, environment)
    try:
        wait_for(ready)
        first_mtime = ready.stat().st_mtime_ns
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and ready.stat().st_mtime_ns == first_mtime:
            time.sleep(0.05)
        assert ready.stat().st_mtime_ns > first_mtime
    finally:
        stop_process_group(process)

    assert not ready.exists()


def test_normally_exited_supervisor_stops_sibling_worker_and_engine(
    tmp_path: Path,
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    sibling_worker = tmp_path / "sibling-worker"
    sibling_engine = tmp_path / "sibling-engine"
    exiting_supervisor = tmp_path / "exiting-supervisor"
    fake_bin = tmp_path / "supervisor-bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "sleep",
        """#!/bin/sh
set -eu
if [ -f "$EXITING_SUPERVISOR" ] \
    && [ "$(cat "$EXITING_SUPERVISOR")" = "$PPID" ]; then
    exit 99
fi
exec /bin/sleep "$@"
""",
    )
    write_executable(
        runtime / "tools" / "agent",
        """#!/bin/sh
set -eu
last=""
for argument in "$@"; do last="$argument"; done
if [ "$last" = "planner-1" ]; then
    while [ ! -f "$SIBLING_ENGINE" ]; do sleep 0.02; done
    printf '%s\n' "$PPID" > "$EXITING_SUPERVISOR"
    exit 0
fi
printf '%s\n' "$$" > "$SIBLING_WORKER"
sleep 30 &
engine=$!
printf '%s\n' "$engine" > "$SIBLING_ENGINE"
stop() {
    kill "$engine" 2>/dev/null || true
    wait "$engine" 2>/dev/null || true
    exit 0
}
trap stop INT TERM
wait "$engine"
""",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=3
    )
    roles = Path(environment["AGENTWS_TEAM_ROLES_DIR"])
    (roles / "worker.md").write_text("Work.\n", encoding="utf-8")
    Path(environment["AGENTWS_TEAM_ROSTER"]).write_text(
        "planner-1 planner pi test/model\n"
        "worker-1 worker pi test/model\n",
        encoding="utf-8",
    )
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment.get('PATH', '')}",
            "SIBLING_WORKER": str(sibling_worker),
            "SIBLING_ENGINE": str(sibling_engine),
            "EXITING_SUPERVISOR": str(exiting_supervisor),
        }
    )

    process = start_supervisor(runtime, environment)
    stdout, stderr = process.communicate(timeout=8)

    assert process.returncode != 0
    assert "agent supervisor planner-1 exited with status 99" in stderr
    assert "started worker-1" in stdout
    worker_pid = int(sibling_worker.read_text(encoding="utf-8").strip())
    engine_pid = int(sibling_engine.read_text(encoding="utf-8").strip())
    wait_for_process_exit(worker_pid)
    wait_for_process_exit(engine_pid)
    assert not (runtime / "agents" / ".team-runs" / "supervisor.ready").exists()


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
exit 75
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


@pytest.mark.parametrize("status", [1, 70, 137])
def test_supervisor_propagates_unclean_agent_status(
    tmp_path: Path, status: int
) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    write_executable(
        runtime / "tools" / "agent",
        f"#!/bin/sh\nprintf '1\\n' > \"$FAKE_AGENT_COUNT\"\nexit {status}\n",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=5
    )
    process = start_supervisor(runtime, environment)
    _stdout, stderr = process.communicate(timeout=8)

    assert process.returncode != 0
    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "1"
    assert f"without a clean settlement (status {status})" in stderr
    assert "restarting the Cyclo container" in stderr
    assert not (
        runtime / "agents" / ".team-runs" / "planner-1.suspended"
    ).exists()


def test_sigkill_worker_exits_team_without_local_replacement(tmp_path: Path) -> None:
    runtime, workspace = copy_runtime(tmp_path)
    worker_pid = tmp_path / "worker-pid"
    write_executable(
        runtime / "tools" / "agent",
        """#!/bin/sh
set -eu
count=0
[ ! -f "$FAKE_AGENT_COUNT" ] || count=$(cat "$FAKE_AGENT_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_AGENT_COUNT"
printf '%s\n' "$$" > "$FAKE_WORKER_PID"
exec sleep 30
""",
    )
    environment = supervisor_environment(
        tmp_path, runtime, workspace, max_failures=5
    )
    environment["FAKE_WORKER_PID"] = str(worker_pid)
    process = start_supervisor(runtime, environment)
    try:
        wait_for(worker_pid)
        os.kill(int(worker_pid.read_text(encoding="utf-8").strip()), signal.SIGKILL)
        _stdout, stderr = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            stop_process_group(process)

    assert process.returncode != 0
    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "1"
    assert "without a clean settlement" in stderr
    assert "restarting the Cyclo container" in stderr


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
    1|3) exit 75 ;;
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
    stale_suspension = (
        runtime / "agents" / ".team-runs" / "removed-agent.suspended"
    )
    stale_suspension.parent.mkdir(parents=True)
    stale_suspension.write_text("last_status=70\n", encoding="utf-8")
    suspended = runtime / "agents" / ".team-runs" / "planner-1.suspended"
    process = start_supervisor(runtime, environment)
    try:
        wait_for(tmp_path / "agent-fifth")
        assert not stale_suspension.exists()
        assert not suspended.exists()
    finally:
        stop_process_group(process)

    assert (tmp_path / "agent-count").read_text(encoding="utf-8").strip() == "5"
