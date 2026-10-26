"""What init() hooks: excepthooks, atexit, logging and outgoing http.client."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
import sys
import textwrap
import threading
import unittest
import urllib.request

from _support import ArgusTestCase, attr_map

import naijacloud_argus as argus
from naijacloud_argus.integrations.logging import ArgusHandler

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _quiet_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers[:] = [logging.NullHandler()]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg


class Excepthooks(ArgusTestCase):
    def test_sys_excepthook_reports_fatal_then_prints(self) -> None:
        self.init()
        try:
            raise RuntimeError("uncaught on main")
        except RuntimeError:
            exc_type, exc, tb = sys.exc_info()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sys.excepthook(exc_type, exc, tb)  # type: ignore[arg-type]
        self.assertIn("uncaught on main", stderr.getvalue())  # Python's own output still happens
        e = self.one_event()
        self.assertEqual(e["level"], "fatal")
        self.assertEqual(e["tags"]["mechanism"], "excepthook")
        self.assertEqual(e["exception"]["values"][-1]["value"], "uncaught on main")

    def test_keyboard_interrupt_is_not_reported(self) -> None:
        self.init()
        with contextlib.redirect_stderr(io.StringIO()):
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        self.flush()
        self.assertEqual(self.ingest.on("/api/"), [])

    def test_thread_exceptions(self) -> None:
        self.init()

        def work() -> None:
            raise ValueError("in a worker thread")

        with contextlib.redirect_stderr(io.StringIO()):
            t = threading.Thread(target=work, name="worker-7")
            t.start()
            t.join()
        e = self.one_event()
        self.assertEqual(e["tags"]["mechanism"], "threading")
        self.assertEqual(e["extra"]["thread"], "worker-7")
        self.assertEqual(e["exception"]["values"][-1]["type"], "ValueError")


class Process(ArgusTestCase):
    """Real interpreters: the hooks as an app gets them, including shutdown."""

    def run_script(self, body: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=SRC, ARGUS_DSN=self.ingest.dsn)
        script = "import naijacloud_argus as argus\nargus.init()\n" + textwrap.dedent(body)
        return subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
        )

    def test_uncaught_exception_is_sent_before_exit(self) -> None:
        r = self.run_script("raise SystemError('process dies')\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("process dies", r.stderr)
        reqs = self.ingest.wait_for("/api/", 1)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].event["level"], "fatal")

    def test_short_script_flushes_at_exit(self) -> None:
        r = self.run_script(
            """
            argus.log.info("from a short script")
            argus.metrics.count("runs")
            with argus.trace("job"):
                pass
            argus.cron.check_in("nightly")
            """
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        for path in ("/v1/logs", "/v1/metrics", "/v1/traces", "/v1/cron/nightly"):
            self.assertEqual(len(self.ingest.wait_for(path, 1)), 1, path)

    def test_no_dsn_process_is_silent(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("ARGUS_DSN", "SENTRY_DSN")}
        env["PYTHONPATH"] = SRC
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                "import naijacloud_argus as a; a.init(); a.log.info('x'); print('ok')",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "ok\n", ""))

    def test_fork_child_sends_its_own_data(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("no fork")
        r = self.run_script(
            """
            import os, warnings
            warnings.simplefilter("ignore")
            argus.log.info("parent before fork")
            argus.flush()
            pid = os.fork()
            if pid == 0:
                argus.log.info("child")
                ok = argus.flush(5)
                os._exit(0 if ok else 3)
            _, status = os.waitpid(pid, 0)
            raise SystemExit(os.WEXITSTATUS(status))
            """
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        bodies = [rec.body for rec in self.ingest.wait_for("/v1/logs", 2)]
        self.assertTrue(any(b"child" in b for b in bodies))


class Logging(ArgusTestCase):
    def test_records_become_breadcrumbs(self) -> None:
        self.init()
        lg = _quiet_logger("shop.checkout")
        lg.debug("too quiet to keep")
        lg.info("basket has %d items", 3)
        lg.warning("slow provider")
        argus.capture_message("x")
        crumbs = self.one_event()["breadcrumbs"]["values"]
        self.assertEqual([c["message"] for c in crumbs], ["basket has 3 items", "slow provider"])
        self.assertEqual(crumbs[1]["category"], "shop.checkout")
        self.assertEqual(crumbs[1]["level"], "warning")
        self.assertEqual(crumbs[1]["type"], "log")

    def test_breadcrumb_level(self) -> None:
        self.init(breadcrumb_level=logging.DEBUG)
        _quiet_logger("dbg").debug("kept now")
        argus.capture_message("x")
        self.assertEqual(self.one_event()["breadcrumbs"]["values"][0]["message"], "kept now")

    def test_init_adds_no_handler_so_basic_config_still_works(self) -> None:
        before = list(logging.root.handlers)
        self.init(capture_logs=True)
        self.assertEqual(logging.root.handlers, before)

    def test_capture_logs_ships_records(self) -> None:
        self.init(capture_logs=logging.WARNING)
        lg = _quiet_logger("payments")
        lg.info("not shipped")
        lg.warning("card %s declined", "4111111111111111")
        try:
            raise ValueError("bad")
        except ValueError:
            lg.exception("charge failed")
        self.flush()
        (req,) = self.ingest.on("/v1/logs")
        recs = req.json["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual([r["severityText"] for r in recs], ["WARN", "ERROR"])
        self.assertEqual(recs[0]["body"]["stringValue"], "card [Filtered] declined")
        a = attr_map(recs[1]["attributes"])
        self.assertEqual(a["logger.name"], "payments")
        self.assertEqual(a["exception.type"], "ValueError")
        self.assertIn("Traceback", a["exception.stacktrace"])
        self.assertTrue(a["code.filepath"].endswith("test_hooks.py"))

    def test_argus_handler_and_capture_logs_ship_once(self) -> None:
        self.init(capture_logs=True)
        lg = _quiet_logger("both")
        lg.addHandler(ArgusHandler())
        lg.info("once")
        self.flush()
        recs = self.ingest.on("/v1/logs")[0].json["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(len(recs), 1)

    def test_argus_handler_alone(self) -> None:
        self.init()
        lg = _quiet_logger("only-handler")
        lg.addHandler(ArgusHandler(logging.INFO))
        lg.debug("below the handler level")
        lg.info("shipped")
        self.flush()
        recs = self.ingest.on("/v1/logs")[0].json["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual([r["body"]["stringValue"] for r in recs], ["shipped"])

    def test_a_broken_record_does_not_break_logging(self) -> None:
        self.init(capture_logs=True)
        lg = _quiet_logger("broken")
        with contextlib.redirect_stderr(io.StringIO()):
            lg.info("%d", "not a number")  # logging itself reports this
        argus.flush(2)


class OutgoingHttp(ArgusTestCase):
    def downstream(self) -> str:
        # `localhost`, not 127.0.0.1: requests to the DSN's host are the SDK's
        # own and are never traced.
        return "http://localhost:{}/downstream?token=abc".format(self.ingest.port)

    def test_inside_a_span(self) -> None:
        self.init()
        with argus.trace("checkout") as parent:
            with urllib.request.urlopen(self.downstream(), timeout=5) as res:
                self.assertEqual(res.status, 200)
        (req,) = self.ingest.on("/downstream")
        tp = argus.parse_traceparent(req.headers.get("traceparent"))
        assert tp is not None
        self.assertEqual(tp.trace_id, parent.trace_id)
        self.flush()
        spans = {
            s["name"]: s
            for r in self.ingest.on("/v1/traces")
            for s in r.json["resourceSpans"][0]["scopeSpans"][0]["spans"]
        }
        client = spans["GET http://localhost:{}/downstream".format(self.ingest.port)]
        self.assertEqual(client["kind"], 3)
        self.assertEqual(client["parentSpanId"], parent.span_id)
        self.assertEqual(client["spanId"], tp.span_id)
        a = attr_map(client["attributes"])
        self.assertEqual(a["http.request.method"], "GET")
        self.assertEqual(a["http.response.status_code"], 200)
        self.assertNotIn("token", a["url.full"])

    def test_outside_a_span_only_a_breadcrumb(self) -> None:
        self.init()
        urllib.request.urlopen(self.downstream(), timeout=5).close()
        (req,) = self.ingest.on("/downstream")
        self.assertNotIn("traceparent", req.headers)
        argus.capture_message("x")
        crumbs = self.one_event()["breadcrumbs"]["values"]
        http = [c for c in crumbs if c.get("type") == "http"]
        self.assertEqual(len(http), 1)
        self.assertEqual(http[0]["data"]["status_code"], 200)
        self.assertNotIn("token", http[0]["data"]["url"])

    def test_the_sdks_own_requests_leave_no_trace(self) -> None:
        self.init()
        argus.capture_message("first")
        self.flush()
        argus.capture_message("second")
        self.flush()
        second = [r.event for r in self.ingest.on("/api/") if r.event.get("message") == "second"][0]
        self.assertEqual(second.get("breadcrumbs", {}).get("values", []), [])


if __name__ == "__main__":
    unittest.main()
