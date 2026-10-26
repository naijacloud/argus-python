"""Logs, metrics and traces as OTLP/HTTP JSON (spec section 5)."""

from __future__ import annotations

import asyncio
import unittest

from _support import TEST_KEY, ArgusTestCase, attr_map

import naijacloud_argus as argus
from naijacloud_argus._scrub import FILTERED


class Common(ArgusTestCase):
    def resource(self, body: dict, key: str) -> dict:
        return attr_map(body[key][0]["resource"]["attributes"])

    def test_resource_and_auth(self) -> None:
        self.init(release="1.4.2", environment="staging", service_name="shop")
        argus.log.info("x")
        self.flush()
        (req,) = self.ingest.on("/v1/logs")
        self.assertEqual(req.headers["authorization"], "Bearer " + TEST_KEY)
        self.assertEqual(req.headers["content-type"], "application/json")
        self.assertEqual(req.headers["user-agent"], "argus.python/0.1.0")
        res = self.resource(req.json, "resourceLogs")
        self.assertEqual(res["service.name"], "shop")
        self.assertEqual(res["service.version"], "1.4.2")
        self.assertEqual(res["deployment.environment"], "staging")
        self.assertEqual(res["telemetry.sdk.name"], "argus.python")
        self.assertEqual(res["telemetry.sdk.version"], "0.1.0")
        self.assertEqual(res["argus.contract"], "argus.sdk/1")


