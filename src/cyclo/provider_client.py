from __future__ import annotations

import http.client
import json
from math import isfinite
from typing import Mapping

from .dcomp_system import PROVIDER_SERVICE
from .errors import CycloError


MAX_RPC_BYTES = 16 * 1024 * 1024


def unary(
    host: str,
    port: int,
    service: str,
    method: str,
    request: Mapping[str, object] | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    """Make one bounded ConnectRPC JSON call to a loopback-published component."""

    if host != "127.0.0.1":
        raise CycloError("host-side provider RPC is restricted to loopback")
    if not 1 <= port <= 65535:
        raise CycloError("provider RPC port must be between 1 and 65535")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise CycloError("provider RPC timeout must be a positive finite number")
    try:
        body = json.dumps(
            {} if request is None else request,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CycloError("provider RPC request is not valid JSON") from exc
    if len(body) > MAX_RPC_BYTES:
        raise CycloError("provider RPC request is too large")

    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(
            "POST",
            f"/{service}/{method}",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RPC_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise CycloError(f"provider RPC failed: {exc}") from exc
    finally:
        connection.close()
    if len(raw) > MAX_RPC_BYTES:
        raise CycloError("provider RPC response is too large")
    try:
        document = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CycloError("provider RPC returned invalid JSON") from exc
    if response.status != 200:
        detail = document.get("message") if isinstance(document, dict) else None
        raise CycloError(
            f"provider RPC failed ({response.status}): "
            f"{detail if isinstance(detail, str) and detail else response.reason}"
        )
    if not isinstance(document, dict):
        raise CycloError("provider RPC response is not a JSON object")
    return document


def list_models(port: int) -> dict[str, object]:
    result = unary("127.0.0.1", port, PROVIDER_SERVICE, "ListModels")
    # ProtoJSON omits an empty repeated field. Present one stable catalogue
    # shape to Cyclo's domain layer without interpreting model contents.
    if "models" not in result:
        result["models"] = []
    return result
