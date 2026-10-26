"""Transport rules (spec section 6)."""

from __future__ import annotations

import time
import unittest

from _support import ArgusTestCase, Canned, closed_port

import naijacloud_argus as argus
from naijacloud_argus import _transport
from naijacloud_argus._transport import Request, Transport, retry_after


class Retries(ArgusTestCase):
    def test_5xx_is_retried_until_it_works(self) -> None:
        self.init()
        self.ingest.script("/api/", Canned(503), Canned(500))
        argus.capture_message("x")
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 3)

    def test_at_most_5_tries(self) -> None:
        self.init()
        self.ingest.script("/api/", *[Canned(502) for _ in range(10)])
        argus.capture_message("x")
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 5)

    def test_backoff_doubles_and_is_capped(self) -> None:
        t = Transport()
        t.backoff_base, t.backoff_cap = 1.0, 60.0
        self.assertEqual([t._backoff(n) for n in range(1, 9)], [1, 2, 4, 8, 16, 32, 60, 60])

    def test_network_errors_are_retried(self) -> None:
        port = closed_port()
        t = Transport()
        attempts = []
        real = _transport.http_post

        def counting(opener, req, timeout):  # type: ignore[no-untyped-def]
            attempts.append(1)
            return real(opener, req, timeout)

        _transport.http_post = counting  # type: ignore[assignment]
        try:
            t.send("errors", Request("http://127.0.0.1:{}/x".format(port), b"x", {}))
            self.assertTrue(t.flush(5))
        finally:
            _transport.http_post = real  # type: ignore[assignment]
        self.assertEqual(len(attempts), 5)

    def test_other_4xx_is_dropped_without_retry(self) -> None:
        self.init()
        self.ingest.script("/api/", Canned(400), Canned(413))
        argus.capture_message("a")
        argus.capture_message("b")
        argus.capture_message("c")
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 3)

    def test_redirects_are_not_followed(self) -> None:
        self.init()
        self.ingest.script("/api/", Canned(307, headers={"Location": "http://evil.example/"}))
        argus.capture_message("x")
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 1)


class RateLimits(ArgusTestCase):
    def test_429_waits_retry_after_then_resends(self) -> None:
        self.init()
        self.ingest.script("/api/", Canned(429, headers={"Retry-After": "1"}))
        start = time.monotonic()
        argus.capture_message("x")
        self.flush()
        reqs = self.ingest.wait_for("/api/", 2)
        self.assertEqual(len(reqs), 2)
        self.assertGreaterEqual(time.monotonic() - start, 0.9)

    def test_429_is_per_endpoint(self) -> None:
        self.init()
        self.ingest.script("/api/", Canned(429, headers={"Retry-After": "30"}))
        argus.capture_message("x")
        self.ingest.wait_for("/api/", 1)
        argus.log.info("logs still flow")
        argus.flush(2)
        self.assertEqual(len(self.ingest.on("/v1/logs")), 1)
        self.assertEqual(len(self.ingest.on("/api/")), 1)  # still waiting

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(retry_after({"retry-after": "12"}), 12)
        self.assertEqual(retry_after({"x-sentry-rate-limits": "30:error;default:organization"}), 30)
        self.assertEqual(retry_after({}), 60)
        self.assertEqual(retry_after({"retry-after": "0.1"}), 1)
        self.assertEqual(retry_after({"retry-after": "999999"}), 3600)
        self.assertEqual(retry_after({"retry-after": "nonsense"}), 60)
        self.assertGreater(retry_after({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"}), 3000)


class RefusedKey(ArgusTestCase):
    def test_401_stops_that_endpoint_only(self) -> None:
        c = self.init()
        self.ingest.script("/api/", Canned(401))
        argus.capture_message("refused")
        self.flush()
        argus.capture_message("dropped, never sent")
        argus.log.info("logs still flow")
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 1)
        self.assertEqual(len(self.ingest.on("/v1/logs")), 1)
        self.assertTrue(c.transport.lane_state("errors")["dead"])

    def test_403_too(self) -> None:
        self.init()
        self.ingest.script("/v1/traces", Canned(403))
        with argus.trace("a"):
            pass
        self.flush()
        with argus.trace("b"):
            pass
        self.flush()
        self.assertEqual(len(self.ingest.on("/v1/traces")), 1)


class Queues(unittest.TestCase):
    def test_at_most_100_items_drop_the_oldest(self) -> None:
        t = Transport()
        lane = _transport._Lane("x")
        t._lanes["x"] = lane
        t._ensure_worker = lambda lane: None  # type: ignore[method-assign]
        for i in range(150):
            t.send("x", Request("http://127.0.0.1:1/", str(i).encode(), {}))
        self.assertEqual(len(lane.queue), 100)
        self.assertEqual(lane.queue[0].body, b"50")

    def test_at_most_1_mb(self) -> None:
        t = Transport()
        lane = _transport._Lane("x")
        t._lanes["x"] = lane
        t._ensure_worker = lambda lane: None  # type: ignore[method-assign]
        for _ in range(5):
            t.send("x", Request("http://127.0.0.1:1/", b"x" * 300_000, {}))
        self.assertLessEqual(lane.bytes, 1 << 20)
        self.assertEqual(len(lane.queue), 3)

    def test_flush_times_out(self) -> None:
        from _support import Ingest

        stub = Ingest()
        try:
            stub.script("/slow", Canned(200, delay=1.0))
            t = Transport()
            t.send("x", Request("http://127.0.0.1:{}/slow".format(stub.port), b"x", {}))
            start = time.monotonic()
            self.assertFalse(t.flush(0.2))
            self.assertLess(time.monotonic() - start, 0.6)
            self.assertTrue(t.flush(3))
        finally:
            stub.stop()

    def test_send_never_raises(self) -> None:
        t = Transport()
        self.assertTrue(t.send("x", Request("not a url", b"x", {})))
        self.assertTrue(t.flush(2))

    def test_after_close_nothing_is_queued(self) -> None:
        t = Transport()
        t.close(0)
        self.assertFalse(t.send("x", Request("http://127.0.0.1:1/", b"x", {})))

    def test_orphaned_lane_is_drained_by_flush(self) -> None:
        # What happens at interpreter shutdown on 3.12+, where no thread starts.
        from _support import Ingest

        stub = Ingest()
        try:
            t = Transport()
            t._ensure_worker = lambda lane: None  # type: ignore[method-assign]
            t.send("x", Request("http://127.0.0.1:{}/v1/logs".format(stub.port), b"x", {}))
            self.assertTrue(t.flush(3))
            self.assertEqual(len(stub.on("/v1/logs")), 1)
        finally:
            stub.stop()


if __name__ == "__main__":
    unittest.main()
