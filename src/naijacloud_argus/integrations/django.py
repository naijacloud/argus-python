"""Django.

Call `init()` in `settings.py` (or `wsgi.py` / `asgi.py`), then put the
middleware first::

    MIDDLEWARE = [
        "naijacloud_argus.integrations.django.ArgusMiddleware",
        ...
    ]

Requests are traced and named after the URL pattern (`GET /orders/<int:pk>/`).
View errors are reported from `process_exception`, before Django turns them
into a 500 page. The middleware works under both WSGI and ASGI, without
Django having to adapt it.

Nothing here imports Django until the middleware is constructed.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional

from .._client import activate_span, deactivate_span
from .._hub import active_client
from .._scope import pop_scope, push_request_scope
from ._http import end_span, set_route, start_request

_ATTR = "_naijacloud_argus"


def _iscoroutinefunction(fn: Any) -> bool:
    try:
        from asgiref.sync import iscoroutinefunction

        return bool(iscoroutinefunction(fn))
    except Exception:
        return inspect.iscoroutinefunction(fn)


def _mark_coroutine(obj: Any) -> None:
    try:
        from asgiref.sync import markcoroutinefunction

        markcoroutinefunction(obj)
    except Exception:
        # Django before 4.1 checks asyncio's private marker.
        marker = getattr(asyncio.coroutines, "_is_coroutine", None)
        if marker is not None:
            obj._is_coroutine = marker


def _headers(request: Any) -> dict:  # type: ignore[type-arg]
    try:
        return {str(k): str(v) for k, v in request.headers.items()}
    except Exception:
        return {}


def _url(request: Any) -> str:
    try:
        return str(request.build_absolute_uri(request.path))
    except Exception:
        return str(getattr(request, "path", "/"))


class ArgusMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        self._async = _iscoroutinefunction(get_response)
        if self._async:
            _mark_coroutine(self)

    def _start(self, request: Any) -> Any:
        scope, token = push_request_scope()
        span = None
        try:
            span = start_request(
                request.method,
                request.path,
                _url(request),
                _headers(request),
                scope,
                request.META.get("REMOTE_ADDR"),
            )
        except Exception:
            pass
        try:
            setattr(request, _ATTR, (span, scope))
        except Exception:
            pass
        return span, (token, activate_span(span))

    @staticmethod
    def _finish(span: Any, tokens: Any, status: Any, error: Optional[BaseException]) -> None:
        end_span(span, status, error)
        deactivate_span(tokens[1])
        pop_scope(tokens[0])

    def __call__(self, request: Any) -> Any:
        if self._async:
            return self.__acall__(request)
        if active_client() is None:
            return self.get_response(request)
        span, tokens = self._start(request)
        try:
            response = self.get_response(request)
        except BaseException as e:
            self._finish(span, tokens, 500, e)
            raise
        self._finish(span, tokens, getattr(response, "status_code", None), None)
        return response

    async def __acall__(self, request: Any) -> Any:
        if active_client() is None:
            return await self.get_response(request)
        span, tokens = self._start(request)
        try:
            response = await self.get_response(request)
        except BaseException as e:
            self._finish(span, tokens, 500, e)
            raise
        self._finish(span, tokens, getattr(response, "status_code", None), None)
        return response

    def process_view(self, request: Any, view_func: Any, view_args: Any, view_kwargs: Any) -> None:
        try:
            span, scope = getattr(request, _ATTR)
            match = getattr(request, "resolver_match", None)
            route: Optional[str] = getattr(match, "route", None) if match is not None else None
            if route is not None:
                set_route(span, scope, request.method, "/" + route.lstrip("^").lstrip("/"))
        except Exception:
            pass
        return None

    def process_exception(self, request: Any, exception: BaseException) -> None:
        c = active_client()
        if c is not None:
            try:
                c.capture_exception(exception, tags={"mechanism": "django"})
            except Exception:
                pass
        return None
