from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from cyclo.provider_client import list_models


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert self.path == "/cyclo.provider.v1.Provider/ListModels"
        length = int(self.headers["Content-Length"])
        assert self.rfile.read(length) == b"{}"
        body = json.dumps({"models": [{"id": "example/model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def test_lists_models_through_transparent_connect_transport() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        assert list_models(server.server_port) == {
            "models": [{"id": "example/model"}]
        }
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_empty_proto_catalogue_is_normalized() -> None:
    original = Handler.do_POST

    def empty(self) -> None:
        length = int(self.headers["Content-Length"])
        self.rfile.read(length)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    Handler.do_POST = empty
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        assert list_models(server.server_port) == {"models": []}
    finally:
        Handler.do_POST = original
        server.shutdown()
        thread.join()
        server.server_close()
