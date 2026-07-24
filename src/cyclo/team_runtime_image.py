from __future__ import annotations

import os
import uuid
from importlib import resources
from pathlib import Path
from typing import Mapping

from .component_runtime import ComponentController
from .errors import CycloError
from .team import team_dockerfile


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
docker_runner = ComponentController()


def build_context_root() -> Path:
    """Return the common packaged component build context."""

    return Path(str(resources.files("cyclo.components"))).resolve()


def context_root() -> Path:
    """Return Cyclo's packaged team-agent files."""

    return build_context_root() / "team-runtime"


def dockerfile_path() -> Path:
    return context_root() / "Dockerfile"


def build_command() -> list[str]:
    return [
        "build",
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
    # Docker omits Config.User entirely when the image uses the default root
    # account. Treat that canonical representation exactly like an empty User;
    # an explicitly present non-string value is still malformed inspection
    # data.
    user = config.get("User", "")
    if not isinstance(user, str):
        raise CycloError("cannot parse Cyclo team image user")
    account = user.partition(":")[0]
    if account not in {"", "0", "root"}:
        raise CycloError(
            "team image must run Cyclo's runtime ENTRYPOINT as root"
        )
    if (
        base_image is not None
        and image_label(info, BASE_IMAGE_LABEL) != base_image
    ):
        raise CycloError("derived team image was built from a different Cyclo base")
    return image_id(info)


def ensure(image: str) -> str:
    """Ask Docker to build the current team-agent image and validate it."""

    return docker_runner.build_image(
        image,
        build_command(),
        _validate_runtime_image,
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
) -> str:
    """Ask Docker to build one team image on the exact Cyclo base."""

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
