from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import CycloError
from .project import MOUNT_NAME_RE, ProjectMount
from .state import Instance


@dataclass(frozen=True)
class InstanceProject:
    """One normalized view of the project fields persisted for an instance."""

    instance_id: str
    name: str
    path: Path | None
    definition: Path | None
    description: str
    generation: str
    mounts: tuple[ProjectMount, ...]
    errors: tuple[str, ...]

    def require_valid(self) -> "InstanceProject":
        if self.errors:
            raise CycloError(
                f"invalid project metadata for Cyclo instance {self.instance_id!r}: "
                + "; ".join(self.errors)
            )
        return self

    def dashboard_value(self) -> dict[str, object]:
        def location(mount: ProjectMount) -> dict[str, str]:
            return {
                "name": mount.name,
                "path": str(mount.path),
                "container_path": str(mount.container_path),
            }

        workspaces = [location(mount) for mount in self.mounts if mount.writable]
        read_only = [location(mount) for mount in self.mounts if mount.read_only]
        return {
            "name": self.name,
            "path": str(self.path) if self.path is not None else "",
            "definition": (
                str(self.definition) if self.definition is not None else None
            ),
            "description": self.description,
            "generation": self.generation,
            "workspaces": workspaces,
            "read_only_mounts": read_only,
        }


def encode_project_mounts(
    mounts: Iterable[ProjectMount],
) -> list[dict[str, str]]:
    """Persist only source facts; container targets are derived from mode/name."""

    return [mount.as_json() for mount in mounts]


def decode_instance_project(instance: Instance) -> InstanceProject:
    """Decode persisted project state once, preserving valid entries on errors."""

    errors: list[str] = []

    def text(value: object, field: str) -> str:
        if isinstance(value, str):
            return value
        errors.append(f"{field} must be a string")
        return ""

    definition_text = text(instance.project_file, "project_file")
    definition = Path(definition_text) if definition_text else None
    if definition is None:
        errors.append("project_file is required")
    elif not definition.is_absolute():
        errors.append("project_file must be absolute")
        definition = None
    path = definition.parent if definition is not None else None

    name = text(instance.project_name, "project_name")
    description = text(instance.project_description, "project_description")
    generation = text(instance.project_generation, "project_generation")
    if not name:
        errors.append("project_name is required")
    if not description:
        errors.append("project_description is required")
    if not generation:
        errors.append("project_generation is required")

    raw_mounts = instance.project_mounts
    if not isinstance(raw_mounts, list):
        errors.append("project_mounts must be a list")
        raw_mounts = []
    elif not raw_mounts:
        errors.append("project_mounts must contain at least one mount")

    mounts: list[ProjectMount] = []
    for index, raw_mount in enumerate(raw_mounts):
        prefix = f"project_mounts[{index}]"
        if not isinstance(raw_mount, dict):
            errors.append(f"{prefix} must be an object")
            continue
        mount_name = raw_mount.get("name")
        mount_path = raw_mount.get("path")
        mode = raw_mount.get("mode")
        if (
            not isinstance(mount_name, str)
            or mount_name in {".", ".."}
            or not MOUNT_NAME_RE.fullmatch(mount_name)
        ):
            errors.append(f"{prefix} has an invalid name")
            continue
        if (
            not isinstance(mount_path, str)
            or not mount_path
            or not Path(mount_path).is_absolute()
        ):
            errors.append(f"{prefix} has an invalid host path")
            continue
        if mode not in {"ro", "rw"}:
            errors.append(f"{prefix} has an invalid mode")
            continue
        mounts.append(
            ProjectMount(mount_name, Path(mount_path), mode)  # type: ignore[arg-type]
        )

    return InstanceProject(
        instance_id=instance.id,
        name=name or (path.name if path is not None else ""),
        path=path,
        definition=definition,
        description=description,
        generation=generation,
        mounts=tuple(mounts),
        errors=tuple(errors),
    )
