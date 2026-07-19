from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .runtime_container import ProviderRuntimeStatus


class RuntimeStatusReader(Protocol):
    def status(self) -> ProviderRuntimeStatus: ...

    def probe_operational(self, *, timeout: float) -> None: ...


@dataclass(frozen=True)
class RuntimeHealth:
    state: Literal[
        "ready",
        "runtime-down",
        "runtime-stale",
        "runtime-unknown",
        "agents-suspended",
        "agents-attention",
        "agents-unknown",
        "inactive",
    ]
    reason: str = ""

    def api_value(self) -> dict[str, str]:
        return {"state": self.state, "reason": self.reason}

    def label(self) -> str:
        if not self.reason:
            return self.state
        return f"{self.state} ({self.reason})"


INACTIVE_TEAM_HEALTH = RuntimeHealth(
    "inactive", "not an active running Cyclo instance"
)


def _error_summary(exc: Exception) -> str:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return detail if len(detail) <= 160 else detail[:157] + "..."


def read_runtime_health(
    reader: RuntimeStatusReader | None, *, probe_timeout: float = 2.0
) -> RuntimeHealth:
    """Check exact runtime metadata, gateway reachability, and runtime health."""

    if reader is None:
        return RuntimeHealth("runtime-unknown", "runtime status unavailable")
    try:
        status = reader.status()
    except Exception as exc:
        return RuntimeHealth(
            "runtime-unknown",
            f"runtime status unavailable: {_error_summary(exc)}",
        )
    if not status.exists:
        return RuntimeHealth("runtime-down", "runtime container absent")
    if not status.running:
        return RuntimeHealth("runtime-down", "runtime container stopped")
    if not status.current:
        return RuntimeHealth("runtime-stale", "configuration or image stale")
    try:
        reader.probe_operational(timeout=probe_timeout)
    except Exception as exc:
        return RuntimeHealth(
            "runtime-down",
            f"runtime dependency health check failed: {_error_summary(exc)}",
        )
    return RuntimeHealth("ready")


def team_health(
    runtime_health: RuntimeHealth,
    suspended_agents: tuple[str, ...] = (),
    supervisor_error: str = "",
    planner_attention_jobs: tuple[str, ...] = (),
) -> RuntimeHealth:
    """Combine shared runtime health with one team's supervisor state."""

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
        details.append(
            f"{len(attention)} unresolved planner {noun}: {shown}"
        )

    if supervisor_error:
        details.insert(
            0,
            f"AgentWS supervisor status unavailable: {supervisor_error}",
        )
        agent_health = RuntimeHealth(
            "agents-unknown",
            "; ".join(details),
        )
    elif agents:
        agent_health = RuntimeHealth(
            "agents-suspended", "; ".join(details)
        )
    elif attention:
        agent_health = RuntimeHealth("agents-attention", "; ".join(details))
    else:
        return runtime_health
    if runtime_health.state == "ready":
        return agent_health
    reasons = [
        reason for reason in (runtime_health.reason, agent_health.reason) if reason
    ]
    return RuntimeHealth(runtime_health.state, "; ".join(reasons))
