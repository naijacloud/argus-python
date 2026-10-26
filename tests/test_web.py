"""The WSGI and ASGI middleware, with plain apps (no framework needed)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Callable, Dict, Iterable, List
from wsgiref.util import setup_testing_defaults

from _support import ArgusTestCase, attr_map

import naijacloud_argus as argus
from naijacloud_argus.integrations.asgi import ArgusASGIMiddleware
from naijacloud_argus.integrations.wsgi import ArgusWSGIMiddleware

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class WebCase(ArgusTestCase):
    def spans(self) -> List[Dict[str, Any]]:
        self.flush()
        return [
            s
            for r in self.ingest.on("/v1/traces")
            for s in r.json["resourceSpans"][0]["scopeSpans"][0]["spans"]
        ]


def call_wsgi(
    app: Callable[..., Iterable[bytes]], path: str = "/orders/81", **environ: Any
) -> tuple:
    env: Dict[str, Any] = {"PATH_INFO": path.split("?")[0], "QUERY_STRING": path.partition("?")[2]}
    env.update(environ)
    setup_testing_defaults(env)
    status: List[str] = []

    def start_response(s: str, headers: Any, exc_info: Any = None) -> None:
        status.append(s)

    it = app(env, start_response)
    try:
        body = b"".join(it)
    finally:
        close = getattr(it, "close", None)
        if close:
            close()
    return status[0], body


class Wsgi(WebCase):
    def test_server_span(self) -> None:
        self.init()

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            argus.log.info("handling")
            start_response("201 Created", [])
            return [b"a", b"b"]

        status, body = call_wsgi(
            ArgusWSGIMiddleware(app),
            "/orders/81?token=abc",
            REQUEST_METHOD="POST",
            HTTP_TRACEPARENT=TRACEPARENT,
            HTTP_HOST="shop.ng",
        )
        self.assertEqual((status, body), ("201 Created", b"ab"))
        (s,) = self.spans()
        self.assertEqual(s["name"], "POST /orders/81")
        self.assertEqual(s["kind"], 2)
        self.assertEqual(s["traceId"], "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(s["parentSpanId"], "b7ad6b7169203331")
        self.assertEqual(s["status"], {"code": 1})
        a = attr_map(s["attributes"])
        self.assertEqual(a["http.request.method"], "POST")
        self.assertEqual(a["http.response.status_code"], 201)
        self.assertEqual(a["url.full"], "http://shop.ng/orders/81")
        self.assertEqual(a["server.address"], "shop.ng")
        log = self.ingest.on("/v1/logs")[0].json["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(log["traceId"], "0af7651916cd43dd8448eb211c80319c")

    def test_unhandled_error_is_reported_with_the_request(self) -> None:
        self.init()

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            argus.set_user({"id": "u-in-request"})
            raise ZeroDivisionError("division by zero")

        with self.assertRaises(ZeroDivisionError):
            call_wsgi(
                ArgusWSGIMiddleware(app),
                "/pay?card=4111111111111111",
                HTTP_COOKIE="sid=1",
                HTTP_AUTHORIZATION="Bearer abcdefghijk",
                REMOTE_ADDR="10.1.1.1",
            )
        e = self.one_event()
        self.assertEqual(e["tags"]["mechanism"], "wsgi")
        self.assertEqual(e["transaction"], "GET /pay")
        req = e["request"]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["url"], "http://127.0.0.1/pay")
        self.assertNotIn("Cookie", req["headers"])
        self.assertEqual(req["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(e["user"], {"id": "u-in-request"})  # no IP without send_default_pii
        (s,) = self.spans()
        self.assertEqual(s["status"]["code"], 2)
        self.assertEqual(attr_map(s["attributes"])["http.response.status_code"], 500)

    def test_5xx_marks_the_span(self) -> None:
        self.init()

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            start_response("503 Service Unavailable", [])
            return [b""]

        call_wsgi(ArgusWSGIMiddleware(app))
        (s,) = self.spans()
        self.assertEqual(s["status"], {"code": 2, "message": "HTTP 503"})

    def test_streamed_body_error(self) -> None:
        self.init()

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            start_response("200 OK", [])

            def gen() -> Iterable[bytes]:
                yield b"partial"
                raise OSError("client went away")

            return gen()

        with self.assertRaises(OSError):
            call_wsgi(ArgusWSGIMiddleware(app))
        self.assertEqual(self.one_event()["exception"]["values"][-1]["type"], "OSError")

    def test_requests_do_not_share_scope(self) -> None:
        self.init()
        argus.set_tag("global", "yes")

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            if environ["PATH_INFO"] == "/a":
                argus.set_user({"id": "alice"})
            argus.capture_message(environ["PATH_INFO"])
            start_response("200 OK", [])
            return [b""]

        mw = ArgusWSGIMiddleware(app)
        call_wsgi(mw, "/a")
        call_wsgi(mw, "/b")
        argus.capture_message("outside")
        self.flush()
        events = {r.event["message"]: r.event for r in self.ingest.on("/api/")}
        self.assertEqual(events["/a"]["user"], {"id": "alice"})
        self.assertNotIn("user", events["/b"])
        self.assertNotIn("user", events["outside"])
        self.assertNotIn("request", events["outside"])
        self.assertEqual(events["/b"]["tags"], {"global": "yes"})

    def test_send_default_pii_adds_the_ip(self) -> None:
        self.init(send_default_pii=True)

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            argus.capture_message("x")
            start_response("200 OK", [])
            return [b""]

        call_wsgi(ArgusWSGIMiddleware(app), REMOTE_ADDR="10.1.1.1")
        self.assertEqual(self.one_event()["user"]["ip_address"], "10.1.1.1")

    def test_without_a_client_it_is_transparent(self) -> None:
        argus.close()

        def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            start_response("200 OK", [])
            return [b"plain"]

        self.assertEqual(call_wsgi(ArgusWSGIMiddleware(app)), ("200 OK", b"plain"))


def call_asgi(
    app: Any, path: str = "/items/7", method: str = "GET", headers: Any = (), **scope: Any
) -> tuple:
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    s: Dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"q=1",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": ("10.2.2.2", 5000),
        "server": ("api.shop.ng", 443),
    }
    s.update(scope)
    asyncio.run(app(s, receive, send))
    return s, sent


class Asgi(WebCase):
    def test_server_span_and_route(self) -> None:
        self.init()

        class Route:
            path = "/items/{item_id}"

        async def app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            scope["route"] = Route()  # what FastAPI's router does
            argus.log.info("handling")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        _, sent = call_asgi(ArgusASGIMiddleware(app), headers=[("traceparent", TRACEPARENT)])
        self.assertEqual(sent[0]["status"], 200)
        (s,) = self.spans()
        self.assertEqual(s["name"], "GET /items/{item_id}")
        self.assertEqual(s["parentSpanId"], "b7ad6b7169203331")
        a = attr_map(s["attributes"])
        self.assertEqual(a["http.route"], "/items/{item_id}")
        self.assertEqual(a["url.path"], "/items/7")
        self.assertEqual(a["url.full"], "https://api.shop.ng/items/7")
        self.assertEqual(a["http.response.status_code"], 200)

    def test_error(self) -> None:
        self.init()

        async def app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            argus.set_tag("in", "request")
            raise LookupError("no such item")

        with self.assertRaises(LookupError):
            call_asgi(ArgusASGIMiddleware(app), headers=[("cookie", "sid=1"), ("x-api-key", "k")])
        argus.capture_message("after")
        self.flush()
        events = {r.event.get("message", "err"): r.event for r in self.ingest.on("/api/")}
        e = events["err"]
        self.assertEqual(e["tags"], {"mechanism": "asgi", "in": "request"})
        self.assertEqual(e["transaction"], "GET /items/7")
        self.assertNotIn("cookie", e["request"]["headers"])
        self.assertEqual(e["request"]["headers"]["x-api-key"], "[Filtered]")
        self.assertNotIn("in", events["after"].get("tags", {}))
        (s,) = self.spans()
        self.assertEqual(s["status"]["code"], 2)

    def test_concurrent_requests_keep_their_own_scope(self) -> None:
        self.init()

        async def app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            argus.set_user({"id": scope["path"]})
            await asyncio.sleep(0.01)
            argus.capture_message(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = ArgusASGIMiddleware(app)

        async def main() -> None:
            async def one(path: str) -> None:
                s = {"type": "http", "method": "GET", "path": path, "headers": []}

                async def receive() -> Dict[str, Any]:
                    return {"type": "http.request"}

                async def send(m: Dict[str, Any]) -> None:
                    return None

                await mw(s, receive, send)

            await asyncio.gather(*(one("/u{}".format(i)) for i in range(5)))

        asyncio.run(main())
        self.flush()
        for r in self.ingest.on("/api/"):
            self.assertEqual(r.event["user"]["id"], r.event["message"])
        self.assertEqual(len(self.ingest.on("/api/")), 5)

    def test_non_http_scopes_pass_through(self) -> None:
        self.init()
        seen = []

        async def app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            seen.append(scope["type"])

        async def main() -> None:
            async def receive() -> Dict[str, Any]:
                return {}

            async def send(m: Dict[str, Any]) -> None:
                return None

            await ArgusASGIMiddleware(app)({"type": "lifespan"}, receive, send)

        asyncio.run(main())
        self.assertEqual(seen, ["lifespan"])
        self.assertEqual(self.spans(), [])


if __name__ == "__main__":
    unittest.main()
