from __future__ import annotations

import ipaddress
import json
import mimetypes
import socket
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from .team.queue import (
    JOB_STATUSES,
    QueueLimits,
    empty_counts as _empty_counts,
    read_agent_supervisor_status,
    scan_agentws_queue,
)
from .dcomp import DCompComponentStatus, DCompStatus
from .errors import CycloError
from .project_state import decode_instance_project
from .runtime import CycloRuntime
from .state import Instance, StateStore, utc_now


API_VERSION = 4
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 0


def _project_metadata(
    instance: Instance,
) -> tuple[dict[str, object], list[str]]:
    """Return a stable dashboard shape without trusting persisted metadata."""

    project = decode_instance_project(instance)
    return project.dashboard_value(), [
        f"invalid project metadata: {error}" for error in project.errors
    ]


def _error_summary(exc: Exception) -> str:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return detail if len(detail) <= 240 else detail[:237] + "..."


def _component_issue(component: DCompComponentStatus) -> str:
    details: list[str] = []
    if component.problem:
        details.append(component.problem)
    if component.status != "running":
        state = component.status or "unknown"
        if component.exit_code:
            state += f" (exit {component.exit_code})"
        details.append(f"status {state}")
    if component.health != "healthy":
        details.append(f"health {component.health or 'unknown'}")
    return "; ".join(dict.fromkeys(details))


def _health(state: str, reason: str = "") -> dict[str, str]:
    return {"state": state, "reason": reason}


def _with_agent_health(
    provider: dict[str, str],
    suspended_agents: tuple[str, ...],
    supervisor_error: str,
    planner_attention_jobs: tuple[str, ...],
) -> dict[str, str]:
    agents = tuple(sorted(set(suspended_agents)))
    attention = tuple(sorted(set(planner_attention_jobs)))
    details: list[str] = []
    if agents:
        shown = ", ".join(agents[:5])
        if len(agents) > 5:
            shown += f", +{len(agents) - 5} more"
        noun = "agent" if len(agents) == 1 else "agents"
        details.append(f"{len(agents)} {noun} suspended: {shown}")
    if attention:
        shown = ", ".join(attention[:5])
        if len(attention) > 5:
            shown += f", +{len(attention) - 5} more"
        noun = "failure" if len(attention) == 1 else "failures"
        details.append(f"{len(attention)} unresolved planner {noun}: {shown}")

    if supervisor_error:
        details.insert(
            0,
            f"AgentWS supervisor status unavailable: {supervisor_error}",
        )
        agent_state = "agents-unknown"
    elif agents:
        agent_state = "agents-suspended"
    elif attention:
        agent_state = "agents-attention"
    else:
        return provider

    reasons = [
        value
        for value in (provider["reason"], "; ".join(details))
        if value
    ]
    return _health(
        agent_state if provider["state"] == "ready" else provider["state"],
        "; ".join(reasons),
    )


def _provider_component_names(runtime: CycloRuntime) -> set[str]:
    return {
        "gateway",
        *(provider.name for provider in runtime.host.providers),
    }


def _provider_health(
    runtime: CycloRuntime | None,
    status: DCompStatus | None,
) -> tuple[dict[str, str], list[str]]:
    if runtime is None or status is None:
        return _health("provider-unknown", "runtime status unavailable"), []

    try:
        names = _provider_component_names(runtime)
        outer_name = runtime.host.outer_component
    except CycloError as exc:
        issue = f"host configuration unavailable: {_error_summary(exc)}"
        gateway = status.component("gateway")
        if gateway is not None:
            gateway_issue = _component_issue(gateway)
            if gateway_issue:
                issue += f"; gateway: {gateway_issue}"
        return _health("provider-unknown", issue), [issue]
    issues: list[str] = []
    by_name = {
        component.name: component
        for component in status.components
        if component.name in names
    }
    for name in sorted(names):
        component = by_name.get(name)
        if component is None:
            issues.append(f"component {name}: absent from runtime status")
            continue
        problem = _component_issue(component)
        if problem:
            issues.append(f"component {name}: {problem}")

    outer = by_name.get(outer_name)
    if outer is None:
        return (
            _health(
                "provider-down",
                f"outer provider component {outer_name} is absent",
            ),
            issues,
        )
    outer_issue = _component_issue(outer)
    if outer_issue:
        return (
            _health("provider-down", f"{outer_name}: {outer_issue}"),
            issues,
        )

    optional = [
        issue
        for issue in issues
        if not issue.startswith(f"component {outer_name}:")
    ]
    reason = (
        "unavailable optional provider components: " + ", ".join(optional)
        if optional
        else ""
    )
    return _health("ready", reason), issues


def _runtime_value(status: DCompStatus | None) -> dict[str, object] | None:
    if status is None:
        return None
    return {
        "name": status.name,
        "desired": status.desired,
        "operational": status.operational,
        "digest": status.digest,
        "operation": status.operation,
        "phase": status.phase,
        "networks": [
            {
                "key": network.key,
                "internal": network.internal,
                "problem": network.problem,
            }
            for network in status.networks
        ],
        "components": [
            {
                "name": component.name,
                "status": component.status,
                "health": component.health,
                "exit_code": component.exit_code,
                "problem": component.problem,
                "published_ports": [
                    {
                        "protocol": port.protocol,
                        "host_ip": port.host_ip,
                        "host_port": port.host_port,
                        "container_port": port.container_port,
                    }
                    for port in component.published_ports
                ],
            }
            for component in status.components
        ],
    }


