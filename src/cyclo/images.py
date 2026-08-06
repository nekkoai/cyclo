from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import CycloError


_MAX_DOCKER_DIAGNOSTIC_CHARS = 16_384
_TRUNCATED_DIAGNOSTIC_PREFIX = "[earlier Docker output omitted]\n"


def _bounded_diagnostic(output: str) -> str:
    detail = output.strip()
    if len(detail) <= _MAX_DOCKER_DIAGNOSTIC_CHARS:
        return detail
    tail_size = _MAX_DOCKER_DIAGNOSTIC_CHARS - len(
        _TRUNCATED_DIAGNOSTIC_PREFIX
    )
    return _TRUNCATED_DIAGNOSTIC_PREFIX + detail[-tail_size:]


@dataclass(frozen=True)
class Image:
    reference: str
    id: str
    config: Mapping[str, object]

    @property
    def has_healthcheck(self) -> bool:
        value = self.config.get("Healthcheck")
        return isinstance(value, Mapping) and bool(value.get("Test"))


class Images:
    """Small Docker image builder; it owns no container lifecycle."""

    def __init__(self, *, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.endpoint:
            environment["DOCKER_HOST"] = self.endpoint
            environment.pop("DOCKER_CONTEXT", None)
        return environment

    def inspect(self, reference: str, *, missing_ok: bool = False) -> Image | None:
        result = self.command(
            ["image", "inspect", "--", reference],
            check=not missing_ok,
        )
        if result.returncode != 0:
            return None
        try:
            documents = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CycloError("Docker returned invalid image inspection JSON") from exc
        if (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], Mapping)
        ):
            raise CycloError("Docker returned invalid image inspection data")
        document = documents[0]
        identifier = document.get("Id")
        config = document.get("Config")
        if (
            not isinstance(identifier, str)
            or not identifier.startswith("sha256:")
            or len(identifier) != 71
            or not isinstance(config, Mapping)
        ):
            raise CycloError("Docker returned an invalid image identity")
        return Image(reference, identifier, config)

    def build(
        self,
        reference: str,
        *,
        dockerfile: Path,
        context: Path,
        build_args: Sequence[tuple[str, str]] = (),
        labels: Sequence[tuple[str, str]] = (),
        require_healthcheck: bool = True,
    ) -> Image:
        selected_dockerfile = dockerfile.expanduser().resolve(strict=True)
        selected_context = context.expanduser().resolve(strict=True)
        if not selected_dockerfile.is_file() or not selected_context.is_dir():
            raise CycloError("Docker build requires a Dockerfile and context directory")
        try:
            selected_dockerfile.relative_to(selected_context)
        except ValueError as exc:
            raise CycloError("Dockerfile must be inside its build context") from exc
        with tempfile.NamedTemporaryFile(
            prefix="cyclo-image-id-",
            delete=False,
        ) as stream:
            iidfile = Path(stream.name)
        try:
            iidfile.unlink(missing_ok=True)
            command = [
                "build",
                "--iidfile",
                str(iidfile),
                "--tag",
                reference,
            ]
            for key, value in build_args:
                command.extend(("--build-arg", f"{key}={value}"))
            for key, value in labels:
                command.extend(("--label", f"{key}={value}"))
            command.extend(
                (
                    "--file",
                    str(selected_dockerfile),
                    str(selected_context),
                )
            )
            self.command(command)
            image = self.inspect(reference)
            assert image is not None
            try:
                built_id = iidfile.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as exc:
                raise CycloError("Docker did not write the built image ID") from exc
            if image.id != built_id:
                raise CycloError(
                    f"Docker tag {reference!r} does not identify the image just built"
                )
            return self._validate(
                image,
                require_healthcheck=require_healthcheck,
                labels=labels,
            )
        finally:
            iidfile.unlink(missing_ok=True)

    @staticmethod
    def _validate(
        image: Image,
        *,
        require_healthcheck: bool,
        labels: Sequence[tuple[str, str]] = (),
    ) -> Image:
        if require_healthcheck and not image.has_healthcheck:
            raise CycloError(
                f"Docker image {image.reference!r} has no OCI HEALTHCHECK"
            )
        actual = image.config.get("Labels")
        actual_labels = actual if isinstance(actual, Mapping) else {}
        for key, value in labels:
            if actual_labels.get(key) != value:
                raise CycloError(
                    f"Docker image {image.reference!r} has an unexpected "
                    f"{key} label"
                )
        return image

    def command(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        input_data: str | None = None,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a Docker command using this installation's endpoint."""

        try:
            result = subprocess.run(
                ["docker", *arguments],
                check=False,
                text=True,
                input=input_data,
                capture_output=capture,
                env=self.environment(),
            )
        except OSError as exc:
            raise CycloError(f"cannot execute Docker: {exc}") from exc
        if check and result.returncode != 0:
            detail = _bounded_diagnostic(result.stderr or result.stdout or "")
            raise CycloError(
                f"Docker command failed ({result.returncode})"
                + (f": {detail}" if detail else "")
            )
        return result
