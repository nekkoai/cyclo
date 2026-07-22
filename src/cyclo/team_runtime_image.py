from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
from typing import Mapping

from .component_stack import ComponentDocker
from .errors import CycloError


SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"
PI_PACKAGES = (
    "npm:pi-web-access",
    "npm:pi-lens",
    "npm:pi-simplify",
    "/opt/cyclo/pi-provider",
)
_IMAGE_CONTEXT_MEMBERS = ("team", "component", "provider", "pi-provider")
docker_runner = ComponentDocker()
_IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "test",
}


def build_context_root() -> Path:
    """Return the common packaged component build context."""

    return Path(str(resources.files("cyclo._bundle"))).resolve()


def context_root() -> Path:
    """Return Cyclo's packaged team-agent files."""

    return build_context_root() / "team"


def dockerfile_path() -> Path:
    return context_root() / "Dockerfile"


def source_files(root: Path | None = None) -> tuple[Path, ...]:
    selected = (build_context_root() if root is None else Path(root)).resolve()
    result: list[Path] = []
    for member in _IMAGE_CONTEXT_MEMBERS:
        for path in (selected / member).rglob("*"):
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

    selected = (build_context_root() if root is None else Path(root)).resolve()
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
        str(build_context_root()),
    ]


def image_label(info: Mapping[str, object], name: str) -> str | None:
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    value = labels.get(name) if isinstance(labels, Mapping) else None
    return value if isinstance(value, str) and value else None


def ensure(image: str, *, build: bool = False) -> None:
    """Build the team-agent image only when explicitly requested or stale."""

    fingerprint = source_fingerprint()
    info = docker_runner.inspect("image", image)
    current = info is not None and image_label(
        info, SOURCE_FINGERPRINT_LABEL
    ) == fingerprint
    if build or not current:
        result = docker_runner.call(
            build_command(image, fingerprint)[1:],
            capture=False,
            check=False,
        )
        if result.returncode != 0:
            raise CycloError(f"failed to build Cyclo team runtime image: {image}")
