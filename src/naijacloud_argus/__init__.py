"""The official Python SDK for Naijacloud Argus.

::

    import naijacloud_argus as argus
    argus.init(dsn="https://<key>@argus-in.naijacloud.com/<id>")

Errors, logs, metrics, traces, cron check-ins and feature flags, with one key.
Implements the Argus SDK contract `argus.sdk/1`. No runtime dependencies.
"""

from __future__ import annotations

from ._api import (
    add_breadcrumb,
    capture_exception,
    capture_message,
    close,
    cron,
    flags,
    flush,
    get_traceparent,
    init,
    log,
    metrics,
    set_context,
    set_extra,
    set_tag,
    set_tags,
    set_user,
    start_span,
    trace,
)
from ._client import Client, Options, Span, SpanContext, SpanKind, current_span, parse_traceparent
from ._hub import get_client
from ._scope import new_scope
from ._types import Breadcrumb, Event, Hint, Level, User
from ._version import CONTRACT, __version__
from .integrations.logging import ArgusHandler

__all__ = [
    "CONTRACT",
    "ArgusHandler",
    "Breadcrumb",
    "Client",
    "Event",
    "Hint",
    "Level",
    "Options",
    "Span",
    "SpanContext",
    "SpanKind",
    "User",
    "__version__",
    "add_breadcrumb",
    "capture_exception",
    "capture_message",
    "close",
    "cron",
    "current_span",
    "flags",
    "flush",
    "get_client",
    "get_traceparent",
    "init",
    "log",
    "metrics",
    "new_scope",
    "parse_traceparent",
    "set_context",
    "set_extra",
    "set_tag",
    "set_tags",
    "set_user",
    "start_span",
    "trace",
]
