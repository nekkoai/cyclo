from __future__ import annotations

import re


MAX_PROVIDER_PREFIX_LENGTH = 64
MAX_LOCAL_MODEL_ID_UTF16_UNITS = 1024
RESERVED_PROVIDER_PREFIXES = frozenset(
    ("__proto__", "constructor", "gateway", "prototype")
)

_PROVIDER_PREFIX = re.compile(
    rf"[a-z0-9][a-z0-9_-]{{0,{MAX_PROVIDER_PREFIX_LENGTH - 1}}}"
)


def is_provider_prefix(value: object) -> bool:
    """Return whether *value* is a safe public provider/account prefix."""

    return bool(
        isinstance(value, str)
        and _PROVIDER_PREFIX.fullmatch(value)
        and value not in RESERVED_PROVIDER_PREFIXES
    )


def utf16_units(value: str) -> int:
    """Count JavaScript-compatible UTF-16 code units."""

    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _has_unpaired_surrogate(value: str) -> bool:
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if (
                index + 1 >= len(value)
                or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF
            ):
                return True
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            return True
        index += 1
    return False


def is_local_model_id(value: object) -> bool:
    """Return whether *value* is a bounded opaque provider-local model ID."""

    return bool(
        isinstance(value, str)
        and value
        and utf16_units(value) <= MAX_LOCAL_MODEL_ID_UTF16_UNITS
        and not _has_unpaired_surrogate(value)
        and not any(
            character.isspace()
            or character == "\ufeff"
            or ord(character) <= 0x1F
            or ord(character) == 0x7F
            for character in value
        )
    )


def split_public_model_id(value: object) -> tuple[str, str] | None:
    """Split a valid ``PROVIDER/MODEL`` public ID, or return ``None``."""

    if not isinstance(value, str):
        return None
    provider, separator, model = value.partition("/")
    if (
        not separator
        or not is_provider_prefix(provider)
        or not is_local_model_id(model)
    ):
        return None
    return provider, model
