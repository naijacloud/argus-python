"""What the web integrations share: one server span and one scope per request."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from .._client import Span, SpanKind, parse_traceparent
from .._hub import active_client
from .._scope import Scope
from .._scrub import strip_query


def start_request(
    method: str,
    path: str,
    url: str,
    headers: Mapping[str, str],
    scope: Scope,
    remote_addr: Optional[str] = None,
) -> Optional[Span]:
    """Opens the server span (not yet current) and fills the request scope.

    None without a client.
    """
    c = active_client()
    if c is None:
        return None
    method = (method or "GET").upper()
    path = strip_query(path or "/")
    lower = {k.lower(): v for k, v in headers.items()}
    attributes: Dict[str, Any] = {
        "http.request.method": method,
        "url.path": path,
        "url.full": strip_query(url),
    }
    if lower.get("host"):
        attributes["server.address"] = lower["host"]
    if lower.get("user-agent"):
        attributes["user_agent.original"] = lower["user-agent"][:300]
    # Built directly rather than through start_span(): a server span is a new
    # root unless the caller sent a traceparent, never a child of whatever
    # span a previous request on this thread may have left active.
    span = Span(
        c,
        "{} {}".format(method, path),
        SpanKind.SERVER,
        parse_traceparent(lower.get("traceparent")),
        attributes,
    )
    scope.transaction = "{} {}".format(method, path)
    scope.request = {
        "method": method,
        "url": strip_query(url),
        # Cookies and the body are never sent (spec section 7); sensitive
        # headers are filtered when the event is built.
        "headers": {k: v for k, v in headers.items() if k.lower() != "cookie"},
    }
    if remote_addr and c.options.send_default_pii:
        scope.user = dict(scope.user or {}, ip_address=remote_addr)
    return span


def set_route(span: Optional[Span], scope: Scope, method: str, route: Optional[str]) -> None:
    """Names the span and the culprit after the route template, not the raw path."""
    if not route:
        return
    method = (method or "GET").upper()
    scope.transaction = "{} {}".format(method, route)
    if span is not None:
        span.name = "{} {}".format(method, route)
        span.set_attribute("http.route", route)


def end_span(span: Optional[Span], status: Optional[int], error: Optional[BaseException]) -> None:
    """Ends the server span: failed on an exception or a 5xx."""
    if span is None:
        return
    try:
        if status is not None:
            span.set_attribute("http.response.status_code", status)
        if error is not None:
            span.end(error)
        elif status is not None and status >= 500:
            span.end("HTTP {}".format(status))
        else:
            span.end()
    except Exception:
        pass


def capture_request_error(error: BaseException, mechanism: str) -> None:
    c = active_client()
    if c is None:
        return
    try:
        c.capture_exception(error, tags={"mechanism": mechanism})
    except Exception:
        pass


def split_status(status: str) -> Tuple[Optional[int], str]:
    try:
        return int(status.split(" ", 1)[0]), status
    except Exception:
        return None, status
