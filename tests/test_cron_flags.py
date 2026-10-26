"""Cron check-ins and feature flags (spec section 3)."""

from __future__ import annotations

import asyncio
import unittest

from _support import TEST_KEY, ArgusTestCase, Canned

import naijacloud_argus as argus
from naijacloud_argus._scrub import FILTERED


class Cron(ArgusTestCase):
    def paths(self) -> list:
        self.flush()
        return [r.path for r in self.ingest.on("/v1/cron/")]

    def test_statuses(self) -> None:
        self.init()
        argus.cron.check_in("nightly-invoices", "start")
        argus.cron.check_in("nightly-invoices", "ok")
        argus.cron.check_in("nightly-invoices", "error", "boom")
        argus.cron.check_in("nightly-invoices", 0)
        argus.cron.check_in("nightly-invoices", 3)
        argus.cron.check_in("a b/c")
        self.assertEqual(
            self.paths(),
            [
                "/v1/cron/nightly-invoices/start",
                "/v1/cron/nightly-invoices",
                "/v1/cron/nightly-invoices/fail",
                "/v1/cron/nightly-invoices/0",
                "/v1/cron/nightly-invoices/3",
                "/v1/cron/a%20b%2Fc",
            ],
        )
        req = self.ingest.on("/v1/cron/")[2]
        self.assertEqual(req.headers["authorization"], "Bearer " + TEST_KEY)
        self.assertTrue(req.headers["content-type"].startswith("text/plain"))
        self.assertEqual(req.body, b"boom")

    def test_output_is_scrubbed_and_capped_at_2_kb(self) -> None:
        self.init()
        argus.cron.check_in("job", "error", "password=hunter2 " + "x" * 5000)
        self.flush()
        body = self.ingest.on("/v1/cron/")[0].body.decode()
        self.assertTrue(body.startswith("password={} ".format(FILTERED)))
        self.assertEqual(len(body), 2048)

    def test_monitor_context_manager(self) -> None:
        self.init()
        with argus.cron.monitor("ok-job"):
            pass
        with self.assertRaises(RuntimeError):
            with argus.cron.monitor("bad-job"):
                raise RuntimeError("disk full")
        self.assertEqual(
            self.paths(),
            [
                "/v1/cron/ok-job/start",
                "/v1/cron/ok-job",
                "/v1/cron/bad-job/start",
                "/v1/cron/bad-job/fail",
            ],
        )
        self.assertEqual(
            self.ingest.on("/v1/cron/bad-job/fail")[0].body, b"RuntimeError: disk full"
        )

    def test_monitor_and_sys_exit(self) -> None:
        self.init()
        for code in (0, None, 2, "bye"):
            with self.assertRaises(SystemExit):
                with argus.cron.monitor("j{}".format(code)):
                    raise SystemExit(code)
        paths = self.paths()
        self.assertIn("/v1/cron/j0", paths)
        self.assertIn("/v1/cron/jNone", paths)
        self.assertIn("/v1/cron/j2/2", paths)
        self.assertIn("/v1/cron/jbye/fail", paths)

    def test_monitor_decorators(self) -> None:
        self.init()

        @argus.cron.monitor("sync-job")
        def sync_job() -> int:
            return 1

        @argus.cron.monitor("async-job")
        async def async_job() -> int:
            return 2

        self.assertEqual(sync_job(), 1)
        self.assertEqual(asyncio.run(async_job()), 2)
        self.assertEqual(
            self.paths(),
            [
                "/v1/cron/sync-job/start",
                "/v1/cron/sync-job",
                "/v1/cron/async-job/start",
                "/v1/cron/async-job",
            ],
        )

    def test_unknown_slug_404_is_dropped(self) -> None:
        self.init()
        self.ingest.script("/v1/cron/", Canned(404))
        argus.cron.check_in("missing")
        argus.cron.check_in("missing")
        self.assertEqual(len(self.paths()), 2)  # 404 is not retried, and does not stop the lane


