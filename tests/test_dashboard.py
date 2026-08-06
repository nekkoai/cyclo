from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.dashboard import (
    DashboardSnapshot,
    QueueLimits,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
    scan_agentws_queue,
    validate_dashboard_host,
)
from cyclo.team.queue import read_agent_supervisor_status
from cyclo.dcomp import (
    DCompComponentStatus,
    DCompNetworkStatus,
    DCompPublishedPort,
    DCompStatus,
)
from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore


@pytest.fixture(autouse=True)
def local_docker_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: "unix:///tmp/cyclo-test-docker.sock",
    )


def instance(
    identifier: str,
    *,
    intent: str = "running",
    offline: bool = False,
) -> Instance:
    return Instance(
        id=identifier,
        team_name=f"team-{identifier}",
        team_path=f"/teams/{identifier}",
        generation=f"generation-{identifier}",
        models=["openai-codex/gpt-test"],
        image="sha256:" + "a" * 64,
        team_write=False,
        offline=offline,
        verbose=False,
        image_override="",
        agentws_host="127.0.0.1",
        intent=intent,
        requested_port=0,
        team_roster="team",
        team_protocol=False,
        pi_default_provider="openai-codex",
        pi_default_model="gpt-test",
        project_name=identifier,
        project_file=f"/projects/{identifier}/project.cyclo",
        project_description=f"Dashboard test project {identifier}.",
        project_generation=f"project-generation-{identifier}",
        project_config=(
            f"name {identifier}\n"
            f"description Dashboard test project {identifier}.\n"
            f"mount source /projects/{identifier} rw\n"
        ),
        project_mounts=[
            {
                "name": "source",
                "path": f"/projects/{identifier}",
                "mode": "rw",
            }
        ],
        runtime_version="0.2.0",
    )


