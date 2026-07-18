from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .errors import CycloError
from .host_config import ProviderDefinition
from .provider_runtime import (
    CONTAINER_PROVIDER_SOCKET,
    ProviderIdentity,
    ProviderRuntime,
    ProviderSpec,
    ProviderStatus,
    provider_generation,
    provider_source_fingerprint,
)


PROVIDER_REGISTRY_VERSION = 1
PROVIDER_CLIENT_PREFIX = "host-provider"
RUNTIME_PROVIDER_SOCKET_ROOT = Path("/run/cyclo/providers")


@dataclass(frozen=True)
class ProviderClient:
    project_id: str
    name: str
    generation: str
    kind: str
    provider_prefix: str


@dataclass(frozen=True)
class PreparedProvider:
    definition: ProviderDefinition
    identity: ProviderIdentity
    source_fingerprint: str
    generation: str
    client: ProviderClient
    ingress_token_file: Path
    upstream_token_file: Path
    socket_id: str
    provider_socket_dir: Path


def provider_client_id(prefix: str) -> str:
    digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:16]
    return f"{PROVIDER_CLIENT_PREFIX}-{prefix[:20]}-{digest}"


def provider_socket_id(prefix: str) -> str:
    """Return the short, collision-resistant runtime socket directory name."""

    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]


def provider_definition_spec(
    runtime_state: Path, definition: ProviderDefinition
) -> ProviderSpec:
    """Describe a configured provider without preparing mutable host state."""

    selected = Path(runtime_state).expanduser().resolve()
    identity = ProviderRuntime(selected).identity(definition.prefix)
    secret_root = selected / "providers" / "secrets" / definition.prefix
    return ProviderSpec(
        identity=identity,
        source=definition.path,
        arguments=definition.arguments,
        runtime_socket_dir=(
            selected
            / "sockets"
            / "runtime"
            / provider_socket_id(definition.prefix)
        ),
        provider_socket_dir=(
            selected / "sockets" / "providers" / provider_socket_id(definition.prefix)
        ),
        upstream_token_file=secret_root / "upstream.token",
        provider_token_file=secret_root / "provider.token",
    )


