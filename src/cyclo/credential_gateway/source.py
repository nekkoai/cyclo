from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path


PACKAGE = "cyclo.credential_gateway"


def package_root() -> Path:
    return Path(str(resources.files(PACKAGE))).resolve()


def gateway_context_root() -> Path:
    return package_root() / "gateway_context"


def gateway_dockerfile_path() -> Path:
    return gateway_context_root() / "Dockerfile"


def filesystem_source_files(root: Path) -> tuple[Path, ...]:
    ignored_dirs = {".git", ".pytest_cache", "__pycache__", "build", "dist", "node_modules", "test"}
    result: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in ignored_dirs or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file() and not path.name.endswith(".pyc"):
            result.append(rel)
    return tuple(sorted(result, key=lambda item: item.as_posix()))


def source_fingerprint(root: Path) -> str:
    """Hash the exact packaged build context, independent of a Git checkout."""

    selected = Path(root).resolve()
    digest = hashlib.sha256()
    for rel in filesystem_source_files(selected):
        path = selected / rel
        stat = path.stat()
        digest.update(rel.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        # Hash the executable contract, not umask-dependent group/other bits.
        # This makes source and installed-wheel fingerprints identical.
        digest.update(b"x" if stat.st_mode & 0o111 else b"-")
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()