def queue(store: StateStore, identifier: str) -> Path:
    root = store.queue_root(identifier)
    for name in ("tasks", "jobs", "agents"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def persist(store: StateStore, selected: Instance) -> None:
    store.save(selected)


class InMemoryInstanceStore(StateStore):
    """Exercise dashboard defenses after strict persisted-state validation."""

    def __init__(self, root: Path, instances: list[Instance]) -> None:
        super().__init__(root)
        self._instances = instances

    def list_report(
        self,
    ) -> tuple[list[Instance], list[str]]:
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
    (task_open / "state").write_text("open\n", encoding="utf-8")
    task_closed = root / "tasks" / "closed-task"
    task_closed.mkdir()
    (task_closed / "state").write_text("done\n", encoding="utf-8")
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
        "tasks": {"total": 2, "open": 1, "closed": 1, "unknown": 0},
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


def test_queue_snapshot_reports_invalid_task_states_as_unknown(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    root = queue(store, "alpha")

    reopened = root / "tasks" / "reopened"
    reopened.mkdir()
    (reopened / "state").write_text("open\n", encoding="utf-8")
    (reopened / "result.md").write_text("result from prior cycle\n", encoding="utf-8")

    done = root / "tasks" / "done"
    done.mkdir()
    (done / "state").write_text("done\n", encoding="utf-8")

    invalid = root / "tasks" / "invalid"
    invalid.mkdir()
    (invalid / "state").write_text("closed\n", encoding="utf-8")
    (invalid / "result.md").write_text("must not mask corruption\n", encoding="utf-8")

    unreadable = root / "tasks" / "unreadable"
    unreadable.mkdir()
    outside = tmp_path / "outside-state"
    outside.write_text("done\n", encoding="utf-8")
    (unreadable / "state").symlink_to(outside)

    result = scan_agentws_queue(root)

    assert result["counts"]["tasks"] == {
        "total": 4,
        "open": 1,
        "closed": 1,
        "unknown": 2,
    }
    assert {
        item["id"]: item["state"] for item in result["recent_tasks"]
    } == {
        "done": "closed",
        "invalid": "unknown",
        "reopened": "open",
        "unreadable": "unknown",
    }
    assert "2 tasks have an unknown or unreadable state" in result["errors"]


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


def component_status(
    name: str,
    *,
    status: str = "running",
    health: str = "healthy",
    problem: str = "",
    host_port: int | None = None,
    host_ip: str = "127.0.0.1",
) -> DCompComponentStatus:
    ports = (
        ()
        if host_port is None
        else (
            DCompPublishedPort(
                "tcp",
                host_ip,
                host_port,
                4137,
            ),
        )
    )
    return DCompComponentStatus(
        name=name,
        container_id="" if status == "missing" else "a" * 64,
        status=status,
        health=health,
        exit_code=0,
        problem=problem,
        published_ports=ports,
    )


def runtime_status(
    *components: DCompComponentStatus,
    desired: bool = True,
    operational: bool | None = None,
    operation: str = "",
    phase: str = "",
    networks: tuple[DCompNetworkStatus, ...] = (),
) -> DCompStatus:
    if operational is None:
        operational = bool(components) and not operation and all(
            component.status == "running"
            and component.health == "healthy"
            and not component.problem
            for component in components
        ) and all(not network.problem for network in networks)
    return DCompStatus(
        api_version=1,
        name="cyclo-test",
        desired=desired,
        operational=operational,
        digest="f" * 64 if desired else "",
        operation=operation,
        phase=phase,
        networks=networks,
        components=components,
    )


class FakeRuntime:
    def __init__(
        self,
        selected: DCompStatus | Exception,
        *,
        outer: str = "gateway",
        providers: tuple[str, ...] = (),
    ) -> None:
        self.selected = selected
        self.calls = 0
        self.port_reads: list[tuple[str, DCompStatus]] = []
        self.host = SimpleNamespace(
            outer_component=outer,
            providers=tuple(
                SimpleNamespace(name=name) for name in providers
            ),
        )

    def status(self) -> DCompStatus:
        self.calls += 1
        if isinstance(self.selected, Exception):
            raise self.selected
        return self.selected

    def component_for_instance(self, identifier: str) -> str:
        return f"mapped-{identifier}"

    def team_port(
        self,
        instance: Instance,
        status: DCompStatus,
    ) -> int:
        self.port_reads.append((instance.id, status))
        component = status.component(self.component_for_instance(instance.id))
        assert component is not None
        ports = [
            port.host_port
            for port in component.published_ports
            if port.container_port == 4137
        ]
        if len(ports) != 1:
            raise CycloError("team has no unique effective dashboard port")
        return ports[0]


def healthy_runtime(
    *identifiers: str,
    offline: tuple[str, ...] = (),
) -> FakeRuntime:
    components = [component_status("gateway")]
    components.extend(
        component_status(
            f"mapped-{identifier}",
            host_port=None if identifier in offline else 4100,
        )
        for identifier in identifiers
    )
    return FakeRuntime(runtime_status(*components))


def test_snapshot_joins_state_runtime_and_queue_from_one_status_read(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    alpha = instance("alpha")
    old = instance("old", intent="stopped", offline=True)
    persist(store, alpha)
    persist(store, old)
    alpha_queue = queue(store, "alpha")
    queue(store, "old")
    mark_supervisor_ready(alpha_queue)
    task = alpha_queue / "tasks" / "work"
    task.mkdir()
    (task / "state").write_text("open\n", encoding="utf-8")
    job = alpha_queue / "jobs" / "job"
    job.mkdir()
    (job / "status").write_text("failed\n", encoding="utf-8")
    (alpha_queue / "agents" / "worker").mkdir()

    runtime = healthy_runtime("alpha")
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    assert result["version"] == 4
    assert result["summary"] == {
        "instances": 2,
        "running": 1,
        "provider_issues": 0,
        "attention": 1,
        "tasks": 1,
        "jobs": 1,
        "agents": 1,
        "errors": 0,
    }
    assert result["source_errors"] == []
    assert "usage" not in result
    running, stopped = result["instances"]
    assert running["id"] == "alpha"
    assert running["desired"] == "running"
    assert running["container"] == "running"
    assert running["readiness"] == "healthy"
    assert running["health"] == {"state": "ready", "reason": ""}
    assert running["agentws_port"] == 4100
    assert "agentws_url" not in running
    assert running["project"] == {
        "name": "alpha",
        "path": "/projects/alpha",
        "definition": "/projects/alpha/project.cyclo",
        "description": "Dashboard test project alpha.",
        "generation": "project-generation-alpha",
        "workspaces": [
            {
                "name": "source",
                "path": "/projects/alpha",
                "container_path": "/workspace/source",
            }
        ],
        "read_only_mounts": [],
    }
    assert "project_read_only" not in running["mode"]
    assert "usage" not in running
    assert stopped["desired"] == "stopped"
    assert stopped["container"] == "absent"
    assert stopped["readiness"] == "absent"
    assert stopped["health"] == {
        "state": "inactive",
        "reason": "instance is not operational",
    }
    assert stopped["agentws_port"] is None
    assert "usage" not in stopped
    assert runtime.calls == 1
    assert [identifier for identifier, _status in runtime.port_reads] == [
        "alpha"
    ]
    assert runtime.port_reads[0][1] is runtime.selected
    assert result["runtime"]["components"][0]["name"] == "gateway"


def test_snapshot_reports_suspended_agents_per_running_instance(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    for identifier in ("alpha", "beta"):
        persist(store, instance(identifier))
        mark_supervisor_ready(queue(store, identifier))
    runs = store.agents_dir("alpha") / ".team-runs"
    (runs / "planner-1.suspended").write_text(
        "reason=fatal-agent-safety-error\n", encoding="utf-8"
    )
    runtime = healthy_runtime("alpha", "beta")
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    rows = {row["id"]: row for row in result["instances"]}
    assert rows["alpha"]["health"] == {
        "state": "agents-suspended",
        "reason": "1 agent suspended: planner-1",
    }
    assert rows["beta"]["health"] == {"state": "ready", "reason": ""}
    assert result["summary"]["provider_issues"] == 0
    assert result["summary"]["attention"] == 1
    assert runtime.calls == 1


def test_snapshot_reports_unresolved_planner_failure_as_attention(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    job = root / "jobs" / "uart-plan"
    job.mkdir()
    (job / "role").write_text("planner\n", encoding="utf-8")
    (job / "status").write_text("failed\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
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
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "agents" / ".team-runs").symlink_to(
        outside, target_is_directory=True
    )

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    health = result["instances"][0]["health"]
    assert health["state"] == "agents-unknown"
    assert health["reason"].startswith("AgentWS supervisor status unavailable:")
    assert result["summary"]["attention"] == 1


def test_snapshot_preserves_optional_provider_failure_beside_agent_suspension(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    runs = root / "agents" / ".team-runs"
    runs.mkdir()
    (runs / "supervisor.ready").write_text("pid=123\n", encoding="utf-8")
    (runs / "planner-1.suspended").write_text("last_status=70\n", encoding="utf-8")

    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status(
                "fusion",
                status="exited",
                health="unhealthy",
                problem="upstream refused the connection",
            ),
            component_status("mapped-alpha", host_port=4100),
            operational=False,
        ),
        providers=("fusion",),
    )
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "agents-suspended",
        "reason": (
            "unavailable optional provider components: component fusion: "
            "upstream refused the connection; status exited; health unhealthy; "
            "1 agent suspended: planner-1"
        ),
    }
    assert result["summary"]["provider_issues"] == 1
    assert result["summary"]["attention"] == 1
    assert result["source_errors"] == [
        "component fusion: upstream refused the connection; "
        "status exited; health unhealthy"
    ]


def test_snapshot_tolerates_an_absent_optional_provider_component(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))
    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status("mapped-alpha", host_port=4100),
        ),
        providers=("fusion",),
    )

    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    assert result["instances"][0]["health"] == {
        "state": "ready",
        "reason": (
            "unavailable optional provider components: "
            "component fusion: absent from runtime status"
        ),
    }
    assert result["summary"]["provider_issues"] == 1
    assert result["source_errors"] == [
        "component fusion: absent from runtime status"
    ]


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (
            None,
            "outer provider component gateway is absent",
        ),
        (
            component_status(
                "gateway",
                status="exited",
                health="unhealthy",
            ),
            "gateway: status exited; health unhealthy",
        ),
        (
            component_status(
                "gateway",
                problem="container identity does not match desired image",
            ),
            "gateway: container identity does not match desired image",
        ),
    ],
)
def test_snapshot_exposes_outer_provider_health_from_dcomp(
    tmp_path: Path,
    provider: DCompComponentStatus | None,
    reason: str,
) -> None:
    store = StateStore(tmp_path / "state")
    for identifier in ("alpha", "beta"):
        persist(store, instance(identifier))
        mark_supervisor_ready(queue(store, identifier))
    persist(store, instance("stopped", intent="stopped"))
    queue(store, "stopped")
    components = [
        component_status("mapped-alpha", host_port=4100),
        component_status("mapped-beta", host_port=4101),
    ]
    if provider is not None:
        components.insert(0, provider)
    runtime = FakeRuntime(runtime_status(*components, operational=False))
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    rows = {row["id"]: row for row in result["instances"]}
    assert rows["alpha"]["desired"] == "running"
    assert rows["alpha"]["container"] == "running"
    assert rows["alpha"]["readiness"] == "healthy"
    assert rows["alpha"]["health"] == {
        "state": "provider-down",
        "reason": reason,
    }
    assert rows["beta"]["health"] == rows["alpha"]["health"]
    assert rows["stopped"]["health"]["state"] == "inactive"
    assert result["summary"]["running"] == 2
    assert result["summary"]["provider_issues"] == 1
    assert result["summary"]["attention"] == 2
    assert runtime.calls == 1


