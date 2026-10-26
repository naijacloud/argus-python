"""Errors: the Sentry envelope (spec section 4)."""

from __future__ import annotations

import unittest

from _support import TEST_KEY, ArgusTestCase, raise_and_catch

import naijacloud_argus as argus
from naijacloud_argus._scrub import FILTERED


def _fail_inner() -> None:
    raise KeyError("missing")


def _fail_outer() -> None:
    try:
        _fail_inner()
    except KeyError as e:
        raise ValueError("payment failed") from e


class Envelope(ArgusTestCase):
    def test_shape(self) -> None:
        self.init(release="1.4.2", environment="staging", server_name="web-1")
        event_id = argus.capture_exception(raise_and_catch(_fail_outer))
        self.flush()
        (req,) = self.ingest.on("/api/")
        self.assertEqual(req.path, "/api/42/envelope/")
        self.assertEqual(req.headers["content-type"], "application/x-sentry-envelope")
        self.assertEqual(req.headers["content-encoding"], "gzip")  # stack frames push it past 1 KB
        self.assertIn("sentry_key={}".format(TEST_KEY), req.headers["x-sentry-auth"])
        self.assertIn("sentry_version=7", req.headers["x-sentry-auth"])
        self.assertEqual(req.headers["user-agent"], "argus.python/0.1.0")

        header, item, event = req.envelope
        self.assertEqual(header["event_id"], event_id)
        self.assertRegex(event_id, r"^[0-9a-f]{32}$")
        self.assertEqual(header["dsn"], self.ingest.dsn)
        self.assertEqual(header["sdk"], {"name": "argus.python", "version": "0.1.0"})
        self.assertRegex(header["sent_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z$")
        self.assertEqual(item, {"type": "event", "content_type": "application/json"})

        self.assertEqual(event["event_id"], event_id)
        self.assertEqual(event["platform"], "python")
        self.assertEqual(event["level"], "error")
        self.assertEqual(event["release"], "1.4.2")
        self.assertEqual(event["environment"], "staging")
        self.assertEqual(event["server_name"], "web-1")
        self.assertEqual(event["sdk"]["name"], "argus.python")
        self.assertIsInstance(event["timestamp"], float)
        self.assertEqual(event["contexts"]["runtime"]["name"], "CPython")

        # The cause first, the error itself last.
        values = event["exception"]["values"]
        self.assertEqual([v["type"] for v in values], ["KeyError", "ValueError"])
        self.assertEqual(values[1]["value"], "payment failed")
        frames = values[1]["stacktrace"]["frames"]
        # Oldest first: the test frame calls _fail_outer, which raised.
        self.assertEqual(frames[-1]["function"], "_fail_outer")
        self.assertEqual(values[0]["stacktrace"]["frames"][-1]["function"], "_fail_inner")
        last = frames[-1]
        self.assertTrue(last["in_app"])
        self.assertTrue(last["abs_path"].endswith("test_errors.py"))
        self.assertEqual(last["filename"], "test_errors.py")
        self.assertEqual(last["module"], "test_errors")
        self.assertIn('raise ValueError("payment failed") from e', last["context_line"])
        self.assertIsInstance(last["lineno"], int)
        self.assertTrue(last["pre_context"])

    def test_small_envelopes_are_not_compressed(self) -> None:
        self.init()
        argus.capture_message("hi")
        self.flush()
        (req,) = self.ingest.on("/api/")
        self.assertNotIn("content-encoding", req.headers)
        self.assertEqual(req.event["message"], "hi")
        self.assertEqual(req.event["level"], "info")

    def test_capture_the_handled_exception(self) -> None:
        self.init()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            self.assertTrue(argus.capture_exception())
        self.assertEqual(argus.capture_exception(), "")  # nothing being handled
        self.assertEqual(self.one_event()["exception"]["values"][-1]["type"], "RuntimeError")

    def test_implicit_context_is_a_cause(self) -> None:
        def f() -> None:
            try:
                {}["x"]
            except KeyError:
                raise TypeError("while handling")  # noqa: B904

        self.init()
        argus.capture_exception(raise_and_catch(f))
        types = [v["type"] for v in self.one_event()["exception"]["values"]]
        self.assertEqual(types, ["KeyError", "TypeError"])

    def test_same_error_is_sent_once(self) -> None:
        self.init()
        e = raise_and_catch(_fail_inner)
        self.assertTrue(argus.capture_exception(e))
        self.assertEqual(argus.capture_exception(e), "")
        self.one_event()

    def test_context(self) -> None:
        self.init()
        argus.set_user({"id": "u1", "email": "ada@example.com", "ip_address": "10.0.0.1"})
        argus.set_tag("region", "af-west")
        argus.set_context("order", {"id": "ord_81"})
        argus.set_extra("attempt", 3)
        argus.add_breadcrumb(
            category="auth", message="signed in", data={"url": "https://a.ng/x?token=1"}
        )
        argus.capture_message(
            "webhook mismatch",
            "warning",
            tags={"provider": "paystack"},
            extra={"order": "ord_81"},
            fingerprint=["webhook", "{{ default }}"],
            transaction="POST /webhooks",
        )
        e = self.one_event()
        self.assertEqual(e["level"], "warning")
        self.assertEqual(e["user"], {"id": "u1", "email": "ada@example.com"})  # no IP by default
        self.assertEqual(e["tags"], {"region": "af-west", "provider": "paystack"})
        self.assertEqual(e["contexts"]["order"], {"id": "ord_81"})
        self.assertEqual(e["extra"], {"attempt": 3, "order": "ord_81"})
        self.assertEqual(e["fingerprint"], ["webhook", "{{ default }}"])
        self.assertEqual(e["transaction"], "POST /webhooks")
        (crumb,) = e["breadcrumbs"]["values"]
        self.assertEqual(crumb["message"], "signed in")
        self.assertEqual(crumb["data"]["url"], "https://a.ng/x")  # query stripped
        self.assertIsInstance(crumb["timestamp"], float)

    def test_send_default_pii_keeps_the_ip(self) -> None:
        self.init(send_default_pii=True)
        argus.set_user({"id": "u1", "ip_address": "10.0.0.1"})
        argus.capture_message("x")
        self.assertEqual(self.one_event()["user"]["ip_address"], "10.0.0.1")

    def test_level_aliases(self) -> None:
        self.init()
        argus.capture_message("a", "warn")  # type: ignore[arg-type]
        argus.capture_message("b", "critical")  # type: ignore[arg-type]
        argus.capture_message("c", "nonsense")  # type: ignore[arg-type]
        self.flush()
        levels = [r.event["level"] for r in self.ingest.on("/api/")]
        self.assertEqual(sorted(levels), ["error", "fatal", "warning"])

    def test_limits(self) -> None:
        self.init(release="r" * 500, environment="e" * 100)
        argus.set_tags({"t{}".format(i): i for i in range(80)})
        for i in range(150):
            argus.add_breadcrumb(message=str(i))
        argus.capture_message("x", transaction="t" * 500)
        e = self.one_event()
        self.assertEqual(len(e["release"]), 200)
        self.assertEqual(len(e["environment"]), 64)
        self.assertEqual(len(e["transaction"]), 300)
        self.assertEqual(len(e["tags"]), 50)
        crumbs = e["breadcrumbs"]["values"]
        self.assertEqual(len(crumbs), 100)
        self.assertEqual(crumbs[-1]["message"], "149")

    def test_max_breadcrumbs(self) -> None:
        self.init(max_breadcrumbs=3)
        for i in range(10):
            argus.add_breadcrumb(message=str(i))
        argus.capture_message("x")
        self.assertEqual(
            [c["message"] for c in self.one_event()["breadcrumbs"]["values"]], ["7", "8", "9"]
        )


class Privacy(ArgusTestCase):
    def test_event_is_scrubbed(self) -> None:
        self.init()
        argus.add_breadcrumb(category="http", data={"Authorization": "Bearer x", "path": "/pay"})
        argus.capture_exception(
            ValueError("charge failed for card 4111111111111111 with password=hunter2"),
            extra={
                "password": "hunter2",
                "nested": {"api_key": "k"},
                "note": "Bearer abcdefgh12345",
            },
            request={
                "method": "POST",
                "url": "https://shop.ng/pay?token=abc",
                "headers": {"Authorization": "Bearer abc", "Cookie": "sid=1", "Accept": "*/*"},
                "data": {"card": "4111111111111111"},
                "cookies": {"sid": "1"},
            },
        )
        e = self.one_event()
        value = e["exception"]["values"][-1]["value"]
        self.assertNotIn("4111", value)
        self.assertNotIn("hunter2", value)
        self.assertEqual(e["extra"]["password"], FILTERED)
        self.assertEqual(e["extra"]["nested"]["api_key"], FILTERED)
        self.assertEqual(e["extra"]["note"], "Bearer " + FILTERED)
        self.assertEqual(e["breadcrumbs"]["values"][0]["data"]["Authorization"], FILTERED)
        req = e["request"]
        self.assertEqual(req["url"], "https://shop.ng/pay")
        self.assertEqual(req["headers"]["Authorization"], FILTERED)
        self.assertNotIn("Cookie", req["headers"])
        self.assertNotIn("data", req)
        self.assertNotIn("cookies", req)


class Hooks(ArgusTestCase):
    def test_before_send_can_change_an_event(self) -> None:
        seen = {}

        def before_send(event: argus.Event, hint: argus.Hint) -> argus.Event:
            seen["exc"] = hint.get("exc_info")
            event["tags"]["changed"] = "yes"
            return event

        self.init(before_send=before_send)
        err = ValueError("x")
        argus.capture_exception(err)
        self.assertEqual(self.one_event()["tags"]["changed"], "yes")
        self.assertIs(seen["exc"][1], err)

    def test_before_send_runs_before_scrubbing(self) -> None:
        def before_send(event: argus.Event, hint: argus.Hint) -> argus.Event:
            event["extra"] = {"secret": "s"}
            return event

        self.init(before_send=before_send)
        argus.capture_message("x")
        self.assertEqual(self.one_event()["extra"]["secret"], FILTERED)

    def test_before_send_can_drop(self) -> None:
        self.init(before_send=lambda e, h: None)
        self.assertEqual(argus.capture_message("x"), "")
        self.flush()
        self.assertEqual(self.ingest.on("/api/"), [])

    def test_before_send_that_raises_sends_unchanged(self) -> None:
        def broken(event: argus.Event, hint: argus.Hint) -> argus.Event:
            raise RuntimeError("bug in my hook")

        self.init(before_send=broken)
        self.assertTrue(argus.capture_message("x"))
        self.assertEqual(self.one_event()["message"], "x")

    def test_before_breadcrumb(self) -> None:
        def bb(crumb: argus.Breadcrumb, hint: argus.Hint) -> argus.Breadcrumb | None:
            return None if crumb.get("category") == "noise" else crumb

        self.init(before_breadcrumb=bb)
        argus.add_breadcrumb(category="noise")
        argus.add_breadcrumb(category="keep")
        argus.capture_message("x")
        self.assertEqual(
            [c["category"] for c in self.one_event()["breadcrumbs"]["values"]], ["keep"]
        )

    def test_sample_rate_zero_sends_nothing(self) -> None:
        self.init(sample_rate=0)
        self.assertEqual(argus.capture_message("x"), "")
        self.flush()
        self.assertEqual(self.ingest.on("/api/"), [])


class Scopes(ArgusTestCase):
    def test_new_scope_does_not_leak(self) -> None:
        self.init()
        argus.set_tag("global", "1")
        with argus.new_scope():
            argus.set_user({"id": "inner"})
            argus.set_tag("inner", "1")
            argus.capture_message("inside")
        argus.capture_message("outside")
        self.flush()
        events = {r.event["message"]: r.event for r in self.ingest.on("/api/")}
        self.assertEqual(events["inside"]["user"], {"id": "inner"})
        self.assertEqual(events["inside"]["tags"], {"global": "1", "inner": "1"})
        self.assertNotIn("user", events["outside"])
        self.assertEqual(events["outside"]["tags"], {"global": "1"})

    def test_trace_context_and_transaction_from_the_active_span(self) -> None:
        self.init()
        with argus.trace("nightly report") as span:
            argus.capture_message("inside a span")
        e = self.one_event()
        self.assertEqual(
            e["contexts"]["trace"], {"trace_id": span.trace_id, "span_id": span.span_id}
        )
        self.assertEqual(e["transaction"], "nightly report")


if __name__ == "__main__":
    unittest.main()