class Logs(ArgusTestCase):
    def test_records(self) -> None:
        self.init()
        argus.log.info("order placed", order_id="ord_81", total=18500, paid=True, ratio=0.5)
        argus.log.warn("slow", {"http.route": "/pay"})
        argus.log.error("card 4111111111111111 declined", password="hunter2")
        for name in ("trace", "debug", "warning", "fatal"):
            getattr(argus.log, name)(name)
        self.flush()
        (req,) = self.ingest.on("/v1/logs")
        recs = req.json["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(len(recs), 7)
        info = recs[0]
        self.assertEqual(info["severityNumber"], 9)
        self.assertEqual(info["severityText"], "INFO")
        self.assertEqual(info["body"], {"stringValue": "order placed"})
        self.assertRegex(info["timeUnixNano"], r"^\d{19}$")
        attrs = {a["key"]: a["value"] for a in info["attributes"]}
        self.assertEqual(attrs["order_id"], {"stringValue": "ord_81"})
        self.assertEqual(attrs["total"], {"intValue": "18500"})
        self.assertEqual(attrs["paid"], {"boolValue": True})
        self.assertEqual(attrs["ratio"], {"doubleValue": 0.5})
        self.assertNotIn("traceId", info)
        self.assertEqual(attr_map(recs[1]["attributes"]), {"http.route": "/pay"})
        self.assertEqual(recs[1]["severityText"], "WARN")
        err = recs[2]
        self.assertEqual(err["severityNumber"], 17)
        self.assertEqual(err["body"]["stringValue"], "card {} declined".format(FILTERED))
        self.assertEqual(attr_map(err["attributes"])["password"], FILTERED)
        self.assertEqual([r["severityNumber"] for r in recs[3:]], [1, 5, 13, 21])

    def test_logs_inside_a_span_carry_its_ids(self) -> None:
        self.init()
        with argus.trace("job") as span:
            argus.log.info("inside")
        self.flush()
        rec = self.ingest.on("/v1/logs")[0].json["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(rec["traceId"], span.trace_id)
        self.assertEqual(rec["spanId"], span.span_id)

    def test_batches_of_at_most_1000(self) -> None:
        self.init()
        for i in range(2500):
            argus.log.info("line {}".format(i))
        self.flush()
        sizes = [
            len(r.json["resourceLogs"][0]["scopeLogs"][0]["logRecords"])
            for r in self.ingest.on("/v1/logs")
        ]
        self.assertEqual(sum(sizes), 2500)
        self.assertLessEqual(max(sizes), 1000)

    def test_big_batches_are_split(self) -> None:
        self.init()
        # 800 KB: over the 512 KB split point, under the 1 MB lane cap.
        for _ in range(50):
            argus.log.info("x" * 16000)
        self.flush()
        reqs = self.ingest.on("/v1/logs")
        self.assertGreater(len(reqs), 1)
        total = sum(len(r.json["resourceLogs"][0]["scopeLogs"][0]["logRecords"]) for r in reqs)
        self.assertEqual(total, 50)
        self.assertTrue(all(len(r.body) <= 600 * 1024 for r in reqs))


class Metrics(ArgusTestCase):
    def metrics(self) -> dict:
        self.flush()
        (req,) = self.ingest.on("/v1/metrics")
        out = {}
        for m in req.json["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
            out.setdefault(m["name"], []).append(m)
        return out

    def test_counter_gauge_histogram(self) -> None:
        self.init()
        argus.metrics.count("orders.placed", attributes={"method": "card"})
        argus.metrics.count("orders.placed", 2, {"method": "card"})
        argus.metrics.count("orders.placed", attributes={"method": "transfer"})
        argus.metrics.gauge("queue.depth", 3)
        argus.metrics.gauge("queue.depth", 7)
        for v in (3, 40, 40, 20000):
            argus.metrics.distribution("checkout.ms", v, unit="ms")
        m = self.metrics()

        placed = {
            attr_map(x["sum"]["dataPoints"][0]["attributes"])["method"]: x
            for x in m["orders.placed"]
        }
        card = placed["card"]["sum"]
        self.assertEqual(card["aggregationTemporality"], 1)  # delta
        self.assertTrue(card["isMonotonic"])
        self.assertEqual(card["dataPoints"][0]["asDouble"], 3.0)
        self.assertEqual(placed["transfer"]["sum"]["dataPoints"][0]["asDouble"], 1.0)
        dp = card["dataPoints"][0]
        self.assertLess(int(dp["startTimeUnixNano"]), int(dp["timeUnixNano"]))

        (gauge,) = m["queue.depth"]
        self.assertEqual(gauge["gauge"]["dataPoints"][0]["asDouble"], 7.0)

        (hist,) = m["checkout.ms"]
        self.assertEqual(hist["unit"], "ms")
        h = hist["histogram"]
        self.assertEqual(h["aggregationTemporality"], 1)
        p = h["dataPoints"][0]
        self.assertEqual(p["count"], "4")
        self.assertEqual(p["sum"], 20083.0)
        self.assertEqual((p["min"], p["max"]), (3.0, 20000.0))
        self.assertEqual(len(p["bucketCounts"]), len(p["explicitBounds"]) + 1)
        self.assertEqual(p["bucketCounts"][0], "1")  # 3 <= 5
        self.assertEqual(p["bucketCounts"][3], "2")  # 25 < 40 <= 50
        self.assertEqual(p["bucketCounts"][-1], "1")  # 20000 > 10000

    def test_delta_resets_after_a_flush(self) -> None:
        self.init()
        argus.metrics.count("c", 5)
        self.flush()
        argus.metrics.count("c", 1)
        self.flush()
        reqs = self.ingest.on("/v1/metrics")
        values = [
            r.json["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"][0][
                "asDouble"
            ]
            for r in reqs
        ]
        self.assertEqual(values, [5.0, 1.0])

    def test_bad_values_are_ignored(self) -> None:
        self.init()
        argus.metrics.count("c", float("nan"))
        argus.metrics.gauge("g", "not a number")  # type: ignore[arg-type]
        argus.metrics.distribution("d", float("inf"))
        self.flush()
        self.assertEqual(self.ingest.on("/v1/metrics"), [])


class Traces(ArgusTestCase):
    def spans(self) -> list:
        self.flush()
        out = []
        for r in self.ingest.on("/v1/traces"):
            out.extend(r.json["resourceSpans"][0]["scopeSpans"][0]["spans"])
        return out

    def test_parent_and_child(self) -> None:
        self.init()
        with argus.trace("charge card", {"provider": "paystack"}) as parent:
            with argus.trace("call bank") as child:
                child.set_attribute("url.full", "https://bank.ng/pay?token=abc")
        spans = {s["name"]: s for s in self.spans()}
        p, c = spans["charge card"], spans["call bank"]
        self.assertEqual(p["traceId"], parent.trace_id)
        self.assertNotIn("parentSpanId", p)
        self.assertEqual(c["traceId"], parent.trace_id)
        self.assertEqual(c["parentSpanId"], parent.span_id)
        self.assertEqual(c["spanId"], child.span_id)
        self.assertEqual(p["kind"], 1)
        self.assertEqual(p["status"], {"code": 1})
        self.assertEqual(attr_map(p["attributes"]), {"provider": "paystack"})
        self.assertEqual(attr_map(c["attributes"])["url.full"], "https://bank.ng/pay")
        self.assertRegex(p["traceId"], r"^[0-9a-f]{32}$")
        self.assertRegex(p["spanId"], r"^[0-9a-f]{16}$")
        self.assertLessEqual(int(p["startTimeUnixNano"]), int(c["startTimeUnixNano"]))
        self.assertLessEqual(int(c["endTimeUnixNano"]), int(p["endTimeUnixNano"]))

    def test_a_failing_block_marks_the_span_and_still_raises(self) -> None:
        self.init()
        with self.assertRaises(ValueError):
            with argus.trace("fails"):
                raise ValueError("bad input token=abc")
        (s,) = self.spans()
        self.assertEqual(s["status"]["code"], 2)
        self.assertEqual(s["status"]["message"], "bad input token={}".format(FILTERED))

    def test_decorators(self) -> None:
        self.init()

        @argus.trace
        def plain() -> int:
            return 1

        @argus.trace("named", {"k": "v"})
        def named() -> int:
            return 2

        @argus.trace("async job")
        async def job() -> int:
            await asyncio.sleep(0)
            return 3

        self.assertEqual((plain(), named(), asyncio.run(job())), (1, 2, 3))
        names = sorted(s["name"] for s in self.spans())
        self.assertEqual(names, ["Traces.test_decorators.<locals>.plain", "async job", "named"])

    def test_async_context_manager(self) -> None:
        self.init()

        async def main() -> None:
            async with argus.trace("outer") as outer:
                async with argus.trace("inner"):
                    pass
            self.assertIsNotNone(outer)

        asyncio.run(main())
        spans = {s["name"]: s for s in self.spans()}
        self.assertEqual(spans["inner"]["parentSpanId"], spans["outer"]["spanId"])

    def test_start_span_by_hand_and_traceparent(self) -> None:
        self.init()
        incoming = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        span = argus.start_span("consume message", parent=incoming, kind=argus.SpanKind.SERVER)
        self.assertEqual(span.trace_id, "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(span.parent_span_id, "b7ad6b7169203331")
        with span:
            self.assertEqual(argus.get_traceparent(), span.traceparent())
            self.assertRegex(
                span.traceparent(), r"^00-0af7651916cd43dd8448eb211c80319c-[0-9a-f]{16}-01$"
            )
        self.assertIsNone(argus.get_traceparent())
        span.end()  # twice is harmless
        (s,) = self.spans()
        self.assertEqual(s["kind"], 2)
        self.assertEqual(s["parentSpanId"], "b7ad6b7169203331")

    def test_unsampled_parent_is_not_recorded(self) -> None:
        self.init()
        with argus.start_span(
            "x", parent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
        ) as s:
            self.assertFalse(s.is_recording)
            self.assertTrue(s.traceparent().endswith("-00"))
        self.assertEqual(self.spans(), [])

    def test_traces_sample_rate_zero(self) -> None:
        self.init(traces_sample_rate=0)
        with argus.trace("x"):
            pass
        self.assertEqual(self.spans(), [])

    def test_parse_traceparent(self) -> None:
        ok = argus.parse_traceparent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        self.assertEqual(
            ok, argus.SpanContext("0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331", True)
        )
        for bad in (None, "", "junk", "00-" + "0" * 32 + "-b7ad6b7169203331-01", "01-abc"):
            self.assertIsNone(argus.parse_traceparent(bad))


if __name__ == "__main__":
    unittest.main()
