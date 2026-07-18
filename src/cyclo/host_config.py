from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import CycloError


DEFAULT_HOST_CONFIG = Path("/etc/cyclo/host.conf")
MAX_HOST_CONFIG_BYTES = 1024 * 1024
MAX_PROVIDER_MODEL_ID_LENGTH = 1024
PROVIDER_PREFIX_RE = re.compile(r"^[a-z0-9_-]+$")
PROVIDER_PARAMETER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_PROVIDER_PREFIXES = frozenset(
    {"__proto__", "constructor", "gateway", "prototype"}
)


@dataclass(frozen=True)
class ProviderDefinition:
    """One host provider implementation and its declared dependency inputs."""

    prefix: str
    path: Path
    arguments: tuple[str, ...]
    inputs: tuple[str, ...]
    parameters: tuple[tuple[str, str], ...]
    line: int
    configuration_sha256: str


def provider_configuration_sha256(
    prefix: str,
    configured_path: str,
    arguments: tuple[str, ...],
) -> str:
    """Identify the exact host.conf fields that select a provider process."""

    payload = json.dumps(
        [prefix, configured_path, *arguments],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HostConfig:
    """Read Cyclo's deliberately small, line-oriented host configuration.

    Provider input models precede provider-owned ``key=value`` parameters.
    The complete tail is also preserved verbatim for the provider process.
    """

    def __init__(self, path: str | Path = DEFAULT_HOST_CONFIG) -> None:
        selected = Path(path).expanduser()
        if not selected.is_absolute():
            selected = Path.cwd() / selected
        # Keep the lexical location of host.conf: relative provider paths are
        # relative to the configured file, even when that file is a symlink.
        self.path = selected.absolute()

    def _read(self) -> str | None:
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CycloError(f"cannot read Cyclo host configuration {self.path}: {exc}") from exc
        if len(content) > MAX_HOST_CONFIG_BYTES:
            raise CycloError(
                f"Cyclo host configuration exceeds {MAX_HOST_CONFIG_BYTES} bytes: "
                f"{self.path}"
            )
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CycloError(
                f"Cyclo host configuration is not valid UTF-8: {self.path}"
            ) from exc

    def _provider_path(self, value: str, line: int) -> Path:
        if value == "~" or value.startswith("~/"):
            raise CycloError(
                f"{self.path}:{line}: provider path must not use '~'; use an "
                "absolute path or a path relative to host.conf"
            )
        source = Path(value)
        if not source.is_absolute():
            source = self.path.parent / source
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CycloError(
                f"{self.path}:{line}: provider path not found: {source}"
            ) from exc
        if not source.is_dir():
            raise CycloError(
                f"{self.path}:{line}: provider path is not a directory: {source}"
            )
        dockerfile = source / "Dockerfile"
        try:
            mode = dockerfile.stat().st_mode
        except OSError as exc:
            raise CycloError(
                f"{self.path}:{line}: provider path has no Dockerfile: {source}"
            ) from exc
        if not stat.S_ISREG(mode):
            raise CycloError(
                f"{self.path}:{line}: provider Dockerfile is not a regular file: "
                f"{dockerfile}"
            )
        return source

    def _provider_input(self, value: str, line: int) -> str:
        provider, separator, model_id = value.partition("/")
        if (
            not separator
            or not PROVIDER_PREFIX_RE.fullmatch(provider)
            or provider in RESERVED_PROVIDER_PREFIXES
            or not model_id
            or len(model_id.encode("utf-16-le")) // 2
            > MAX_PROVIDER_MODEL_ID_LENGTH
            or any(
                character.isspace()
                or ord(character) < 0x20
                or ord(character) == 0x7F
                for character in model_id
            )
        ):
            raise CycloError(
                f"{self.path}:{line}: invalid provider input {value!r}; "
                "expected provider/model"
            )
        return value

    def _provider_arguments(
        self, values: tuple[str, ...], line: int
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        inputs: list[str] = []
        input_set: set[str] = set()
        parameters: list[tuple[str, str]] = []
        parameter_keys: set[str] = set()
        saw_parameter = False
        for value in values:
            if "=" not in value:
                if saw_parameter:
                    raise CycloError(
                        f"{self.path}:{line}: provider input {value!r} appears "
                        "after a parameter; list every input before key=value "
                        "parameters"
                    )
                selected = self._provider_input(value, line)
                if selected in input_set:
                    raise CycloError(
                        f"{self.path}:{line}: duplicate provider input {selected!r}"
                    )
                input_set.add(selected)
                inputs.append(selected)
                continue

            saw_parameter = True
            key, parameter_value = value.split("=", 1)
            if not PROVIDER_PARAMETER_KEY_RE.fullmatch(key):
                raise CycloError(
                    f"{self.path}:{line}: invalid provider parameter key {key!r}; "
                    "use lowercase letters, numbers, underscore, or hyphen"
                )
            if key in parameter_keys:
                raise CycloError(
                    f"{self.path}:{line}: duplicate provider parameter {key!r}"
                )
            parameter_keys.add(key)
            parameters.append((key, parameter_value))

        if not inputs:
            raise CycloError(
                f"{self.path}:{line}: provider requires at least one input model; "
                "expected provider/model before any key=value parameters"
            )
        return tuple(inputs), tuple(parameters)

    def load(self) -> tuple[ProviderDefinition, ...]:
        """Return configured providers; a missing or empty file is gateway-only."""

        text = self._read()
        if text is None:
            return ()

        providers: list[ProviderDefinition] = []
        prefixes: set[str] = set()
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if fields[0] != "provider":
                raise CycloError(
                    f"{self.path}:{line_number}: unknown host directive "
                    f"{fields[0]!r}; expected 'provider'"
                )
            if len(fields) < 3:
                raise CycloError(
                    f"{self.path}:{line_number}: expected "
                    "provider <prefix> <path> <provider/model> ... "
                    "[key=value ...]"
                )
            prefix = fields[1]
            if (
                not PROVIDER_PREFIX_RE.fullmatch(prefix)
                or prefix in RESERVED_PROVIDER_PREFIXES
            ):
                raise CycloError(
                    f"{self.path}:{line_number}: invalid provider prefix "
                    f"{prefix!r}; use lowercase letters, numbers, underscore, "
                    "or hyphen"
                )
            if prefix in prefixes:
                raise CycloError(
                    f"{self.path}:{line_number}: duplicate provider prefix {prefix!r}"
                )
            arguments = tuple(fields[3:])
            inputs, parameters = self._provider_arguments(arguments, line_number)
            providers.append(
                ProviderDefinition(
                    prefix=prefix,
                    path=self._provider_path(fields[2], line_number),
                    arguments=arguments,
                    inputs=inputs,
                    parameters=parameters,
                    line=line_number,
                    configuration_sha256=provider_configuration_sha256(
                        prefix,
                        fields[2],
                        arguments,
                    ),
                )
            )
            prefixes.add(prefix)
        earlier: set[str] = set()
        for provider in providers:
            for model in provider.inputs:
                dependency = model.partition("/")[0]
                if dependency in prefixes and dependency not in earlier:
                    raise CycloError(
                        f"{self.path}:{provider.line}: provider input {model!r} "
                        "is a forward or self reference"
                    )
            earlier.add(provider.prefix)
        return tuple(providers)
