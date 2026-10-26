"""What `init()` installs once per process (with `default_integrations=True`).

- `sys.excepthook`: uncaught exceptions on the main thread, reported as
  `fatal` and flushed before Python prints the traceback and exits.
- `threading.excepthook`: uncaught exceptions in other threads.
- `atexit`: a final flush, so a short script's data is not lost.
- `logging`: records become breadcrumbs, and with `capture_logs`, logs. This
  hooks `Logger.callHandlers` rather than adding a handler to the root logger,
  because a root handler would silently turn a later `logging.basicConfig()`
  into a no-op.
- `http.client`: outgoing requests made through it (urllib, requests,
  urllib3; not httpx or aiohttp, which have their own stacks) get a breadcrumb,
  and inside a span, a child span and a `traceparent` header.

Every hook looks the client up when it fires and does nothing without one, and
every hook swallows its own errors: a monitoring SDK must never be the reason
an app's logging or HTTP call failed.
"""

from __future__ import annotations

import atexit
import http.client
import logging
import sys
import threading
from types import TracebackType
from typing import Any, Callable, Dict, Optional, Type

from ._client import Span, SpanKind, current_span
from ._hub import active_client, get_client
from ._scrub import strip_query

_installed = False
_install_lock = threading.Lock()


def install() -> None:
    global _installed
    with _install_lock:
        if _installed:
            return
        _installed = True
        _install_excepthooks()
        atexit.register(_at_exit)
        _install_logging()
        _install_http_client()


# -- uncaught exceptions ------------------------------------------------------


def _install_excepthooks() -> None:
    prev_sys = sys.excepthook

    def argus_excepthook(
        exc_type: Type[BaseException], exc: BaseException, tb: Optional[TracebackType]
    ) -> None:
        try:
            c = active_client()
            if c is not None and not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                if exc.__traceback__ is None and tb is not None:
                    exc = exc.with_traceback(tb)
                c.capture_exception(exc, level="fatal", tags={"mechanism": "excepthook"})
                c.flush(c.options.shutdown_timeout)
        except Exception:
            pass
        prev_sys(exc_type, exc, tb)

    sys.excepthook = argus_excepthook

    prev_thread = threading.excepthook

    def argus_thread_excepthook(args: Any) -> None:
        try:
            c = active_client()
            exc = args.exc_value
            if c is not None and exc is not None and not isinstance(exc, SystemExit):
                thread = getattr(args, "thread", None)
                c.capture_exception(
                    exc,
                    tags={"mechanism": "threading"},
                    extra={"thread": getattr(thread, "name", None)},
                )
        except Exception:
            pass
        prev_thread(args)

    threading.excepthook = argus_thread_excepthook


def _at_exit() -> None:
    c = get_client()
    if c is not None:
        try:
            c.close(c.options.shutdown_timeout)
        except Exception:
            pass


# -- logging -------------------------------------------------------------------

_SEVERITY = (
    (logging.CRITICAL, "fatal"),
    (logging.ERROR, "error"),
    (logging.WARNING, "warn"),
    (logging.INFO, "info"),
    (logging.DEBUG, "debug"),
)


def log_severity(levelno: int) -> str:
    for threshold, name in _SEVERITY:
        if levelno >= threshold:
            return name
    return "trace"


def breadcrumb_level(levelno: int) -> str:
    sev = log_severity(levelno)
    return {"warn": "warning", "trace": "debug"}.get(sev, sev)


_IGNORED_LOGGERS = ("naijacloud_argus",)
_in_hook = threading.local()


def _ignored(name: str) -> bool:
    return any(name == n or name.startswith(n + ".") for n in _IGNORED_LOGGERS)


def record_attributes(record: logging.LogRecord) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "logger.name": record.name,
        "code.filepath": record.pathname,
        "code.lineno": record.lineno,
        "code.function": record.funcName,
        "thread.name": record.threadName,
    }
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        attrs["exception.type"] = type(exc).__name__
        attrs["exception.message"] = str(exc)[:1000]
        try:
            import traceback

            attrs["exception.stacktrace"] = "".join(traceback.format_exception(*record.exc_info))[
                -8000:
            ]
        except Exception:
            pass
    return attrs


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:
        return str(record.msg)