class DashboardSnapshot:
    """Build a best-effort, read-only host view of all Cyclo instances."""

    def __init__(
        self,
        store: StateStore,
        *,
        runtime: CycloRuntime | None = None,
        queue_limits: QueueLimits | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.queue_limits = queue_limits or QueueLimits()

    def build(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        instances, instance_state_errors = self.store.list_report()
        source_errors = [*instance_state_errors]
        runtime_status: DCompStatus | None = None
        if self.runtime is None:
            source_errors.append("runtime status unavailable: runtime is not configured")
        else:
            try:
                runtime_status = self.runtime.status()
            except Exception as exc:
                source_errors.append(
                    f"runtime status unavailable: {_error_summary(exc)}"
                )

        provider_health, provider_issues = _provider_health(
            self.runtime,
            runtime_status,
        )
        source_errors.extend(provider_issues)
        if runtime_status is not None:
            if runtime_status.operation:
                phase = runtime_status.phase or "unknown"
                source_errors.append(
                    "runtime operation in progress: "
                    f"{runtime_status.operation} ({phase})"
                )
            source_errors.extend(
                f"runtime network {network.key}: {network.problem}"
                for network in runtime_status.networks
                if network.problem
            )

        for instance in instances:
            queue_root = self.store.queue_root(instance.id)
            errors: list[str] = []
            desired = instance.intent
            component_name = ""
            component: DCompComponentStatus | None = None
            if self.runtime is not None:
                try:
                    component_name = self.runtime.component_for_instance(
                        instance.id
                    )
                except Exception as exc:
                    errors.append(
                        "runtime component mapping unavailable: "
                        f"{_error_summary(exc)}"
                    )
            if runtime_status is not None and component_name:
                component = runtime_status.component(component_name)
            if component is None:
                container_state = "absent" if runtime_status is not None else "unknown"
                readiness = "absent" if runtime_status is not None else "unknown"
                ready = False
                if desired == "running" and runtime_status is not None:
                    errors.append(
                        f"runtime component {component_name or instance.id} is absent"
                    )
            else:
                container_state = component.status or "unknown"
                readiness = component.health or "unknown"
                component_problem = _component_issue(component)
                if component_problem:
                    errors.append(
                        f"runtime component {component.name}: {component_problem}"
                    )
                ready = not component_problem
            operational = desired == "running" and ready
            if operational:
                try:
                    supervisor = read_agent_supervisor_status(queue_root)
                    suspended_agents = supervisor.suspended_agents
                    planner_attention_jobs = supervisor.planner_attention_jobs
                    supervisor_error = supervisor.error
                except Exception as exc:
                    suspended_agents = ()
                    planner_attention_jobs = ()
                    supervisor_error = str(exc)
                health = _with_agent_health(
                    provider_health,
                    suspended_agents,
                    supervisor_error,
                    planner_attention_jobs,
                )
            else:
                health = _health("inactive", "instance is not operational")
            try:
                queue = scan_agentws_queue(
                    queue_root, limits=self.queue_limits
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
            agentws_port = None
            if operational and not instance.offline:
                try:
                    if self.runtime is None or runtime_status is None:
                        raise CycloError("runtime status unavailable")
                    agentws_port = self.runtime.team_port(
                        instance, runtime_status
                    )
                except Exception as exc:
                    errors.append(
                        f"AgentWS port unavailable: {_error_summary(exc)}"
                    )
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
                    "desired": desired,
                    "container": container_state,
                    "readiness": readiness,
                    "health": health,
                    "mode": {
                        "offline": instance.offline,
                        "team_write": instance.team_write,
                    },
                    "generation": instance.generation,
                    "agentws_port": agentws_port,
                    "counts": queue["counts"],
                    "recent_tasks": queue["recent_tasks"],
                    "recent_activity": queue["recent_activity"],
                    "errors": list(dict.fromkeys(errors)),
                }
            )

        def lifecycle_operational(item: dict[str, object]) -> bool:
            return (
                item["desired"] == "running"
                and item["container"] == "running"
                and item["readiness"] == "healthy"
            )

        def lifecycle_settled(item: dict[str, object]) -> bool:
            return lifecycle_operational(item) or (
                item["desired"] == "stopped"
                and item["container"] == "absent"
            )

        def lifecycle_rank(item: dict[str, object]) -> int:
            if lifecycle_operational(item):
                return 0
            return {
                "running": 1,
                "absent": 2,
                "stopped": 3,
            }.get(str(item["desired"]), 4)

        rows.sort(
            key=lambda item: (
                lifecycle_rank(item),
                str(item["id"]),
            )
        )
        source_errors = list(dict.fromkeys(source_errors))
        instance_error_count = sum(len(item["errors"]) for item in rows)  # type: ignore[arg-type]
        summary = {
            "instances": len(rows),
            "running": sum(
                1
                for item in rows
                if item["container"] == "running"
            ),
            "provider_issues": len(provider_issues)
            + int(self.runtime is None or runtime_status is None),
            "attention": sum(
                1
                for item in rows
                if not lifecycle_settled(item)
                or str(item["health"]["state"]).startswith("provider-")  # type: ignore[index]
                or str(item["health"]["state"]).startswith("agents-")  # type: ignore[index]
                or (
                    item["health"]["state"] == "ready"  # type: ignore[index]
                    and bool(item["health"]["reason"])  # type: ignore[index]
                )
                or item["counts"]["jobs"]["failed"] > 0  # type: ignore[index]
                or bool(item["errors"])
            ),
            "tasks": sum(item["counts"]["tasks"]["total"] for item in rows),  # type: ignore[index]
            "jobs": sum(item["counts"]["jobs"]["total"] for item in rows),  # type: ignore[index]
            "agents": sum(item["counts"]["agents"]["total"] for item in rows),  # type: ignore[index]
            "errors": len(source_errors) + instance_error_count,
        }
        return {
            "version": API_VERSION,
            "generated_at": utc_now(),
            "summary": summary,
            "source_errors": source_errors,
            "runtime": _runtime_value(runtime_status),
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
