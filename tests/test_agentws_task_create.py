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


def prepare_planner_claim(
    runtime: Path,
    root: Path,
) -> tuple[list[str], dict[str, str]]:
    return (
        [
            str(runtime / "bin" / "job-claim"),
            "-r",
            "planner",
            "--agent-id",
            "planner-1",
        ],
        {
            **os.environ,
            "JOBS_DIR": str(root / "jobs"),
            "TASKS_DIR": str(root / "tasks"),
        },
    )


def run_startup_recovery(
    runtime: Path,
    root: Path,
) -> subprocess.CompletedProcess[str]:
    runtime_lock = os.open(
        runtime,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        fcntl.flock(runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return subprocess.run(
            [str(runtime / "bin" / "job-reset-orphans"), "--all-active"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(runtime_lock,),
            env={
                **os.environ,
                "CYCLO_RUNTIME_LOCK_FD": str(runtime_lock),
                "JOBS_DIR": str(root / "jobs"),
                "TASKS_DIR": str(root / "tasks"),
            },
            check=False,
            timeout=10,
        )
    finally:
        os.close(runtime_lock)


def executable_script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"process exited before {path.name}: {stdout=} {stderr=}"
            )
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def assert_complete_pair(state: Path, task_id: str = "change-1") -> None:
    task = state / "tasks" / task_id
    job = state / "jobs" / f"{task_id}-plan"
    assert (task / "state").read_text(encoding="utf-8").strip() == "open"
    assert (task / "spec.md").is_file()
    assert (task / "log.md").is_file()
    assert (task / ".control.lock").is_file()
    assert not (task / ".creating").exists()
    assert (job / "role").read_text(encoding="utf-8").strip() == "planner"
    assert (job / "task-id").read_text(encoding="utf-8").strip() == task_id
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert (job / "spec.md").stat().st_size > 0
    assert (job / "log.md").is_file()
    assert (job / ".control.lock").is_file()
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
    assert not (state / "tasks" / ".change-1+.task-create-stage").exists()
    assert not (state / "jobs" / ".change-1-plan+.task-create-stage").exists()


def test_sigkill_during_private_task_build_never_publishes_fixed_stage(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "private-task-copy-entered"
    release = tmp_path / "private-task-copy-release"
    real_cp = shutil.which("cp")
    assert real_cp is not None
    executable_script(
        wrappers / "cp",
        "#!/bin/sh\n"
        '"$REAL_CP" "$@"\n'
        ': > "$ENTERED"\n'
        'while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n',
    )
    process = start_task_create(
        runtime,
        state,
        environment={
            "PATH": f"{wrappers}:{os.environ['PATH']}",
            "REAL_CP": real_cp,
            "ENTERED": str(entered),
            "RELEASE": str(release),
        },
    )
    try:
        wait_for(entered, process)
        assert not (state / "tasks" / ".change-1+.task-create-stage").exists()
        assert list((state / "tasks").glob(".task-create-build+*"))
        process.kill()
        process.wait(timeout=5)
    finally:
        release.touch(exist_ok=True)
        process.communicate(timeout=5)

    assert process.returncode == -signal.SIGKILL
    assert not (state / "tasks" / "change-1").exists()
    assert not (state / "jobs" / "change-1-plan").exists()

    recovered = invoke_task_create(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert_complete_pair(state)


def test_sigkill_during_private_planner_build_recovers_task_only_stage(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "private-planner-workspace-entered"
    release = tmp_path / "private-planner-workspace-release"
    real_mkdir = shutil.which("mkdir")
    assert real_mkdir is not None
    executable_script(
        wrappers / "mkdir",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        '"$REAL_MKDIR" "$@"\n'
        'case "$last" in\n'
        '  "$JOBS_ROOT"/.task-create-job-build+*/workspace)\n'
        '    : > "$ENTERED"\n'
        '    while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        "    ;;\n"
        "esac\n",
    )
    process = start_task_create(
        runtime,
        state,
        environment={
            "PATH": f"{wrappers}:{os.environ['PATH']}",
            "REAL_MKDIR": real_mkdir,
            "JOBS_ROOT": str(state / "jobs"),
            "ENTERED": str(entered),
            "RELEASE": str(release),
        },
    )
    try:
        wait_for(entered, process)
        staged_task = state / "tasks" / ".change-1+.task-create-stage"
        assert (staged_task / ".creating").is_file()
        assert (staged_task / "state").read_text(encoding="utf-8").strip() == "open"
        assert not (state / "jobs" / ".change-1-plan+.task-create-stage").exists()
        assert list((state / "jobs").glob(".task-create-job-build+*"))
        process.kill()
        process.wait(timeout=5)
    finally:
        release.touch(exist_ok=True)
        process.communicate(timeout=5)

    assert process.returncode == -signal.SIGKILL

    recovered = invoke_task_create(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert "recovered completed task creation" in recovered.stdout
    assert_complete_pair(state)


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
    assert (state / "jobs" / ".change-1-plan+.task-create-stage").is_dir()

    recovered = invoke_task_create(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert "recovered completed task creation" in recovered.stdout
    assert_complete_pair(state)


def test_pending_planner_waits_for_task_finalization_before_claim(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "task-finalization-entered"
    release = tmp_path / "task-finalization-release"
    marker = state / "tasks" / "change-1" / ".creating"
    real_rm = shutil.which("rm")
    assert real_rm is not None
    executable_script(
        wrappers / "rm",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        'if [ "$last" = "$FINAL_MARKER" ]; then\n'
        '    : > "$ENTERED"\n'
        '    while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        "fi\n"
        'exec "$REAL_RM" "$@"\n',
    )
    creator = start_task_create(
        runtime,
        state,
        environment={
            "PATH": f"{wrappers}:{os.environ['PATH']}",
            "REAL_RM": real_rm,
            "FINAL_MARKER": str(marker),
            "ENTERED": str(entered),
            "RELEASE": str(release),
        },
    )
    claim: subprocess.Popen[str] | None = None
    try:
        wait_for(entered, creator)
        job = state / "jobs" / "change-1-plan"
        assert marker.is_file()
        assert (job / "status").read_text(encoding="utf-8").strip() == "pending"

        claim_command, claim_environment = prepare_planner_claim(runtime, state)
        claim = subprocess.Popen(
            claim_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=claim_environment,
        )
        time.sleep(0.2)
        assert claim.poll() is None
        assert not (job / "lock").exists()
    finally:
        release.touch(exist_ok=True)
    creator_stdout, creator_stderr = creator.communicate(timeout=10)
    assert claim is not None
    claim_stdout, claim_stderr = claim.communicate(timeout=10)

    assert creator.returncode == 0, creator_stderr
    assert "created task" in creator_stdout
    assert claim.returncode == 0, claim_stderr
    assert claim_stdout.strip() == "CLAIMED: change-1-plan"
    assert not marker.exists()


def test_startup_recovers_sigkill_after_planner_job_publication(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    marker = state / "tasks" / "change-1" / ".creating"
    job = state / "jobs" / "change-1-plan"
    real_mv = shutil.which("mv")
    assert real_mv is not None
    executable_script(
        wrappers / "mv",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        '"$REAL_MV" "$@"\n'
        "status=$?\n"
        'if [ "$status" -eq 0 ] && [ "$last" = "$KILL_TARGET" ]; then\n'
        '    kill -KILL "$PPID"\n'
        "fi\n"
        'exit "$status"\n',
    )
    interrupted = invoke_task_create(
        runtime,
        state,
        environment={
            "PATH": f"{wrappers}:{os.environ['PATH']}",
            "REAL_MV": real_mv,
            "KILL_TARGET": str(job),
        },
    )

    assert interrupted.returncode == -signal.SIGKILL
    assert marker.is_file()
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"

    recovered = run_startup_recovery(runtime, state)

    assert recovered.returncode == 0, recovered.stderr
    assert_complete_pair(state)
    assert "Recovered interrupted task creation" in (
        job / "log.md"
    ).read_text(encoding="utf-8")
    claim_command, claim_environment = prepare_planner_claim(runtime, state)
    claimed = subprocess.run(
        claim_command,
        text=True,
        capture_output=True,
        env=claim_environment,
        check=False,
        timeout=10,
    )
    assert claimed.returncode == 0, claimed.stderr
    assert claimed.stdout.strip() == "CLAIMED: change-1-plan"


def test_claim_recovers_only_a_matching_task_creation_transaction(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    state = tmp_path / "state"
    assert invoke_task_create(runtime, state, "matching").returncode == 0
    matching_task = state / "tasks" / "matching"
    matching_job = state / "jobs" / "matching-plan"
    transaction = (matching_job / ".task-create-transaction").read_text(
        encoding="utf-8"
    )
    (matching_task / ".creating").write_text(transaction, encoding="utf-8")

    claim_command, claim_environment = prepare_planner_claim(runtime, state)
    claimed = subprocess.run(
        [claim_command[0], "matching-plan", *claim_command[1:]],
        text=True,
        capture_output=True,
        env=claim_environment,
        check=False,
        timeout=10,
    )

    assert claimed.returncode == 0, claimed.stderr
    assert claimed.stdout.strip() == "CLAIMED: matching-plan"
    assert not (matching_task / ".creating").exists()

    assert invoke_task_create(runtime, state, "mismatch").returncode == 0
    mismatch_task = state / "tasks" / "mismatch"
    mismatch_job = state / "jobs" / "mismatch-plan"
    marker = mismatch_task / ".creating"
    marker.write_text("different-transaction\n", encoding="utf-8")
    refused = subprocess.run(
        [claim_command[0], "mismatch-plan", *claim_command[1:]],
        text=True,
        capture_output=True,
        env=claim_environment,
        check=False,
        timeout=10,
    )

    assert refused.returncode != 0
    assert "does not match" in refused.stderr
    assert marker.read_text(encoding="utf-8") == "different-transaction\n"
    assert (mismatch_job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (mismatch_job / "lock").exists()


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
    staged_job = state / "jobs" / ".change-1-plan+.task-create-stage"
    assert staged_job.is_dir()
    assert (staged_job / "status").read_text(encoding="utf-8").strip() == "pending"
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
    assert "invalid job ID" in claim.stderr
    assert not (staged_job / "lock").exists()

    retry = invoke_task_create(runtime, state)

    assert retry.returncode != 0
    assert "ambiguous" in retry.stderr or "corrupt" in retry.stderr
    assert sentinel.read_text(encoding="utf-8") == "foreign\n"
    assert staged_job.is_dir()
