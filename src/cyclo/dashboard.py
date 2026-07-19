from __future__ import annotations

import ipaddress
import json
import mimetypes
import socket
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .agentws_queue import (
    JOB_STATUSES,
    QueueLimits,
    empty_counts as _empty_counts,
    read_agent_supervisor_status,
    scan_agentws_queue,
)
from .docker import Docker
from .errors import CycloError
from .health import (
    INACTIVE_TEAM_HEALTH,
    RuntimeHealth,
    RuntimeStatusReader,
    read_runtime_health,
    team_health,
)
from .project_state import decode_instance_project
from .state import DEFAULT_AGENTWS_HOST, Instance, StateStore, utc_now


API_VERSION = 2
DEFAULT_DASHBOARD_HOST = DEFAULT_AGENTWS_HOST
DEFAULT_DASHBOARD_PORT = 0


class UsageReader(Protocol):
    def usage(self) -> dict[str, object]: ...


def _safe_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        return 0
    return int(value)


def _usage_counters(value: object) -> dict[str, int]:
    data = value if isinstance(value, dict) else {}
    input_tokens = _safe_number(data.get("input_tokens"))
    output_tokens = _safe_number(data.get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "requests": _safe_number(data.get("requests")),
    }


def _instance_state(instance: Instance, running: bool | None) -> str:
    if running is None:
        return "unknown"
    if running and instance.active:
        return "running"
    if running:
        return "orphan"
    if instance.active:
        return "stale"
    return "stopped"


def _project_metadata(
    instance: Instance,
) -> tuple[dict[str, object], list[str]]:
    """Return a stable dashboard shape without trusting persisted metadata."""

    project = decode_instance_project(instance)
    return project.dashboard_value(), [
        f"invalid project metadata: {error}" for error in project.errors
    ]


