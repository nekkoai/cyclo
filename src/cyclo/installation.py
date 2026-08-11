from __future__ import annotations

import hashlib
import os
from pathlib import Path


SYSTEM_STATE_ROOT = Path("/var/lib/cyclo")
SYSTEM_HOST_CONFIG = Path("/etc/cyclo/host.conf")


def local_state_root() -> Path:
    """Return the explicit private-realm state root for the current user."""

    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "cyclo").resolve()


def realm_id(state_root: Path) -> str:
    """Return the stable DComp namespace for one canonical Cyclo state root."""

    canonical = state_root.expanduser().resolve()
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:12]


def installation_id(state_root: Path) -> str:
    """Compatibility alias for the pre-realm API name."""

    return realm_id(state_root)