def test_snapshot_reports_runtime_status_errors_without_hiding_queues(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    task = root / "tasks" / "saved"
    task.mkdir()
    (task / "state").write_text("open\n", encoding="utf-8")
    runtime = FakeRuntime(CycloError("dcomp state is unreadable"))
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["container"] == "unknown"
    assert row["readiness"] == "unknown"
    assert result["instances"][0]["health"] == {
        "state": "inactive",
        "reason": "instance is not operational",
    }
    assert row["counts"]["tasks"]["total"] == 1
    assert result["runtime"] is None
    assert result["source_errors"] == [
        "runtime status unavailable: dcomp state is unreadable"
    ]
    assert result["summary"]["provider_issues"] == 1
    assert runtime.calls == 1


def test_snapshot_keeps_applied_state_when_host_configuration_is_malformed(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    observed = runtime_status(component_status("gateway"))

    class Runtime:
        @property
        def host(self):
            raise CycloError("host.conf:1: expected provider declaration")

        @staticmethod
        def component_for_instance(identifier: str) -> str:
            return f"mapped-{identifier}"

        @staticmethod
        def status() -> DCompStatus:
            return observed

    result = DashboardSnapshot(
        store,
        runtime=Runtime(),  # type: ignore[arg-type]
    ).build()

    assert result["runtime"]["components"][0]["name"] == "gateway"
    assert result["source_errors"] == [
        "host configuration unavailable: "
        "host.conf:1: expected provider declaration"
    ]
    assert result["summary"]["provider_issues"] == 1


def test_snapshot_exposes_dcomp_component_and_network_problems(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))
    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status(
                "mapped-alpha",
                problem="unexpected image sha256:foreign",
                host_port=4100,
            ),
            operational=False,
            operation="up",
            phase="start",
            networks=(
                DCompNetworkStatus(
                    key="link:mapped-alpha.provider",
                    id="network-id",
                    internal=True,
                    problem="recorded network is absent",
                ),
            ),
        )
    )
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["container"] == "running"
    assert row["readiness"] == "healthy"
    assert row["health"]["state"] == "inactive"
    assert row["errors"] == [
        "runtime component mapped-alpha: unexpected image sha256:foreign"
    ]
    assert result["source_errors"] == [
        "runtime operation in progress: up (start)",
        "runtime network link:mapped-alpha.provider: "
        "recorded network is absent",
    ]
    assert result["runtime"]["components"][1]["problem"] == (
        "unexpected image sha256:foreign"
    )


