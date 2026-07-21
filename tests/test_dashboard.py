from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cyclo.agentws_queue import read_agent_supervisor_status
from cyclo.dashboard import (
    DashboardSnapshot,
    QueueLimits,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
    scan_agentws_queue,
    validate_dashboard_host,
)
from cyclo.component_stack import (
    ComponentStatus,
    DockerStatus,
    GatewayStatus,
    StackStatus,
)
from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore


PROVIDER_GENERATION = "provider-generation"
PROVIDER_SOCKET = Path("/var/lib/cyclo/providers/component.sock")


def instance(identifier: str, *, active: bool = True, offline: bool = False) -> Instance:
    return Instance(
        id=identifier,
        team_name=f"team-{identifier}",
        team_path=f"/teams/{identifier}",
        project_path=f"/projects/{identifier}",
        generation=f"generation-{identifier}",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=offline,
        active=active,
        port=4100 if not offline else None,
        provider_socket_path=str(PROVIDER_SOCKET),
        provider_generation=PROVIDER_GENERATION,
    )


def queue(store: StateStore, identifier: str) -> Path:
    root = store.queue_root(identifier)
    for name in ("tasks", "jobs", "agents"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


class InMemoryInstanceStore(StateStore):
    """Exercise dashboard defenses after strict persisted-state validation."""

    def __init__(self, root: Path, instances: list[Instance]) -> None:
        super().__init__(root)
        self._instances = instances

    def list_report(self) -> tuple[list[Instance], list[str]]:
        return list(self._instances), []


def mark_supervisor_ready(root: Path) -> Path:
    runs = root / "agents" / ".team-runs"
    runs.mkdir(parents=True, exist_ok=True)
    ready = runs / "supervisor.ready"
    ready.write_text("pid=123\n", encoding="utf-8")
    return runs


def test_queue_snapshot_counts_agentws_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    task_open = root / "tasks" / "open-task"
    task_open.mkdir()
    (task_open / "spec.md").write_text("# Fix the widget\n", encoding="utf-8")
    task_closed = root / "tasks" / "closed-task"
    task_closed.mkdir()
    (task_closed / "result.md").write_text("done\n", encoding="utf-8")

    running = root / "jobs" / "job-running"
    running.mkdir()
    (running / "status").write_text("running\n", encoding="utf-8")
    (running / "task-id").write_text("open-task\n", encoding="utf-8")
    (running / "agent-id").write_text("worker-1\n", encoding="utf-8")
    failed = root / "jobs" / "job-failed"
    failed.mkdir()
    (failed / "status").write_text("failed\n", encoding="utf-8")
    for name in ("worker-1", "reviewer-1"):
        (root / "agents" / name).mkdir()

    result = scan_agentws_queue(root)

    assert result["counts"] == {
        "tasks": {"total": 2, "open": 1, "closed": 1},
        "jobs": {
            "total": 2,
            "pending": 0,
            "claimed": 0,
            "running": 1,
            "done": 0,
            "failed": 1,
            "unknown": 0,
        },
        "agents": {"total": 2, "active": 1},
    }
    assert {item["title"] for item in result["recent_tasks"]} == {
        "Fix the widget",
        "closed-task",
    }
    assert {item["kind"] for item in result["recent_activity"]} == {"task", "job"}
    assert result["errors"] == []


def test_queue_activity_uses_log_mtime_not_only_directory_mtime(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    job = root / "jobs" / "job"
    job.mkdir()
    log = job / "log.md"
    log.write_text("first\n", encoding="utf-8")
    directory_mtime = job.stat().st_mtime
    newer = directory_mtime + 120
    log.touch()
    # Use an explicit future timestamp so filesystems with coarse timestamp
    # resolution cannot make this check flaky.
    os.utime(log, (newer, newer))

    result = scan_agentws_queue(root)

    expected = datetime.fromtimestamp(newer, timezone.utc).isoformat().replace("+00:00", "Z")
    assert result["recent_activity"][0]["updated_at"] == expected


def test_queue_snapshot_never_follows_symlinks(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state").write_text("closed\n", encoding="utf-8")
    (root / "tasks" / "escape").symlink_to(outside, target_is_directory=True)

    safe = root / "jobs" / "safe"
    safe.mkdir()
    (safe / "status").symlink_to(outside / "state")
    (root / "agents" / "worker").mkdir()

    result = scan_agentws_queue(root)

    assert result["counts"]["tasks"]["total"] == 0
    assert result["counts"]["jobs"] == {
        "total": 1,
        "pending": 0,
        "claimed": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "unknown": 1,
    }
    assert result["counts"]["agents"] == {"total": 1, "active": 0}
    assert any("unknown or unreadable status" in error for error in result["errors"])

    linked_root = tmp_path / "linked-queue"
    linked_root.symlink_to(root, target_is_directory=True)
    linked_result = scan_agentws_queue(linked_root)
    assert linked_result["counts"]["tasks"]["total"] == 0
    assert any("unavailable" in error for error in linked_result["errors"])


def test_supervisor_status_reports_only_regular_suspension_markers(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    runs = root / "agents" / ".team-runs"
    runs.mkdir()
    (runs / "supervisor.ready").write_text("pid=123\n", encoding="utf-8")
    (runs / "planner-1.suspended").write_text("last_status=70\n", encoding="utf-8")
    outside = tmp_path / "outside.suspended"
    outside.write_text("not a supervisor marker\n", encoding="utf-8")
    (runs / "linked.suspended").symlink_to(outside)
    (runs / "ignored.last-status").write_text("70\n", encoding="utf-8")

    status = read_agent_supervisor_status(root)

    assert status.suspended_agents == ("planner-1",)
    assert status.error == ""


def test_supervisor_status_requires_a_fresh_readiness_heartbeat(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    runs = mark_supervisor_ready(root)
    os.utime(runs / "supervisor.ready", (95.0, 95.0))

    fresh = read_agent_supervisor_status(
        root, now=100.0, max_ready_age_seconds=15.0
    )
    assert fresh.error == ""

    os.utime(runs / "supervisor.ready", (70.0, 70.0))
    stale = read_agent_supervisor_status(
        root, now=100.0, max_ready_age_seconds=15.0
    )
    assert "supervisor heartbeat stale" in stale.error


def test_supervisor_status_reports_unresolved_planner_failure(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    job = root / "jobs" / "uart-plan"
    job.mkdir()
    (job / "role").write_text("planner\n", encoding="utf-8")
    (job / "status").write_text("failed\n", encoding="utf-8")

    status = read_agent_supervisor_status(root)

    assert status.planner_attention_jobs == ("uart-plan",)
    assert status.error == ""


def test_supervisor_status_fails_closed_on_linked_run_directory(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "agents" / ".team-runs").symlink_to(
        outside, target_is_directory=True
    )

    status = read_agent_supervisor_status(root)

    assert status.suspended_agents == ()
    assert status.error


def test_queue_snapshot_has_a_shared_entry_budget(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")
    for index in range(8):
        (root / "tasks" / f"task-{index}").mkdir()

    result = scan_agentws_queue(root, QueueLimits(max_entries=3, max_read_bytes=1024))

    assert result["counts"]["tasks"]["total"] == 3
    assert any("truncated" in error for error in result["errors"])


class FakeDocker:
    def __init__(self, running: dict[str, bool]) -> None:
        self.running = running

    def container_running(self, name: str) -> bool:
        return self.running[name]


class FakeUsage:
    def usage(self) -> dict[str, object]:
        return {
            "totals": {
                "requests": 4,
                "input_tokens": 107,
                "output_tokens": 27,
            },
            "by_provider": {
                "openai-codex": {
                    "requests": 4,
                    "input_tokens": 107,
                    "output_tokens": 27,
                }
            },
        }


class FakeProvider:
    def __init__(self, status: StackStatus) -> None:
        self.selected_status = status
        self.calls = 0

    def status(self) -> StackStatus:
        self.calls += 1
        return self.selected_status


def docker_status(
    *,
    present: bool = True,
    running: bool = True,
    current: bool = True,
    lifecycle: str | None = None,
    engine_health: str = "healthy",
) -> DockerStatus:
    return DockerStatus(
        "sha256:" + "1" * 64,
        "1" * 64 if present else None,
        running,
        lifecycle or ("running" if running else "stopped"),
        engine_health,
        current,
    )


def provider_status(
    *,
    gateway_docker: DockerStatus | None = None,
    gateway_ready: bool = True,
    component_name: str | None = None,
    component_docker: DockerStatus | None = None,
    component_ready: bool = True,
) -> StackStatus:
    gateway_docker = gateway_docker or docker_status()
    components = ()
    if component_name is not None:
        selected_docker = component_docker or docker_status()
        components = (
            ComponentStatus(
                component_name,
                "passthrough",
                True,
                component_ready,
                component_ready,
                selected_docker,
            ),
        )
    ready = gateway_ready and all(component.ready for component in components)
    return StackStatus(
        PROVIDER_GENERATION,
        PROVIDER_SOCKET,
        GatewayStatus(
            Path("/var/lib/cyclo/gateway/component.sock"),
            True,
            gateway_ready,
            gateway_ready,
            gateway_docker,
        ),
        components,
        ready,
    )


def ready_provider() -> FakeProvider:
    return FakeProvider(provider_status())


def test_snapshot_joins_state_docker_queue_and_usage(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    alpha = instance("alpha")
    old = instance("old", active=False, offline=True)
    store.save(alpha)
    store.save(old)
    alpha_queue = queue(store, "alpha")
    queue(store, "old")
    mark_supervisor_ready(alpha_queue)
    task = alpha_queue / "tasks" / "work"
    task.mkdir()
    job = alpha_queue / "jobs" / "job"
    job.mkdir()
    (job / "status").write_text("failed\n", encoding="utf-8")
    (alpha_queue / "agents" / "worker").mkdir()

    provider = ready_provider()
    usage = FakeUsage().usage()
    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True, "cyclo-old": False}),  # type: ignore[arg-type]
        usage_reader=FakeUsage(),
        provider_reader=provider,
    ).build()

    assert result["version"] == 3
    assert result["summary"] == {
        "instances": 2,
        "running": 1,
        "provider_issues": 0,
        "attention": 1,
        "tasks": 1,
        "jobs": 1,
        "agents": 1,
        "tokens": 134,
        "requests": 4,
        "errors": 0,
    }
    assert result["source_errors"] == []
    assert result["usage"] == usage
    running, stopped = result["instances"]
    assert running["id"] == "alpha"
    assert running["state"] == "running"
    assert running["health"] == {"state": "ready", "reason": ""}
    assert running["agentws_port"] == 4100
    assert "agentws_url" not in running
    assert running["project"] == {
        "name": "alpha",
        "path": "/projects/alpha",
        "definition": None,
        "description": "",
        "generation": "",
        "workspaces": [
            {
                "name": "alpha",
                "path": "/projects/alpha",
                "container_path": "/workspace",
            }
        ],
        "read_only_mounts": [],
    }
    assert "project_read_only" not in running["mode"]
    assert "usage" not in running
    assert stopped["state"] == "stopped"
    assert stopped["health"] == {
        "state": "inactive",
        "reason": "not an active running Cyclo instance",
    }
    assert stopped["agentws_port"] is None
    assert "usage" not in stopped
    assert provider.calls == 1


def test_snapshot_reports_suspended_agents_per_running_instance(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    for identifier in ("alpha", "beta"):
        store.save(instance(identifier))
        mark_supervisor_ready(queue(store, identifier))
    runs = store.agents_dir("alpha") / ".team-runs"
    (runs / "planner-1.suspended").write_text(
        "reason=fatal-agent-safety-error\n", encoding="utf-8"
    )
    provider = ready_provider()

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True, "cyclo-beta": True}),  # type: ignore[arg-type]
        provider_reader=provider,
    ).build()

    rows = {row["id"]: row for row in result["instances"]}
    assert rows["alpha"]["health"] == {
        "state": "agents-suspended",
        "reason": "1 agent suspended: planner-1",
    }
    assert rows["beta"]["health"] == {"state": "ready", "reason": ""}
    assert result["summary"]["provider_issues"] == 0
    assert result["summary"]["attention"] == 1
    assert provider.calls == 1


def test_snapshot_reports_unresolved_planner_failure_as_attention(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    job = root / "jobs" / "uart-plan"
    job.mkdir()
    (job / "role").write_text("planner\n", encoding="utf-8")
    (job / "status").write_text("failed\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "agents-attention",
        "reason": "1 unresolved planner failure: uart-plan",
    }
    assert result["summary"]["attention"] == 1


def test_snapshot_does_not_report_ready_when_supervisor_state_is_unreadable(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    root = queue(store, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "agents" / ".team-runs").symlink_to(
        outside, target_is_directory=True
    )

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    health = result["instances"][0]["health"]
    assert health["state"] == "agents-unknown"
    assert health["reason"].startswith("AgentWS supervisor status unavailable:")
    assert result["summary"]["attention"] == 1


def test_snapshot_preserves_provider_failure_beside_agent_suspension(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    root = queue(store, "alpha")
    runs = root / "agents" / ".team-runs"
    runs.mkdir()
    (runs / "supervisor.ready").write_text("pid=123\n", encoding="utf-8")
    (runs / "planner-1.suspended").write_text("last_status=70\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=FakeProvider(
            provider_status(
                component_name="fusion",
                component_docker=docker_status(running=False),
                component_ready=False,
            )
        ),
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "provider-down",
        "reason": "fusion stopped; 1 agent suspended: planner-1",
    }
    assert result["summary"]["provider_issues"] == 1
    assert result["summary"]["attention"] == 1


@pytest.mark.parametrize(
    ("status", "health_state", "reason"),
    [
        (
            provider_status(
                gateway_docker=docker_status(present=False, running=False),
                gateway_ready=False,
            ),
            "provider-down",
            "gateway absent",
        ),
        (
            provider_status(
                component_name="fusion",
                component_docker=docker_status(running=False),
                component_ready=False,
            ),
            "provider-down",
            "fusion stopped",
        ),
        (
            provider_status(
                component_name="fusion",
                component_docker=docker_status(current=False),
            ),
            "provider-stale",
            "configuration or image stale: fusion",
        ),
    ],
)
def test_snapshot_exposes_exact_provider_health_and_reads_it_once(
    tmp_path: Path,
    status: StackStatus,
    health_state: str,
    reason: str,
) -> None:
    store = StateStore(tmp_path / "state")
    for identifier in ("alpha", "beta"):
        store.save(instance(identifier))
        mark_supervisor_ready(queue(store, identifier))
    store.save(instance("stopped", active=False))
    queue(store, "stopped")
    provider = FakeProvider(status)

    result = DashboardSnapshot(
        store,
        docker=FakeDocker(
            {
                "cyclo-alpha": True,
                "cyclo-beta": True,
                "cyclo-stopped": False,
            }
        ),  # type: ignore[arg-type]
        provider_reader=provider,
    ).build()

    rows = {row["id"]: row for row in result["instances"]}
    assert rows["alpha"]["state"] == "running"
    assert rows["alpha"]["health"] == {
        "state": health_state,
        "reason": reason,
    }
    assert rows["beta"]["health"] == rows["alpha"]["health"]
    assert rows["stopped"]["health"]["state"] == "inactive"
    assert result["summary"]["running"] == 2
    assert result["summary"]["provider_issues"] == 1
    assert result["summary"]["attention"] == 2
    assert provider.calls == 1


def test_snapshot_reports_provider_status_errors_as_unknown(tmp_path: Path) -> None:
    class BrokenProvider:
        calls = 0

        @classmethod
        def status(cls) -> StackStatus:
            cls.calls += 1
            raise CycloError("Docker socket denied")

    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=BrokenProvider(),
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "provider-unknown",
        "reason": "provider status unavailable: Docker socket denied",
    }
    assert result["summary"]["provider_issues"] == 1
    assert BrokenProvider.calls == 1


def test_snapshot_fails_closed_when_provider_health_is_not_ready(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=FakeProvider(
            provider_status(gateway_ready=False)
        ),
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "provider-down",
        "reason": "gateway not ready",
    }


def test_snapshot_does_not_persist_an_active_team_inactive_when_not_operational(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha")
    store.save(selected)

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": False}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert result["instances"][0]["state"] == "stale"
    assert result["instances"][0]["health"]["state"] == "inactive"
    assert store.load("alpha").active is True


def test_snapshot_exposes_project_definition_and_named_mounts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    configured = instance("alpha")
    configured.project_name = "silicon"
    configured.project_file = "/configs/silicon/project.cyclo"
    configured.project_description = "RTL development"
    configured.project_generation = "project-generation"
    configured.project_mounts = [
        {
            "name": "source",
            "path": "/host/core-et",
            "mode": "rw",
        },
        {
            "name": "specifications",
            "path": "/host/specifications",
            "mode": "ro",
        },
    ]
    store.save(configured)
    mark_supervisor_ready(queue(store, configured.id))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({configured.container_name: True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert result["instances"][0]["project"] == {
        "name": "silicon",
        "path": "/projects/alpha",
        "definition": "/configs/silicon/project.cyclo",
        "description": "RTL development",
        "generation": "project-generation",
        "workspaces": [
            {
                "name": "source",
                "path": "/host/core-et",
                "container_path": "/workspace/source",
            }
        ],
        "read_only_mounts": [
            {
                "name": "specifications",
                "path": "/host/specifications",
                "container_path": "/readonly/specifications",
            }
        ],
    }


def test_snapshot_isolates_malformed_project_mount_state(tmp_path: Path) -> None:
    good = instance("good")
    broken = instance("broken")
    broken.project_file = "/configs/broken/project.cyclo"
    broken.project_name = "broken-project"
    broken.project_description = "Broken mount state"
    broken.project_generation = "broken-generation"
    broken.project_mounts = None  # type: ignore[assignment]
    store = InMemoryInstanceStore(tmp_path / "state", [good, broken])
    mark_supervisor_ready(queue(store, good.id))
    mark_supervisor_ready(queue(store, broken.id))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker(
            {good.container_name: True, broken.container_name: True}
        ),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert {row["id"] for row in result["instances"]} == {"good", "broken"}
    broken_row = next(row for row in result["instances"] if row["id"] == "broken")
    assert broken_row["project"]["workspaces"] == []
    assert broken_row["project"]["read_only_mounts"] == []
    assert broken_row["errors"] == [
        "invalid project metadata: project_mounts must be a list"
    ]
    assert result["summary"]["attention"] == 1


def test_snapshot_preserves_valid_mounts_beside_invalid_entries(
    tmp_path: Path,
) -> None:
    configured = instance("alpha")
    configured.project_file = "/configs/alpha/project.cyclo"
    configured.project_name = "alpha-project"
    configured.project_description = "Mixed valid and invalid mount state"
    configured.project_generation = "alpha-generation"
    configured.project_mounts = [
        {
            "name": "source",
            "path": "/host/source",
            "mode": "rw",
        },
        None,
        {
            "name": "bad-mode",
            "path": "/host/bad",
            "mode": "write",
        },
        {
            "name": "docs",
            "path": "/host/docs",
            "mode": "ro",
        },
    ]  # type: ignore[list-item]
    store = InMemoryInstanceStore(tmp_path / "state", [configured])
    mark_supervisor_ready(queue(store, configured.id))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({configured.container_name: True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    row = result["instances"][0]
    assert [mount["name"] for mount in row["project"]["workspaces"]] == [
        "source"
    ]
    assert [mount["name"] for mount in row["project"]["read_only_mounts"]] == [
        "docs"
    ]
    assert len(row["errors"]) == 2
    assert any("project_mounts[1] must be an object" in error for error in row["errors"])
    assert any("project_mounts[2] has an invalid mode" in error for error in row["errors"])


def test_snapshot_reports_legacy_read_only_state_as_requiring_restart(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    legacy = instance("legacy")
    legacy.legacy_project_read_only = True
    store.save(legacy)
    mark_supervisor_ready(queue(store, legacy.id))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({legacy.container_name: True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    row = result["instances"][0]
    assert row["project"]["workspaces"] == []
    assert row["project"]["read_only_mounts"] == [
        {
            "name": "legacy",
            "path": "/projects/legacy",
            "container_path": "/workspace",
        }
    ]
    assert any("stop and rerun" in error for error in row["errors"])
    assert result["summary"]["attention"] == 1


def test_snapshot_rejects_empty_or_non_string_project_definition_state(
    tmp_path: Path,
) -> None:
    empty = instance("empty")
    empty.project_file = "/configs/empty/project.cyclo"
    empty.project_name = "empty-project"
    empty.project_description = "Missing mounts"
    empty.project_generation = "empty-generation"
    malformed = instance("malformed")
    malformed.project_file = {"unexpected": True}  # type: ignore[assignment]
    store = InMemoryInstanceStore(tmp_path / "state", [empty, malformed])
    mark_supervisor_ready(queue(store, empty.id))
    mark_supervisor_ready(queue(store, malformed.id))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker(
            {empty.container_name: True, malformed.container_name: True}
        ),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    by_id = {row["id"]: row for row in result["instances"]}
    assert any("must contain at least one mount" in error for error in by_id["empty"]["errors"])
    assert by_id["malformed"]["project"]["workspaces"] == []
    assert any("project_file must be a string" in error for error in by_id["malformed"]["errors"])


def test_snapshot_counts_queue_errors_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert result["summary"]["attention"] == 1
    assert result["summary"]["errors"] == 1
    assert result["instances"][0]["errors"]


def test_snapshot_reports_bad_instance_records_without_hiding_readable_ones(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    assert [row["id"] for row in result["instances"]] == ["alpha"]
    assert len(result["source_errors"]) == 1
    assert str(broken) in result["source_errors"][0]
    assert "invalid Cyclo instance metadata" in result["source_errors"][0]
    assert result["summary"]["errors"] == 1


def test_snapshot_counts_unknown_job_status_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    job = root / "jobs" / "job-without-status"
    job.mkdir()

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        provider_reader=ready_provider(),
    ).build()

    row = result["instances"][0]
    assert row["counts"]["jobs"]["unknown"] == 1
    assert any("unknown or unreadable status" in error for error in row["errors"])
    assert result["summary"]["attention"] == 1


def test_snapshot_separates_gateway_source_errors_from_instance_errors(
    tmp_path: Path,
) -> None:
    class BrokenUsage:
        def usage(self) -> dict[str, object]:
            raise RuntimeError("gateway stopped")

    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        usage_reader=BrokenUsage(),
        provider_reader=ready_provider(),
    ).build()

    assert result["source_errors"] == ["gateway usage unavailable: gateway stopped"]
    assert result["summary"]["errors"] == 1
    assert result["instances"][0]["errors"] == []


def test_dashboard_http_server_is_read_only_and_serves_injected_assets() -> None:
    payload = {
        "version": 1,
        "generated_at": "now",
        "summary": {},
        "instances": [],
    }
    snapshot_calls = 0

    def snapshot() -> dict[str, object]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return payload

    server = make_dashboard_server(
        snapshot,
        static_assets={
            "/": ("text/html; charset=utf-8", "<h1>dashboard</h1>"),
            "/app.js": ("application/javascript; charset=utf-8", "void 0"),
        },
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot") as response:
            assert json.loads(response.read()) == payload
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            assert response.read() == b"<h1>dashboard</h1>"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/snapshot", method=method
            )
            with pytest.raises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request)
            assert rejected.value.code == 405
            assert rejected.value.headers["Allow"] == "GET, HEAD"
        assert snapshot_calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_packaged_dashboard_assets_use_fixed_browser_routes() -> None:
    assets = packaged_dashboard_assets()

    assert set(assets) == {"/", "/static/styles.css", "/static/app.js"}
    assert b'/static/styles.css' in assets["/"][1]
    assert b'/static/app.js' in assets["/"][1]
    assert b'/api/snapshot' in assets["/static/app.js"][1]


def test_dashboard_accepts_explicit_non_loopback_hosts() -> None:
    validate_dashboard_host("127.0.0.1")
    validate_dashboard_host("0.0.0.0")

    assert dashboard_host_is_loopback("127.0.0.1") is True
    assert dashboard_host_is_loopback("0.0.0.0") is False


def test_dashboard_rejects_invalid_hosts(monkeypatch) -> None:
    with pytest.raises(CycloError, match="non-empty"):
        validate_dashboard_host("")

    def unresolved(*_args, **_kwargs):
        raise socket.gaierror("test lookup failed")

    monkeypatch.setattr("cyclo.dashboard.socket.getaddrinfo", unresolved)
    with pytest.raises(CycloError, match="cannot resolve"):
        validate_dashboard_host("not-a-real-cyclo-host.invalid")
