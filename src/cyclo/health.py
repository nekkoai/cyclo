from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .component_stack import StackStatus
from .state import Instance


class ProviderStatusReader(Protocol):
    def status(self) -> StackStatus: ...


@dataclass(frozen=True)
class ProviderHealth:
    state: Literal[
        "ready",
        "provider-down",
        "provider-stale",
        "provider-unknown",
        "agents-suspended",
        "agents-attention",
        "agents-unknown",
        "inactive",
    ]
    reason: str = ""

    def api_value(self) -> dict[str, str]:
        return {"state": self.state, "reason": self.reason}

    def label(self) -> str:
        return self.state if not self.reason else f"{self.state} ({self.reason})"


INACTIVE_TEAM_HEALTH = ProviderHealth(
    "inactive", "not an active running Cyclo instance"
)


def _error_summary(exc: Exception) -> str:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return detail if len(detail) <= 160 else detail[:157] + "..."


def provider_health(status: StackStatus) -> ProviderHealth:
    desired = (("gateway", status.gateway.docker, status.gateway.ready),) + tuple(
        (component.instance, component.docker, component.ready)
        for component in status.components
    )
    stale = [name for name, docker, _ready in desired if docker.container_id and not docker.current]
    if stale:
        return ProviderHealth(
            "provider-stale",
            "configuration or image stale: " + ", ".join(stale),
        )
    unavailable = []
    for name, docker, ready in desired:
        if ready:
            continue
        if not docker.container_id:
            state = "absent"
        elif not docker.running:
            state = docker.lifecycle
        elif docker.engine_health == "unhealthy":
            state = "unhealthy"
        else:
            state = "not ready"
        unavailable.append(f"{name} {state}")
    if unavailable:
        return ProviderHealth("provider-down", ", ".join(unavailable))
    return ProviderHealth("ready")


def read_provider_status(
    reader: ProviderStatusReader | None,
) -> tuple[ProviderHealth, StackStatus | None]:
    if reader is None:
        return ProviderHealth("provider-unknown", "provider status unavailable"), None
    try:
        status = reader.status()
    except Exception as exc:
        return (
            ProviderHealth(
                "provider-unknown",
                f"provider status unavailable: {_error_summary(exc)}",
            ),
            None,
        )
    return provider_health(status), status


def instance_provider_health(
    shared: ProviderHealth,
    status: StackStatus | None,
    instance: Instance,
) -> ProviderHealth:
    if status is None or shared.state != "ready":
        return shared
    if (
        instance.provider_generation != status.generation
        or not instance.provider_socket_path
        or Path(instance.provider_socket_path) != status.provider_socket_path
    ):
        return ProviderHealth(
            "provider-stale",
            "team was launched against a different provider assembly; restart the project",
        )
    return shared


def team_health(
    provider: ProviderHealth,
    suspended_agents: tuple[str, ...] = (),
    supervisor_error: str = "",
    planner_attention_jobs: tuple[str, ...] = (),
) -> ProviderHealth:
    """Combine provider-stack health with one team's supervisor state."""

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
        details.insert(0, f"AgentWS supervisor status unavailable: {supervisor_error}")
        agent_health = ProviderHealth("agents-unknown", "; ".join(details))
    elif agents:
        agent_health = ProviderHealth("agents-suspended", "; ".join(details))
    elif attention:
        agent_health = ProviderHealth("agents-attention", "; ".join(details))
    else:
        return provider
    if provider.state == "ready":
        return agent_health
    reasons = [reason for reason in (provider.reason, agent_health.reason) if reason]
    return ProviderHealth(provider.state, "; ".join(reasons))