class DashboardSnapshot:
    """Build a best-effort, read-only host view of all Cyclo instances."""

    def __init__(
        self,
        store: StateStore,
        *,
        docker: Docker | None = None,
        usage_reader: UsageReader | None = None,
        runtime_reader: RuntimeStatusReader | None = None,
        queue_limits: QueueLimits | None = None,
    ) -> None:
        self.store = store
        self.docker = docker or Docker()
        self.usage_reader = usage_reader
        self.runtime_reader = runtime_reader
        self.queue_limits = queue_limits or QueueLimits()

    def _gateway_usage(self) -> tuple[dict[str, object], str | None]:
        if self.usage_reader is None:
            return {}, None
        try:
            usage = self.usage_reader.usage()
        except Exception as exc:
            return {}, f"gateway usage unavailable: {exc}"
        if not isinstance(usage, dict):
            return {}, "gateway usage unavailable: response is not an object"
        return usage, None

    def build(self) -> dict[str, object]:
        gateway_usage, usage_error = self._gateway_usage()
        by_client_value = gateway_usage.get("by_client")
        by_client = by_client_value if isinstance(by_client_value, dict) else {}
        rows: list[dict[str, object]] = []
        shared_runtime_health: RuntimeHealth | None = None
        instances, instance_state_errors = self.store.list_report()

        for instance in instances:
            errors: list[str] = []
            try:
                running: bool | None = self.docker.container_running(instance.container_name)
            except Exception as exc:
                running = None
                errors.append(f"Docker status unavailable: {exc}")
            state = _instance_state(instance, running)
            if state == "running":
                if shared_runtime_health is None:
                    shared_runtime_health = read_runtime_health(self.runtime_reader)
                try:
                    supervisor = read_agent_supervisor_status(
                        self.store.queue_root(instance.id)
                    )
                    suspended_agents = supervisor.suspended_agents
                    planner_attention_jobs = supervisor.planner_attention_jobs
                    supervisor_error = supervisor.error
                except Exception as exc:
                    suspended_agents = ()
                    planner_attention_jobs = ()
                    supervisor_error = str(exc)
                health = team_health(
                    shared_runtime_health,
                    suspended_agents,
                    supervisor_error,
                    planner_attention_jobs,
                )
            else:
                health = INACTIVE_TEAM_HEALTH
            try:
                queue = scan_agentws_queue(
                    self.store.queue_root(instance.id), limits=self.queue_limits
                )
            except Exception as exc:
                queue = {
                    "counts": _empty_counts(),
                    "recent_tasks": [],
                    "recent_activity": [],
                    "errors": [f"queue status unavailable: {exc}"],
                }
            queue_errors = queue.get("errors")
            if isinstance(queue_errors, list):
                errors.extend(str(item) for item in queue_errors)
            usage = _usage_counters(by_client.get(instance.id))
            agentws_port = None
            if running and not instance.offline and instance.port:
                agentws_port = instance.port
            try:
                project, project_errors = _project_metadata(instance)
            except Exception as exc:
                project = {
                    "name": "",
                    "path": "",
                    "definition": None,
                    "description": "",
                    "generation": "",
                    "workspaces": [],
                    "read_only_mounts": [],
                }
                project_errors = [f"invalid project metadata: {exc}"]
            errors.extend(project_errors)
            rows.append(
                {
                    "id": instance.id,
                    "team": instance.team_name,
                    "project": project,
                    "state": state,
                    "health": health.api_value(),
                    "mode": {
                        "offline": instance.offline,
                        "team_write": instance.team_write,
                    },
                    "generation": instance.generation,
                    "agentws_port": agentws_port,
                    "counts": queue["counts"],
                    "usage": usage,
                    "recent_tasks": queue["recent_tasks"],
                    "recent_activity": queue["recent_activity"],
                    "errors": list(dict.fromkeys(errors)),
                }
            )

        state_priority = {"running": 0, "stale": 1, "orphan": 2, "unknown": 3, "stopped": 4}
        rows.sort(key=lambda item: (state_priority.get(str(item["state"]), 9), str(item["id"])))
        source_errors = [*instance_state_errors]
        if usage_error:
            source_errors.append(usage_error)
        instance_error_count = sum(len(item["errors"]) for item in rows)  # type: ignore[arg-type]
        summary = {
            "instances": len(rows),
            "running": sum(1 for item in rows if item["state"] == "running"),
            "runtime_issues": int(
                shared_runtime_health is not None
                and shared_runtime_health.state.startswith("runtime-")
            ),
            "attention": sum(
                1
                for item in rows
                if item["state"] in {"stale", "orphan", "unknown"}
                or str(item["health"]["state"]).startswith("runtime-")  # type: ignore[index]
                or str(item["health"]["state"]).startswith("agents-")  # type: ignore[index]
                or item["counts"]["jobs"]["failed"] > 0  # type: ignore[index]
                or bool(item["errors"])
            ),
            "tasks": sum(item["counts"]["tasks"]["total"] for item in rows),  # type: ignore[index]
            "jobs": sum(item["counts"]["jobs"]["total"] for item in rows),  # type: ignore[index]
            "agents": sum(item["counts"]["agents"]["total"] for item in rows),  # type: ignore[index]
            "tokens": sum(item["usage"]["total_tokens"] for item in rows),  # type: ignore[index]
            "requests": sum(item["usage"]["requests"] for item in rows),  # type: ignore[index]
            "errors": len(source_errors) + instance_error_count,
        }
        return {
            "version": API_VERSION,
            "generated_at": utc_now(),
            "summary": summary,
            "source_errors": source_errors,
            "instances": rows,
        }

    __call__ = build


StaticAsset = str | bytes | tuple[str, str | bytes]


