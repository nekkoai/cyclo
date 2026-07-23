from __future__ import annotations

import json
from pathlib import Path

import pytest

import cyclo.component as component_module
from cyclo.component import (
    COMPONENT_INTERFACE,
    connect_unary,
    parse_declaration,
    probe_component,
)
from cyclo.errors import CycloError


def test_component_declaration_names_interfaces_and_requirements(
    tmp_path: Path,
) -> None:
    declaration = tmp_path / "component.conf"
    declaration.write_text(
        "component pass\n"
        f"provide {COMPONENT_INTERFACE}\n"
        "provide cyclo.provider.v1.Provider\n"
        "require upstream cyclo.provider.v1.Provider\n",
        encoding="utf-8",
    )

    parsed = parse_declaration(declaration)

    assert parsed.name == "pass"
    assert parsed.provides == (
        COMPONENT_INTERFACE,
        "cyclo.provider.v1.Provider",
    )
    assert [(item.name, item.service) for item in parsed.requires] == [
        ("upstream", "cyclo.provider.v1.Provider")
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    (
        (
            "component pass\nprovide cyclo.provider.v1.Provider\n",
            "every component must provide",
        ),
        (
            f"component ../pass\nprovide {COMPONENT_INTERFACE}\n",
            "invalid component name",
        ),
        (
            f"component pass\nprovide {COMPONENT_INTERFACE}\n"
            "require upstream invalid\n",
            "expected: require",
        ),
        (
            f"component pass\nprovide {COMPONENT_INTERFACE}\n"
            f"provide {COMPONENT_INTERFACE}\n",
            "duplicate provided interface",
        ),
    ),
)
def test_component_declaration_rejects_invalid_contracts(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    declaration = tmp_path / "component.conf"
    declaration.write_text(text, encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        parse_declaration(declaration)


def test_component_declaration_rejects_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.conf"
    real.write_text(
        f"component pass\nprovide {COMPONENT_INTERFACE}\n",
        encoding="utf-8",
    )
    declaration = tmp_path / "component.conf"
    declaration.symlink_to(real.name)

    with pytest.raises(CycloError, match="not a regular file"):
        parse_declaration(declaration)


def _fake_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    body: bytes,
) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []

    class Response:
        reason = "test response"

        def __init__(self) -> None:
            self.status = status

        def read(self, limit: int) -> bytes:
            return body[:limit]

    class Connection:
        def __init__(self, socket_path: Path, timeout: float) -> None:
            self.socket_path = socket_path
            self.timeout = timeout

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers,
        ) -> None:
            requests.append(
                {
                    "socket_path": self.socket_path,
                    "timeout": self.timeout,
                    "method": method,
                    "path": path,
                    "headers": dict(headers),
                    "body": body,
                }
            )

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        component_module,
        "_UnixHTTPConnection",
        Connection,
    )
    return requests


def test_connect_unary_speaks_connect_json_over_a_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = (tmp_path / "component.sock").resolve()
    payload = json.dumps(
        {"status": "HEALTH_STATUS_READY", "message": "ready"}
    ).encode("utf-8")
    requests = _fake_connection(monkeypatch, status=200, body=payload)

    assert probe_component(socket_path) == ("ready", "")

    assert len(requests) == 1
    assert requests[0]["socket_path"] == socket_path
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == f"/{COMPONENT_INTERFACE}/Health"
    assert requests[0]["body"] == b"{}"
    headers = requests[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Type"] == "application/json"
    assert headers["Connect-Protocol-Version"] == "1"
    assert headers["Content-Length"] == "2"


@pytest.mark.parametrize(
    ("socket_path", "service", "method", "timeout", "message"),
    [
        (
            Path("relative.sock"),
            COMPONENT_INTERFACE,
            "Health",
            1,
            "must be absolute",
        ),
        (
            Path("/tmp/component.sock"),
            "bad/service",
            "Health",
            1,
            "service name",
        ),
        (
            Path("/tmp/component.sock"),
            COMPONENT_INTERFACE,
            "Bad/Method",
            1,
            "method name",
        ),
        (
            Path("/tmp/component.sock"),
            COMPONENT_INTERFACE,
            "Health",
            0,
            "timeout",
        ),
        (
            Path("/tmp/component.sock"),
            COMPONENT_INTERFACE,
            "Health",
            float("inf"),
            "timeout",
        ),
    ],
)
def test_connect_unary_rejects_invalid_calls_before_connecting(
    socket_path: Path,
    service: str,
    method: str,
    timeout: float,
    message: str,
) -> None:
    with pytest.raises(CycloError, match=message):
        connect_unary(socket_path, service, method, timeout=timeout)


def test_connect_unary_fails_closed_on_errors_and_oversized_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = (tmp_path / "component.sock").resolve()
    _fake_connection(
        monkeypatch,
        status=503,
        body=b'{"message":"not ready"}',
    )
    with pytest.raises(CycloError, match=r"failed \(503\): not ready"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    _fake_connection(monkeypatch, status=200, body=b"[]")
    with pytest.raises(CycloError, match="not a JSON object"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    _fake_connection(monkeypatch, status=200, body=b'{"status":NaN}')
    with pytest.raises(CycloError, match="invalid JSON"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    monkeypatch.setattr(component_module, "MAX_RPC_BYTES", 16)
    _fake_connection(
        monkeypatch,
        status=200,
        body=b'{"message":"far too large"}',
    )
    with pytest.raises(CycloError, match="response exceeds"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")
    with pytest.raises(CycloError, match="request exceeds"):
        connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            {"message": "far too large"},
        )
    with pytest.raises(CycloError, match="request is not valid JSON"):
        connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            {"temperature": float("nan")},
        )


def test_probe_component_preserves_a_concrete_connection_error(
    tmp_path: Path,
) -> None:
    socket_path = (tmp_path / "missing.sock").resolve()

    health, error = probe_component(socket_path)

    assert health == "unreachable"
    assert str(socket_path) in error


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        (" upstream provider unavailable ", "upstream provider unavailable"),
        ("", "component reported not ready"),
    ),
)
def test_probe_component_preserves_not_ready_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected: str,
) -> None:
    socket_path = (tmp_path / "component.sock").resolve()
    _fake_connection(
        monkeypatch,
        status=200,
        body=json.dumps(
            {
                "status": "HEALTH_STATUS_NOT_READY",
                "message": message,
            }
        ).encode("utf-8"),
    )

    assert probe_component(socket_path) == ("not-ready", expected)
