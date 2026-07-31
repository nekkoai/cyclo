from __future__ import annotations

from importlib import resources
from pathlib import Path


def package_root() -> Path:
    return Path(str(resources.files("cyclo"))).resolve()


def components_root() -> Path:
    return package_root() / "components"
