from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import CycloError
from ..model_ids import split_public_model_id
from .resources import packaged_agentws_runtime
from .templates import packaged_team_template


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SUPPORTED_ENGINES = {"pi", "pi-interactive"}
ROSTER_NAMES = ("team", "default.team")
MAX_TEAM_FILE_BYTES = 1024 * 1024
_MINIMAL_TEAM_DOCKERFILE = "ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n"


@dataclass(frozen=True)
class Agent:
    name: str
    role: str
    engine: str
    model: str

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_id(self) -> str:
        return self.model.split("/", 1)[1]


@dataclass(frozen=True)
class Team:
    root: Path
    roster: Path
    roles_dir: Path
    protocol: Path | None
    dockerfile: Path
    agents: tuple[Agent, ...]

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({agent.provider for agent in self.agents}))


@dataclass(frozen=True)
class _TeamSnapshot:
    roster: Path
    roles_dir: Path
    protocol: Path | None
    dockerfile: Path
    files: tuple[tuple[Path, str, bytes], ...]

    @property
    def roster_content(self) -> bytes:
        return self.files[0][2]

    @property
    def dockerfile_content(self) -> bytes:
        return next(
            content
            for path, label, content in self.files
            if path == self.dockerfile and label == "Dockerfile"
        )

    @property
    def role_names(self) -> frozenset[str]:
        return frozenset(
            path.name for path, label, _content in self.files if label.startswith("role ")
        )