@pytest.mark.parametrize(
    "container_state",
    [
        "exited",
        "paused",
        "restarting",
    ],
)
def test_snapshot_reports_raw_nonoperational_lifecycle_without_persisting_it(
    tmp_path: Path,
    container_state: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha")
    persist(store, selected)

    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status(
                "mapped-alpha",
                status=container_state,
                health="none",
            ),
            operational=False,
        )
    )
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["desired"] == "running"
    assert row["container"] == container_state
    assert row["readiness"] == "none"
    assert row["health"]["state"] == "inactive"
    assert f"status {container_state}" in row["errors"][0]
    assert result["summary"]["attention"] == 1
    assert store.load("alpha").intent == "running"


@pytest.mark.parametrize("readiness", ["starting", "unhealthy", "missing"])
def test_snapshot_exposes_raw_running_container_readiness(
    tmp_path: Path,
    readiness: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha")
    persist(store, selected)
    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status(
                "mapped-alpha",
                health=readiness,
                host_port=4100,
            ),
            operational=False,
        )
    )

    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["desired"] == "running"
    assert row["container"] == "running"
    assert row["readiness"] == readiness
    assert row["health"]["state"] == "inactive"
    assert row["agentws_port"] is None
    assert result["summary"]["running"] == 1
    assert result["summary"]["attention"] == 1
    assert runtime.calls == 1
    assert runtime.port_reads == []


