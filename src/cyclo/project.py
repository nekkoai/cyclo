from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import CycloError


MAX_PROJECT_FILE_BYTES = 1024 * 1024
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MOUNT_NAME_RE = PROJECT_NAME_RE
TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AccessMode = Literal["ro", "rw"]
CONTAINER_WORKSPACE_ROOT = Path("/workspace")
CONTAINER_READONLY_ROOT = Path("/readonly")


def mount_container_path(name: str, mode: AccessMode) -> Path:
    root = CONTAINER_WORKSPACE_ROOT if mode == "rw" else CONTAINER_READONLY_ROOT
    return root / name


@dataclass(frozen=True)
class ProjectTeam:
    """One team repository selected by a project definition."""

    path: Path
    mode: AccessMode
    line: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    @property
    def read_only(self) -> bool:
        return self.mode == "ro"


@dataclass(frozen=True)
class ProjectMount:
    """One named host directory exposed to every selected team."""

    name: str
    path: Path
    mode: AccessMode
    line: int = 0

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    @property
    def read_only(self) -> bool:
        return self.mode == "ro"

    @property
    def container_path(self) -> Path:
        return mount_container_path(self.name, self.mode)

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "path": str(self.path), "mode": self.mode}


@dataclass(frozen=True)
class ProjectDefinition:
    """A complete, immutable ``project.cyclo`` definition."""

    path: Path
    name: str
    description: str
    teams: tuple[ProjectTeam, ...]
    mounts: tuple[ProjectMount, ...]
    definition_sha256: str


def _selected_path(value: str | os.PathLike[str]) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    # Do not resolve the final component before opening it: project definition
    # files are host authority and must not be silently followed through a
    # replaceable symlink.
    return Path(os.path.abspath(selected))