def handle_record(record: logging.LogRecord) -> None:
    """Breadcrumb, and with capture_logs a log, for one logging record."""
    if getattr(_in_hook, "busy", False) or _ignored(record.name):
        return
    c = active_client()
    if c is None:
        return
    _in_hook.busy = True
    try:
        opts = c.options
        if record.levelno >= opts.breadcrumb_level:
            c.add_breadcrumb(
                {
                    "type": "log",
                    "category": record.name,
                    "level": breadcrumb_level(record.levelno),
                    "message": _message(record)[:1000],
                    "timestamp": record.created,
                },
                {"log_record": record},
            )
        cl = opts.capture_logs
        if cl is not False and not getattr(record, "_argus_shipped", False):
            level = logging.INFO if cl is True else int(cl)
            if record.levelno >= level:
                c.log(log_severity(record.levelno), _message(record), record_attributes(record))  # type: ignore[arg-type]
                # An ArgusHandler further down must not ship it a second time.
                record._argus_shipped = True
    except Exception:
        pass
    finally:
        _in_hook.busy = False


def _install_logging() -> None:
    orig: Callable[[logging.Logger, logging.LogRecord], None] = logging.Logger.callHandlers

    def callHandlers(self: logging.Logger, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            handle_record(record)
        except Exception:
            pass
        orig(self, record)

    logging.Logger.callHandlers = callHandlers  # type: ignore[method-assign]


# -- outgoing http.client -------------------------------------------------------


def _install_http_client() -> None:
    conn_cls = http.client.HTTPConnection
    orig_put: Callable[..., Any] = conn_cls.putrequest
    orig_get: Callable[..., Any] = conn_cls.getresponse

    def putrequest(
        self: http.client.HTTPConnection, method: str, url: str, *a: Any, **kw: Any
    ) -> Any:
        rv = orig_put(self, method, url, *a, **kw)
        try:
            _start_outgoing(self, method, url)
        except Exception:
            pass
        return rv

    def getresponse(self: http.client.HTTPConnection, *a: Any, **kw: Any) -> Any:
        info = getattr(self, "_argus_call", None)
        try:
            res = orig_get(self, *a, **kw)
        except BaseException as e:
            if info is not None:
                self._argus_call = None  # type: ignore[attr-defined]
                _end_outgoing(info, None, e)
            raise
        if info is not None:
            self._argus_call = None  # type: ignore[attr-defined]
            _end_outgoing(info, getattr(res, "status", None), None)
        return res

    conn_cls.putrequest = putrequest  # type: ignore[method-assign]
    conn_cls.getresponse = getresponse  # type: ignore[method-assign]


def _start_outgoing(conn: http.client.HTTPConnection, method: str, url: str) -> None:
    c = active_client()
    if c is None or c.dsn is None:
        return
    host = str(getattr(conn, "host", "") or "")
    if host == c.dsn.hostname:
        return  # the SDK's own requests
    if url.startswith(("http://", "https://")):
        full = url
    else:
        scheme = "https" if isinstance(conn, http.client.HTTPSConnection) else "http"
        port = getattr(conn, "port", None)
        default = 443 if scheme == "https" else 80
        netloc = host if not port or port == default else "{}:{}".format(host, port)
        full = "{}://{}{}".format(scheme, netloc, url)
    full = strip_query(full)
    method = (method or "GET").upper()
    span: Optional[Span] = None
    parent = current_span()
    if parent is not None:
        span = c.start_span(
            "{} {}".format(method, full),
            kind=SpanKind.CLIENT,
            attributes={"http.request.method": method, "url.full": full, "server.address": host},
            parent=parent,
        )
        try:
            conn.putheader("traceparent", span.traceparent())
        except Exception:
            pass
    conn._argus_call = (method, full, span)  # type: ignore[attr-defined]


def _end_outgoing(info: Any, status: Optional[int], error: Optional[BaseException]) -> None:
    try:
        method, url, span = info
        if span is not None:
            if status is not None:
                span.set_attribute("http.response.status_code", status)
            span.end(
                error
                if error is not None
                else ("HTTP {}".format(status) if status and status >= 500 else None)
            )
        c = active_client()
        if c is not None:
            data: Dict[str, Any] = {"method": method, "url": url}
            if status is not None:
                data["status_code"] = status
            c.add_breadcrumb(
                {
                    "type": "http",
                    "category": "http.client",
                    "level": "error" if error is not None or (status or 0) >= 500 else "info",
                    "data": data,
                }
            )
    except Exception:
        pass