@pytest.mark.parametrize(
    "container_state",
    ["exited", "dead"],
)
def test_snapshot_reports_raw_present_container_with_stopped_desire(
    tmp_path: Path,
    container_state: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha", intent="stopped")
    persist(store, selected)

    runtime = FakeRuntime(
        runtime_status(
            component_status("gateway"),
            component_status(
                "mapped-alpha",
                status=container_state,
                health="none",
            ),
            operational=False,
        )
    )
    result = DashboardSnapshot(
        store,
        runtime=runtime,  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["desired"] == "stopped"
    assert row["container"] == container_state
    assert row["readiness"] == "none"
    assert row["agentws_port"] is None
    assert result["summary"]["attention"] == 1


def test_snapshot_keeps_stopped_instance_queue_visible(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("retiring", intent="stopped")
    persist(store, selected)
    root = queue(store, selected.id)
    task = root / "tasks" / "saved-task"
    task.mkdir()
    (task / "spec.md").write_text("durable work\n", encoding="utf-8")
    (task / "state").write_text("open\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime(),  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["id"] == selected.id
    assert row["desired"] == "stopped"
    assert row["container"] == "absent"
    assert row["readiness"] == "absent"
    assert row["agentws_port"] is None
    assert row["counts"]["tasks"]["total"] == 1
    assert result["summary"]["instances"] == 1
    assert result["summary"]["attention"] == 0


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
    persist(store, configured)
    mark_supervisor_ready(queue(store, configured.id))

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    assert result["instances"][0]["project"] == {
        "name": "silicon",
        "path": "/configs/silicon",
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
        runtime=healthy_runtime("good", "broken"),  # type: ignore[arg-type]
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
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
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


def test_snapshot_rejects_empty_or_non_string_project_definition_state(
    tmp_path: Path,
) -> None:
    empty = instance("empty")
    empty.project_file = "/configs/empty/project.cyclo"
    empty.project_name = "empty-project"
    empty.project_description = "Missing mounts"
    empty.project_generation = "empty-generation"
    empty.project_mounts = []
    malformed = instance("malformed")
    malformed.project_file = {"unexpected": True}  # type: ignore[assignment]
    store = InMemoryInstanceStore(tmp_path / "state", [empty, malformed])
    mark_supervisor_ready(queue(store, empty.id))
    mark_supervisor_ready(queue(store, malformed.id))

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("empty", "malformed"),  # type: ignore[arg-type]
    ).build()

    by_id = {row["id"]: row for row in result["instances"]}
    assert any("must contain at least one mount" in error for error in by_id["empty"]["errors"])
    assert by_id["malformed"]["project"]["workspaces"] == [
        {
            "name": "source",
            "path": "/projects/malformed",
            "container_path": "/workspace/source",
        }
    ]
    assert any("project_file must be a string" in error for error in by_id["malformed"]["errors"])


def test_snapshot_counts_queue_errors_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    assert result["summary"]["attention"] == 1
    assert result["summary"]["errors"] == 1
    assert result["instances"][0]["errors"]


def test_snapshot_preserves_fleet_visibility_without_a_runtime_reader(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha")
    persist(store, selected)
    mark_supervisor_ready(queue(store, selected.id))

    result = DashboardSnapshot(store).build()

    row = result["instances"][0]
    assert row["container"] == "unknown"
    assert row["readiness"] == "unknown"
    assert row["health"] == {
        "state": "inactive",
        "reason": "instance is not operational",
    }
    assert result["summary"]["provider_issues"] == 1
    assert result["source_errors"] == [
        "runtime status unavailable: runtime is not configured"
    ]


def test_snapshot_reports_bad_instance_records_without_hiding_readable_ones(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    mark_supervisor_ready(queue(store, "alpha"))
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json\n", encoding="utf-8")

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    assert [row["id"] for row in result["instances"]] == ["alpha"]
    assert len(result["source_errors"]) == 1
    assert str(broken) in result["source_errors"][0]
    assert "invalid Cyclo instance state" in result["source_errors"][0]
    assert result["summary"]["errors"] == 1


def test_snapshot_counts_unknown_job_status_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    job = root / "jobs" / "job-without-status"
    job.mkdir()

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["counts"]["jobs"]["unknown"] == 1
    assert any("unknown or unreadable status" in error for error in row["errors"])
    assert result["summary"]["attention"] == 1


def test_snapshot_counts_unknown_task_state_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("alpha"))
    root = queue(store, "alpha")
    mark_supervisor_ready(root)
    task = root / "tasks" / "task-without-state"
    task.mkdir()

    result = DashboardSnapshot(
        store,
        runtime=healthy_runtime("alpha"),  # type: ignore[arg-type]
    ).build()

    row = result["instances"][0]
    assert row["counts"]["tasks"]["unknown"] == 1
    assert any("unknown or unreadable state" in error for error in row["errors"])
    assert result["summary"]["attention"] == 1


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
