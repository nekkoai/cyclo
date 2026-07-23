from __future__ import annotations

import hashlib
import os
import uuid
from importlib import resources
from pathlib import Path
from typing import Mapping

from .component_stack import ComponentDocker
from .errors import CycloError
from .team import team_dockerfile


SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"
BASE_IMAGE_LABEL = "cyclo.team-base-image"
TEAM_RUNTIME_ENTRYPOINT = (
    "tini",
    "--",
    "/usr/local/bin/cyclo-container-entrypoint",
)
PI_PACKAGES = (
    "npm:pi-web-access",
    "npm:pi-lens",
    "npm:pi-simplify",
    "/opt/cyclo/pi-provider",
)
_IMAGE_CONTEXT_MEMBERS = (
    "team-runtime",
    "protocol/component",
    "protocol/provider",
    "pi-provider",
)
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

    return Path(str(resources.files("cyclo.components"))).resolve()


def context_root() -> Path:
    """Return Cyclo's packaged team-agent files."""

    return build_context_root() / "team-runtime"


def dockerfile_path() -> Path:
    return context_root() / "Dockerfile"


def source_files(root: Path | None = None) -> tuple[Path, ...]:
    selected = (build_context_root() if root is None else Path(root)).resolve()
    result: list[Path] = []
    for member in _IMAGE_CONTEXT_MEMBERS:
        for directory, directories, filenames in os.walk(selected / member):
            directories[:] = sorted(
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES and not name.endswith(".egg-info")
            )
            for name in sorted(filenames):
                if name.endswith(".pyc"):
                    continue
                path = Path(directory) / name
                if path.is_file():
                    result.append(path.relative_to(selected))
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


def build_command(fingerprint: str) -> list[str]:
    return [
        "build",
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


def image_id(info: Mapping[str, object]) -> str:
    value = info.get("Id")
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise CycloError("cannot parse Cyclo team image ID")
    return value


def _validate_runtime_image(
    info: Mapping[str, object],
    *,
    fingerprint: str | None = None,
    base_image: str | None = None,
) -> str:
    config = info.get("Config")
    if not isinstance(config, Mapping):
        raise CycloError("cannot parse Cyclo team image configuration")
    entrypoint = config.get("Entrypoint")
    if entrypoint != list(TEAM_RUNTIME_ENTRYPOINT):
        raise CycloError(
            "team image must inherit Cyclo's runtime ENTRYPOINT unchanged"
        )
    if (
        fingerprint is not None
        and image_label(info, SOURCE_FINGERPRINT_LABEL) != fingerprint
    ):
        raise CycloError("team image has an unexpected source fingerprint")
    if (
        base_image is not None
        and image_label(info, BASE_IMAGE_LABEL) != base_image
    ):
        raise CycloError("derived team image was built from a different Cyclo base")
    return image_id(info)


def ensure(image: str, *, build: bool = False) -> str:
    """Build the team-agent image only when explicitly requested or stale."""

    fingerprint = source_fingerprint()
    info = docker_runner.inspect("image", image)
    current = info is not None and image_label(
        info, SOURCE_FINGERPRINT_LABEL
    ) == fingerprint
    if not build and current:
        assert info is not None
        return _validate_runtime_image(info, fingerprint=fingerprint)
    return docker_runner.build_image(
        image,
        build_command(fingerprint),
        lambda built: _validate_runtime_image(
            built,
            fingerprint=fingerprint,
        ),
    )


def require(image: str) -> str:
    """Require an operator-supplied team runtime image without rebuilding it."""

    info = docker_runner.inspect("image", image)
    if info is None:
        raise CycloError(f"team runtime image is not built: {image}")
    return _validate_runtime_image(info)


def derived_build_command(
    root: Path,
    dockerfile: Path,
    base_reference: str,
    base_image_id: str,
) -> list[str]:
    return [
        "build",
        "--label",
        f"{BASE_IMAGE_LABEL}={base_image_id}",
        "--build-arg",
        f"CYCLO_TEAM_BASE={base_reference}",
        "--file",
        str(dockerfile),
        str(root),
    ]


def ensure_derived(
    image: str,
    root: Path,
    base_image: str,
    *,
    build: bool = False,
) -> str:
    """Build or reuse one team repository's image on the exact Cyclo base."""

    info = docker_runner.inspect("image", image)
    current = (
        info is not None
        and image_label(info, BASE_IMAGE_LABEL) == base_image
    )
    if not build and current:
        assert info is not None
        return _validate_runtime_image(
            info,
            base_image=base_image,
        )
    if not build:
        state = "stale" if info is not None else "missing"
        raise CycloError(
            f"derived team image is {state}: {image}; "
            "run the project with --build"
        )
    if team_dockerfile(root) is None:
        raise CycloError(f"team Dockerfile disappeared before build: {root}")
    base_reference = f"cyclo-team-base-pin:{os.getpid()}-{uuid.uuid4()}"
    try:
        docker_runner.call(["image", "tag", "--", base_image, base_reference])
        pinned = docker_runner.inspect("image", base_reference, missing=False)
        assert pinned is not None
        if image_id(pinned) != base_image:
            raise CycloError(
                "temporary Cyclo team base tag has an unexpected image ID"
            )

        def require_pinned_base() -> None:
            current_base = docker_runner.inspect(
                "image",
                base_reference,
                missing=False,
            )
            assert current_base is not None
            if image_id(current_base) != base_image:
                raise CycloError(
                    "temporary Cyclo team base tag changed during the build"
                )

        return docker_runner.build_image(
            image,
            derived_build_command(
                root,
                root / "Dockerfile",
                base_reference,
                base_image,
            ),
            lambda built: _validate_runtime_image(
                built,
                base_image=base_image,
            ),
            before_promote=require_pinned_base,
        )
    finally:
        docker_runner.call(["image", "rm", "--", base_reference], check=False)
