"""The module-level API. Every function talks to the client `init()` made;
before `init()`, or without a DSN, each one does nothing and returns its
default. None of them raises into the app."""

from __future__ import annotations

import asyncio
import functools
import inspect
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

from . import _hooks
from ._client import (
    Client,
    Span,
    SpanContext,
    SpanKind,
    current_span,
    env_options,
    parse_traceparent,
)
from ._hub import active_client, get_client, set_client
from ._scope import current_scope
from ._types import Breadcrumb, CheckInStatus, Level, User

F = TypeVar("F", bound=Callable[..., Any])


def init(dsn: Optional[str] = None, **options: Any) -> Client:
    """Starts Argus and returns the client.

    Options win over `ARGUS_*`, then `SENTRY_*` environment variables. Without
    a DSN the SDK is a no-op. Calling it again replaces the previous client
    (which is flushed and closed first).
    """
    options["dsn"] = dsn
    try:
        old = get_client()
        if old is not None:
            old.close(0.5)
        client = Client(env_options(options))
    except Exception:
        from ._client import Options

        client = Client(Options(enabled=False))
    set_client(client)
    if client.enabled and client.options.default_integrations:
        try:
            _hooks.install()
        except Exception:
            pass
    return client


# -- errors ---------------------------------------------------------------------


def capture_exception(error: Optional[BaseException] = None, **ctx: Any) -> str:
    """Sends an error (the one being handled when `error` is omitted).

    Keyword context: `level`, `tags`, `extra`, `user`, `contexts`,
    `fingerprint`, `request`, `transaction`. Returns the event id, or "" when
    nothing was sent.
    """
    c = active_client()
    if c is None:
        return ""
    try:
        return c.capture_exception(error, **ctx)
    except Exception:
        return ""


def capture_message(message: str, level: Level = "info", **ctx: Any) -> str:
    """Sends a message event. Same keyword context as `capture_exception`."""
    c = active_client()
    if c is None:
        return ""
    try:
        return c.capture_message(message, level, **ctx)
    except Exception:
        return ""


# -- scope ----------------------------------------------------------------------


def set_user(user: Optional[User]) -> None:
    """The user on the next events. `None` clears it."""
    try:
        current_scope().user = dict(user) if user else None  # type: ignore[arg-type]
    except Exception:
        pass


def set_tag(key: str, value: Any) -> None:
    try:
        current_scope().tags[str(key)] = str(value)
    except Exception:
        pass


def set_tags(tags: Mapping[str, Any]) -> None:
    for k, v in (tags or {}).items():
        set_tag(k, v)


def set_context(name: str, context: Optional[Mapping[str, Any]]) -> None:
    """A named block of structured context. `None` removes it."""
    try:
        if context is None:
            current_scope().contexts.pop(name, None)
        else:
            current_scope().contexts[name] = dict(context)
    except Exception:
        pass


def set_extra(key: str, value: Any) -> None:
    try:
        current_scope().extra[str(key)] = value
    except Exception:
        pass


def add_breadcrumb(crumb: Optional[Breadcrumb] = None, **fields: Any) -> None:
    """Records a breadcrumb: `add_breadcrumb(category="auth", message="signed in")`."""
    c = active_client()
    if c is None:
        return
    try:
        merged: Dict[str, Any] = dict(crumb or {})
        merged.update(fields)
        c.add_breadcrumb(merged)  # type: ignore[arg-type]
    except Exception:
        pass


# -- logs -----------------------------------------------------------------------


