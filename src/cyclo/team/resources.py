"""Access the immutable AgentWS runtime owned by the team component.

Runtime callers use these package resources directly. Discovering another
package, a sibling checkout, or a path under the user's home directory is
deliberately unnecessary.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Final

from ..errors import CycloError


_RESOURCE_PACKAGE: Final = "cyclo.components.team"


def _filesystem_resource(*parts: str, kind: str) -> Path:
    resource = resources.files(_RESOURCE_PACKAGE).joinpath(*parts)
    try:
        path = Path(os.fspath(resource)).resolve()  # type: ignore[arg-type]
    except TypeError as exc:
        raise CycloError(
            "Cyclo's bundled AgentWS resources are not installed as filesystem files"
        ) from exc
    predicate = path.is_dir if kind == "directory" else path.is_file
    if not predicate():
        joined = "/".join(parts) or "."
        raise CycloError(f"Cyclo's bundled AgentWS {kind} is missing: {joined}")
    return path


def packaged_agentws_runtime() -> Path:
    """Return the complete AgentWS tree installed in every team component."""

    return _filesystem_resource("agentws", kind="directory")


def packaged_default_team() -> Path:
    """Return the bundled default roster used by ``cyclo team init``."""

    return _filesystem_resource("agentws", "default.team", kind="file")


def packaged_default_roles() -> Path:
    """Return the bundled default role definitions used by ``cyclo team init``."""

    return _filesystem_resource("agentws", "roles", kind="directory")


def packaged_agentws_protocol() -> Path:
    """Return the bundled generic AgentWS protocol."""

    return _filesystem_resource("agentws", "AGENTS.md", kind="file")
