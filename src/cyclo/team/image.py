from __future__ import annotations

import hashlib
import os
from typing import Mapping

from .. import __version__
from ..dcomp_system import component_name
from ..errors import CycloError
from ..images import Image, Images
from ..resources import components_root
from .definition import Team


class TeamImageBuilder:
    """Build and validate the common or team-derived component image."""

    def __init__(self, images: Images, system: str) -> None:
        self.images = images
        self.system = system
        self._base: Image | None = None

    def build(self, team: Team, *, override: str = "") -> Image:
        require_non_root_team_host()
        if override:
            image = self.images.inspect(override, missing_ok=True)
            if image is None:
                raise CycloError(f"team runtime image is not built: {override}")
            self.validate(image)
            return image

        base = self._base_image()
        if team.dockerfile is None:
            return base

        identity = hashlib.sha256(str(team.root).encode("utf-8")).hexdigest()[:12]
        reference = (
            f"cyclo-{self.system}-team-"
            f"{component_name('team', team.name)[5:33]}-{identity}:{__version__}"
        )
        image = self.images.build(
            reference,
            dockerfile=team.dockerfile,
            context=team.root,
            build_args=(("CYCLO_TEAM_BASE", base.reference),),
            labels=(("io.cyclo.team-base", base.id),),
        )
        self.validate(image)
        label = _labels(image).get("io.cyclo.team-base")
        if label != base.id:
            raise CycloError(
                f"derived team image {reference!r} was not built from the "
                "current Cyclo team base"
            )
        return image

    def _base_image(self) -> Image:
        if self._base is not None:
            return self._base
        root = components_root()
        reference = f"cyclo-{self.system}-team:{__version__}"
        image = self.images.build(
            reference,
            dockerfile=root / "team" / "Dockerfile",
            context=root,
            build_args=(
                ("CYCLO_HOST_UID", str(os.getuid())),
                ("CYCLO_HOST_GID", str(os.getgid())),
            ),
        )
        self.validate(image)
        self._base = image
        return image

    @staticmethod
    def validate(image: Image) -> None:
        if image.config.get("Entrypoint") != [
            "/usr/local/bin/cyclo-container-entrypoint"
        ]:
            raise CycloError(
                "team image must inherit Cyclo's runtime ENTRYPOINT unchanged"
            )
        if image.config.get("User", "") not in {"", "0", "root"}:
            raise CycloError(
                "team image must let Cyclo's entrypoint drop host identity"
            )
        if not image.has_healthcheck:
            raise CycloError("team image must inherit Cyclo's OCI HEALTHCHECK")


def _labels(image: Image) -> dict[str, str]:
    value = image.config.get("Labels")
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def require_non_root_team_host() -> None:
    if os.getuid() == 0:
        raise CycloError(
            "refusing to run a Cyclo team as host root because that would "
            "make the agent root inside its container"
        )
