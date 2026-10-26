"""Scopes: the user, tags, context and breadcrumbs that ride on the next event.

There is one process-wide scope. A web request gets its own copy (the WSGI,
ASGI, Django and Flask integrations open one with `new_scope()`), held in a
`ContextVar`, so `set_user()` inside one request never leaks into another,
whether requests run on threads or on an event loop.
"""

from __future__ import annotations

import contextvars
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, Optional, Tuple

#: The spec's breadcrumb ceiling (section 4: "the SDK keeps the last 100").
MAX_BREADCRUMBS = 100


class Scope:
    __slots__ = ("user", "tags", "contexts", "extra", "breadcrumbs", "transaction", "request")

    def __init__(self) -> None:
        self.user: Optional[Dict[str, Any]] = None
        self.tags: Dict[str, str] = {}
        self.contexts: Dict[str, Any] = {}
        self.extra: Dict[str, Any] = {}
        self.breadcrumbs: Deque[Dict[str, Any]] = deque(maxlen=MAX_BREADCRUMBS)
        #: The route or job name; becomes the event's `transaction` (the culprit).
        self.transaction: Optional[str] = None
        #: `{"method", "url", "headers"}` for errors raised inside a request.
        self.request: Optional[Dict[str, Any]] = None

    def copy(self) -> Scope:
        s = Scope()
        s.user = dict(self.user) if self.user is not None else None
        s.tags = dict(self.tags)
        s.contexts = dict(self.contexts)
        s.extra = dict(self.extra)
        s.breadcrumbs = deque(self.breadcrumbs, maxlen=MAX_BREADCRUMBS)
        s.transaction = self.transaction
        s.request = dict(self.request) if self.request is not None else None
        return s

    def clear(self) -> None:
        self.__init__()  # type: ignore[misc]


_global = Scope()
_current: contextvars.ContextVar[Optional[Scope]] = contextvars.ContextVar(
    "naijacloud_argus_scope", default=None
)


def global_scope() -> Scope:
    return _global


def current_scope() -> Scope:
    """The scope of the current request or task, else the process-wide one."""
    return _current.get() or _global


@contextmanager
def new_scope() -> Iterator[Scope]:
    """A copy of the current scope for the duration of the block.

    Changes made inside (`set_user`, `set_tag`, breadcrumbs) are gone when the
    block exits::

        with argus.new_scope():
            argus.set_user({"id": job.owner_id})
            run(job)
    """
    scope = current_scope().copy()
    token = _current.set(scope)
    try:
        yield scope
    finally:
        try:
            _current.reset(token)
        except ValueError:
            # Exited in a different context (a generator finished elsewhere);
            # that context is gone anyway.
            pass


def push_request_scope() -> Tuple[Scope, contextvars.Token[Optional[Scope]]]:
    """A fresh request scope copied from the process-wide one, made current.

    Copied from the global scope rather than the current one, so a scope a
    misbehaving server leaked on a reused thread never seeds the next request.
    """
    scope = _global.copy()
    return scope, _current.set(scope)


def pop_scope(token: contextvars.Token[Optional[Scope]]) -> None:
    try:
        _current.reset(token)
    except ValueError:
        pass
