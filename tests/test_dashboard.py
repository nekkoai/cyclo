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

from cyclo.dashboard import (
    DashboardSnapshot,
    QueueLimits,
    dashboard_host_is_loopback,
    make_dashboard_server,
    packaged_dashboard_assets,
    scan_agentws_queue,
    validate_dashboard_host,
)
from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore


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
        project_read_only=False,
        offline=offline,
        active=active,
        port=4100 if not offline else None,
    )


def queue(store: StateStore, identifier: str) -> Path:
    root = store.queue_root(identifier)
    for name in ("tasks", "jobs", "agents"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


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
            "by_client": {
                "alpha": {"requests": 3, "input_tokens": 100, "output_tokens": 25},
                "old": {"requests": 1, "input_tokens": 7, "output_tokens": 2},
            }
        }


def test_snapshot_joins_state_docker_queue_and_usage(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    alpha = instance("alpha")
    old = instance("old", active=False, offline=True)
    store.save(alpha)
    store.save(old)
    alpha_queue = queue(store, "alpha")
    queue(store, "old")
    task = alpha_queue / "tasks" / "work"
    task.mkdir()
    job = alpha_queue / "jobs" / "job"
    job.mkdir()
    (job / "status").write_text("failed\n", encoding="utf-8")
    (alpha_queue / "agents" / "worker").mkdir()

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True, "cyclo-old": False}),  # type: ignore[arg-type]
        usage_reader=FakeUsage(),
    ).build()

    assert result["version"] == 1
    assert result["summary"] == {
        "instances": 2,
        "running": 1,
        "attention": 1,
        "tasks": 1,
        "jobs": 1,
        "agents": 1,
        "tokens": 134,
        "requests": 4,
        "errors": 0,
    }
    assert result["source_errors"] == []
    running, stopped = result["instances"]
    assert running["id"] == "alpha"
    assert running["state"] == "running"
    assert running["agentws_url"] == "http://127.0.0.1:4100/"
    assert running["usage"] == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "requests": 3,
    }
    assert stopped["state"] == "stopped"
    assert stopped["agentws_url"] is None


def test_snapshot_counts_queue_errors_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
    ).build()

    assert result["summary"]["attention"] == 1
    assert result["summary"]["errors"] == 1
    assert result["instances"][0]["errors"]


def test_snapshot_counts_unknown_job_status_as_attention(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(instance("alpha"))
    root = queue(store, "alpha")
    job = root / "jobs" / "job-without-status"
    job.mkdir()

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
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
    queue(store, "alpha")

    result = DashboardSnapshot(
        store,
        docker=FakeDocker({"cyclo-alpha": True}),  # type: ignore[arg-type]
        usage_reader=BrokenUsage(),
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
