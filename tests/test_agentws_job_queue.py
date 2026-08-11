from __future__ import annotations

import fcntl
import os
import re
import runpy
import shutil
import signal
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cyclo.team.resources import packaged_agentws_runtime


def runtime_copy(tmp_path: Path) -> Path:
    runtime = Path(
        shutil.copytree(
            packaged_agentws_runtime(),
            tmp_path / "runtime",
            copy_function=shutil.copy2,
        )
    )
    subprocess.run(
        [str(runtime / "bin" / "job-init")], check=True, capture_output=True
    )
    return runtime


@contextmanager
def startup_runtime_lock(runtime: Path) -> Iterator[int]:
    lock = os.open(runtime, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock
    finally:
        os.close(lock)


def run_startup_recovery(runtime: Path) -> subprocess.CompletedProcess[str]:
    with startup_runtime_lock(runtime) as lock:
        return subprocess.run(
            [str(runtime / "bin" / "job-reset-orphans"), "--all-active"],
            text=True,
            capture_output=True,
            check=False,
            pass_fds=(lock,),
            env={**os.environ, "CYCLO_RUNTIME_LOCK_FD": str(lock)},
        )


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


def make_job(
    runtime: Path,
    job_id: str,
    status: str,
    *,
    owner: str | None = None,
) -> Path:
    job = runtime / "jobs" / job_id
    job.mkdir()
    (job / "workspace").mkdir()
    (job / "spec.md").write_text("# Test job\n", encoding="utf-8")
    (job / "role").write_text("worker\n", encoding="utf-8")
    (job / "task-id").write_text("test-task\n", encoding="utf-8")
    (job / "log.md").touch()
    (job / "status").write_text(f"{status}\n", encoding="utf-8")
    (job / ".control.lock").touch()
    (job / "lock").mkdir()
    if owner is not None:
        (job / "agent-id").write_text(f"{owner}\n", encoding="utf-8")
    old = time.time() - 10 * 60
    os.utime(job / "lock", (old, old))
    return job


def make_task(runtime: Path, root: Path, task_id: str = "shared-task") -> Path:
    spec = root / f"{task_id}-spec.md"
    spec.write_text(f"# Task {task_id}\n", encoding="utf-8")
    result = subprocess.run(
        [str(runtime / "bin" / "task-create"), task_id, str(spec)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return runtime / "tasks" / task_id


def make_running_job(
    runtime: Path,
    root: Path,
    *,
    task_id: str,
    job_id: str,
    role: str,
    agent_id: str,
) -> Path:
    if not (runtime / "tasks" / task_id).is_dir():
        make_task(runtime, root, task_id)
    spec = root / f"{job_id}-spec.md"
    spec.write_text(f"# Work for {role}\n", encoding="utf-8")
    created = subprocess.run(
        [
            str(runtime / "bin" / "job-create"),
            job_id,
            "-r",
            role,
            "-t",
            task_id,
            str(spec),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    claimed = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            job_id,
            "-r",
            role,
            "--agent-id",
            agent_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert claimed.returncode == 0, claimed.stderr
    started = subprocess.run(
        [
            str(runtime / "bin" / "job-start"),
            job_id,
            "--agent-id",
            agent_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    return runtime / "jobs" / job_id


def complete_job(
    runtime: Path,
    job_id: str,
    agent_id: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(runtime / "bin" / "job-done"),
            job_id,
            "--agent-id",
            agent_id,
            "-m",
            "test completion",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_task_and_job_ids_must_start_alphanumeric(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("# Hidden ID rejection\n", encoding="utf-8")
    (runtime / "tasks" / "parent").mkdir()

    for command in (
        [str(runtime / "bin" / "task-create"), ".hidden", str(spec)],
        [
            str(runtime / "bin" / "job-create"),
            ".hidden",
            "-r",
            "worker",
            "-t",
            "parent",
            str(spec),
        ],
    ):
        result = subprocess.run(
            command, text=True, capture_output=True, check=False
        )
        assert result.returncode != 0
        assert "must start with an alphanumeric" in result.stderr

    assert not (runtime / "tasks" / ".hidden").exists()
    assert not (runtime / "jobs" / ".hidden").exists()


def test_nonplanner_job_done_publishes_planner_notice_before_terminal_state(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    source = make_running_job(
        runtime,
        tmp_path,
        task_id="handoff",
        job_id="handoff-implementation",
        role="implementer",
        agent_id="implementer-1",
    )
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}

    result = complete_job(runtime, source.name, "implementer-1")

    assert result.returncode == 0, result.stderr
    assert (source / "status").read_text(encoding="utf-8").strip() == "done"
    assert not (source / "lock").exists()
    created = {
        path.name for path in (runtime / "jobs").iterdir()
    } - jobs_before
    assert len(created) == 1
    notification = runtime / "jobs" / created.pop()
    assert (notification / "role").read_text(encoding="utf-8").strip() == "planner"
    assert (notification / "task-id").read_text(encoding="utf-8").strip() == "handoff"
    assert (notification / "status").read_text(encoding="utf-8").strip() == "pending"
    spec = (notification / "spec.md").read_text(encoding="utf-8")
    assert "# Notify Planner: handoff-implementation" in spec
    assert "## Source Job\nhandoff-implementation" in spec
    assert "## Source Role\nimplementer" in spec
    assert "before the source job becomes terminal" in spec


def test_planner_job_done_does_not_create_recursive_planner_notice(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    make_task(runtime, tmp_path, "planner-only")
    source = runtime / "jobs" / "planner-only-plan"
    claimed = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            source.name,
            "-r",
            "planner",
            "--agent-id",
            "planner-1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert claimed.returncode == 0, claimed.stderr
    started = subprocess.run(
        [
            str(runtime / "bin" / "job-start"),
            source.name,
            "--agent-id",
            "planner-1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}

    result = complete_job(runtime, source.name, "planner-1")

    assert result.returncode == 0, result.stderr
    assert (source / "status").read_text(encoding="utf-8").strip() == "done"
    assert {path.name for path in (runtime / "jobs").iterdir()} == jobs_before


def test_job_done_reuses_prepublished_planner_notice(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    source = make_running_job(
        runtime,
        tmp_path,
        task_id="prepublished",
        job_id="prepublished-work",
        role="implementer",
        agent_id="implementer-1",
    )
    jobs_before = {path.name for path in (runtime / "jobs").iterdir()}
    command = [
        str(runtime / "bin" / "job-notify-planner"),
        source.name,
    ]

    first = subprocess.run(
        command, text=True, capture_output=True, check=False
    )
    second = subprocess.run(
        command, text=True, capture_output=True, check=False
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout.strip() == second.stdout.strip()
    notification_id = first.stdout.strip()
    assert notification_id
    jobs_after_publication = {
        path.name for path in (runtime / "jobs").iterdir()
    }
    assert jobs_after_publication - jobs_before == {notification_id}
    original_spec = (runtime / "jobs" / notification_id / "spec.md").read_text(
        encoding="utf-8"
    )

    completed = complete_job(runtime, source.name, "implementer-1")

    assert completed.returncode == 0, completed.stderr
    assert (source / "status").read_text(encoding="utf-8").strip() == "done"
    assert {
        path.name for path in (runtime / "jobs").iterdir()
    } == jobs_after_publication
    assert (runtime / "jobs" / notification_id / "spec.md").read_text(
        encoding="utf-8"
    ) == original_spec


def test_terminal_notice_waits_for_source_terminal_state(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    make_task(runtime, tmp_path, "gated")
    plan = runtime / "jobs" / "gated-plan"
    for command in (
        [
            str(runtime / "bin" / "job-claim"),
            plan.name,
            "-r",
            "planner",
            "--agent-id",
            "planner-1",
        ],
        [
            str(runtime / "bin" / "job-start"),
            plan.name,
            "--agent-id",
            "planner-1",
        ],
        [
            str(runtime / "bin" / "job-done"),
            plan.name,
            "--agent-id",
            "planner-1",
            "-m",
            "initial planning complete",
        ],
    ):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

    source = make_running_job(
        runtime,
        tmp_path,
        task_id="gated",
        job_id="gated-work",
        role="implementer",
        agent_id="implementer-1",
    )
    prepared = subprocess.run(
        [str(runtime / "bin" / "job-notify-planner"), source.name],
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    notification_id = prepared.stdout.strip()
    notification = runtime / "jobs" / notification_id
    assert (
        notification / ".terminal-notice-source"
    ).read_text(encoding="utf-8").strip() == source.name

    premature = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            notification_id,
            "-r",
            "planner",
            "--agent-id",
            "planner-2",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert premature.returncode != 0
    assert "waiting for source job 'gated-work' to become terminal" in (
        premature.stderr
    )

    environment = {**os.environ, "AGENTWS_WAIT_INTERVAL": "1"}
    waiter = subprocess.Popen(
        [str(runtime / "bin" / "job-wait"), "-r", "planner"],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.2)
        assert waiter.poll() is None
        completed = complete_job(runtime, source.name, "implementer-1")
        assert completed.returncode == 0, completed.stderr
        assert waiter.wait(timeout=3) == 0
    finally:
        if waiter.poll() is None:
            waiter.terminate()
            waiter.wait(timeout=3)

    claimed = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            notification_id,
            "-r",
            "planner",
            "--agent-id",
            "planner-2",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert claimed.returncode == 0, claimed.stderr


def test_terminal_planner_notice_cannot_satisfy_source_settlement(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    source = make_running_job(
        runtime,
        tmp_path,
        task_id="terminal-notice",
        job_id="terminal-notice-work",
        role="implementer",
        agent_id="implementer-1",
    )
    prepared = subprocess.run(
        [str(runtime / "bin" / "job-notify-planner"), source.name],
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    notification = runtime / "jobs" / prepared.stdout.strip()
    (notification / "status").write_text("failed\n", encoding="utf-8")

    completed = complete_job(runtime, source.name, "implementer-1")

    assert completed.returncode != 0
    assert "planner notification collision" in completed.stderr
    assert (source / "status").read_text(encoding="utf-8").strip() == "running"
    assert (source / "agent-id").read_text(encoding="utf-8").strip() == (
        "implementer-1"
    )
    assert (source / "lock").is_dir()


def test_concurrent_terminal_notice_publication_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    source = make_running_job(
        runtime,
        tmp_path,
        task_id="concurrent-notice",
        job_id="concurrent-notice-work",
        role="implementer",
        agent_id="implementer-1",
    )

    def publish(_index: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(runtime / "bin" / "job-notify-planner"), source.name],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish, range(16)))

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results if result.returncode != 0
    ]
    notification_ids = {result.stdout.strip() for result in results}
    assert len(notification_ids) == 1
    notification = runtime / "jobs" / notification_ids.pop()
    assert (
        notification / ".terminal-notice-source"
    ).read_text(encoding="utf-8").strip() == source.name


def test_job_done_notification_collision_leaves_source_running_and_owned(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    source = make_running_job(
        runtime,
        tmp_path,
        task_id="collision",
        job_id="collision-work",
        role="implementer",
        agent_id="implementer-1",
    )
    notification_module = runpy.run_path(
        str(runtime / "tools" / "planner_notification.py")
    )
    notification_id = notification_module["planner_notification_id"](
        "collision", source.name
    )
    foreign_spec = tmp_path / "foreign-notice.md"
    foreign_spec.write_text("# Unrelated planner work\n", encoding="utf-8")
    foreign = subprocess.run(
        [
            str(runtime / "bin" / "job-create"),
            notification_id,
            "-r",
            "planner",
            "-t",
            "collision",
            str(foreign_spec),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert foreign.returncode == 0, foreign.stderr
    foreign_job = runtime / "jobs" / notification_id

    result = complete_job(runtime, source.name, "implementer-1")

    assert result.returncode != 0
    assert "planner notification collision" in result.stderr
    assert "planner notification was not published" in result.stderr
    assert (source / "status").read_text(encoding="utf-8").strip() == "running"
    assert (source / "agent-id").read_text(encoding="utf-8").strip() == "implementer-1"
    assert (source / "lock").is_dir()
    assert (foreign_job / "spec.md").read_text(encoding="utf-8") == (
        foreign_spec.read_text(encoding="utf-8")
    )


def test_concurrent_task_comments_remain_complete_records(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    task = make_task(runtime, tmp_path)
    count = 200

    def comment(index: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(runtime / "bin" / "task-comment"),
                task.name,
                f"comment-{index}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(comment, range(count)))

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results if result.returncode != 0
    ]
    log = (task / "log.md").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"(?:## [^\n]+ - Comment\n\ncomment-[0-9]+\n\n)+",
        log,
    )
    observed = [int(value) for value in re.findall(r"comment-([0-9]+)", log)]
    assert sorted(observed) == list(range(count))
    assert (task / ".control.lock").is_file()


def test_task_result_and_state_hold_one_lock_across_publication(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    task = make_task(runtime, tmp_path)
    initial = subprocess.run(
        [str(runtime / "bin" / "task-state"), task.name, "done", "-m", "initial"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert initial.returncode == 0, initial.stderr

    result_file = tmp_path / "result.md"
    result_file.write_text("# Final result\n\ncomplete\n", encoding="utf-8")
    wrappers = tmp_path / "task-wrappers"
    wrappers.mkdir()
    entered = tmp_path / "result-copy-entered"
    release = tmp_path / "release-result-copy"
    real_cp = shutil.which("cp")
    assert real_cp is not None
    executable_script(
        wrappers / "cp",
        "#!/bin/sh\n"
        ': > "$ENTERED"\n'
        'while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        'exec "$REAL_CP" "$@"\n',
    )
    result_process = subprocess.Popen(
        [str(runtime / "bin" / "task-result"), task.name, str(result_file)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PATH": f"{wrappers}:{os.environ['PATH']}",
            "REAL_CP": real_cp,
            "ENTERED": str(entered),
            "RELEASE": str(release),
        },
    )
    try:
        wait_for(entered, result_process)
        state_process = subprocess.Popen(
            [
                str(runtime / "bin" / "task-state"),
                task.name,
                "open",
                "-m",
                "reopened after result",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.15)
        assert state_process.poll() is None
        assert (task / "state").read_text(encoding="utf-8").strip() == "done"
        assert not (task / "result.md").exists()
        release.touch()
        _result_stdout, result_stderr = result_process.communicate(timeout=5)
        _state_stdout, state_stderr = state_process.communicate(timeout=5)
    finally:
        release.touch(exist_ok=True)
        if result_process.poll() is None:
            result_process.kill()
            result_process.communicate(timeout=5)
        if "state_process" in locals() and state_process.poll() is None:
            state_process.kill()
            state_process.communicate(timeout=5)

    assert result_process.returncode == 0, result_stderr
    assert state_process.returncode == 0, state_stderr
    assert (task / "result.md").read_text(encoding="utf-8") == result_file.read_text(
        encoding="utf-8"
    )
    assert (task / "state").read_text(encoding="utf-8").strip() == "open"
    log = (task / "log.md").read_text(encoding="utf-8")
    assert log.index("Result recorded") < log.index("State: open")


def test_task_mutations_upgrade_legacy_lock_and_reject_symlinked_files(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    task = runtime / "tasks" / "legacy-task"
    task.mkdir()
    (task / "spec.md").write_text("# Legacy task\n", encoding="utf-8")
    (task / "state").write_text("open\n", encoding="utf-8")
    (task / "log.md").touch()

    upgraded = subprocess.run(
        [str(runtime / "bin" / "task-comment"), task.name, "upgrade"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert (task / ".control.lock").is_file()

    outside = tmp_path / "outside-log.md"
    outside.write_text("do not change\n", encoding="utf-8")
    (task / "log.md").unlink()
    (task / "log.md").symlink_to(outside)
    rejected = subprocess.run(
        [str(runtime / "bin" / "task-comment"), task.name, "unsafe"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "invalid log.md" in rejected.stderr
    assert outside.read_text(encoding="utf-8") == "do not change\n"


def test_crashed_job_create_is_invisible_and_same_id_can_be_retried(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    (runtime / "tasks" / "test-task").mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("# Complete specification\n", encoding="utf-8")
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "copy-entered"
    real_cp = shutil.which("cp")
    assert real_cp is not None
    executable_script(
        wrappers / "cp",
        "#!/bin/sh\n"
        '"$REAL_CP" "$@"\n'
        ': > "$ENTERED"\n'
        'while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n',
    )
    release = tmp_path / "release-copy"
    environment = {
        **os.environ,
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_CP": real_cp,
        "ENTERED": str(entered),
        "RELEASE": str(release),
    }
    process = subprocess.Popen(
        [
            str(runtime / "bin" / "job-create"),
            "new-job",
            "-r",
            "worker",
            "-t",
            "test-task",
            str(spec),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        wait_for(entered, process)
        assert not (runtime / "jobs" / "new-job").exists()
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    assert process.returncode == -signal.SIGKILL
    assert not (runtime / "jobs" / "new-job").exists()
    abandoned = [
        path
        for path in (runtime / "jobs").iterdir()
        if path.name.startswith(".job-create") and path.is_dir()
    ]
    assert abandoned
    assert "+" in abandoned[0].name
    hidden_claim = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            abandoned[0].name,
            "-r",
            "worker",
            "--agent-id",
            "intruder",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert hidden_claim.returncode != 0
    assert "invalid job ID" in hidden_claim.stderr

    retry = subprocess.run(
        [
            str(runtime / "bin" / "job-create"),
            "new-job",
            "-r",
            "worker",
            "-t",
            "test-task",
            str(spec),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    job = runtime / "jobs" / "new-job"
    assert retry.returncode == 0, retry.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert (job / "role").read_text(encoding="utf-8").strip() == "worker"
    assert (job / "task-id").read_text(encoding="utf-8").strip() == "test-task"
    assert (job / "spec.md").read_text(encoding="utf-8") == spec.read_text(
        encoding="utf-8"
    )
    assert (job / "workspace").is_dir()
    assert (job / ".control.lock").is_file()


def test_pending_ownership_is_repaired_by_wait_and_claim(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "interrupted-release", "pending", owner="old-agent")
    waited = subprocess.run(
        [str(runtime / "bin" / "job-wait"), "-r", "worker"],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert waited.returncode == 0, waited.stderr
    assert not (job / "lock").exists()
    assert not (job / "agent-id").exists()

    (job / "lock").mkdir()
    (job / "agent-id").write_text("old-agent\n", encoding="utf-8")
    claimed = subprocess.run(
        [
            str(runtime / "bin" / "job-claim"),
            "interrupted-release",
            "-r",
            "worker",
            "--agent-id",
            "new-agent",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert claimed.returncode == 0, claimed.stderr
    assert claimed.stdout.strip() == "CLAIMED: interrupted-release"
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "new-agent"
    assert (job / "lock").is_dir()
    assert (job / "log.md").read_text(encoding="utf-8").count(
        "Recovered interrupted transition"
    ) == 2


def test_pending_nonempty_ownership_lock_is_fatal(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "corrupt-pending", "pending", owner="old-agent")
    (job / "lock" / "foreign").write_text("keep\n", encoding="utf-8")

    result = subprocess.run(
        [str(runtime / "bin" / "job-wait"), "-r", "worker"],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert result.returncode != 0
    assert "non-empty ownership lock" in result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "old-agent"
    assert (job / "lock" / "foreign").read_text(encoding="utf-8") == "keep\n"


def test_reap_never_resets_active_job_from_age_alone(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "long-job", "running", owner="worker-1")
    (runtime / "agents" / "worker-1").mkdir()

    result = subprocess.run(
        [str(runtime / "bin" / "job-reap"), "1"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "age never proves its owner is dead" in result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "running"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "worker-1"
    assert (job / "lock").is_dir()


def test_forced_reap_serializes_against_owner_transition(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "forced-job", "running", owner="worker-1")
    (runtime / "agents" / "worker-1").mkdir()
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "remove-owner-entered"
    release = tmp_path / "release-remove-owner"
    real_rm = shutil.which("rm")
    assert real_rm is not None
    executable_script(
        wrappers / "rm",
        "#!/bin/sh\n"
        'last=""\n'
        'for argument do last="$argument"; done\n'
        'if [ "$last" = "$OWNER_FILE" ]; then\n'
        '  : > "$ENTERED"\n'
        '  while [ ! -e "$RELEASE" ]; do sleep 0.01; done\n'
        "fi\n"
        'exec "$REAL_RM" "$@"\n',
    )
    environment = {
        **os.environ,
        "PATH": f"{wrappers}:{os.environ['PATH']}",
        "REAL_RM": real_rm,
        "OWNER_FILE": str(job / "agent-id"),
        "ENTERED": str(entered),
        "RELEASE": str(release),
    }
    reaper = subprocess.Popen(
        [str(runtime / "bin" / "job-reap"), "--force", "1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    wait_for(entered, reaper)
    transition = subprocess.Popen(
        [
            str(runtime / "bin" / "job-done"),
            "forced-job",
            "--agent-id",
            "worker-1",
            "-m",
            "late completion",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.15)
    assert transition.poll() is None
    release.touch()
    _reap_stdout, reap_stderr = reaper.communicate(timeout=5)
    _done_stdout, done_stderr = transition.communicate(timeout=5)

    assert reaper.returncode == 0, reap_stderr
    assert transition.returncode != 0
    assert "from status 'pending'" in done_stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
    assert not (job / "agent-id").exists()
    assert not (job / "lock").exists()


def test_reset_orphans_rechecks_after_acquiring_control_lock(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "recovered-owner", "claimed", owner="worker-1")
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "flock-entered"
    real_flock = shutil.which("flock")
    assert real_flock is not None
    executable_script(
        wrappers / "flock",
        "#!/bin/sh\n"
        ': > "$ENTERED"\n'
        'exec "$REAL_FLOCK" "$@"\n',
    )

    with (job / ".control.lock").open("a", encoding="utf-8") as control:
        fcntl.flock(control, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [str(runtime / "bin" / "job-reset-orphans")],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PATH": f"{wrappers}:{os.environ['PATH']}",
                "REAL_FLOCK": real_flock,
                "ENTERED": str(entered),
            },
        )
        wait_for(entered, process)
        (runtime / "agents" / "worker-1").mkdir()
        fcntl.flock(control, fcntl.LOCK_UN)

    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "No orphaned jobs found" in stdout
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "worker-1"
    assert (job / "lock").is_dir()


def test_runtime_start_recovery_resets_active_jobs_and_agent_assignments(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    claimed = make_job(runtime, "claimed-job", "claimed", owner="worker-1")
    running = make_job(runtime, "running-job", "running", owner="worker-2")
    pending = make_job(runtime, "pending-job", "pending", owner="old-worker")
    done = make_job(runtime, "done-job", "done", owner="worker-3")
    (claimed / ".agent-attempts").write_text("1\n", encoding="utf-8")
    (running / ".agent-attempts").write_text("2\n", encoding="utf-8")

    for agent, job_id in (
        ("worker-1", claimed.name),
        ("worker-2", running.name),
        ("worker-3", done.name),
    ):
        agent_dir = runtime / "agents" / agent
        agent_dir.mkdir(parents=True)
        (agent_dir / "current-job").write_text(f"{job_id}\n", encoding="utf-8")

    result = run_startup_recovery(runtime)

    assert result.returncode == 0, result.stderr
    for job in (claimed, running, pending):
        assert (job / "status").read_text(encoding="utf-8").strip() == "pending"
        assert not (job / "agent-id").exists()
        assert not (job / "lock").exists()
    assert (claimed / ".agent-attempts").read_text(encoding="utf-8") == "1\n"
    assert (running / ".agent-attempts").read_text(encoding="utf-8") == "2\n"
    assert (done / "status").read_text(encoding="utf-8").strip() == "done"
    for agent in ("worker-1", "worker-2", "worker-3"):
        assert not (
            runtime / "agents" / agent / "current-job"
        ).read_text(encoding="utf-8").strip()
    assert "fresh runtime startup proved" in (claimed / "log.md").read_text(
        encoding="utf-8"
    )


def test_all_active_recovery_requires_inherited_runtime_lock(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "live-job", "running", owner="worker-1")

    result = subprocess.run(
        [str(runtime / "bin" / "job-reset-orphans"), "--all-active"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "runtime-lock capability" in result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "running"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "worker-1"
    assert (job / "lock").is_dir()


def test_startup_recovery_validates_lock_before_publishing_pending(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "corrupt-lock", "claimed", owner="worker-1")
    (job / "lock" / "unexpected").write_text("do not discard\n", encoding="utf-8")

    result = run_startup_recovery(runtime)

    assert result.returncode != 0
    assert "lock directory is not empty" in result.stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "claimed"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "worker-1"
    assert (job / "lock" / "unexpected").is_file()


def test_startup_recovery_rejects_symlinked_queue_entries(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    target = tmp_path / "outside-queue"
    target.mkdir()
    (target / "status").write_text("running\n", encoding="utf-8")
    (target / "log.md").write_text("outside\n", encoding="utf-8")
    (runtime / "jobs" / "unsafe-job").symlink_to(target, target_is_directory=True)

    result = run_startup_recovery(runtime)

    assert result.returncode != 0
    assert "symbolic-link queue entry" in result.stderr
    assert (target / "status").read_text(encoding="utf-8") == "running\n"
    assert (target / "log.md").read_text(encoding="utf-8") == "outside\n"


def test_runtime_start_recovery_rechecks_status_under_control_lock(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    job = make_job(runtime, "completed-during-recovery", "claimed", owner="worker-1")
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    entered = tmp_path / "flock-entered"
    real_flock = shutil.which("flock")
    assert real_flock is not None
    executable_script(
        wrappers / "flock",
        "#!/bin/sh\n"
        ': > "$ENTERED"\n'
        'exec "$REAL_FLOCK" "$@"\n',
    )

    with startup_runtime_lock(runtime) as runtime_lock:
        with (job / ".control.lock").open("a", encoding="utf-8") as control:
            fcntl.flock(control, fcntl.LOCK_EX)
            process = subprocess.Popen(
                [
                    str(runtime / "bin" / "job-reset-orphans"),
                    "--all-active",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(runtime_lock,),
                env={
                    **os.environ,
                    "PATH": f"{wrappers}:{os.environ['PATH']}",
                    "REAL_FLOCK": real_flock,
                    "ENTERED": str(entered),
                    "CYCLO_RUNTIME_LOCK_FD": str(runtime_lock),
                },
            )
            wait_for(entered, process)
            (job / "status").write_text("done\n", encoding="utf-8")
            fcntl.flock(control, fcntl.LOCK_UN)

    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert (job / "status").read_text(encoding="utf-8").strip() == "done"
    assert (job / "agent-id").read_text(encoding="utf-8").strip() == "worker-1"
    assert (job / "lock").is_dir()
