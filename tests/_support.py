"""A local stand-in for Argus ingest, plus the sys.path shim.

The suite runs against a `ThreadingHTTPServer` on 127.0.0.1:0: a real socket,
real HTTP, real headers. Nothing monkeypatches urllib, so the parts most
likely to break (headers, gzip, the retry loop, the thread hand-off) are
exercised for real, and the suite passes with no network.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

# Run from a checkout as well as against an installed wheel; an installed
# package wins so CI tests what it is about to publish.
if importlib.util.find_spec("naijacloud_argus") is None:
    _SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)

import naijacloud_argus as argus  # noqa: E402
from naijacloud_argus import _client, _scope, _transport  # noqa: E402

#: A test key: a run of zeros, never a real one.
TEST_KEY = "0000000000000000000000000000beef"


class Recorded:
    __slots__ = ("method", "path", "query", "headers", "body")

    def __init__(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> None:
        self.method = method
        self.path, _, self.query = path.partition("?")
        self.headers = headers
        if headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        self.body = body

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def envelope(self) -> List[Any]:
        """The envelope's lines, parsed: [header, item header, event]."""
        return [json.loads(line) for line in self.body.decode("utf-8").splitlines() if line]

    @property
    def event(self) -> Dict[str, Any]:
        return self.envelope[2]

    def __repr__(self) -> str:
        return "<Recorded {} {}>".format(self.method, self.path)


class Canned:
    def __init__(
        self,
        status: int = 200,
        body: Any = b"",
        headers: Optional[Dict[str, str]] = None,
        delay: float = 0.0,
    ) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.delay = delay


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        stub: Ingest = self.server.stub  # type: ignore[attr-defined]
        rec = Recorded(
            self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
        )
        canned = stub.respond(rec)
        if canned.delay:
            time.sleep(canned.delay)
        self.send_response(canned.status)
        for k, v in canned.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(canned.body)))
        self.end_headers()
        self.wfile.write(canned.body)

    do_GET = do_POST

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


class Ingest:
    """Records every request; answers from per-path scripts, else 200."""

    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self._scripts: Dict[str, List[Canned]] = {}
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def dsn(self) -> str:
        return "http://{}@127.0.0.1:{}/42".format(TEST_KEY, self.port)

    def script(self, path_prefix: str, *responses: Canned) -> None:
        with self._lock:
            self._scripts.setdefault(path_prefix, []).extend(responses)

    def respond(self, rec: Recorded) -> Canned:
        with self._lock:
            self.requests.append(rec)
            for prefix, queue in self._scripts.items():
                if rec.path.startswith(prefix) and queue:
                    return queue.pop(0)
        if rec.path == "/v1/flags/evaluate":
            return Canned(200, {"flags": {}, "environment": "production"})
        if rec.path.startswith("/api/"):
            return Canned(200, {"id": "0" * 32})
        return Canned(200, b"{}", {"Content-Type": "application/json"})

    def on(self, path_prefix: str) -> List[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.path.startswith(path_prefix)]

    def wait_for(self, path_prefix: str, count: int = 1, timeout: float = 5.0) -> List[Recorded]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.on(path_prefix)
            if len(got) >= count:
                return got
            time.sleep(0.01)
        return self.on(path_prefix)


ENV_VARS = (
    "ARGUS_DSN",
    "SENTRY_DSN",
    "ARGUS_RELEASE",
    "SENTRY_RELEASE",
    "ARGUS_ENVIRONMENT",
    "SENTRY_ENVIRONMENT",
    "ARGUS_SERVICE_NAME",
    "OTEL_SERVICE_NAME",
    "ARGUS_DEBUG",
)


class ArgusTestCase(unittest.TestCase):
    """A fresh stub and a clean SDK per test."""

    ingest: Ingest

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.pop(k) for k in ENV_VARS if k in os.environ}
        _transport.Transport.backoff_base = 0.01
        _scope.global_scope().clear()
        # Belt and braces: no test inherits another's current span or scope.
        _scope._current.set(None)
        _client._current_span.set(None)
        self.ingest = Ingest()

    def tearDown(self) -> None:
        argus.close(1)
        _scope.global_scope().clear()
        self.ingest.stop()
        _transport.Transport.backoff_base = 1.0
        for k in ENV_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)

    def init(self, **options: Any) -> argus.Client:
        options.setdefault("dsn", self.ingest.dsn)
        return argus.init(**options)

    def flush(self) -> None:
        self.assertTrue(argus.flush(5), "flush timed out")

    def one_event(self) -> Dict[str, Any]:
        self.flush()
        got = self.ingest.on("/api/")
        self.assertEqual(len(got), 1, got)
        return got[0].event


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def attr_map(attrs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """OTLP attributes as a plain dict of their values."""
    out = {}
    for a in attrs:
        ((kind, value),) = a["value"].items()
        out[a["key"]] = int(value) if kind == "intValue" else value
    return out


def raise_and_catch(fn: Callable[[], Any]) -> BaseException:
    try:
        fn()
    except BaseException as e:  # noqa: B036
        return e
    raise AssertionError("did not raise")