def _open_state_directory(anchor: Path, path: Path, mode: int) -> int:
    """Open/create a descendant without following symlinks in its path."""

    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise CycloError(f"provider state path escapes its root: {path}") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not relative.parts:
        try:
            descriptor = os.open(anchor, flags)
            os.fchmod(descriptor, mode)
            return descriptor
        except OSError as exc:
            raise CycloError(
                f"cannot prepare provider state directory {path}: {exc}"
            ) from exc
    descriptor = -1
    try:
        descriptor = os.open(anchor, flags)
        current = anchor
        for index, part in enumerate(relative.parts):
            current /= part
            child_mode = mode if index == len(relative.parts) - 1 else 0o700
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, child_mode, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise CycloError(
                        f"refusing symlinked provider state directory or "
                        f"non-directory: {current}"
                    ) from exc
                raise
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, mode)
        result = descriptor
        descriptor = -1
        return result
    except OSError as exc:
        raise CycloError(f"cannot prepare provider state directory {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_atomic(
    anchor: Path,
    path: Path,
    content: bytes,
    mode: int,
    *,
    parent_mode: int = 0o700,
) -> None:
    parent_descriptor = _open_state_directory(anchor, path.parent, parent_mode)
    temporary = f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode, dir_fd=parent_descriptor, follow_symlinks=False)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise CycloError(f"cannot write provider state {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _read_token(anchor: Path, path: Path) -> str:
    parent_descriptor = _open_state_directory(anchor, path.parent, 0o700)
    descriptor = -1
    try:
        metadata = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        os.close(parent_descriptor)
        raise CycloError(f"cannot read provider capability {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        os.close(parent_descriptor)
        raise CycloError(f"provider capability is not a regular file: {path}")
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise CycloError(f"provider capability changed while reading: {path}")
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(64 * 1024 + 1)
    except OSError as exc:
        raise CycloError(f"cannot read provider capability {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    if not raw or len(raw) > 64 * 1024:
        raise CycloError(f"provider capability has an invalid size: {path}")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CycloError(f"provider capability is not UTF-8: {path}") from exc
    if not value or any(character.isspace() for character in value):
        raise CycloError(f"provider capability is malformed: {path}")
    return value


def _stable_ingress_token(anchor: Path, path: Path) -> str:
    if path.exists() or path.is_symlink():
        value = _read_token(anchor, path)
        return value
    # This is an opaque, provider-local capability. It never grants access to
    # real credentials and is accepted only for the preauthorized route.
    value = secrets.token_urlsafe(48)
    _write_atomic(anchor, path, (value + "\n").encode("utf-8"), 0o444)
    return value


class HostProviders:
    """Explicit provider-container launch state owned by the provider runtime."""

    def __init__(self, runtime_state: Path) -> None:
        selected = Path(runtime_state).expanduser()
        if selected.is_symlink():
            raise CycloError(
                f"refusing symlinked provider state root: {selected}"
            )
        try:
            selected.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CycloError(
                f"cannot prepare provider state root {selected}: {exc}"
            ) from exc
        if selected.is_symlink() or not selected.is_dir():
            raise CycloError(
                f"refusing unsafe provider state root: {selected}"
            )
        os.chmod(selected, 0o700)
        self.state_root = selected.resolve()
        self.runtime = ProviderRuntime(self.state_root)
        self.registry_dir = self.state_root / "registry"
        self.registry_path = self.registry_dir / "expected-providers.json"
        self.secrets_dir = self.state_root / "providers" / "secrets"
        self.runtime_socket_dir = self.state_root / "sockets" / "runtime"
        self.provider_sockets_dir = self.state_root / "sockets" / "providers"

    def ensure_socket_layout(self) -> None:
        # The root is runtime-private. Each provider receives only its own
        # world-traversable socket subdirectory as a separate read-only mount.
        descriptor = _open_state_directory(
            self.state_root, self.runtime_socket_dir, 0o700
        )
        os.close(descriptor)
        descriptor = _open_state_directory(
            self.state_root, self.provider_sockets_dir, 0o755
        )
        os.close(descriptor)

    def ensure_registry(self) -> None:
        # The runtime runs as the invoking host UID and reads this hash-only
        # registry from its persistent state mount.
        descriptor = _open_state_directory(
            self.state_root, self.registry_dir, 0o700
        )
        os.close(descriptor)
        if not self.registry_path.exists():
            self.publish(())

    def _secret_paths(self, prefix: str) -> tuple[Path, Path]:
        root = self.secrets_dir / prefix
        descriptor = _open_state_directory(self.state_root, root, 0o700)
        os.close(descriptor)
        return root / "provider.token", root / "upstream.token"

    def rotate_capabilities(self, item: PreparedProvider) -> None:
        """Replace both provider-local bearers before an explicit relaunch."""

        for path in (item.ingress_token_file, item.upstream_token_file):
            value = secrets.token_urlsafe(48)
            _write_atomic(
                self.state_root,
                path,
                (value + "\n").encode("utf-8"),
                0o444,
            )

    def prepare(
        self,
        definitions: Iterable[ProviderDefinition],
        *,
        selected_prefixes: Iterable[str] | None = None,
    ) -> tuple[PreparedProvider, ...]:
        self.ensure_socket_layout()
        selected = tuple(definitions)
        wanted = None if selected_prefixes is None else set(selected_prefixes)
        declared = {definition.prefix for definition in selected}
        earlier: set[str] = set()
        prepared: list[PreparedProvider] = []
        for definition in selected:
            for model in definition.inputs:
                input_prefix = model.partition("/")[0]
                if input_prefix in declared and input_prefix not in earlier:
                    raise CycloError(
                        f"{definition.prefix} input {model!r} is a forward or "
                        "self reference; host providers are dependency ordered"
                    )
            if wanted is not None and definition.prefix not in wanted:
                earlier.add(definition.prefix)
                continue
            identity = self.runtime.identity(definition.prefix)
            source_fingerprint = provider_source_fingerprint(definition.path)
            generation = provider_generation(
                identity,
                definition.arguments,
                source_fingerprint,
            )
            ingress_path, upstream_path = self._secret_paths(definition.prefix)
            _stable_ingress_token(self.state_root, ingress_path)
            _stable_ingress_token(self.state_root, upstream_path)
            socket_id = provider_socket_id(definition.prefix)
            runtime_socket_dir = self.runtime_socket_dir / socket_id
            descriptor = _open_state_directory(
                self.state_root, runtime_socket_dir, 0o777
            )
            os.close(descriptor)
            provider_socket_dir = self.provider_sockets_dir / socket_id
            descriptor = _open_state_directory(
                self.state_root, provider_socket_dir, 0o777
            )
            os.close(descriptor)
            client = ProviderClient(
                project_id=provider_client_id(definition.prefix),
                name=f"provider:{definition.prefix}",
                generation=generation,
                kind="provider",
                provider_prefix=definition.prefix,
            )
            prepared.append(
                PreparedProvider(
                    definition=definition,
                    identity=identity,
                    source_fingerprint=source_fingerprint,
                    generation=generation,
                    client=client,
                    ingress_token_file=ingress_path,
                    upstream_token_file=upstream_path,
                    socket_id=socket_id,
                    provider_socket_dir=provider_socket_dir,
                )
            )
            earlier.add(definition.prefix)
        return tuple(prepared)

    @staticmethod
    def provider_scopes(
        prepared: Iterable[PreparedProvider],
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
        providers: dict[str, tuple[str, ...]] = {}
        models: dict[str, tuple[str, ...]] = {}
        for item in prepared:
            providers[item.client.project_id] = tuple(
                dict.fromkeys(model.partition("/")[0] for model in item.definition.inputs)
            )
            models[item.client.project_id] = item.definition.inputs
        return providers, models

    def spec(self, item: PreparedProvider) -> ProviderSpec:
        return ProviderSpec(
            identity=item.identity,
            source=item.definition.path,
            arguments=item.definition.arguments,
            runtime_socket_dir=self.runtime_socket_dir / item.socket_id,
            provider_socket_dir=item.provider_socket_dir,
            upstream_token_file=item.upstream_token_file,
            provider_token_file=item.ingress_token_file,
        )

    def build_spec(self, definition: ProviderDefinition) -> ProviderSpec:
        """Describe an image build without issuing capabilities or socket state."""

        return provider_definition_spec(self.state_root, definition)

    def expectation(
        self, item: PreparedProvider, status: ProviderStatus | None = None
    ) -> dict[str, object]:
        if status is not None and (
            status.identity != item.identity or status.generation != item.generation
        ):
            raise CycloError(
                f"provider runtime identity changed before registry publication: "
                f"{item.definition.prefix}"
            )
        ingress = _read_token(self.state_root, item.ingress_token_file)
        return {
            "prefix": item.definition.prefix,
            "generation": item.generation,
            "configuration_sha256": item.definition.configuration_sha256,
            "token_sha256": hashlib.sha256(ingress.encode("utf-8")).hexdigest(),
            "inputs": list(item.definition.inputs),
            "socket_path": str(
                RUNTIME_PROVIDER_SOCKET_ROOT
                / item.socket_id
                / CONTAINER_PROVIDER_SOCKET.name
            ),
        }

    def client_record(self, item: PreparedProvider) -> dict[str, object]:
        """Return the runtime capability record for component-to-runtime calls."""

        upstream = _read_token(self.state_root, item.upstream_token_file)
        input_providers = tuple(
            dict.fromkeys(model.partition("/")[0] for model in item.definition.inputs)
        )
        return {
            "client_id": item.client.project_id,
            "kind": "provider",
            "provider_prefix": item.definition.prefix,
            "team_id": item.client.name,
            "binding_generation": item.generation,
            "token_sha256": hashlib.sha256(
                upstream.encode("utf-8")
            ).hexdigest(),
            "providers": list(input_providers),
            "models": list(item.definition.inputs),
            "enabled": True,
            "revoked": False,
            "expires_at": None,
        }

    def client_records(
        self, prepared: Iterable[PreparedProvider]
    ) -> tuple[dict[str, object], ...]:
        return tuple(self.client_record(item) for item in prepared)

    def publish(self, providers: Iterable[Mapping[str, object]]) -> None:
        directory_descriptor = _open_state_directory(
            self.state_root, self.registry_dir, 0o700
        )
        document = {
            "version": PROVIDER_REGISTRY_VERSION,
            "providers": [dict(provider) for provider in providers],
        }
        content = (
            json.dumps(document, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        # Unlike capability files this contains hashes and route metadata only.
        # The runtime reads it as the invoking host UID.
        temporary = (
            f".{self.registry_path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(
                temporary,
                0o644,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            os.replace(
                temporary,
                self.registry_path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise CycloError(
                f"cannot publish host provider registry {self.registry_path}: {exc}"
            ) from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            os.close(directory_descriptor)

    def published_expectations(self) -> list[dict[str, object]]:
        try:
            document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise CycloError(
                f"cannot read host provider registry {self.registry_path}: {exc}"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != PROVIDER_REGISTRY_VERSION
            or not isinstance(document.get("providers"), list)
        ):
            raise CycloError(f"invalid host provider registry: {self.registry_path}")
        published: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in document["providers"]:
            if not isinstance(raw, dict):
                raise CycloError(f"invalid host provider registry: {self.registry_path}")
            prefix = raw.get("prefix")
            if not isinstance(prefix, str) or prefix in seen:
                raise CycloError(f"invalid host provider registry: {self.registry_path}")
            seen.add(prefix)
            published.append(dict(raw))
        return published

    def remove_expectations(self, prefixes: Iterable[str]) -> None:
        removed = set(prefixes)
        self.publish(
            record
            for record in self.published_expectations()
            if record.get("prefix") not in removed
        )

    def upsert_expectations(
        self, providers: Iterable[Mapping[str, object]]
    ) -> None:
        published = self.published_expectations()
        by_prefix = {
            record["prefix"]: dict(record)
            for record in published
            if isinstance(record.get("prefix"), str)
        }
        order = [str(record["prefix"]) for record in published]
        for provider in providers:
            selected = dict(provider)
            prefix = selected.get("prefix")
            if not isinstance(prefix, str) or not prefix:
                raise CycloError("invalid provider expectation prefix")
            if prefix not in by_prefix:
                order.append(prefix)
            by_prefix[prefix] = selected
        self.publish(by_prefix[prefix] for prefix in order)
