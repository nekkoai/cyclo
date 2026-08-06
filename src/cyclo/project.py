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
CONTEXT_MARKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
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
    context: str
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


def _read_regular_file(path: Path, label: str) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise CycloError(f"safe {label} reads require O_NOFOLLOW")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CycloError(f"{label} not found: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CycloError(f"{label} must not be a symlink: {path}") from exc
        raise CycloError(f"cannot open {label} {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CycloError(f"{label} is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            content = stream.read(MAX_PROJECT_FILE_BYTES + 1)
    except CycloError:
        raise
    except OSError as exc:
        raise CycloError(f"cannot read {label} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(content) > MAX_PROJECT_FILE_BYTES:
        raise CycloError(
            f"{label} exceeds the {MAX_PROJECT_FILE_BYTES}-byte limit: {path}"
        )
    return content


def _decode_text_file(content: bytes, path: Path, label: str) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CycloError(f"{label} is not valid UTF-8: {path}") from exc

    # Accept ordinary Unix and Windows line endings, but no embedded control
    # characters. Tabs are deliberately not an alternate quoting/separation
    # language for this small format.
    text = text.replace("\r\n", "\n")
    for character in text:
        codepoint = ord(character)
        if character != "\n" and (codepoint < 0x20 or codepoint == 0x7F):
            raise CycloError(f"{label} contains a control character: {path}")
    return text


def read_project_context(value: str | os.PathLike[str]) -> str:
    """Read one literal context file through the project trust boundary."""

    source = _selected_path(value)
    text = _decode_text_file(
        _read_regular_file(source, "project context"),
        source,
        "project context",
    ).strip("\n")
    if not text.strip():
        raise CycloError(f"project context must not be empty: {source}")
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
    context: str,
    teams: tuple[ProjectTeam, ...],
    mounts: tuple[ProjectMount, ...],
) -> str:
    payload = {
        "context": context,
        "description": description,
        "mounts": [
            {"mode": mount.mode, "name": mount.name, "path": str(mount.path)}
            for mount in mounts
        ],
        "name": name,
        "schema": 3,
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
    text = _decode_text_file(
        _read_regular_file(source, "project definition"),
        source,
        "project definition",
    )

    name: str | None = None
    description: str | None = None
    context: str | None = None
    teams: list[ProjectTeam] = []
    mounts: list[ProjectMount] = []
    team_paths: dict[Path, int] = {}
    team_names: dict[str, int] = {}
    mount_names: dict[str, int] = {}
    mount_paths: dict[Path, int] = {}

    line_iterator = iter(enumerate(text.splitlines(), 1))
    for line_number, raw in line_iterator:
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

        if directive == "context":
            fields = line.split()
            if (
                len(fields) != 2
                or not fields[1].startswith("<<")
                or not CONTEXT_MARKER_RE.fullmatch(fields[1][2:])
            ):
                raise CycloError(
                    f"{source}:{line_number}: expected context <<MARKER"
                )
            if context is not None:
                raise CycloError(
                    f"{source}:{line_number}: duplicate context directive"
                )
            marker = fields[1][2:]
            context_lines: list[str] = []
            for _context_line_number, context_line in line_iterator:
                if context_line == marker:
                    break
                context_lines.append(context_line)
            else:
                raise CycloError(
                    f"{source}:{line_number}: unterminated context block; "
                    f"expected {marker!r}"
                )
            first = 0
            last = len(context_lines)
            while first < last and not context_lines[first].strip():
                first += 1
            while last > first and not context_lines[last - 1].strip():
                last -= 1
            context_lines = context_lines[first:last]
            if not context_lines:
                raise CycloError(
                    f"{source}:{line_number}: project context must not be empty"
                )
            context = "\n".join(context_lines)
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
            "expected name, description, context, team, or mount"
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
        context=context or "",
        teams=team_tuple,
        mounts=mount_tuple,
        definition_sha256=_definition_sha256(
            name,
            description,
            context or "",
            team_tuple,
            mount_tuple,
        ),
    )


def project_context_marker(context: str) -> str:
    """Choose a deterministic heredoc marker absent from literal context."""

    lines = set(context.splitlines())
    marker = "CYCLO_CONTEXT"
    suffix = 1
    while marker in lines:
        suffix += 1
        marker = f"CYCLO_CONTEXT_{suffix}"
    return marker


def render_container_project(
    project: ProjectDefinition,
    *,
    team: ProjectTeam,
) -> str:
    """Render the selected team's project definition in its container namespace."""

    if team not in project.teams:
        raise ValueError("selected team is not part of this project definition")

    lines = [
        "# Generated by Cyclo for this team container.",
        "# Team and mount paths are container paths; host paths are omitted.",
        f"name {project.name}",
        f"description {project.description}",
    ]
    if project.context:
        marker = project_context_marker(project.context)
        lines.extend(["", f"context <<{marker}", project.context, marker])
    lines.extend(["", f"team /team {team.mode}", ""])
    lines.extend(
        f"mount {mount.name} {mount.container_path} {mount.mode}"
        for mount in project.mounts
    )
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode("utf-8")) > MAX_PROJECT_FILE_BYTES:
        raise CycloError(
            "container project configuration exceeds the "
            f"{MAX_PROJECT_FILE_BYTES}-byte project.cyclo limit"
        )
    return rendered
