from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .component import ComponentStatus
from .providers import ProviderConnection
from .state import Instance


class ProviderStatusReader(Protocol):
    def statuses(self) -> tuple[ComponentStatus, ...]: ...

    def connection(
        self,
        statuses: tuple[ComponentStatus, ...] | None = None,
    ) -> ProviderConnection: ...

    def catalogue(
        self,
        connection: ProviderConnection,
    ) -> tuple[ProviderConnection, dict[str, object]]: ...


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


def provider_health(
    components: tuple[ComponentStatus, ...],
) -> ProviderHealth:
    gateway = next(
        (component for component in components if component.name == "gateway"),
        None,
    )
    if gateway is None:
        return ProviderHealth("provider-down", "gateway missing from inventory")
    if gateway.container_id and not gateway.current:
        return ProviderHealth(
            "provider-stale",
            "configuration or image stale: gateway",
        )
    if not gateway.works:
        if gateway.container_state == "unknown":
            state = "unknown"
        elif not gateway.container_id:
            state = "absent"
        elif not gateway.running:
            state = gateway.container_state
        elif gateway.engine_health == "unhealthy":
            state = "unhealthy"
        else:
            state = "not ready"
        if gateway.error:
            state += f": {gateway.error}"
        return ProviderHealth("provider-down", f"gateway {state}")
    unavailable = []
    for component in components:
        if component.name == "gateway" or component.works:
            continue
        if component.container_state == "unknown":
            state = "unknown"
        elif component.container_id and not component.current:
            state = "stale"
        elif not component.container_id:
            state = "absent"
        elif not component.running:
            state = component.container_state
        elif component.engine_health == "unhealthy":
            state = "unhealthy"
        else:
            state = "not ready"
        if component.error:
            state += f": {component.error}"
        unavailable.append(f"{component.name} {state}")
    if unavailable:
        return ProviderHealth(
            "ready",
            "ignored unavailable optional components: " + ", ".join(unavailable),
        )
    return ProviderHealth("ready")


def read_provider_status(
    reader: ProviderStatusReader | None,
) -> tuple[ProviderHealth, ProviderConnection | None]:
    if reader is None:
        return ProviderHealth("provider-unknown", "provider status unavailable"), None
    try:
        components = reader.statuses()
    except Exception as exc:
        return (
            ProviderHealth(
                "provider-unknown",
                f"provider status unavailable: {_error_summary(exc)}",
            ),
            None,
        )
    health = provider_health(components)
    if health.state != "ready":
        return health, None
    try:
        connection = reader.connection(components)
        connection, _catalogue = reader.catalogue(connection)
    except Exception as exc:
        return (
            ProviderHealth(
                "provider-unknown",
                f"provider route unavailable: {_error_summary(exc)}",
            ),
            None,
        )
    return provider_health(connection.components), connection


def instance_provider_health(
    shared: ProviderHealth,
    connection: ProviderConnection | None,
    instance: Instance,
) -> ProviderHealth:
    if connection is None or shared.state != "ready":
        return shared
    if (
        instance.provider_generation != connection.generation
        or not instance.provider_socket_path
        or Path(instance.provider_socket_path) != connection.socket_path
    ):
        return ProviderHealth(
            "provider-stale",
            "team was launched against a different provider configuration; "
            "restart the project",
        )
    return shared


def team_health(
    provider: ProviderHealth,
    suspended_agents: tuple[str, ...] = (),
    supervisor_error: str = "",
    planner_attention_jobs: tuple[str, ...] = (),
) -> ProviderHealth:
    """Combine provider health with one team's supervisor state."""

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
    if provider.state == "ready" and not provider.reason:
        return agent_health
    reasons = [reason for reason in (provider.reason, agent_health.reason) if reason]
    return ProviderHealth(
        agent_health.state if provider.state == "ready" else provider.state,
        "; ".join(reasons),
    )
