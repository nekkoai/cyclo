from __future__ import annotations

import os
from itertools import combinations
from pathlib import Path
from typing import Iterable

from .docker_endpoint import unix_socket_from_endpoint
from .errors import CycloError


HOST_PSEUDO_FILESYSTEMS = (
    (Path("/proc"), "host process filesystem"),
    (Path("/sys"), "host system filesystem"),
    (Path("/dev"), "host device filesystem"),
    (Path("/run"), "host runtime filesystem"),
)


def paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def paths_overlap_lexically(left: Path, right: Path) -> bool:
    """Also protect a host path name before its final symlink is followed."""

    left = Path(os.path.abspath(left.expanduser()))
    right = Path(os.path.abspath(right.expanduser()))
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def docker_socket_paths(endpoint: str) -> tuple[Path, ...]:
    candidates = [
        Path("/var/run/docker.sock"),
        Path(f"/run/user/{os.getuid()}/docker.sock"),
        Path.home() / ".docker" / "run" / "docker.sock",
    ]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime).expanduser() / "docker.sock")
    selected = unix_socket_from_endpoint(endpoint)
    if selected is not None:
        candidates.append(selected)

    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise CycloError(
                f"cannot resolve selected Docker socket path: {exc}"
            ) from exc
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return tuple(result)


def validate_mount_authority(
    teams: Iterable[tuple[Path, str]],
    mounts: Iterable[tuple[Path, str]],
    *,
    state_root: Path,
    docker_endpoint: str,
    trusted_roots: Iterable[tuple[Path, str]] = (),
) -> None:
    """Keep agent-writable trees away from Cyclo's host authority."""

    selected = [*teams, *mounts]
    for (left, left_label), (right, right_label) in combinations(selected, 2):
        if paths_overlap(left, right) or paths_overlap_lexically(left, right):
            raise CycloError(
                f"{left_label} and {right_label} must be separate filesystem "
                f"trees: {left} and {right}"
            )

    protected: list[tuple[Path, str]] = [
        (state_root, "Cyclo state"),
        (Path.home() / ".pi" / "agent", "host Pi configuration"),
        *HOST_PSEUDO_FILESYSTEMS,
        *((path, "Docker socket") for path in docker_socket_paths(docker_endpoint)),
        *trusted_roots,
    ]
    for mounted_path, mounted_label in selected:
        for protected_path, protected_label in protected:
            if paths_overlap(mounted_path, protected_path) or paths_overlap_lexically(
                mounted_path, protected_path
            ):
                raise CycloError(
                    f"{mounted_label} overlaps {protected_label}: "
                    f"{mounted_path} and {protected_path}"
                )


def validate_team_mount_separation(
    teams: Iterable[tuple[Path, str]],
    mounts: Iterable[tuple[Path, str]],
) -> None:
    """Prevent one project's writable tree from containing another team."""

    for team_path, team_label in teams:
        for mount_path, mount_label in mounts:
            if paths_overlap(team_path, mount_path) or paths_overlap_lexically(
                team_path, mount_path
            ):
                raise CycloError(
                    f"{team_label} and {mount_label} must be separate filesystem "
                    f"trees: {team_path} and {mount_path}"
                )


def validate_strict_source_root_separation(
    sources: Iterable[tuple[Path, str]],
) -> None:
    """Reject nested roots globally while allowing deliberate exact reuse."""

    selected = list(sources)
    for (left, left_label), (right, right_label) in combinations(selected, 2):
        if left == right:
            continue
        if paths_overlap(left, right) or paths_overlap_lexically(left, right):
            raise CycloError(
                f"{left_label} and {right_label} must not contain one another: "
                f"{left} and {right}"
            )
