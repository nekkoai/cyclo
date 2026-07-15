from __future__ import annotations

import errno
import ipaddress
import json
import mimetypes
import os
import re
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .docker import Docker
from .errors import CycloError
from .state import DEFAULT_AGENTWS_HOST, Instance, StateStore, utc_now


API_VERSION = 1
DEFAULT_DASHBOARD_HOST = DEFAULT_AGENTWS_HOST
DEFAULT_DASHBOARD_PORT = 0
SAFE_QUEUE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
KNOWN_JOB_STATUSES = ("pending", "claimed", "running", "done", "failed")
JOB_STATUSES = (*KNOWN_JOB_STATUSES, "unknown")
_OPEN_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_FILE = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


class UsageReader(Protocol):
    def usage(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class QueueLimits:
    """Hard limits for one dashboard queue scan.

    The scanner only visits direct AgentWS task/job/agent entries. These limits
    prevent a corrupt or hostile queue from turning a dashboard refresh into an
    unbounded filesystem walk.
    """

    max_entries: int = 4096
    max_read_bytes: int = 2 * 1024 * 1024
    recent_tasks: int = 8
    recent_activity: int = 12

    def __post_init__(self) -> None:
        for name in ("max_entries", "max_read_bytes", "recent_tasks", "recent_activity"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass
class _ScanBudget:
    limits: QueueLimits
    entries: int = 0
    read_bytes: int = 0
    truncated: bool = False

    def take_entry(self) -> bool:
        if self.entries >= self.limits.max_entries:
            self.truncated = True
            return False
        self.entries += 1
        return True

    def read_allowance(self, requested: int) -> int:
        remaining = self.limits.max_read_bytes - self.read_bytes
        if remaining <= 0:
            self.truncated = True
            return 0
        return min(requested, remaining)


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


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


def _open_directory(path: Path) -> int:
    # O_NOFOLLOW only protects the final component when passed a full path.
    # Walk from an already-open root so a swapped parent directory cannot turn
    # a queue refresh into a read outside the requested tree.
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", _OPEN_DIRECTORY | _OPEN_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            replacement = _open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = replacement
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, _OPEN_DIRECTORY | _OPEN_NOFOLLOW, dir_fd=parent_fd)


def _read_regular_at(
    directory_fd: int,
    name: str,
    budget: _ScanBudget,
    *,
    max_bytes: int,
    default: str = "",
) -> str:
    allowance = budget.read_allowance(max_bytes)
    if allowance == 0:
        return default
    try:
        descriptor = os.open(name, _OPEN_FILE | _OPEN_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        return default
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return default
        raw = os.read(descriptor, allowance)
        budget.read_bytes += len(raw)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return default
    finally:
        os.close(descriptor)


def _regular_file_at(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _latest_regular_mtime_at(directory_fd: int, baseline: float, names: tuple[str, ...]) -> float:
    """Return the latest safe file mtime without following mutable queue links."""

    result = baseline
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            result = max(result, info.st_mtime)
    return result


def _first_title(value: str, fallback: str) -> str:
    for line in value.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title[:160]
    for paragraph in value.split("\n\n"):
        title = " ".join(paragraph.split())
        if title:
            return title[:160]
    return fallback


def _iter_queue_directories(
    parent_fd: int,
    budget: _ScanBudget,
    errors: list[str],
):
    try:
        entries = os.scandir(parent_fd)
    except OSError as exc:
        errors.append(f"cannot list queue directory: {exc.strerror or exc}")
        return
    with entries:
        for entry in entries:
            if not budget.take_entry():
                return
            if not SAFE_QUEUE_ID.fullmatch(entry.name) or entry.name in {".", ".."}:
                errors.append("ignored an unsafe queue entry name")
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child_fd = _open_directory_at(parent_fd, entry.name)
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                    errors.append(f"cannot inspect queue entry {entry.name}: {exc.strerror or exc}")
                continue
            try:
                yield entry.name, child_fd, os.fstat(child_fd)
            finally:
                os.close(child_fd)


def _open_category(root_fd: int, name: str, errors: list[str]) -> int | None:
    try:
        return _open_directory_at(root_fd, name)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
            errors.append(f"AgentWS queue directory is unavailable: {name}")
        else:
            errors.append(f"cannot open AgentWS queue directory {name}: {exc.strerror or exc}")
        return None


def scan_agentws_queue(root: Path, limits: QueueLimits | None = None) -> dict[str, object]:
    """Read aggregate AgentWS queue data without following queue symlinks.

    Every directory and file is opened relative to an already-open descriptor
    with ``O_NOFOLLOW``. The scan is shallow, entry-bounded and byte-bounded.
    Files that disappear during the scan are treated as concurrent queue churn.
    """

    limits = limits or QueueLimits()
    budget = _ScanBudget(limits)
    errors: list[str] = []
    task_rows: list[dict[str, object]] = []
    job_rows: list[dict[str, object]] = []
    agent_ids: set[str] = set()

    try:
        root_fd = _open_directory(root)
    except OSError as exc:
        return {
            "counts": _empty_counts(),
            "recent_tasks": [],
            "recent_activity": [],
            "errors": [f"AgentWS queue is unavailable: {exc.strerror or exc}"],
        }

    try:
        tasks_fd = _open_category(root_fd, "tasks", errors)
        if tasks_fd is not None:
            try:
                for task_id, task_fd, info in _iter_queue_directories(tasks_fd, budget, errors):
                    raw_state = _read_regular_at(task_fd, "state", budget, max_bytes=128, default="open")
                    state = raw_state.strip().lower() or "open"
                    closed = state != "open" or _regular_file_at(task_fd, "result.md")
                    updated = _latest_regular_mtime_at(
                        task_fd,
                        info.st_mtime,
                        ("state", "spec.md", "log.md", "result.md"),
                    )
                    task_rows.append(
                        {
                            "id": task_id,
                            "state": "closed" if closed else "open",
                            "updated_ts": updated,
                        }
                    )
            finally:
                os.close(tasks_fd)

        jobs_fd = _open_category(root_fd, "jobs", errors)
        if jobs_fd is not None:
            try:
                for job_id, job_fd, info in _iter_queue_directories(jobs_fd, budget, errors):
                    status_value = _read_regular_at(
                        job_fd, "status", budget, max_bytes=64, default="unknown"
                    )
                    task_id = _read_regular_at(job_fd, "task-id", budget, max_bytes=128).strip()
                    agent_id = _read_regular_at(job_fd, "agent-id", budget, max_bytes=128).strip()
                    status_value = status_value.strip().lower() or "unknown"
                    if status_value not in KNOWN_JOB_STATUSES:
                        status_value = "unknown"
                    updated = _latest_regular_mtime_at(
                        job_fd,
                        info.st_mtime,
                        ("status", "log.md", "spec.md", "agent-id", "task-id", "role"),
                    )
                    job_rows.append(
                        {
                            "id": job_id,
                            "status": status_value,
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "updated_ts": updated,
                        }
                    )
            finally:
                os.close(jobs_fd)

        agents_fd = _open_category(root_fd, "agents", errors)
        if agents_fd is not None:
            try:
                for agent_id, _agent_fd, _info in _iter_queue_directories(
                    agents_fd, budget, errors
                ):
                    # Hidden AgentWS bookkeeping directories are not agents.
                    if not agent_id.startswith("."):
                        agent_ids.add(agent_id)
            finally:
                os.close(agents_fd)

        recent_tasks = [
            dict(item)
            for item in sorted(
                task_rows, key=lambda item: (-float(item["updated_ts"]), str(item["id"]))
            )[: limits.recent_tasks]
        ]
        # Titles are presentation-only, so read them after the status/count scan.
        if recent_tasks:
            tasks_fd = _open_category(root_fd, "tasks", errors)
            if tasks_fd is not None:
                try:
                    for task in recent_tasks:
                        task_id = str(task["id"])
                        try:
                            task_fd = _open_directory_at(tasks_fd, task_id)
                        except OSError:
                            task["title"] = task_id
                            continue
                        try:
                            spec = _read_regular_at(
                                task_fd, "spec.md", budget, max_bytes=4096
                            )
                            task["title"] = _first_title(spec, task_id)
                        finally:
                            os.close(task_fd)
                finally:
                    os.close(tasks_fd)
        for task in recent_tasks:
            task["updated_at"] = _timestamp(float(task.pop("updated_ts")))

        activity: list[dict[str, object]] = [
            {
                "kind": "task",
                "id": str(task["id"]),
                "state": str(task["state"]),
                "updated_ts": float(task["updated_ts"]),
            }
            for task in task_rows
        ]
        activity.extend(
            {
                "kind": "job",
                "id": str(job["id"]),
                "status": str(job["status"]),
                "task_id": str(job["task_id"]),
                "agent": str(job["agent_id"]),
                "updated_ts": float(job["updated_ts"]),
            }
            for job in job_rows
        )
        recent_activity = sorted(
            activity, key=lambda item: (-float(item["updated_ts"]), str(item["id"]))
        )[: limits.recent_activity]
        for item in recent_activity:
            item["updated_at"] = _timestamp(float(item.pop("updated_ts")))

        if budget.truncated:
            errors.append(
                f"queue scan truncated at {limits.max_entries} entries or "
                f"{limits.max_read_bytes} bytes"
            )

        task_open = sum(1 for item in task_rows if item["state"] == "open")
        statuses = {name: 0 for name in JOB_STATUSES}
        for item in job_rows:
            value = item["status"]
            if value in statuses:
                statuses[str(value)] += 1
        if statuses["unknown"]:
            count = statuses["unknown"]
            errors.append(
                f"{count} job{'s have' if count != 1 else ' has'} an unknown or unreadable status"
            )
        active_agent_ids = {
            str(item["agent_id"])
            for item in job_rows
            if item["status"] in {"claimed", "running"} and item["agent_id"]
        }
        counts = {
            "tasks": {
                "total": len(task_rows),
                "open": task_open,
                "closed": len(task_rows) - task_open,
            },
            "jobs": {"total": len(job_rows), **statuses},
            "agents": {
                "total": len(agent_ids),
                "active": len(agent_ids & active_agent_ids),
            },
        }
        return {
            "counts": counts,
            "recent_tasks": recent_tasks,
            "recent_activity": recent_activity,
            "errors": list(dict.fromkeys(errors)),
        }
    finally:
        os.close(root_fd)


def _empty_counts() -> dict[str, dict[str, int]]:
    return {
        "tasks": {"total": 0, "open": 0, "closed": 0},
        "jobs": {"total": 0, **{name: 0 for name in JOB_STATUSES}},
        "agents": {"total": 0, "active": 0},
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


class DashboardSnapshot:
    """Build a best-effort, read-only host view of all Cyclo instances."""

    def __init__(
        self,
        store: StateStore,
        *,
        docker: Docker | None = None,
        usage_reader: UsageReader | None = None,
        queue_limits: QueueLimits | None = None,
    ) -> None:
        self.store = store
        self.docker = docker or Docker()
        self.usage_reader = usage_reader
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

        for instance in self.store.list():
            errors: list[str] = []
            try:
                running: bool | None = self.docker.container_running(instance.container_name)
            except Exception as exc:
                running = None
                errors.append(f"Docker status unavailable: {exc}")
            state = _instance_state(instance, running)
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
            agentws_url = None
            if running and not instance.offline and instance.port:
                agentws_url = f"http://{instance.agentws_host}:{instance.port}/"
            rows.append(
                {
                    "id": instance.id,
                    "team": instance.team_name,
                    "project": instance.project_path,
                    "state": state,
                    "mode": {
                        "offline": instance.offline,
                        "team_write": instance.team_write,
                        "project_read_only": instance.project_read_only,
                    },
                    "generation": instance.generation,
                    "agentws_url": agentws_url,
                    "counts": queue["counts"],
                    "usage": usage,
                    "recent_tasks": queue["recent_tasks"],
                    "recent_activity": queue["recent_activity"],
                    "errors": list(dict.fromkeys(errors)),
                }
            )

        state_priority = {"running": 0, "stale": 1, "orphan": 2, "unknown": 3, "stopped": 4}
        rows.sort(key=lambda item: (state_priority.get(str(item["state"]), 9), str(item["id"])))
        source_errors = [usage_error] if usage_error else []
        instance_error_count = sum(len(item["errors"]) for item in rows)  # type: ignore[arg-type]
        summary = {
            "instances": len(rows),
            "running": sum(1 for item in rows if item["state"] == "running"),
            "attention": sum(
                1
                for item in rows
                if item["state"] in {"stale", "orphan", "unknown"}
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
