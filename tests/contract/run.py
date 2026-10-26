#!/usr/bin/env python3
"""Contract test against a real Argus (staging or prod), not a stand-in.

    ARGUS_CONTRACT_DSN=<dsn of a test source> \\
    ARGUS_CONTRACT_API=https://argus-api.naijacloud-staging.com \\
    ARGUS_CONTRACT_TOKEN=<a token that can read that workspace> \\
    python tests/contract/run.py

Sends an error, a log, a span, a metric, a cron check-in and a flag
evaluation through the package, checks Argus answered 2xx to each, and, when
ARGUS_CONTRACT_API and _TOKEN are set, waits for the error to show up as an
issue. The cron check-in expects a monitor with slug `argus-python-contract`
(a 404 is reported, not failed, when it is missing).

Runs against the installed package, or src/ from a checkout. Not part of the
unit suite: it needs the network and a real DSN.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")
try:
    import naijacloud_argus  # noqa: F401
except ImportError:
    sys.path.insert(0, SRC)

import naijacloud_argus as argus  # noqa: E402
from naijacloud_argus import _transport  # noqa: E402

statuses: List[Tuple[str, int]] = []
_orig_post = _transport.http_post


def _recording_post(opener: Any, req: Any, timeout: float) -> Any:
    res = _orig_post(opener, req, timeout)
    statuses.append((urllib.parse.urlsplit(req.url).path, res.status))
    return res


def main() -> int:
    dsn = os.environ.get("ARGUS_CONTRACT_DSN")
    if not dsn:
        print("set ARGUS_CONTRACT_DSN to a test source DSN", file=sys.stderr)
        return 2
    # Both the queued transport and the flag client call http_post through the
    # module, so recording here sees every request without changing behaviour.
    _transport.http_post = _recording_post  # type: ignore[assignment]
    import naijacloud_argus._client as client_mod

    client_mod.http_post = _recording_post  # type: ignore[assignment]

    argus.init(
        dsn=dsn,
        release="argus-python@contract",
        environment="contract",
        service_name="argus-python-contract",
        debug=bool(os.environ.get("ARGUS_DEBUG")),
    )
    marker = "argus-python contract {}".format(os.urandom(4).hex())
    with argus.trace("contract.run"):
        try:
            try:
                json.loads("{not json")
            except ValueError as cause:
                raise RuntimeError(marker) from cause
        except RuntimeError as err:
            event_id = argus.capture_exception(err, extra={"password": "hunter2"})
        argus.log.info("contract log line", marker=marker)
    argus.metrics.count("argus_python_contract_runs", 1)
    argus.cron.check_in("argus-python-contract", "ok")
    flag_value = argus.flags.is_on("argus-python-contract-flag", "contract-user", False)
    if not argus.close(15):
        print("flush timed out", file=sys.stderr)
        return 1

    failed = False

    def by(prefix: str) -> List[int]:
        return [s for p, s in statuses if p.startswith(prefix)]

    def ok(prefix: str) -> None:
        nonlocal failed
        got = by(prefix)
        if got and all(200 <= s < 300 for s in got):
            print("✓ {} {}".format(prefix, ",".join(map(str, got))))
        else:
            print("✗ {} {}".format(prefix, got))
            failed = True

    ok("/api/")
    ok("/v1/logs")
    ok("/v1/traces")
    ok("/v1/metrics")
    cron = by("/v1/cron/")
    if cron and all(200 <= s < 300 for s in cron):
        print("✓ /v1/cron/ {}".format(",".join(map(str, cron))))
    elif cron and all(s == 404 for s in cron):
        print(
            "• /v1/cron/ 404 (create a cron monitor with slug argus-python-contract to check this)"
        )
    else:
        print("✗ /v1/cron/ {}".format(cron))
        failed = True
    ok("/v1/flags/evaluate")
    print("  flag argus-python-contract-flag = {}".format(flag_value))
    print("  event {}".format(event_id))

    api = os.environ.get("ARGUS_CONTRACT_API")
    token = os.environ.get("ARGUS_CONTRACT_TOKEN")
    if api and token and not failed:
        found = None
        for _ in range(30):
            time.sleep(2)
            url = "{}/v1/issues?q={}&limit=5".format(api, urllib.parse.quote(marker))
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    issues = json.loads(res.read().decode()).get("issues") or []
            except Exception:
                continue
            found = next((i for i in issues if marker in str(i.get("title", ""))), None)
            if found:
                break
        if not found:
            print("✗ the error {!r} never showed up as an issue".format(marker))
            failed = True
        else:
            print("✓ issue {} grouped from the Argus SDK".format(found.get("id")))

    print("contract failed" if failed else "contract passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
