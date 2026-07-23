from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import CycloError


LABEL_SYSTEM = "io.cyclo.system"
LABEL_KIND = "io.cyclo.kind"
LABEL_INSTANCE = "io.cyclo.instance"

TEAM_KIND = "team"
TEAM_NETWORK_KIND = "team-network"

_SYSTEM_RE = re.compile(r"^[0-9a-f]{12}$")


def installation_id(state_root: Path) -> str:
    """Return the stable Docker namespace for one canonical resource root."""

    canonical = state_root.expanduser().resolve()
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:12]


def validate_installation_id(value: str) -> str:
    if not isinstance(value, str) or not _SYSTEM_RE.fullmatch(value):
        raise CycloError(f"invalid Cyclo installation ID: {value!r}")
    return value


def resource_name(system: str, kind: str, identifier: str) -> str:
    validate_installation_id(system)
    if not kind or not identifier:
        raise CycloError("Cyclo Docker resource kind and identifier are required")
    return f"cyclo-{system}-{kind}-{identifier}"


def gateway_name(system: str) -> str:
    validate_installation_id(system)
    return f"cyclo-{system}-gateway"


def provider_name(system: str, instance: str) -> str:
    return resource_name(system, "provider", instance)


def team_container_name(system: str, instance: str) -> str:
    return resource_name(system, TEAM_KIND, instance)


def team_network_name(system: str, instance: str) -> str:
    return f"{team_container_name(system, instance)}-net"


def team_image_name(system: str, version: str) -> str:
    validate_installation_id(system)
    return f"cyclo-{system}-team:{version}"


def derived_team_image_name(
    system: str,
    version: str,
    team_root: Path,
    team_name: str,
) -> str:
    """Return the stable installation-local image tag for one team repository."""

    validate_installation_id(system)
    identity = hashlib.sha256(
        str(team_root.expanduser().resolve()).encode("utf-8")
    ).hexdigest()[:12]
    name = re.sub(r"[^a-z0-9_.-]+", "-", team_name.lower()).strip("-._")
    return f"cyclo-{system}-team-{(name or 'team')[:28]}-{identity}:{version}"


def resource_labels(system: str, kind: str, instance: str) -> dict[str, str]:
    validate_installation_id(system)
    return {
        LABEL_SYSTEM: system,
        LABEL_KIND: kind,
        LABEL_INSTANCE: instance,
    }
