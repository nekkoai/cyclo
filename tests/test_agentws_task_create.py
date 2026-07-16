from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from cyclo.agentws_bundle import packaged_agentws_template


def runtime_copy(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(
            packaged_agentws_template(),
            tmp_path / "runtime",
            copy_function=shutil.copy2,
        )
    )


def prepare_task_create(
    runtime: Path,
    root: Path,
    task_id: str = "change-1",
    *,
    environment: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    jobs = root / "jobs"
    tasks = root / "tasks"
    jobs.mkdir(parents=True, exist_ok=True)
    tasks.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    spec.write_text("# Objective\n\nMake the bounded change.\n", encoding="utf-8")
    process_environment = os.environ.copy()
    process_environment.update({"JOBS_DIR": str(jobs), "TASKS_DIR": str(tasks)})
    process_environment.update(environment or {})
    return (
        [str(runtime / "bin" / "task-create"), task_id, str(spec)],
        process_environment,
    )


def invoke_task_create(
    runtime: Path,
    root: Path,
    task_id: str = "change-1",
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command, process_environment = prepare_task_create(
        runtime,
        root,
        task_id,
        environment=environment,
    )
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=process_environment,
        check=False,
        timeout=10,
    )


def start_task_create(
    runtime: Path,
    root: Path,
    task_id: str = "change-1",
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    command, process_environment = prepare_task_create(
        runtime,
        root,
        task_id,
        environment=environment,
    )
    return subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=process_environment,
    )


def executable_script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def assert_complete_pair(state: Path, task_id: str = "change-1") -> None:
    task = state / "tasks" / task_id
    job = state / "jobs" / f"{task_id}-plan"
    assert (task / "state").read_text(encoding="utf-8").strip() == "open"
    assert (task / "spec.md").is_file()
    assert (task / "log.md").is_file()
    assert not (task / ".creating").exists()
    assert (job / "role").read_text(encoding="utf-8").strip() == "planner"
    assert (job / "task-id").read_text(encoding="utf-8").strip() == task_id
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert (job / "spec.md").stat().st_size > 0
    assert (job / "log.md").is_file()
    assert (job / "workspace").is_dir()
    assert (job / ".task-create-transaction").stat().st_size > 0


def test_task_create_publishes_complete_task_and_planner_job(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"

    result = invoke_task_create(runtime, state)

    assert result.returncode == 0, result.stderr
    assert_complete_pair(state)
    assert (state / "tasks" / ".task-create.lock").is_file()
    assert not list((state / "tasks").glob(".*.task-create-stage"))
    assert not list((state / "jobs").glob(".*.task-create-stage"))


def test_preexisting_planner_job_collision_is_preserved(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    foreign_job = state / "jobs" / "change-1-plan"
    foreign_job.mkdir(parents=True)
    sentinel = foreign_job / "foreign"
    sentinel.write_text("keep\n", encoding="utf-8")

    result = invoke_task_create(runtime, state)

    assert result.returncode != 0
    assert "collides" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (state / "tasks" / "change-1").exists()


def test_active_global_flock_serializes_creation(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    command, environment = prepare_task_create(runtime, state)
    lock_path = state / "tasks" / ".task-create.lock"

    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        time.sleep(0.2)
        assert process.poll() is None
        assert not (state / "tasks" / "change-1").exists()
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert "created task" in stdout
    assert_complete_pair(state)


def test_two_actual_concurrent_creators_publish_exactly_one_pair(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    command, environment = prepare_task_create(runtime, state)
    processes = [
        subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for _ in range(2)
    ]

    results = [process.communicate(timeout=10) for process in processes]

    assert sorted(process.returncode for process in processes) == [0, 1]
    failure = next(
        stderr
        for process, (_stdout, stderr) in zip(processes, results)
        if process.returncode != 0
    )
    assert "collides" in failure
    assert_complete_pair(state)


def test_staging_failure_removes_only_owned_hidden_stages(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    executable_script(wrappers / "cp", "#!/bin/sh\nexit 42\n")
    environment = {"PATH": f"{wrappers}:{os.environ['PATH']}"}

    result = invoke_task_create(runtime, state, environment=environment)

    assert result.returncode == 42
    assert not (state / "tasks" / "change-1").exists()
    assert not (state / "jobs" / "change-1-plan").exists()
    assert not (state / "tasks" / ".change-1.task-create-stage").exists()
    assert not (state / "jobs" / ".change-1-plan.task-create-stage").exists()


def test_ambiguous_partial_creation_is_refused_without_deletion(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    task = state / "tasks" / "change-1"
    task.mkdir(parents=True)
    marker = task / ".creating"
    marker.write_text("unknown-transaction\n", encoding="utf-8")
    partial = task / "partial"
    partial.write_text("keep task data\n", encoding="utf-8")
    partial_job = state / "jobs" / "change-1-plan"
    partial_job.mkdir(parents=True)
    foreign = partial_job / "foreign"
    foreign.write_text("keep job data\n", encoding="utf-8")

    result = invoke_task_create(runtime, state)

    assert result.returncode != 0
    assert "corrupt" in result.stderr or "ambiguous" in result.stderr
    assert marker.read_text(encoding="utf-8") == "unknown-transaction\n"
    assert partial.read_text(encoding="utf-8") == "keep task data\n"
    assert foreign.read_text(encoding="utf-8") == "keep job data\n"


def test_sigterm_at_task_publish_boundary_is_masked_until_pair_is_complete(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    real_mv = shutil.which("mv")
    assert real_mv is not None
    executable_script(
        wrappers / "mv",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        '"$REAL_MV" "$@"\n'
        "status=$?\n"
        'if [ "$status" -eq 0 ] && [ "$last" = "$SIGNAL_TASK_TARGET" ]; then\n'
        '    kill -TERM "$PPID"\n'
        "fi\n"
        'exit "$status"\n',
    )
    environment = {
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_MV": real_mv,
        "SIGNAL_TASK_TARGET": str(state / "tasks" / "change-1"),
    }

    result = invoke_task_create(runtime, state, environment=environment)

    assert result.returncode == 0, result.stderr
    assert_complete_pair(state)


def test_sigkill_between_task_and_job_publication_recovers_staged_pair(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    real_mv = shutil.which("mv")
    assert real_mv is not None
    executable_script(
        wrappers / "mv",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        '"$REAL_MV" "$@"\n'
        "status=$?\n"
        'if [ "$status" -eq 0 ] && [ "$last" = "$KILL_TASK_TARGET" ]; then\n'
        '    kill -KILL "$PPID"\n'
        "fi\n"
        'exit "$status"\n',
    )
    environment = {
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_MV": real_mv,
        "KILL_TASK_TARGET": str(state / "tasks" / "change-1"),
    }

    interrupted = invoke_task_create(runtime, state, environment=environment)

    assert interrupted.returncode == -signal.SIGKILL
    assert (state / "tasks" / "change-1" / ".creating").is_file()
    assert not (state / "jobs" / "change-1-plan").exists()
    assert (state / "jobs" / ".change-1-plan.task-create-stage").is_dir()

    recovered = invoke_task_create(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert "recovered completed task creation" in recovered.stdout
    assert_complete_pair(state)


def test_sigkill_after_pair_publication_recovers_only_complete_pair(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    real_rm = shutil.which("rm")
    assert real_rm is not None
    executable_script(
        wrappers / "rm",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        'if [ "$last" = "$KILL_MARKER" ]; then\n'
        '    kill -KILL "$PPID"\n'
        "    exit 99\n"
        "fi\n"
        'exec "$REAL_RM" "$@"\n',
    )
    environment = {
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_RM": real_rm,
        "KILL_MARKER": str(state / "tasks" / "change-1" / ".creating"),
    }

    interrupted = invoke_task_create(runtime, state, environment=environment)

    assert interrupted.returncode == -signal.SIGKILL
    task_marker = state / "tasks" / "change-1" / ".creating"
    assert task_marker.stat().st_size > 0
    assert (state / "jobs" / "change-1-plan" / ".task-create-transaction").is_file()

    recovered = invoke_task_create(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert "recovered completed task creation" in recovered.stdout
    assert_complete_pair(state)


def test_foreign_job_collision_at_commit_boundary_is_never_deleted(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    real_mv = shutil.which("mv")
    assert real_mv is not None
    foreign_job = state / "jobs" / "change-1-plan"
    sentinel = foreign_job / "foreign"
    executable_script(
        wrappers / "mv",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        '"$REAL_MV" "$@"\n'
        "status=$?\n"
        'if [ "$status" -eq 0 ] && [ "$last" = "$TASK_TARGET" ]; then\n'
        '    mkdir -p "$FOREIGN_JOB_TARGET"\n'
        '    printf "%s\\n" foreign > "$FOREIGN_JOB_TARGET/foreign"\n'
        "fi\n"
        'exit "$status"\n',
    )
    environment = {
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_MV": real_mv,
        "TASK_TARGET": str(state / "tasks" / "change-1"),
        "FOREIGN_JOB_TARGET": str(foreign_job),
    }

    result = invoke_task_create(runtime, state, environment=environment)

    assert result.returncode != 0
    assert "publish initial planner job" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "foreign\n"
    assert (state / "tasks" / "change-1" / ".creating").is_file()
    staged_job = state / "jobs" / ".change-1-plan.task-create-stage"
    assert staged_job.is_dir()
    assert not (staged_job / "status").exists()
    claim = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            staged_job.name,
            "--agent-id",
            "intruder",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "JOBS_DIR": str(state / "jobs"),
            "TASKS_DIR": str(state / "tasks"),
        },
        check=False,
    )
    assert claim.returncode != 0
    assert not (staged_job / "lock").exists()

    retry = invoke_task_create(runtime, state)

    assert retry.returncode != 0
    assert "ambiguous" in retry.stderr or "corrupt" in retry.stderr
    assert sentinel.read_text(encoding="utf-8") == "foreign\n"
    assert staged_job.is_dir()