class Flags(ArgusTestCase):
    def answer(self, flags: dict) -> None:
        self.ingest.script(
            "/v1/flags/evaluate", Canned(200, {"flags": flags, "environment": "production"})
        )

    def test_request_and_answer(self) -> None:
        self.init(environment="staging")
        self.answer({"new-checkout": "on", "old": "off", "theme": "dark"})
        self.assertTrue(argus.flags.is_on("new-checkout", "user-1", attributes={"plan": "pro"}))
        (req,) = self.ingest.on("/v1/flags/evaluate")
        self.assertEqual(req.headers["authorization"], "Bearer " + TEST_KEY)
        self.assertEqual(req.headers["content-type"], "application/json")
        self.assertEqual(
            req.json, {"subject": "user-1", "environment": "staging", "attributes": {"plan": "pro"}}
        )

    def test_variants_defaults_and_cache(self) -> None:
        self.init()
        self.answer({"new-checkout": "on", "old": "off", "theme": "dark", "legacy": "true"})
        self.assertTrue(argus.flags.is_on("new-checkout", "u"))
        self.assertFalse(argus.flags.is_on("old", "u", True))
        self.assertTrue(argus.flags.is_on("legacy", "u"))
        self.assertTrue(argus.flags.is_on("unknown", "u", True))
        self.assertFalse(argus.flags.is_on("unknown", "u"))
        self.assertEqual(argus.flags.variant("theme", "u", "light"), "dark")
        self.assertEqual(argus.flags.variant("unknown", "u", "light"), "light")
        self.assertEqual(argus.flags.all("u")["theme"], "dark")
        # One request for all of that: cached 30 s per subject.
        self.assertEqual(len(self.ingest.on("/v1/flags/evaluate")), 1)
        argus.flags.is_on("x", "another-user")
        self.assertEqual(len(self.ingest.on("/v1/flags/evaluate")), 2)

    def test_async(self) -> None:
        self.init()
        self.answer({"f": "on", "theme": "dark"})

        async def main() -> tuple:
            return (
                await argus.flags.is_on_async("f", "u"),
                await argus.flags.variant_async("theme", "u", "light"),
                await argus.flags.all_async("u"),
            )

        on, theme, every = asyncio.run(main())
        self.assertTrue(on)
        self.assertEqual(theme, "dark")
        self.assertEqual(every, {"f": "on", "theme": "dark"})

    def test_errors_return_the_default(self) -> None:
        self.init()
        self.ingest.script("/v1/flags/evaluate", Canned(500), Canned(200, b"not json"))
        self.assertTrue(argus.flags.is_on("f", "u", True))
        self.assertEqual(argus.flags.variant("f", "u2", "fallback"), "fallback")

    def test_401_stops_asking(self) -> None:
        self.init()
        self.ingest.script("/v1/flags/evaluate", Canned(401))
        self.assertFalse(argus.flags.is_on("f", "u"))
        self.assertFalse(argus.flags.is_on("f", "u2"))
        self.assertEqual(len(self.ingest.on("/v1/flags/evaluate")), 1)

    def test_429_waits(self) -> None:
        self.init()
        self.ingest.script("/v1/flags/evaluate", Canned(429, headers={"Retry-After": "60"}))
        self.assertTrue(argus.flags.is_on("f", "u", True))
        self.assertTrue(argus.flags.is_on("f", "u2", True))
        self.assertEqual(len(self.ingest.on("/v1/flags/evaluate")), 1)

    def test_slow_argus_returns_the_default_in_time(self) -> None:
        import time

        self.init(flags_timeout=0.2)
        self.ingest.script("/v1/flags/evaluate", Canned(200, {"flags": {"f": "on"}}, delay=1.0))
        start = time.monotonic()
        self.assertFalse(argus.flags.is_on("f", "u"))
        self.assertLess(time.monotonic() - start, 0.8)

    def test_empty_subject(self) -> None:
        self.init()
        self.assertEqual(argus.flags.variant("f", "", "d"), "d")
        self.assertEqual(self.ingest.on("/v1/flags/evaluate"), [])


if __name__ == "__main__":
    unittest.main()
