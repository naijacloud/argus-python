"""No DSN is a no-op, and SDK code never raises into the app (spec 1 and 6)."""

from __future__ import annotations

import asyncio
import threading
import unittest

from _support import ArgusTestCase

import naijacloud_argus as argus


def _everything() -> None:
    argus.set_user({"id": "u"})
    argus.set_user(None)
    argus.set_tag("k", "v")
    argus.set_tags({"a": 1})
    argus.set_context("c", {"x": 1})
    argus.set_context("c", None)
    argus.set_extra("e", object())
    argus.add_breadcrumb(message="m")
    assert argus.capture_exception(ValueError("x")) == ""
    assert argus.capture_message("m") == ""
    argus.log.info("i", k=1)
    argus.metrics.count("c")
    argus.metrics.gauge("g", 1)
    argus.metrics.distribution("d", 1)
    with argus.trace("t") as span:
        span.set_attribute("a", 1)
        assert not span.is_recording
    span2 = argus.start_span("s")
    span2.end()
    argus.cron.check_in("slug", "start")
    with argus.cron.monitor("slug"):
        pass
    assert argus.flags.is_on("f", "u", True) is True
    assert argus.flags.is_on("f", "u") is False
    assert argus.flags.variant("f", "u", "blue") == "blue"
    assert argus.flags.all("u") == {}
    assert asyncio.run(argus.flags.is_on_async("f", "u", True)) is True
    assert argus.flush() is True


class NoDsn(ArgusTestCase):
    def test_before_init(self) -> None:
        argus.close()
        self.assertIsNone(argus.get_client())
        _everything()
        self.assertEqual(self.ingest.requests, [])

    def test_init_without_a_dsn(self) -> None:
        c = argus.init()
        self.assertFalse(c.enabled)
        _everything()
        self.assertEqual(self.ingest.requests, [])

    def test_invalid_dsn(self) -> None:
        self.assertFalse(argus.init(dsn="not a dsn").enabled)
        _everything()

    def test_enabled_false(self) -> None:
        self.assertFalse(self.init(enabled=False).enabled)
        _everything()
        self.assertEqual(self.ingest.requests, [])

    def test_no_threads_are_started_without_a_dsn(self) -> None:
        before = {t.name for t in threading.enumerate()}
        argus.init()
        _everything()
        after = {t.name for t in threading.enumerate() if t.name.startswith("argus")}
        self.assertTrue(after <= before)

    def test_after_close(self) -> None:
        self.init()
        argus.close()
        _everything()
        self.assertEqual(self.ingest.requests, [])


class NeverRaises(ArgusTestCase):
    def test_odd_inputs(self) -> None:
        self.init()

        class Hostile:
            def __str__(self) -> str:
                raise RuntimeError("no str")

            def __repr__(self) -> str:
                raise RuntimeError("no repr")

        class BadError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no str")

        argus.capture_exception(BadError())
        argus.capture_message(Hostile())  # type: ignore[arg-type]
        argus.set_tag("k", Hostile())
        argus.set_tags(None)  # type: ignore[arg-type]
        argus.set_user("not a dict")  # type: ignore[arg-type]
        argus.add_breadcrumb("not a dict")  # type: ignore[arg-type]
        argus.log.info(Hostile())  # type: ignore[arg-type]
        argus.log.info("x", {"h": Hostile()})
        argus.metrics.count(Hostile())  # type: ignore[arg-type]
        argus.metrics.count("c", attributes={"h": Hostile()})
        argus.cron.check_in(None)  # type: ignore[arg-type]
        argus.flags.is_on("f", None)  # type: ignore[arg-type]
        argus.flags.all("u", {"h": Hostile()})
        with argus.trace("t") as s:
            s.set_attribute("h", Hostile())
        argus.flush(2)

    def test_hooks_that_raise(self) -> None:
        def boom(*a: object) -> None:
            raise RuntimeError("hook bug")

        self.init(before_send=boom, before_breadcrumb=boom)
        argus.add_breadcrumb(message="x")
        self.assertTrue(argus.capture_message("still sent"))
        self.flush()
        self.assertEqual(len(self.ingest.on("/api/")), 1)

    def test_unreachable_ingest(self) -> None:
        from _support import closed_port

        argus.init(dsn="http://k@127.0.0.1:{}/1".format(closed_port()), timeout=0.2)
        self.assertTrue(argus.capture_message("x"))
        self.assertFalse(argus.flags.is_on("f", "u"))
        argus.flush(0.5)  # returns, whatever happened

    def test_user_exceptions_still_propagate(self) -> None:
        self.init()
        with self.assertRaises(KeyError):
            with argus.trace("t"):
                raise KeyError("mine")
        with self.assertRaises(KeyError):
            with argus.cron.monitor("job"):
                raise KeyError("mine")


if __name__ == "__main__":
    unittest.main()
