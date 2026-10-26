"""WSGI middleware: any WSGI app (Flask, Django, Pyramid, Bottle, Falcon).

::

    from naijacloud_argus.integrations.wsgi import ArgusWSGIMiddleware
    app = ArgusWSGIMiddleware(app)

Each request gets a server span (continuing the caller's `traceparent`) and its
own scope. Exceptions that escape the app are reported and re-raised. The span
ends when the body has been sent (or the server closes it), so streamed
responses are timed in full.

The span and the scope are current only while the app's code runs: during the
call, and around each chunk a streamed body yields. Nothing stays current on
the server's thread afterwards, so a response the server never closes cannot
leak one request's user or trace into the next.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from .._client import Span, activate_span, deactivate_span
from .._hub import active_client
from .._scope import Scope, _current as _current_scope, global_scope
from ._http import capture_request_error, end_span, split_status, start_request

#: Where the request's span and scope sit in the WSGI environ, so framework
#: integrations can add the route template.
ENVIRON_SPAN = "naijacloud_argus.span"
ENVIRON_SCOPE = "naijacloud_argus.scope"

WSGIApp = Callable[[Dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def _headers(environ: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            out[k[5:].replace("_", "-").title()] = str(v)
    for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
        if environ.get(k):
            out[k.replace("_", "-").title()] = str(environ[k])
    return out


def _url(environ: Dict[str, Any]) -> str:
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST")
    if not host:
        host = environ.get("SERVER_NAME", "localhost")
        port = str(environ.get("SERVER_PORT", ""))
        if port and port != ("443" if scheme == "https" else "80"):
            host = "{}:{}".format(host, port)
    return "{}://{}{}{}".format(
        scheme, host, environ.get("SCRIPT_NAME", ""), environ.get("PATH_INFO", "")
    )


class _Active:
    """Makes the request's span and scope current for a `with` block."""

    __slots__ = ("span", "scope", "_tokens")

    def __init__(self, span: Optional[Span], scope: Scope) -> None:
        self.span = span
        self.scope = scope
        self._tokens: List[Any] = []

    def __enter__(self) -> None:
        self._tokens.append((activate_span(self.span), _current_scope.set(self.scope)))

    def __exit__(self, *exc: Any) -> None:
        if self._tokens:
            span_token, scope_token = self._tokens.pop()
            deactivate_span(span_token)
            _reset_scope(scope_token)


def _reset_scope(token: contextvars.Token[Optional[Scope]]) -> None:
    try:
        _current_scope.reset(token)
    except ValueError:
        pass


class _Response:
    """Wraps the app's iterable: times the body, reports errors raised while
    streaming it, and ends the span when it is exhausted or closed."""

    def __init__(
        self,
        inner: Iterable[bytes],
        active: _Active,
        finish: Callable[[Optional[BaseException]], None],
    ) -> None:
        self._inner = inner
        self._it: Optional[Iterator[bytes]] = None
        self._active = active
        self._finish = finish
        self._done = False

    def __iter__(self) -> _Response:
        return self

    def __next__(self) -> bytes:
        with self._active:
            try:
                if self._it is None:
                    self._it = iter(self._inner)
                return next(self._it)
            except StopIteration:
                # Servers must also call close(), but some test clients and
                # hand-rolled servers do not, and the request is over anyway.
                self._end(None)
                raise
            except Exception as e:
                capture_request_error(e, "wsgi")
                self._end(e)
                raise

    def _end(self, error: Optional[BaseException]) -> None:
        if not self._done:
            self._done = True
            self._finish(error)

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                with self._active:
                    close()
        finally:
            self._end(None)


class ArgusWSGIMiddleware:
    def __init__(self, app: WSGIApp) -> None:
        self.app = app

    def __call__(
        self, environ: Dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        if active_client() is None:
            return self.app(environ, start_response)
        # A copy of the process-wide scope, current only inside _Active blocks.
        scope = global_scope().copy()
        span = None
        try:
            span = start_request(
                environ.get("REQUEST_METHOD", "GET"),
                environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", ""),
                _url(environ),
                _headers(environ),
                scope,
                environ.get("REMOTE_ADDR"),
            )
            environ[ENVIRON_SPAN] = span
            environ[ENVIRON_SCOPE] = scope
        except Exception:
            pass
        active = _Active(span, scope)
        status: List[Optional[int]] = [None]

        def _start_response(status_line: str, headers: Any, *exc_info: Any) -> Any:
            status[0] = split_status(status_line)[0]
            return start_response(status_line, headers, *exc_info)

        def finish(error: Optional[BaseException]) -> None:
            end_span(span, status[0] if error is None else (status[0] or 500), error)

        with active:
            try:
                inner = self.app(environ, _start_response)
            except Exception as e:
                capture_request_error(e, "wsgi")
                finish(e)
                raise
            except BaseException as e:
                finish(e)
                raise
        return _Response(inner, active, finish)