def resolve_directory(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise CycloError(f"{label} directory not found: {path}")
    if "," in str(path):
        raise CycloError(f"{label} path cannot contain a comma: {path}")
    return path


def _validate_name(label: str, value: str, source: Path, line_no: int) -> None:
    if not NAME_RE.fullmatch(value):
        raise CycloError(
            f"{source}:{line_no}: invalid {label} {value!r}; start with a "
            "letter or number and use only letters, numbers, dot, underscore, "
            "or hyphen"
        )


def validate_proxy_model(model: str) -> tuple[str, str]:
    if "/" not in model:
        raise CycloError("model must be a proxy name such as provider/model")
    parsed = split_public_model_id(model)
    if parsed is None:
        raise CycloError(f"invalid proxy model {model!r}")
    return parsed


def _regular_file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise CycloError("safe team file reads require O_NOFOLLOW")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _read_regular_file_at(
    directory_fd: int,
    name: str,
    path: Path,
    label: str,
) -> bytes:
    """Read one named child without following any team-controlled symlink."""

    try:
        descriptor = os.open(name, _regular_file_flags(), dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise CycloError(f"team {label} not found: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CycloError(f"team {label} must not be a symlink: {path}") from exc
        raise CycloError(f"cannot open team {label} {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CycloError(f"team {label} is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            content = stream.read(MAX_TEAM_FILE_BYTES + 1)
    except CycloError:
        raise
    except OSError as exc:
        raise CycloError(f"cannot read team {label} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(content) > MAX_TEAM_FILE_BYTES:
        raise CycloError(
            f"team {label} exceeds the {MAX_TEAM_FILE_BYTES}-byte limit: {path}"
        )
    return content


def _decode_team_file(content: bytes, path: Path, label: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CycloError(f"team {label} is not valid UTF-8: {path}") from exc


def _open_team_root(root: Path) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise CycloError("safe team directory reads require O_DIRECTORY and O_NOFOLLOW")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise CycloError(
            f"team directory must be a real directory, not a symlink: {root}"
        ) from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CycloError(f"team directory is not a directory: {root}")
    return descriptor


def _open_roles_directory(root_fd: int, root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    roles_dir = root / "roles"
    try:
        descriptor = os.open("roles", flags, dir_fd=root_fd)
    except FileNotFoundError as exc:
        raise CycloError(f"team roles directory not found: {roles_dir}") from exc
    except OSError as exc:
        raise CycloError(
            f"team roles directory must be a real directory, not a symlink: {roles_dir}"
        ) from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CycloError(f"team roles directory not found: {roles_dir}")
    return descriptor


def _directory_entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CycloError(f"cannot inspect team definition {name!r}: {exc}") from exc
    return True


def _read_team_snapshot(root: Path, roster_name: str | None = None) -> _TeamSnapshot:
    root_fd = _open_team_root(root)
    roles_fd = -1
    try:
        if roster_name is None:
            roster_name = next(
                (name for name in ROSTER_NAMES if _directory_entry_exists(root_fd, name)),
                None,
            )
        if roster_name is None:
            names = " or ".join(ROSTER_NAMES)
            raise CycloError(f"team roster not found: expected {names} in {root}")
        if roster_name not in ROSTER_NAMES:
            raise CycloError(f"unsupported team roster name: {roster_name}")

        roster = root / roster_name
        files: list[tuple[Path, str, bytes]] = [
            (
                roster,
                "roster",
                _read_regular_file_at(root_fd, roster_name, roster, "roster"),
            )
        ]

        roles_dir = root / "roles"
        roles_fd = _open_roles_directory(root_fd, root)
        try:
            with os.scandir(roles_fd) as entries:
                role_names = sorted(
                    entry.name for entry in entries if entry.name.endswith(".md")
                )
        except OSError as exc:
            raise CycloError(f"cannot inspect team roles directory {roles_dir}: {exc}") from exc
        for name in role_names:
            path = roles_dir / name
            files.append(
                (
                    path,
                    f"role {name!r}",
                    _read_regular_file_at(roles_fd, name, path, f"role {name!r}"),
                )
            )

        protocol_name = "AGENTS.md"
        protocol: Path | None = None
        if _directory_entry_exists(root_fd, protocol_name):
            protocol = root / protocol_name
            files.append(
                (
                    protocol,
                    "protocol",
                    _read_regular_file_at(root_fd, protocol_name, protocol, "protocol"),
                )
            )
        dockerfile = root / "Dockerfile"
        files.append(
            (
                dockerfile,
                "Dockerfile",
                _read_regular_file_at(
                    root_fd,
                    "Dockerfile",
                    dockerfile,
                    "Dockerfile",
                ),
            )
        )
        for path, label, content in files:
            _decode_team_file(content, path, label)
        return _TeamSnapshot(
            roster=roster,
            roles_dir=roles_dir,
            protocol=protocol,
            dockerfile=dockerfile,
            files=tuple(files),
        )
    finally:
        if roles_fd >= 0:
            os.close(roles_fd)
        os.close(root_fd)


def _parse_roster(snapshot: _TeamSnapshot) -> tuple[Agent, ...]:
    roster = snapshot.roster
    roles_dir = snapshot.roles_dir
    lines = _decode_team_file(
        snapshot.roster_content, snapshot.roster, "roster"
    ).splitlines()

    agents: list[Agent] = []
    names: set[str] = set()
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise CycloError(
                f"{roster}:{line_no}: expected <name> <role> <agent> <provider/model>"
            )
        name, role, engine, model = fields
        _validate_name("agent name", name, roster, line_no)
        _validate_name("role", role, roster, line_no)
        if name in names:
            raise CycloError(f"{roster}:{line_no}: duplicate agent name {name!r}")
        if engine not in SUPPORTED_ENGINES:
            supported = ", ".join(sorted(SUPPORTED_ENGINES))
            raise CycloError(
                f"{roster}:{line_no}: Cyclo's proxy runtime supports {supported}; got {engine!r}"
            )
        try:
            validate_proxy_model(model)
        except CycloError as exc:
            raise CycloError(f"{roster}:{line_no}: {exc}") from exc
        role_file = roles_dir / f"{role}.md"
        if role_file.name not in snapshot.role_names:
            raise CycloError(
                f"{roster}:{line_no}: team role file not found: {role_file}"
            )
        names.add(name)
        agents.append(Agent(name=name, role=role, engine=engine, model=model))

    if not agents:
        raise CycloError(f"team roster has no agents: {roster}")
    if not any(agent.role == "planner" for agent in agents):
        raise CycloError(
            f"team roster must include at least one planner agent because planner "
            f"is the task entry role: {roster}"
        )
    return tuple(agents)


def _validate_team_dockerfile(snapshot: _TeamSnapshot) -> Path:
    """Validate the required image extension owned by a team repository."""

    path = snapshot.dockerfile
    content = _decode_team_file(
        snapshot.dockerfile_content,
        path,
        "Dockerfile",
    )

    directives = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    from_directives = [
        (index, line)
        for index, line in enumerate(directives)
        if line.split(None, 1)[0].upper() == "FROM"
    ]
    if not from_directives:
        raise CycloError(f"team Dockerfile has no FROM directive: {path}")
    first_from = from_directives[0][0]
    arguments = {
        line.split(None, 1)[1].split("=", 1)[0].strip()
        for line in directives[:first_from]
        if len(line.split(None, 1)) == 2
        and line.split(None, 1)[0].upper() == "ARG"
    }
    if "CYCLO_TEAM_BASE" not in arguments:
        raise CycloError(
            f"team Dockerfile must declare ARG CYCLO_TEAM_BASE before FROM: {path}"
        )
    from_fields = from_directives[-1][1].split()
    image_index = (
        2 if len(from_fields) > 1 and from_fields[1].startswith("--") else 1
    )
    if (
        len(from_fields) <= image_index
        or from_fields[image_index] not in {"${CYCLO_TEAM_BASE}", "$CYCLO_TEAM_BASE"}
    ):
        raise CycloError(
            f"team Dockerfile's final stage must use FROM "
            f"${{CYCLO_TEAM_BASE}}: {path}"
        )
    return path


def load_team(value: str | os.PathLike[str]) -> Team:
    root = resolve_directory(value, "team")
    snapshot = _read_team_snapshot(root)
    agents = _parse_roster(snapshot)
    return Team(
        root=root,
        roster=snapshot.roster,
        roles_dir=snapshot.roles_dir,
        protocol=snapshot.protocol,
        dockerfile=_validate_team_dockerfile(snapshot),
        agents=agents,
    )


def git_root(path: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CycloError(f"team is not a Git repository: {path}")
    return Path(proc.stdout.strip()).resolve()


def require_team_repository(team: Team) -> None:
    root = git_root(team.root)
    if root != team.root:
        raise CycloError(
            f"the team must live at its Git repository root: team={team.root}, git={root}"
        )


def team_generation(team: Team) -> str:
    proc = subprocess.run(
        ["git", "-C", str(team.root), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    commit = proc.stdout.strip() if proc.returncode == 0 else "unborn"
    snapshot = _read_team_snapshot(team.root, team.roster.name)
    if (snapshot.protocol is None) != (team.protocol is None):
        raise CycloError("team protocol changed after validation; reload the team")
    if _parse_roster(snapshot) != team.agents:
        raise CycloError("team roster changed after validation; reload the team")
    _validate_team_dockerfile(snapshot)
    digest = hashlib.sha256()
    for path, _label, content in snapshot.files:
        digest.update(str(path.relative_to(team.root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    content = digest.hexdigest()[:16]
    # Never run `git status` over an untrusted/self-modifiable team repository:
    # repository-local core.fsmonitor hooks execute on the host. The content
    # digest is cheap, deterministic, and captures the complete team definition.
    return f"{commit}-content-{content}"


def verify_agentws_runtime(runtime: Path) -> None:
    required = (
        runtime / "tools" / "run_agentws",
        runtime / "tools" / "agent",
        runtime / "tools" / "agentws",
        runtime / "bin" / "agent-new",
        runtime / "bin" / "job-reset-orphans",
        runtime / "bin" / "task-create",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise CycloError("Cyclo's bundled job-loop runtime is incomplete: " + ", ".join(missing))
    seams = {
        runtime / "tools" / "run_agentws": (
            "AGENTWS_SYSTEM_PROTOCOL",
            "AGENTWS_TEAM_PROTOCOL",
            "AGENTWS_TEAM_ROLES_DIR",
            "AGENTWS_WORKSPACE",
        ),
        runtime / "tools" / "agent": (
            "AGENTWS_SYSTEM_PROTOCOL",
            "AGENTWS_TEAM_PROTOCOL",
            "AGENTWS_WORKSPACE",
        ),
        runtime / "tools" / "agentws": ("--pin-root", "--read-only"),
        runtime / "bin" / "agent-new": ("AGENTWS_TEAM_ROLES_DIR",),
        runtime / "bin" / "job-reset-orphans": (
            "--all-active",
            "CYCLO_RUNTIME_LOCK_FD",
        ),
    }
    for path, markers in seams.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        absent = [marker for marker in markers if marker not in text]
        if absent:
            raise CycloError(
                f"Cyclo's bundled job-loop runtime lacks a required marker in "
                f"{path.relative_to(runtime)}: "
                + ", ".join(absent)
            )


def init_team(
    destination: Path,
    model: str,
    *,
    initialize_git: bool = True,
    template_name: str | None = None,
) -> Path:
    validate_proxy_model(model)
    try:
        destination = destination.expanduser().resolve()
        if destination.exists():
            if not destination.is_dir():
                raise CycloError(f"destination exists and is not a directory: {destination}")
            if any(destination.iterdir()):
                raise CycloError(f"destination is not empty: {destination}")
    except CycloError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CycloError(f"cannot inspect team destination {destination}: {exc}") from exc
    temporary = destination.with_name(
        f".{destination.name}.new.{os.getpid()}.{os.urandom(6).hex()}"
    )
    template = (
        packaged_team_template(template_name)
        if template_name is not None
        else packaged_agentws_runtime()
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.mkdir(mode=0o700)
        if template_name is None:
            shutil.copytree(template / "roles", temporary / "roles")
            roster_source = template / "default.team"
            (temporary / "Dockerfile").write_text(
                _MINIMAL_TEAM_DOCKERFILE,
                encoding="utf-8",
            )
        else:
            shutil.copytree(
                template,
                temporary,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            roster_source = template / "team"
        output: list[str] = []
        for raw in roster_source.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                fields = stripped.split()
                if len(fields) == 3:
                    raw = f"{raw} {model}"
                elif len(fields) == 4:
                    raw = " ".join([*fields[:3], model])
            output.append(raw)
        (temporary / "team").write_text("\n".join(output) + "\n", encoding="utf-8")
        if initialize_git:
            proc = subprocess.run(["git", "init", "-q", str(temporary)], check=False)
            if proc.returncode != 0:
                raise CycloError(f"git init failed: {destination}")
        # The staging directory is a sibling, so this is an atomic same-filesystem
        # install. POSIX rename replaces a pre-existing empty directory directly.
        os.replace(temporary, destination)
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary)
        if isinstance(exc, CycloError):
            raise
        if isinstance(exc, (OSError, UnicodeError)):
            raise CycloError(f"cannot initialize team repository {destination}: {exc}") from exc
        raise
    return destination
