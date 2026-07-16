from __future__ import annotations

from . import source


SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"


def docker_build_command(image: str, fingerprint: str) -> list[str]:
    return [
        "docker",
        "build",
        "-t",
        image,
        "--label",
        f"{SOURCE_FINGERPRINT_LABEL}={fingerprint}",
        "-f",
        str(source.dockerfile_path()),
        str(source.runtime_context_root()),
    ]
