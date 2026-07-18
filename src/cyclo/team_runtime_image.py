from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

from .credential_gateway import docker as docker_runner
from .errors import CycloError


SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"
_IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "test",
}


def context_root() -> Path:
    """Return Cyclo's packaged team-agent image context."""

    return Path(str(resources.files("cyclo"))).resolve() / "team_runtime_context"


def dockerfile_path() -> Path:
    return context_root() / "Dockerfile"


def source_files(root: Path | None = None) -> tuple[Path, ...]:
    selected = (context_root() if root is None else Path(root)).resolve()
    result: list[Path] = []
    for path in selected.rglob("*"):
        relative = path.relative_to(selected)
        if any(
            part in _IGNORED_DIRECTORIES or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        if path.is_file() and not path.name.endswith(".pyc"):
            result.append(relative)
    return tuple(sorted(result, key=lambda item: item.as_posix()))


def source_fingerprint(root: Path | None = None) -> str:
    """Hash the exact packaged team-agent image context."""

    selected = (context_root() if root is None else Path(root)).resolve()
    digest = hashlib.sha256()
    for relative in source_files(selected):
        path = selected / relative
        metadata = path.stat()
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(b"x" if metadata.st_mode & 0o111 else b"-")
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def build_command(image: str, fingerprint: str) -> list[str]:
    return [
        "docker",
        "build",
        "-t",
        image,
        "--label",
        f"{SOURCE_FINGERPRINT_LABEL}={fingerprint}",
        "-f",
        str(dockerfile_path()),
        str(context_root()),
    ]


def ensure(image: str, *, build: bool = False) -> None:
    """Build the team-agent image only when explicitly requested or stale."""

    fingerprint = source_fingerprint()
    current = (
        docker_runner.docker_image_exists(image)
        and docker_runner.docker_image_label(image, SOURCE_FINGERPRINT_LABEL)
        == fingerprint
    )
    if build or not current:
        if docker_runner.run_command(build_command(image, fingerprint)) != 0:
            raise CycloError(f"failed to build Cyclo team runtime image: {image}")
