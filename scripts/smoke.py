#!/usr/bin/env python3
"""Smoke test for an INSTALLED naijacloud-argus (a wheel in a fresh venv).

    python scripts/smoke.py

Refuses to run against a checkout's src/, starts a local stand-in for
ingest, sends one of every signal and checks each arrived in the right shape.
No network, no third-party packages. Exits non-zero on any failure.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

# Never pick up src/ by accident: this checks what pip installed.
here = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") not in (here, os.path.dirname(here))]

import naijacloud_argus as argus  # noqa: E402

received: List[Dict[str, Any]] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        out = b'{"flags":{"smoke":"on"}}' if self.path == "/v1/flags/evaluate" else b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a: Any) -> None:
        return


def check(cond: bool, what: str) -> None:
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        sys.exit(1)


def main() -> None:
    location = os.path.dirname(argus.__file__)
    check("site-packages" in location or "dist-packages" in location, "installed copy: " + location)
    check(argus.__version__ == "0.1.0" and argus.CONTRACT == "argus.sdk/1", "version and contract")

    # No DSN: a no-op that never raises.
    for k in ("ARGUS_DSN", "SENTRY_DSN"):
        os.environ.pop(k, None)
    check(not argus.init().enabled, "no DSN is a no-op")
    check(argus.capture_message("x") == "" and argus.flags.is_on("f", "u", True), "no-op answers")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    argus.init(dsn="http://smoke0000@127.0.0.1:{}/7".format(server.server_address[1]))
    with argus.trace("smoke"):
        try:
            raise ValueError("smoke test error")
        except ValueError as e:
            check(len(argus.capture_exception(e)) == 32, "capture_exception returns an event id")
        argus.log.info("smoke log", step=1)
    argus.metrics.count("smoke.runs")
    argus.cron.check_in("smoke", "ok")
    check(argus.flags.is_on("smoke", "user-1"), "flag evaluated")
    check(argus.close(5), "flushed")

    paths = sorted(r["path"] for r in received)
    for p in ("/api/7/envelope/", "/v1/logs", "/v1/metrics", "/v1/traces", "/v1/cron/smoke"):
        check(p in paths, "received " + p)
    env = next(r for r in received if r["path"].startswith("/api/"))
    lines = [json.loads(x) for x in env["body"].decode().splitlines() if x]
    check(lines[0]["sdk"]["name"] == "argus.python", "envelope sdk.name argus.python")
    check(lines[2]["platform"] == "python", "event platform python")
    check(lines[2]["exception"]["values"][-1]["value"] == "smoke test error", "exception value")
    server.shutdown()
    print("smoke passed")


if __name__ == "__main__":
    main()