def _attrs(attributes: Optional[Mapping[str, Any]], kw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not attributes and not kw:
        return None
    out = dict(attributes or {})
    out.update(kw)
    return out


class _Log:
    """Logs sent to Argus Logs. Your own logging setup is untouched.

    ``argus.log.info("order placed", order_id="ord_81", total=18500)``, or pass
    a dict for keys that are not identifiers: ``{"http.route": "/pay"}``.
    """

    def _emit(
        self, severity: str, body: str, attributes: Optional[Mapping[str, Any]], kw: Dict[str, Any]
    ) -> None:
        c = active_client()
        if c is None:
            return
        try:
            c.log(severity, body, _attrs(attributes, kw))  # type: ignore[arg-type]
        except Exception:
            pass

    def trace(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("trace", body, attributes, kw)

    def debug(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("debug", body, attributes, kw)

    def info(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("info", body, attributes, kw)

    def warn(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("warn", body, attributes, kw)

    warning = warn

    def error(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("error", body, attributes, kw)

    def fatal(self, body: str, attributes: Optional[Mapping[str, Any]] = None, **kw: Any) -> None:
        self._emit("fatal", body, attributes, kw)


log = _Log()


# -- metrics --------------------------------------------------------------------


class _Metrics:
    """Custom metrics, added up in the process and sent every 10 seconds."""

    def count(
        self, name: str, value: float = 1, attributes: Optional[Mapping[str, Any]] = None
    ) -> None:
        """Adds to a counter (default 1)."""
        c = active_client()
        if c is not None:
            try:
                c.count(name, value, attributes)
            except Exception:
                pass

    def gauge(
        self,
        name: str,
        value: float,
        attributes: Optional[Mapping[str, Any]] = None,
        unit: Optional[str] = None,
    ) -> None:
        """Sets a gauge to its latest value."""
        c = active_client()
        if c is not None:
            try:
                c.gauge(name, value, attributes, unit)
            except Exception:
                pass

    def distribution(
        self,
        name: str,
        value: float,
        attributes: Optional[Mapping[str, Any]] = None,
        unit: Optional[str] = None,
    ) -> None:
        """Records a value into a histogram (durations, sizes)."""
        c = active_client()
        if c is not None:
            try:
                c.distribution(name, value, attributes, unit)
            except Exception:
                pass


metrics = _Metrics()


# -- traces ---------------------------------------------------------------------


def start_span(
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
    kind: int = SpanKind.INTERNAL,
    parent: Optional[Union[Span, SpanContext, str]] = None,
) -> Span:
    """Starts a span you end yourself (or use with ``with``).

    It joins the active span, or `parent`: a Span, a SpanContext, or a W3C
    `traceparent` string from an incoming message. Before `init()` it is a
    span that records nothing, so calling code needs no checks.
    """
    try:
        p: Optional[Union[Span, SpanContext]]
        p = parse_traceparent(parent) if isinstance(parent, str) else parent
        c = active_client()
        if c is None:
            return Span(None, name, kind, p or current_span(), attributes)
        return c.start_span(name, kind, attributes, p)
    except Exception:
        return Span(None, name, kind, None, None)


def get_traceparent() -> Optional[str]:
    """The active span's `traceparent`, to pass to another service by hand."""
    span = current_span()
    return span.traceparent() if span is not None else None


class _Traced:
    """`trace()`'s return value: a context manager and a decorator."""

    def __init__(
        self, name: Optional[str], attributes: Optional[Mapping[str, Any]], kind: int
    ) -> None:
        self._name = name
        self._attributes = dict(attributes or {})
        self._kind = kind
        self._spans: List[Span] = []

    def __enter__(self) -> Span:
        span = start_span(self._name or "span", self._attributes, self._kind)
        self._spans.append(span)
        return span.__enter__()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self._spans:
            self._spans.pop().__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __call__(self, fn: F) -> F:
        name = str(
            self._name or getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "span")
        )
        attributes, kind = self._attributes, self._kind

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with start_span(name, attributes, kind):
                    return await fn(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with start_span(name, attributes, kind):
                return fn(*args, **kwargs)

        return cast(F, wrapper)


@overload
def trace(name: F) -> F: ...


@overload
def trace(
    name: Optional[str] = None,
    attributes: Optional[Mapping[str, Any]] = None,
    kind: int = SpanKind.INTERNAL,
) -> _Traced: ...


def trace(
    name: Union[str, Callable[..., Any], None] = None,
    attributes: Optional[Mapping[str, Any]] = None,
    kind: int = SpanKind.INTERNAL,
) -> Any:
    """Runs a block or a function inside a span.

    ::

        with argus.trace("charge card", {"provider": "paystack"}) as span:
            charge(order)

        @argus.trace("nightly report")
        def build(): ...

        @argus.trace          # named after the function
        async def fetch_rates(): ...

    The span joins the active one, ends when the block or call does, and is
    marked failed when it raises. The exception still propagates.
    """
    if callable(name):
        return _Traced(None, None, SpanKind.INTERNAL)(name)
    return _Traced(name, attributes, kind)


# -- crons ----------------------------------------------------------------------


class _Monitor:
    """`cron.monitor()`'s return value: a context manager and a decorator."""

    def __init__(self, slug: str) -> None:
        self._slug = slug

    def __enter__(self) -> _Monitor:
        cron.check_in(self._slug, "start")
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc is None:
            cron.check_in(self._slug, "ok")
        elif isinstance(exc, SystemExit):
            code = exc.code
            if code is None or code == 0:
                cron.check_in(self._slug, "ok")
            elif isinstance(code, int) and not isinstance(code, bool):
                cron.check_in(self._slug, code)
            else:
                cron.check_in(self._slug, "error", str(code))
        else:
            cron.check_in(self._slug, "error", "{}: {}".format(type(exc).__name__, exc))

    async def __aenter__(self) -> _Monitor:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __call__(self, fn: F) -> F:
        slug = self._slug
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _Monitor(slug):
                    return await fn(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _Monitor(slug):
                return fn(*args, **kwargs)

        return cast(F, wrapper)


class _Cron:
    """Cron check-ins by the monitor's slug. Create the monitor in Argus first."""

    def check_in(
        self, slug: str, status: CheckInStatus = "ok", output: Optional[str] = None
    ) -> None:
        """`status`: "start", "ok", "error", or the job's exit code."""
        c = active_client()
        if c is not None:
            try:
                c.check_in(slug, status, output)
            except Exception:
                pass

    def monitor(self, slug: str) -> _Monitor:
        """Reports a job's start, then success or failure. Re-raises its error.

        ::

            with argus.cron.monitor("nightly-invoices"):
                send_invoices()

            @argus.cron.monitor("nightly-invoices")
            def send_invoices(): ...
        """
        return _Monitor(slug)


cron = _Cron()


# -- flags ----------------------------------------------------------------------


def _run_in_thread(fn: Callable[[], Any]) -> Awaitable[Any]:
    return asyncio.get_running_loop().run_in_executor(None, fn)


class _Flags:
    """Feature flags, evaluated by Argus for a subject (usually a user id).

    Answers are cached for 30 seconds per subject. If Argus can't be reached
    within `flags_timeout`, or the flag doesn't exist, you get your default.
    The plain methods block for at most `flags_timeout`; in async code use the
    `*_async` ones.
    """

    def all(self, subject: str, attributes: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
        c = active_client()
        if c is None:
            return {}
        try:
            return dict(c.evaluate_flags(subject, attributes))
        except Exception:
            return {}

    def variant(
        self,
        key: str,
        subject: str,
        default: str,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> str:
        return self.all(subject, attributes).get(key, default)

    def is_on(
        self,
        key: str,
        subject: str,
        default: bool = False,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        return self.variant(key, subject, "on" if default else "off", attributes) in ("on", "true")

    async def all_async(
        self, subject: str, attributes: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, str]:
        if active_client() is None:
            return {}
        try:
            return cast(Dict[str, str], await _run_in_thread(lambda: self.all(subject, attributes)))
        except Exception:
            return {}

    async def variant_async(
        self,
        key: str,
        subject: str,
        default: str,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> str:
        return (await self.all_async(subject, attributes)).get(key, default)

    async def is_on_async(
        self,
        key: str,
        subject: str,
        default: bool = False,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        v = await self.variant_async(key, subject, "on" if default else "off", attributes)
        return v in ("on", "true")


flags = _Flags()


# -- lifecycle ------------------------------------------------------------------


def flush(timeout: float = 2.0) -> bool:
    """Sends what's queued. True when it all went out within `timeout` seconds."""
    c = get_client()
    if c is None:
        return True
    try:
        return c.flush(timeout)
    except Exception:
        return False


def close(timeout: float = 2.0) -> bool:
    """Flushes and stops the SDK."""
    c = get_client()
    set_client(None)
    if c is None:
        return True
    try:
        return c.close(timeout)
    except Exception:
        return False