DEFAULT_INDEX = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Cyclo</title><body><h1>Cyclo</h1><p>The dashboard UI is not installed.</p></body></html>
"""


def packaged_dashboard_assets() -> dict[str, StaticAsset]:
    """Load the fixed, package-owned dashboard asset set.

    Route names are intentionally not derived from request paths: this keeps the
    server a tiny fixed-function observer rather than a general file server.
    """

    package = resources.files("cyclo.dashboard_static")
    return {
        "/": ("text/html; charset=utf-8", package.joinpath("index.html").read_bytes()),
        "/static/styles.css": (
            "text/css; charset=utf-8",
            package.joinpath("styles.css").read_bytes(),
        ),
        "/static/app.js": (
            "application/javascript; charset=utf-8",
            package.joinpath("app.js").read_bytes(),
        ),
    }


def _normalize_assets(assets: Mapping[str, StaticAsset] | None) -> dict[str, tuple[str, bytes]]:
    source: dict[str, StaticAsset] = {"/": ("text/html; charset=utf-8", DEFAULT_INDEX)}
    if assets:
        source.update(assets)
    result: dict[str, tuple[str, bytes]] = {}
    for route, asset in source.items():
        parsed = urlsplit(route)
        if (
            not route.startswith("/")
            or parsed.path != route
            or parsed.query
            or parsed.fragment
            or route.startswith("/api/")
            or ".." in Path(route).parts
        ):
            raise ValueError(f"invalid dashboard asset route: {route!r}")
        if isinstance(asset, tuple):
            content_type, body = asset
        else:
            content_type = mimetypes.guess_type(route)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            body = asset
        result[route] = (content_type, body.encode("utf-8") if isinstance(body, str) else body)
    return result


def _dashboard_host_addresses(host: str) -> set[str]:
    if not isinstance(host, str) or not host or host != host.strip():
        raise CycloError("dashboard host must be a non-empty address or hostname")
    try:
        addresses = {
            item[4][0].split("%", 1)[0]
            for item in socket.getaddrinfo(
                host,
                None,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise CycloError(f"cannot resolve dashboard host {host!r}: {exc}") from exc
    if not addresses:
        raise CycloError(f"cannot resolve dashboard host {host!r}")
    return addresses


def validate_dashboard_host(host: str) -> None:
    """Require a bindable IPv4 host; callers decide whether exposure is safe."""

    _dashboard_host_addresses(host)


def dashboard_host_is_loopback(host: str) -> bool:
    return all(
        ipaddress.ip_address(value).is_loopback
        for value in _dashboard_host_addresses(host)
    )


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        snapshot: Callable[[], dict[str, object]],
        assets: Mapping[str, StaticAsset] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.assets = _normalize_assets(assets)
        super().__init__(server_address, DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    server_version = "CycloDashboard/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _headers(self, status_code: int, content_type: str, length: int) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _send(self, status_code: int, content_type: str, body: bytes, *, head: bool) -> None:
        self._headers(status_code, content_type, len(body))
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Browsers routinely cancel an obsolete refresh while the next
                # snapshot starts.  That is not a dashboard server failure.
                return

    def _json(self, status_code: int, payload: object, *, head: bool) -> None:
        body = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        self._send(status_code, "application/json; charset=utf-8", body, head=head)

    def _read(self, *, head: bool) -> None:
        route = urlsplit(self.path).path
        if route == "/api/health":
            self._json(200, {"ok": True, "version": API_VERSION}, head=head)
            return
        if route == "/api/snapshot":
            try:
                payload = self.server.snapshot()
            except Exception:
                self._json(500, {"error": "dashboard snapshot unavailable"}, head=head)
            else:
                self._json(200, payload, head=head)
            return
        asset = self.server.assets.get(route)
        if asset is None:
            self._json(404, {"error": "not found"}, head=head)
            return
        content_type, body = asset
        self._send(200, content_type, body, head=head)

    def do_GET(self) -> None:
        self._read(head=False)

    def do_HEAD(self) -> None:
        self._read(head=True)

    def _read_only(self) -> None:
        body = b'{"error":"read-only dashboard"}\n'
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only


def make_dashboard_server(
    snapshot: Callable[[], dict[str, object]],
    *,
    host: str = DEFAULT_DASHBOARD_HOST,
    port: int = DEFAULT_DASHBOARD_PORT,
    static_assets: Mapping[str, StaticAsset] | None = None,
) -> DashboardHTTPServer:
    validate_dashboard_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise CycloError("dashboard port must be between 0 and 65535")
    try:
        return DashboardHTTPServer((host, port), snapshot, static_assets)
    except OSError as exc:
        raise CycloError(f"cannot start Cyclo dashboard on {host}:{port}: {exc}") from exc