def _read_project_file(path: Path) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise CycloError("safe project definition reads require O_NOFOLLOW")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CycloError(f"project definition not found: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CycloError(f"project definition must not be a symlink: {path}") from exc
        raise CycloError(f"cannot open project definition {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CycloError(f"project definition is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            content = stream.read(MAX_PROJECT_FILE_BYTES + 1)
    except CycloError:
        raise
    except OSError as exc:
        raise CycloError(f"cannot read project definition {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(content) > MAX_PROJECT_FILE_BYTES:
        raise CycloError(
            f"project definition exceeds the {MAX_PROJECT_FILE_BYTES}-byte limit: "
            f"{path}"
        )
    return content


def _decode_project_file(content: bytes, path: Path) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CycloError(f"project definition is not valid UTF-8: {path}") from exc

    # Accept ordinary Unix and Windows line endings, but no embedded control
    # characters. Tabs are deliberately not an alternate quoting/separation
    # language for this small format.
    text = text.replace("\r\n", "\n")
    for character in text:
        codepoint = ord(character)
        if character != "\n" and (codepoint < 0x20 or codepoint == 0x7F):
            raise CycloError(
                f"project definition contains a control character: {path}"
            )
    return text


def _validate_identifier(
    value: str,
    *,
    label: str,
    pattern: re.Pattern[str],
    source: Path,
    line: int,
) -> None:
    if value in {".", ".."} or not pattern.fullmatch(value):
        raise CycloError(
            f"{source}:{line}: invalid {label} {value!r}; use at most 64 "
            "letters, numbers, dot, underscore, or hyphen, beginning with a "
            "letter or number"
        )


def _validate_mode(value: str, source: Path, line: int) -> AccessMode:
    if value not in {"ro", "rw"}:
        raise CycloError(
            f"{source}:{line}: invalid access mode {value!r}; expected 'ro' or 'rw'"
        )
    return value  # type: ignore[return-value]


def _validate_path_token(value: str, source: Path, line: int) -> None:
    if (
        not value
        or "~" in value
        or "," in value
        or "'" in value
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CycloError(
            f"{source}:{line}: invalid path {value!r}; use one unquoted path "
            "without whitespace, '~', comma, quote, or backslash"
        )


def _resolve_directory(value: str, source: Path, line: int, label: str) -> Path:
    _validate_path_token(value, source, line)
    selected = Path(value)
    if not selected.is_absolute():
        selected = source.parent / selected
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CycloError(f"{source}:{line}: {label} path not found: {selected}") from exc
    if not resolved.is_dir():
        raise CycloError(f"{source}:{line}: {label} path is not a directory: {resolved}")
    if (
        "," in str(resolved)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in str(resolved))
    ):
        raise CycloError(
            f"{source}:{line}: {label} resolved path cannot contain a comma or "
            f"control character: {resolved}"
        )
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _definition_sha256(
    name: str,
    description: str,
    teams: tuple[ProjectTeam, ...],
    mounts: tuple[ProjectMount, ...],
) -> str:
    payload = {
        "description": description,
        "mounts": [
            {"mode": mount.mode, "name": mount.name, "path": str(mount.path)}
            for mount in mounts
        ],
        "name": name,
        "schema": 2,
        "teams": [
            {"mode": team.mode, "path": str(team.path)} for team in teams
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_project(value: str | os.PathLike[str]) -> ProjectDefinition:
    """Parse and validate one strict, line-oriented project definition."""

    source = _selected_path(value)
    text = _decode_project_file(_read_project_file(source), source)

    name: str | None = None
    description: str | None = None
    teams: list[ProjectTeam] = []
    mounts: list[ProjectMount] = []
    team_paths: dict[Path, int] = {}
    team_names: dict[str, int] = {}
    mount_names: dict[str, int] = {}
    mount_paths: dict[Path, int] = {}

    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        directive, separator, remainder = line.partition(" ")

        if directive == "name":
            fields = line.split()
            if len(fields) != 2:
                raise CycloError(
                    f"{source}:{line_number}: expected name <project-name>"
                )
            if name is not None:
                raise CycloError(f"{source}:{line_number}: duplicate name directive")
            _validate_identifier(
                fields[1],
                label="project name",
                pattern=PROJECT_NAME_RE,
                source=source,
                line=line_number,
            )
            name = fields[1]
            continue

        if directive == "description":
            if not separator or not remainder.strip():
                raise CycloError(
                    f"{source}:{line_number}: expected description <text>"
                )
            if description is not None:
                raise CycloError(
                    f"{source}:{line_number}: duplicate description directive"
                )
            description = remainder.strip()
            continue

        fields = line.split()
        if directive == "team":
            if len(fields) != 3:
                raise CycloError(
                    f"{source}:{line_number}: expected team <path> <ro|rw>"
                )
            path = _resolve_directory(fields[1], source, line_number, "team")
            mode = _validate_mode(fields[2], source, line_number)
            _validate_identifier(
                path.name,
                label="team repository name",
                pattern=TEAM_NAME_RE,
                source=source,
                line=line_number,
            )
            if path in team_paths:
                raise CycloError(
                    f"{source}:{line_number}: duplicate team path {path}; "
                    f"first declared on line {team_paths[path]}"
                )
            folded_name = path.name.casefold()
            if folded_name in team_names:
                raise CycloError(
                    f"{source}:{line_number}: duplicate team name {path.name!r}; "
                    f"first declared on line {team_names[folded_name]}"
                )
            team_paths[path] = line_number
            team_names[folded_name] = line_number
            teams.append(ProjectTeam(path=path, mode=mode, line=line_number))
            continue

        if directive == "mount":
            if len(fields) != 4:
                raise CycloError(
                    f"{source}:{line_number}: expected "
                    "mount <name> <path> <ro|rw>"
                )
            mount_name = fields[1]
            _validate_identifier(
                mount_name,
                label="mount name",
                pattern=MOUNT_NAME_RE,
                source=source,
                line=line_number,
            )
            path = _resolve_directory(fields[2], source, line_number, "mount")
            mode = _validate_mode(fields[3], source, line_number)
            if mount_name in mount_names:
                raise CycloError(
                    f"{source}:{line_number}: duplicate mount name {mount_name!r}; "
                    f"first declared on line {mount_names[mount_name]}"
                )
            if path in mount_paths:
                raise CycloError(
                    f"{source}:{line_number}: duplicate mount path {path}; "
                    f"first declared on line {mount_paths[path]}"
                )
            mount_names[mount_name] = line_number
            mount_paths[path] = line_number
            mounts.append(
                ProjectMount(
                    name=mount_name,
                    path=path,
                    mode=mode,
                    line=line_number,
                )
            )
            continue

        if directive == "mcp":
            raise CycloError(
                f"{source}:{line_number}: MCP servers are not supported by this "
                "Cyclo version"
            )
        raise CycloError(
            f"{source}:{line_number}: unknown project directive {directive!r}; "
            "expected name, description, team, or mount"
        )

    if name is None:
        raise CycloError(f"{source}: missing required name directive")
    if description is None:
        raise CycloError(f"{source}: missing required description directive")
    if not teams:
        raise CycloError(f"{source}: project definition has no teams")
    if not mounts:
        raise CycloError(f"{source}: project definition has no mounts")

    roots: list[tuple[Path, str, int]] = [
        (team.path, f"team {team.name!r}", team.line)
        for team in teams
    ]
    roots.extend(
        (mount.path, f"mount {mount.name!r}", mount.line)
        for mount in mounts
    )
    for index, (path, label, line) in enumerate(roots):
        for earlier_path, earlier_label, earlier_line in roots[:index]:
            if _paths_overlap(path, earlier_path):
                raise CycloError(
                    f"{source}:{line}: {label} overlaps {earlier_label} declared "
                    f"on line {earlier_line}: {path} and {earlier_path}"
                )

    team_tuple = tuple(teams)
    mount_tuple = tuple(mounts)
    return ProjectDefinition(
        path=source,
        name=name,
        description=description,
        teams=team_tuple,
        mounts=mount_tuple,
        definition_sha256=_definition_sha256(
            name,
            description,
            team_tuple,
            mount_tuple,
        ),
    )


def render_project_manifest(
    project: ProjectDefinition,
    *,
    team: ProjectTeam | None = None,
) -> str:
    """Return deterministic agent context without exposing host source paths."""

    if team is not None and team not in project.teams:
        raise ValueError("selected team is not part of this project definition")

    lines = [
        "# Cyclo project",
        "",
        f"Name: {project.name}",
        f"Description: {project.description}",
        f"Definition: {project.definition_sha256}",
        "",
        "The current working directory is /workspace.",
        "Writable work lives below /workspace; read-only inputs live below /readonly.",
        "Use the named paths below; host filesystem paths are not available here.",
        "",
        "## Writable workspace mounts",
        "",
    ]
    writable = tuple(mount for mount in project.mounts if mount.writable)
    readonly = tuple(mount for mount in project.mounts if mount.read_only)
    if writable:
        for mount in writable:
            lines.append(f"- {mount.container_path} (read-write)")
    else:
        lines.append("- none")

    lines.extend(["", "## Read-only mounts", ""])
    if readonly:
        for mount in readonly:
            lines.append(f"- {mount.container_path} (read-only)")
    else:
        lines.append("- none")

    lines.extend(["", "## Team definition", ""])
    if team is not None:
        access = "read-write" if team.writable else "read-only"
        lines.append(f"- /team ({access}; {team.name})")
    else:
        lines.append("Each running team sees its own definition at /team:")
        for configured_team in project.teams:
            access = "read-write" if configured_team.writable else "read-only"
            lines.append(f"- {configured_team.name} ({access})")
    return "\n".join(lines) + "\n"
