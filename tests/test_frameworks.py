"""Flask, Django and FastAPI. Each class skips itself unless its framework is
installed (`pip install -e ".[frameworks]"`); CI runs them in their own job."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from typing import Any, Dict, List

from _support import ArgusTestCase, attr_map
from test_web import call_asgi

import naijacloud_argus as argus

HAS_FLASK = importlib.util.find_spec("flask") is not None
HAS_DJANGO = importlib.util.find_spec("django") is not None
HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


class FrameworkCase(ArgusTestCase):
    def spans(self) -> List[Dict[str, Any]]:
        self.flush()
        return [
            s
            for r in self.ingest.on("/v1/traces")
            for s in r.json["resourceSpans"][0]["scopeSpans"][0]["spans"]
        ]

    def errors(self) -> List[Dict[str, Any]]:
        self.flush()
        return [r.event for r in self.ingest.on("/api/")]


@unittest.skipUnless(HAS_FLASK, "flask is not installed")
class Flask(FrameworkCase):
    def make_app(self) -> Any:
        from flask import Flask as FlaskApp

        from naijacloud_argus.integrations.flask import init_app

        app = FlaskApp("argus_test")

        @app.route("/orders/<int:oid>")
        def order(oid: int) -> Any:
            argus.log.info("order", oid=oid)
            return {"id": oid}

        @app.route("/boom")
        def boom() -> Any:
            raise RuntimeError("flask view failed")

        @app.route("/handled")
        def handled() -> Any:
            raise KeyError("mine")

        @app.errorhandler(KeyError)
        def on_key_error(e: Exception) -> Any:
            return "handled", 400

        return init_app(app)

    def test_route_and_span(self) -> None:
        self.init()
        res = (
            self.make_app()
            .test_client()
            .get(
                "/orders/81?token=x",
                headers={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
            )
        )
        self.assertEqual(res.status_code, 200)
        res.close()
        (s,) = self.spans()
        self.assertEqual(s["name"], "GET /orders/<int:oid>")
        self.assertEqual(s["traceId"], "0af7651916cd43dd8448eb211c80319c")
        a = attr_map(s["attributes"])
        self.assertEqual(a["http.route"], "/orders/<int:oid>")
        self.assertEqual(a["http.response.status_code"], 200)
        self.assertEqual(a["url.path"], "/orders/81")

    def test_unhandled_error(self) -> None:
        self.init()
        res = self.make_app().test_client().get("/boom")
        self.assertEqual(res.status_code, 500)
        res.close()
        (e,) = self.errors()
        self.assertEqual(e["tags"]["mechanism"], "flask")
        self.assertEqual(e["transaction"], "GET /boom")
        self.assertEqual(e["exception"]["values"][-1]["value"], "flask view failed")
        self.assertEqual(e["request"]["method"], "GET")
        (s,) = self.spans()
        self.assertEqual(s["status"]["code"], 2)

    def test_handled_errors_are_not_reported(self) -> None:
        self.init()
        res = self.make_app().test_client().get("/handled")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.errors(), [])

    def test_init_app_twice_is_harmless(self) -> None:
        from naijacloud_argus.integrations.flask import Argus, init_app

        self.init()
        app = self.make_app()
        init_app(app)
        Argus(app)
        app.test_client().get("/boom")
        self.assertEqual(len(self.errors()), 1)


_django_ready = False


def _setup_django() -> None:
    global _django_ready
    if _django_ready:
        return
    import django
    from django.conf import settings

    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-only-0000000000000000",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF="_django_urls",
        MIDDLEWARE=["naijacloud_argus.integrations.django.ArgusMiddleware"],
        INSTALLED_APPS=[],
        DATABASES={},
        LOGGING_CONFIG=None,
    )
    django.setup()
    _django_ready = True


@unittest.skipUnless(HAS_DJANGO, "django is not installed")
class Django(FrameworkCase):
    def setUp(self) -> None:
        super().setUp()
        _setup_django()

    def test_sync_route(self) -> None:
        from django.test import Client

        self.init()
        res = Client().get("/orders/81/?q=1")
        self.assertEqual(res.status_code, 200)
        (s,) = self.spans()
        self.assertEqual(s["name"], "GET /orders/<int:pk>/")
        self.assertEqual(attr_map(s["attributes"])["http.route"], "/orders/<int:pk>/")
        self.assertEqual(attr_map(s["attributes"])["http.response.status_code"], 200)

    def test_sync_error(self) -> None:
        from django.test import Client

        self.init()
        res = Client(raise_request_exception=False).get("/boom/")
        self.assertEqual(res.status_code, 500)
        (e,) = self.errors()
        self.assertEqual(e["tags"]["mechanism"], "django")
        self.assertEqual(e["transaction"], "GET /boom/")
        self.assertEqual(e["exception"]["values"][-1]["value"], "django view failed")
        (s,) = self.spans()
        self.assertEqual(attr_map(s["attributes"])["http.response.status_code"], 500)
        self.assertEqual(s["status"]["code"], 2)

    def test_async_route_and_error(self) -> None:
        from django.test import AsyncClient

        self.init()

        async def main() -> tuple:
            c = AsyncClient(raise_request_exception=False)
            return (await c.get("/async/orders/7/")).status_code, (
                await c.get("/async/boom/")
            ).status_code

        self.assertEqual(asyncio.run(main()), (200, 500))
        names = sorted(s["name"] for s in self.spans())
        self.assertEqual(names, ["GET /async/boom/", "GET /async/orders/<int:pk>/"])
        (e,) = self.errors()
        self.assertEqual(e["exception"]["values"][-1]["value"], "async django view failed")

    def test_middleware_is_async_capable(self) -> None:
        from asgiref.sync import iscoroutinefunction

        from naijacloud_argus.integrations.django import ArgusMiddleware

        async def get_response(request: Any) -> Any:
            return None

        self.assertTrue(iscoroutinefunction(ArgusMiddleware(get_response)))
        self.assertFalse(iscoroutinefunction(ArgusMiddleware(lambda r: None)))


@unittest.skipUnless(HAS_FASTAPI, "fastapi is not installed")
class FastAPI(FrameworkCase):
    def make_app(self) -> Any:
        from fastapi import FastAPI as App, HTTPException

        from naijacloud_argus.integrations.fastapi import instrument

        app = App()

        @app.get("/items/{item_id}")
        async def item(item_id: int) -> Dict[str, int]:
            if item_id == 404:
                raise HTTPException(404, "no such item")
            if item_id == 500:
                raise RuntimeError("fastapi route failed")
            return {"id": item_id}

        return instrument(app)

    def test_route_template(self) -> None:
        self.init()
        _, sent = call_asgi(self.make_app(), "/items/7")
        self.assertEqual(sent[0]["status"], 200)
        (s,) = self.spans()
        self.assertEqual(s["name"], "GET /items/{item_id}")
        self.assertEqual(attr_map(s["attributes"])["http.route"], "/items/{item_id}")

    def test_http_exception_is_an_answer(self) -> None:
        self.init()
        _, sent = call_asgi(self.make_app(), "/items/404")
        self.assertEqual(sent[0]["status"], 404)
        self.assertEqual(self.errors(), [])
        (s,) = self.spans()
        self.assertEqual(s["status"], {"code": 1})

    def test_unhandled_error(self) -> None:
        self.init()
        with self.assertRaises(RuntimeError):
            call_asgi(self.make_app(), "/items/500")
        (e,) = self.errors()
        self.assertEqual(e["tags"]["mechanism"], "asgi")
        self.assertEqual(e["transaction"], "GET /items/{item_id}")
        (s,) = self.spans()
        self.assertEqual(s["status"]["code"], 2)
        self.assertEqual(attr_map(s["attributes"])["http.response.status_code"], 500)

    def test_plain_starlette_route(self) -> None:
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from naijacloud_argus.integrations.asgi import ArgusASGIMiddleware

        async def hello(request: Any) -> Any:
            return PlainTextResponse("hi")

        self.init()
        app = Starlette(routes=[Route("/hello/{name}", hello)])
        app.add_middleware(ArgusASGIMiddleware)
        call_asgi(app, "/hello/ada")
        (s,) = self.spans()
        self.assertEqual(s["name"], "GET /hello/{name}")


if __name__ == "__main__":
    unittest.main()
