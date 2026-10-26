"""ASGI middleware: FastAPI, Starlette, Quart, Litestar, Django under ASGI.

::

    from naijacloud_argus.integrations.asgi import ArgusASGIMiddleware
    app = ArgusASGIMiddleware(app)            # any ASGI app
    app.add_middleware(ArgusASGIMiddleware)   # FastAPI / Starlette

Each HTTP request gets a server span (continuing the caller's `traceparent`)
and its own scope. Exceptions that escape the app are reported and re-raised.
For FastAPI and Starlette the span is named after the route template
(`GET /items/{item_id}`) and carries `http.route`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional

from .._client import activate_span, deactivate_span
from .._hub import active_client
from .._scope import pop_scope, push_request_scope
from ._http import capture_request_error, end_span, set_route, start_request

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[MutableMapping[str, Any], Receive, Send], Awaitable[None]]


def _headers(scope: MutableMapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in scope.get("headers") or []:
        try:
            name = k.decode("latin-1") if isinstance(k, bytes) else str(k)
            value = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        except Exception:
            continue
        out[name] = "{}, {}".format(out[name], value) if name in out else value
    return out


def _url(scope: MutableMapping[str, Any], headers: Dict[str, str]) -> str:
    scheme = scope.get("scheme", "http")
    host = next((v for k, v in headers.items() if k.lower() == "host"), None)
    if not host:
        server = scope.get("server")
        if server and len(server) == 2 and server[0]:
            port = server[1]
            default = 443 if scheme == "https" else 80
            host = server[0] if port in (None, default) else "{}:{}".format(server[0], port)
        else:
            host = "localhost"
    return "{}://{}{}{}".format(scheme, host, scope.get("root_path", ""), scope.get("path", "/"))


def route_of(scope: MutableMapping[str, Any]) -> Optional[str]:
    """The route template FastAPI or Starlette matched, if it can be found."""
    route = scope.get("route")
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    if isinstance(path, str):
        return path
    # Plain Starlette sets no scope["route"]; ask the router which route matches.
    router = scope.get("router")
    for r in getattr(router, "routes", None) or []:
        try:
            match, _ = r.matches(scope)
        except Exception:
            continue
        if getattr(match, "name", "") == "FULL":
            p = getattr(r, "path", None)
            return p if isinstance(p, str) else None
    return None


class ArgusASGIMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or active_client() is None:
            await self.app(scope, receive, send)
            return
        req_scope, token = push_request_scope()
        method = str(scope.get("method", "GET"))
        span = None
        try:
            headers = _headers(scope)
            client = scope.get("client")
            span = start_request(
                method,
                str(scope.get("root_path", "")) + str(scope.get("path", "/")),
                _url(scope, headers),
                headers,
                req_scope,
                client[0] if client else None,
            )
        except Exception:
            pass
        status: List[Optional[int]] = [None]

        def name_route() -> None:
            try:
                set_route(span, req_scope, method, route_of(scope))
            except Exception:
                pass

        async def _send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status[0] = message.get("status")
                name_route()
            await send(message)

        span_token = activate_span(span)
        error: Optional[BaseException] = None
        try:
            await self.app(scope, receive, _send)
        except Exception as e:
            error = e
            name_route()
            capture_request_error(e, "asgi")
            raise
        except BaseException as e:  # cancellation
            error = e
            raise
        finally:
            name_route()
            if error is not None and not isinstance(error, Exception):
                end_span(span, status[0], error)
            elif error is not None:
                end_span(span, status[0] or 500, error)
            else:
                end_span(span, status[0], None)
            deactivate_span(span_token)
            pop_scope(token)
