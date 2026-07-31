from __future__ import annotations

from importlib import resources
from pathlib import Path

from ...errors import CycloError


def packaged_team_templates_root() -> Path:
    """Return the example-team directory shipped inside Cyclo itself."""

    return Path(str(resources.files("cyclo.team.templates"))).resolve()


def packaged_team_template(name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise CycloError(f"invalid bundled team template name: {name!r}")
    root = packaged_team_templates_root()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CycloError(f"bundled team template escapes its package root: {name!r}") from exc
    if not (candidate / "team").is_file() or not (candidate / "roles").is_dir():
        raise CycloError(f"unknown bundled team template: {name}")
    return candidate


def bundled_team_template_names() -> tuple[str, ...]:
    root = packaged_team_templates_root()
    try:
        return tuple(
            sorted(
                path.name
                for path in root.iterdir()
                if path.is_dir() and (path / "team").is_file() and (path / "roles").is_dir()
            )
        )
    except OSError as exc:
        raise CycloError(f"cannot read bundled team templates: {exc}") from exc
