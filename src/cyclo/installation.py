from __future__ import annotations

import hashlib
from pathlib import Path


def installation_id(state_root: Path) -> str:
    """Return the stable DComp namespace for one canonical Cyclo state root."""

    canonical = state_root.expanduser().resolve()
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:12]
