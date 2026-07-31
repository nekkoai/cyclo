from __future__ import annotations

import json
from functools import lru_cache
from typing import Mapping

from .errors import CycloError
from .model_ids import split_public_model_id
from .resources import components_root


_MAX_SAFE_INTEGER = 2**53 - 1


@lru_cache(maxsize=1)
def inference_format() -> str:
    """Return the Pi payload ABI shared with the packaged Node runtime."""

    path = (
        components_root()
        / "protocol"
        / "provider"
        / "src"
        / "abi.json"
    )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        value = document.get("piInferenceFormat")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise CycloError(f"cannot read the bundled provider ABI: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise CycloError("the bundled provider ABI is invalid")
    return value


def _positive_token_limit(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= 16
        and 0 < int(value) <= _MAX_SAFE_INTEGER
    )


def model_incompatibility(model: Mapping[str, object]) -> str:
    """Explain why one typed Provider model cannot drive the Pi team engine."""

    if split_public_model_id(model.get("id")) is None:
        return "invalid provider/model route"
    if model.get("inferenceFormat") != inference_format():
        return "unsupported inference format"
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return "invalid capabilities"
    inputs = capabilities.get("inputModalities")
    if (
        not isinstance(inputs, list)
        or "MODALITY_TEXT" not in inputs
        or any(
            not isinstance(modality, str)
            or modality not in ("MODALITY_TEXT", "MODALITY_IMAGE")
            for modality in inputs
        )
    ):
        return "unsupported input modalities"
    if capabilities.get("outputModalities") != ["MODALITY_TEXT"]:
        return "unsupported output modalities"
    if capabilities.get("extensionTypes", []) != [] or model.get(
        "extensions", []
    ) != []:
        return "unsupported catalogue extensions"
    display_name = model.get("displayName")
    if display_name is not None and not isinstance(display_name, str):
        return "invalid display name"
    if not _positive_token_limit(model.get("contextWindowTokens")):
        return "missing usable context-window limit"
    if not _positive_token_limit(model.get("maxOutputTokens")):
        return "missing usable output-token limit"
    return ""
