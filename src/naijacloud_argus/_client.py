"""The client: builds events and OTLP batches and hands them to the transport."""

from __future__ import annotations

import contextvars
import gzip
import json
import math
import os
import platform as _platform
import random
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from . import _otlp
from ._dsn import Dsn, cron_path, parse_dsn
from ._scope import current_scope
from ._scrub import FILTERED, scrub, scrub_string, strip_query
from ._stack import exception_chain
from ._transport import Request, Transport, build_opener, debug_log, http_post, retry_after
from ._types import BeforeBreadcrumb, BeforeSend, Breadcrumb, Event, Hint, Level, LogSeverity
from ._version import CONTRACT, SDK_NAME, __version__

USER_AGENT = "{}/{}".format(SDK_NAME, __version__)

FLUSH_LOGS_S = 5.0
FLUSH_SPANS_S = 5.0
FLUSH_METRICS_S = 10.0
MAX_LOGS = 1000
MAX_SPANS = 512
MAX_SERIES = 2000
#: Split OTLP batches above this so every request fits a 1 MB lane (spec: 1 MB
#: compressed for traces, 5 MB for logs and metrics).
MAX_OTLP_BODY = 512 * 1024
FLAG_CACHE_S = 30.0

LEVELS = ("fatal", "error", "warning", "info", "debug")
_LEVEL_ALIASES = {"warn": "warning", "critical": "fatal", "exception": "error"}

#: Set on an exception once it has been reported, so the same error object is
#: sent once however many handlers see it.
_CAPTURED = "__argus_captured__"


def _level(level: Any) -> str:
    lv = str(level or "error").lower()
    lv = _LEVEL_ALIASES.get(lv, lv)
    return lv if lv in LEVELS else "error"


def _new_trace_id() -> str:
    return os.urandom(16).hex()


def _new_span_id() -> str:
    return os.urandom(8).hex()


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _env(*names: str) -> Optional[str]:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return None


# --------------------------------------------------------------------------
# Spans
# --------------------------------------------------------------------------


class SpanContext(NamedTuple):
    trace_id: str
    span_id: str
    sampled: bool


_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def parse_traceparent(header: Optional[str]) -> Optional[SpanContext]:
    """Parses a W3C `traceparent` header, or returns None."""
    if not header or not isinstance(header, str):
        return None
    m = _TRACEPARENT.match(header.strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return SpanContext(m.group(1), m.group(2), (int(m.group(3), 16) & 1) == 1)


_current_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar(
    "naijacloud_argus_span", default=None
)


def current_span() -> Optional[Span]:
    return _current_span.get()


def activate_span(span: Optional[Span]) -> contextvars.Token[Optional[Span]]:
    """Makes a span current without ending it later (the integrations' tool)."""
    return _current_span.set(span)


def deactivate_span(token: contextvars.Token[Optional[Span]]) -> None:
    try:
        _current_span.reset(token)
    except ValueError:
        pass


class SpanKind:
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3


_URL_KEYS = ("url.full", "http.url", "url")


class Span:
    """A unit of work in a trace. End it once, or use it as a context manager.

    ``with span:`` makes it the active span (children and logs join it) and
    ends it on exit, marking it failed if the block raised.
    """

    __slots__ = (
        "_client",
        "trace_id",
        "span_id",
        "parent_span_id",
        "sampled",
        "kind",
        "name",
        "attributes",
        "start_ns",
        "_error",
        "_ended",
        "_tokens",
    )

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    sampled: bool

    def __init__(
        self,
        client: Optional[Client],
        name: str,
        kind: int = SpanKind.INTERNAL,
        parent: Optional[Union[SpanContext, Span]] = None,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._client = client
        self.name = name
        self.kind = kind if kind in (1, 2, 3) else 1
        self.attributes: Dict[str, Any] = dict(attributes or {})
        if parent is not None:
            self.trace_id = parent.trace_id
            self.parent_span_id: Optional[str] = parent.span_id
            self.sampled = bool(parent.sampled)
        else:
            self.trace_id = _new_trace_id()
            self.parent_span_id = None
            self.sampled = client.sample_trace() if client is not None else False
        self.span_id = _new_span_id()
        self.start_ns = time.time_ns()
        self._error: Optional[str] = None
        self._ended = False
        self._tokens: List[contextvars.Token[Optional[Span]]] = []

    @property
    def is_recording(self) -> bool:
        return self.sampled and self._client is not None and not self._ended

    def set_attribute(self, key: str, value: Any) -> Span:
        self.attributes[key] = value
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> Span:
        self.attributes.update(attributes)
        return self

    def set_error(self, error: Union[BaseException, str, None]) -> Span:
        """Marks the span failed without ending it."""
        self._error = _error_text(error)
        return self

    def traceparent(self) -> str:
        """The W3C `traceparent` value for an outgoing request."""
        return "00-{}-{}-{}".format(self.trace_id, self.span_id, "01" if self.sampled else "00")

    def end(self, error: Union[BaseException, str, bool, None] = None) -> None:
        if self._ended:
            return
        self._ended = True
        if error is not None and error is not False:
            self._error = _error_text(error if error is not True else "error")
        if not self.sampled or self._client is None:
            return
        try:
            self._client.record_span(
                _otlp.SpanData(
                    self.trace_id,
                    self.span_id,
                    self.parent_span_id,
                    self.name,
                    self.kind,
                    self.start_ns,
                    time.time_ns(),
                    self._error,
                    self.attributes,
                )
            )
        except Exception:
            pass

    def __enter__(self) -> Span:
        self._tokens.append(_current_span.set(self))
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self._tokens:
            try:
                _current_span.reset(self._tokens.pop())
            except ValueError:
                pass
        self.end(exc if exc is not None and not isinstance(exc, GeneratorExit) else None)

    def __repr__(self) -> str:
        return "<Span {!r} trace_id={} span_id={}>".format(self.name, self.trace_id, self.span_id)


def _error_text(error: Union[BaseException, str, None]) -> Optional[str]:
    if error is None:
        return None
    if isinstance(error, BaseException):
        try:
            msg = str(error)
        except Exception:
            msg = ""
        return (msg or type(error).__name__)[:1000]
    return str(error)[:1000]


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


@dataclass
class Options:
    dsn: Optional[str] = None
    release: Optional[str] = None
    environment: str = "production"
    service_name: str = "app"
    server_name: Optional[str] = None
    sample_rate: float = 1.0
    traces_sample_rate: float = 1.0
    send_default_pii: bool = False
    max_breadcrumbs: int = 100
    before_send: Optional[BeforeSend] = None
    before_breadcrumb: Optional[BeforeBreadcrumb] = None
    #: Ship `logging` records as Argus logs: False, True (INFO and up), or a level.
    capture_logs: Union[bool, int] = False
    #: `logging` records at this level and up become breadcrumbs.
    breadcrumb_level: int = 20
    #: Hooks installed by init: excepthooks, atexit, logging, outgoing http.client.
    default_integrations: bool = True
    debug: bool = False
    enabled: bool = True
    #: Seconds `atexit` and the excepthooks wait for queued data to go out.
    shutdown_timeout: float = 2.0
    #: Seconds a flag evaluation may take before the default is returned.
    flags_timeout: float = 2.0
    #: Seconds for one HTTP request to ingest.
    timeout: float = 10.0


class _MetricAgg:
    __slots__ = (
        "kind",
        "name",
        "unit",
        "attributes",
        "value",
        "count",
        "sum",
        "min",
        "max",
        "buckets",
    )

    def __init__(
        self, kind: str, name: str, unit: Optional[str], attributes: Optional[Dict[str, Any]]
    ):
        self.kind = kind
        self.name = name
        self.unit = unit
        self.attributes = attributes
        self.value = 0.0
        self.count = 0
        self.sum = 0.0
        self.min = math.inf
        self.max = -math.inf
        self.buckets = [0] * (len(_otlp.HIST_BOUNDS) + 1)


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class Client:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.dsn: Optional[Dsn] = parse_dsn(options.dsn) if options.enabled else None
        if options.dsn and options.enabled and self.dsn is None:
            self._debug("the DSN is not valid; nothing will be sent")
        self.transport = Transport(debug=options.debug, timeout=options.timeout)
        self._closed = False
        self._reset_state()
        self._contexts = {
            "runtime": {
                "name": _platform.python_implementation(),
                "version": _platform.python_version(),
            },
            "os": {"name": _platform.system(), "version": _platform.release()},
        }
        self._server_name: Optional[str]
        try:
            self._server_name = options.server_name or socket.gethostname()
        except Exception:
            self._server_name = options.server_name
        if self.dsn is not None:
            self._debug("sending to {} (project {})".format(self.dsn.origin, self.dsn.project_id))

    def _reset_state(self) -> None:
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._logs: List[_otlp.LogRecord] = []
        self._spans: List[_otlp.SpanData] = []
        self._metrics: Dict[Tuple[str, str, str], _MetricAgg] = {}
        self._metric_start = time.time_ns()
        self._flag_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}
        self._flags_dead = False
        self._flags_blocked_until = 0.0
        self._flusher: Optional[threading.Thread] = None
        self._wake = threading.Event()
        # Kept across fork(): it holds no threads or sockets, only handlers.
        if getattr(self, "_opener", None) is None:
            self._opener = build_opener()

    def _check_fork(self) -> None:
        if os.getpid() != self._pid:
            self._reset_state()

    def _debug(self, msg: str) -> None:
        debug_log(self.options.debug, msg)

    @property
    def enabled(self) -> bool:
        """Whether a DSN was given and the SDK will send anything."""
        return self.dsn is not None and not self._closed

    def _ensure_flusher(self) -> None:
        self._check_fork()
        t = self._flusher
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._flush_loop, name="argus-flusher", daemon=True)
        try:
            t.start()
            self._flusher = t
        except RuntimeError:
            self._flusher = None  # interpreter shutdown; flush() still sends

    def _flush_loop(self) -> None:
        last_logs = last_spans = last_metrics = time.monotonic()
        while not self._closed:
            self._wake.wait(1.0)
            self._wake.clear()
            if self._closed:
                return
            now = time.monotonic()
            try:
                if now - last_logs >= FLUSH_LOGS_S:
                    last_logs = now
                    self.flush_logs()
                if now - last_spans >= FLUSH_SPANS_S:
                    last_spans = now
                    self.flush_spans()
                if now - last_metrics >= FLUSH_METRICS_S:
                    last_metrics = now
                    self.flush_metrics()
            except Exception as e:  # pragma: no cover
                self._debug("flush loop: {!r}".format(e))

    # -- breadcrumbs -------------------------------------------------------

    def add_breadcrumb(self, crumb: Breadcrumb, hint: Optional[Hint] = None) -> None:
        if not self.enabled:
            return
        c: Optional[Breadcrumb] = dict(crumb)  # type: ignore[assignment]
        assert c is not None
        c.setdefault("timestamp", time.time())
        data = c.get("data")
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            c["data"] = dict(data, url=strip_query(data["url"]))
        if self.options.before_breadcrumb is not None:
            try:
                c = self.options.before_breadcrumb(c, hint or {})
            except Exception as e:
                self._debug("before_breadcrumb raised {!r}; keeping the breadcrumb".format(e))
        if c:
            current_scope().breadcrumbs.append(c)  # type: ignore[arg-type]

    # -- errors ------------------------------------------------------------

    def capture_exception(self, error: Optional[BaseException] = None, **ctx: Any) -> str:
        if not self.enabled:
            return ""
        if error is None:
            exc_type, exc_value, tb = sys.exc_info()
            if exc_value is None:
                return ""
            error = exc_value
        if getattr(error, _CAPTURED, False):
            return ""
        try:
            setattr(error, _CAPTURED, True)
        except Exception:
            pass
        hint: Hint = {"exc_info": (type(error), error, error.__traceback__)}
        return self._send_event({"exception": {"values": exception_chain(error)}}, ctx, hint)

    def capture_message(self, message: str, level: Level = "info", **ctx: Any) -> str:
        if not self.enabled:
            return ""
        ctx.setdefault("level", level)
        return self._send_event({"message": str(message)[:8192]}, ctx, {})

    def _send_event(self, partial: Event, ctx: Dict[str, Any], hint: Hint) -> str:
        opts = self.options
        if opts.sample_rate < 1 and random.random() >= opts.sample_rate:
            return ""
        scope = current_scope()
        span = current_span()
        tags: Dict[str, str] = {}
        for k, v in list(scope.tags.items()) + list((ctx.get("tags") or {}).items()):
            if len(tags) >= 50 and str(k) not in tags:
                continue
            tags[str(k)[:200]] = str(v)[:200]
        contexts: Dict[str, Any] = dict(self._contexts)
        contexts.update(scope.contexts)
        contexts.update(ctx.get("contexts") or {})
        if span is not None:
            contexts["trace"] = {"trace_id": span.trace_id, "span_id": span.span_id}
        extra = dict(scope.extra)
        extra.update(ctx.get("extra") or {})
        crumbs = list(scope.breadcrumbs)
        if opts.max_breadcrumbs < len(crumbs):
            crumbs = crumbs[len(crumbs) - max(0, opts.max_breadcrumbs) :]
        transaction = ctx.get("transaction") or scope.transaction or (span.name if span else None)
        event: Event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "platform": "python",
            "level": _level(ctx.get("level", "error")),
            "release": opts.release[:200] if opts.release else None,
            "environment": (opts.environment or "production")[:64],
            "server_name": self._server_name[:200] if self._server_name else None,
            "transaction": transaction[:300] if transaction else None,
            "tags": tags,
            "user": ctx.get("user") if ctx.get("user") is not None else scope.user,
            "breadcrumbs": {"values": crumbs},
            "contexts": contexts,
            "extra": extra or None,
            "request": ctx.get("request") if ctx.get("request") is not None else scope.request,
            "fingerprint": list(ctx["fingerprint"]) if ctx.get("fingerprint") else None,
        }
        event.update(partial)
        event = {k: v for k, v in event.items() if v is not None}
        user = event.get("user")
        if isinstance(user, dict) and not opts.send_default_pii:
            event["user"] = {k: v for k, v in user.items() if k != "ip_address"}
        req = event.get("request")
        if isinstance(req, dict):
            event["request"] = _clean_request(req)
        if opts.before_send is not None:
            try:
                out = opts.before_send(event, hint)
            except Exception as e:
                self._debug("before_send raised {!r}; sending the event unchanged".format(e))
                out = event
            if not out:
                return ""
            event = out
        clean = scrub(event)
        if not isinstance(clean, dict):
            return ""
        clean["sdk"] = {"name": SDK_NAME, "version": __version__}
        event_id = str(clean.get("event_id") or uuid.uuid4().hex)
        clean["event_id"] = event_id
        self._send_envelope(clean)
        return event_id

    def _send_envelope(self, event: Event) -> None:
        d = self.dsn
        assert d is not None
        header = {
            "event_id": event["event_id"],
            "sent_at": _rfc3339(time.time()),
            "dsn": d.raw,
            "sdk": event["sdk"],
        }
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        body = "{}\n{}\n{}\n".format(
            json.dumps(header, separators=(",", ":")),
            json.dumps({"type": "event", "content_type": "application/json"}),
            payload,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": "Sentry sentry_version=7, sentry_client={}, sentry_key={}".format(
                USER_AGENT, d.public_key
            ),
            "User-Agent": USER_AGENT,
        }
        if len(body) > 1024:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
        self.transport.send("errors", Request(d.envelope_url(), body, headers))

    # -- logs --------------------------------------------------------------

    def log(
        self, severity: LogSeverity, body: str, attributes: Optional[Mapping[str, Any]] = None
    ) -> None:
        if not self.enabled:
            return
        self._ensure_flusher()
        span = current_span()
        sev = severity if severity in _otlp.SEVERITY else "info"
        rec = _otlp.LogRecord(
            time.time_ns(),
            sev,
            scrub_string(str(body))[:16384],
            scrub(dict(attributes)) if attributes else None,
            span.trace_id if span is not None else None,
            span.span_id if span is not None else None,
        )
        with self._lock:
            self._logs.append(rec)
            full = len(self._logs) >= MAX_LOGS
        if full:
            self.flush_logs()

    # -- metrics -----------------------------------------------------------

    def _metric(
        self,
        kind: str,
        name: str,
        value: float,
        attributes: Optional[Mapping[str, Any]],
        unit: Optional[str],
    ) -> None:
        if not self.enabled:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v):
            return
        self._ensure_flusher()
        attrs = scrub(dict(attributes)) if attributes else None
        key = (kind, str(name), json.dumps(attrs, sort_keys=True, default=str) if attrs else "")
        with self._lock:
            m = self._metrics.get(key)
            if m is None:
                if len(self._metrics) >= MAX_SERIES:
                    return
                m = self._metrics[key] = _MetricAgg(kind, str(name), unit, attrs)
            if kind == "sum":
                m.value += v
            elif kind == "gauge":
                m.value = v
            else:
                m.count += 1
                m.sum += v
                m.min = min(m.min, v)
                m.max = max(m.max, v)
                i = 0
                while i < len(_otlp.HIST_BOUNDS) and v > _otlp.HIST_BOUNDS[i]:
                    i += 1
                m.buckets[i] += 1

    def count(
        self, name: str, value: float = 1, attributes: Optional[Mapping[str, Any]] = None
    ) -> None:
        self._metric("sum", name, value, attributes, None)

    def gauge(
        self,
        name: str,
        value: float,
        attributes: Optional[Mapping[str, Any]] = None,
        unit: Optional[str] = None,
    ) -> None:
        self._metric("gauge", name, value, attributes, unit)

    def distribution(
        self,
        name: str,
        value: float,
        attributes: Optional[Mapping[str, Any]] = None,
        unit: Optional[str] = None,
    ) -> None:
        self._metric("histogram", name, value, attributes, unit)

    # -- traces ------------------------------------------------------------

    def sample_trace(self) -> bool:
        rate = self.options.traces_sample_rate
        return self.enabled and (rate >= 1 or random.random() < rate)

    def start_span(
        self,
        name: str,
        kind: int = SpanKind.INTERNAL,
        attributes: Optional[Mapping[str, Any]] = None,
        parent: Optional[Union[SpanContext, Span]] = None,
    ) -> Span:
        if parent is None:
            parent = current_span()
        return Span(self if self.enabled else None, name, kind, parent, attributes)

    def record_span(self, s: _otlp.SpanData) -> None:
        if not self.enabled:
            return
        self._ensure_flusher()
        attrs = dict(s.attributes or {})
        for k in _URL_KEYS:
            if isinstance(attrs.get(k), str):
                attrs[k] = strip_query(attrs[k])
        s = s._replace(attributes=scrub(attrs), error=scrub_string(s.error) if s.error else None)
        with self._lock:
            self._spans.append(s)
            full = len(self._spans) >= MAX_SPANS
        if full:
            self.flush_spans()

    # -- crons -------------------------------------------------------------

    def check_in(
        self, slug: str, status: Union[str, int] = "ok", output: Optional[str] = None
    ) -> None:
        """Reports a cron run to the Argus monitor with this slug (spec section 3)."""
        if not self.enabled or not slug:
            return
        if status == "start":
            suffix = "/start"
        elif status in ("error", "fail", "failed"):
            suffix = "/fail"
        elif isinstance(status, int) and not isinstance(status, bool):
            suffix = "/{}".format(status)
        else:
            suffix = ""
        body = scrub_string(output)[:2048].encode("utf-8") if output else b""
        self._send_ingest("crons", cron_path(str(slug), suffix), body, "text/plain; charset=utf-8")

    # -- flags -------------------------------------------------------------

    def evaluate_flags(
        self,
        subject: str,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, str]:
        """Every flag's variant for a subject, cached 30 s. {} when Argus can't answer."""
        if not self.enabled or not subject:
            return {}
        self._check_fork()
        attrs = {str(k): str(v) for k, v in (attributes or {}).items()}
        cache_key = "{}|{}".format(subject, json.dumps(attrs, sort_keys=True))
        now = time.monotonic()
        with self._lock:
            hit = self._flag_cache.get(cache_key)
        if hit is not None and now - hit[0] < FLAG_CACHE_S:
            return hit[1]
        stale = hit[1] if hit is not None else {}
        if self._flags_dead or time.time() < self._flags_blocked_until:
            return stale
        d = self.dsn
        assert d is not None
        body = json.dumps(
            {"subject": str(subject), "environment": self.options.environment, "attributes": attrs}
        ).encode("utf-8")
        req = Request(
            d.ingest_url("/v1/flags/evaluate"),
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + d.public_key,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            res = http_post(self._opener, req, self.options.flags_timeout)
        except Exception as e:
            self._debug("flags: {!r}".format(e))
            return stale
        if res.status in (401, 403):
            self._flags_dead = True
            self._debug("flags: {}, the key was refused; stopping until restart".format(res.status))
            return stale
        if res.status == 429:
            self._flags_blocked_until = time.time() + retry_after(res.headers)
            return stale
        if not 200 <= res.status < 300:
            self._debug("flags: {}".format(res.status))
            return stale
        try:
            raw = json.loads(res.body.decode("utf-8")).get("flags") or {}
            flags = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception:
            return stale
        with self._lock:
            if len(self._flag_cache) > 1000:
                self._flag_cache.clear()
            self._flag_cache[cache_key] = (now, flags)
        return flags

    def variant(
        self,
        key: str,
        subject: str,
        default: str,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> str:
        return self.evaluate_flags(subject, attributes).get(key, default)

    def is_on(
        self,
        key: str,
        subject: str,
        default: bool = False,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        v = self.variant(key, subject, "on" if default else "off", attributes)
        return v in ("on", "true")

    # -- sending -----------------------------------------------------------

    def resource(self) -> Dict[str, Any]:
        return {
            "service.name": self.options.service_name or "app",
            "service.version": self.options.release,
            "deployment.environment": self.options.environment,
            "telemetry.sdk.name": SDK_NAME,
            "telemetry.sdk.language": "python",
            "telemetry.sdk.version": __version__,
            "argus.contract": CONTRACT,
        }

    def _send_ingest(self, lane: str, path: str, body: bytes, content_type: str) -> None:
        d = self.dsn
        if d is None:
            return
        self.transport.send(
            lane,
            Request(
                d.ingest_url(path),
                body,
                {
                    "Content-Type": content_type,
                    "Authorization": "Bearer " + d.public_key,
                    "User-Agent": USER_AGENT,
                },
            ),
        )

    def _send_otlp(
        self,
        lane: str,
        path: str,
        build: Callable[[Mapping[str, Any], Sequence[Any]], str],
        items: Sequence[Any],
    ) -> None:
        body = build(self.resource(), items).encode("utf-8")
        if len(body) > MAX_OTLP_BODY and len(items) > 1:
            half = len(items) // 2
            self._send_otlp(lane, path, build, items[:half])
            self._send_otlp(lane, path, build, items[half:])
            return
        self._send_ingest(lane, path, body, "application/json")

    def flush_logs(self) -> None:
        if self.dsn is None:
            return
        with self._lock:
            batch, self._logs = self._logs[:MAX_LOGS], self._logs[MAX_LOGS:]
        if batch:
            self._send_otlp("logs", "/v1/logs", _otlp.logs_request, batch)

    def flush_spans(self) -> None:
        if self.dsn is None:
            return
        with self._lock:
            batch, self._spans = self._spans[:MAX_SPANS], self._spans[MAX_SPANS:]
        if batch:
            self._send_otlp("traces", "/v1/traces", _otlp.traces_request, batch)

    def flush_metrics(self) -> None:
        if self.dsn is None:
            return
        end = time.time_ns()
        with self._lock:
            aggs = list(self._metrics.values())
            self._metrics = {}
            start, self._metric_start = self._metric_start, end
        if not aggs:
            return
        points = []
        for m in aggs:
            if m.kind == "histogram":
                points.append(
                    _otlp.MetricPoint(
                        "histogram",
                        m.name,
                        m.unit,
                        m.attributes,
                        0.0,
                        start,
                        end,
                        m.count,
                        m.sum,
                        m.min,
                        m.max,
                        tuple(m.buckets),
                    )
                )
            else:
                points.append(
                    _otlp.MetricPoint(m.kind, m.name, m.unit, m.attributes, m.value, start, end)
                )
        self._send_otlp("metrics", "/v1/metrics", _otlp.metrics_request, points)

    def flush(self, timeout: float = 2.0) -> bool:
        """Sends everything queued. True when it all went out within `timeout` seconds."""
        try:
            self.flush_logs()
            self.flush_spans()
            self.flush_metrics()
        except Exception as e:
            self._debug("flush: {!r}".format(e))
        return self.transport.flush(timeout)

    def close(self, timeout: float = 2.0) -> bool:
        """Flushes, then stops sending."""
        if self._closed:
            return True
        ok = self.flush(timeout)
        self._closed = True
        self._wake.set()
        self.transport.close(0)
        return ok


def _clean_request(req: Mapping[str, Any]) -> Dict[str, Any]:
    """Request context without a body or cookies, and with the query removed (spec 7)."""
    out: Dict[str, Any] = {}
    for k, v in req.items():
        if k in ("data", "body", "cookies", "query_string"):
            continue
        if k == "url" and isinstance(v, str):
            out[k] = strip_query(v)
        elif k == "headers" and isinstance(v, Mapping):
            out[k] = {
                str(hk): FILTERED if sensitive_header(str(hk)) else hv
                for hk, hv in v.items()
                if str(hk).lower() != "cookie"
            }
        else:
            out[k] = v
    return out


def sensitive_header(name: str) -> bool:
    from ._scrub import sensitive_key

    return sensitive_key(name) or name.lower() in (
        "x-sentry-auth",
        "x-argus-key",
        "proxy-authorization",
    )


def env_options(kwargs: Dict[str, Any]) -> Options:
    """Options from keyword arguments, then ARGUS_*, then SENTRY_* (spec section 1)."""
    known = set(Options.__dataclass_fields__)
    opts = {k: v for k, v in kwargs.items() if k in known}
    unknown = sorted(set(kwargs) - known)
    o = Options(**opts)
    if unknown:
        debug_log(
            o.debug or bool(_env("ARGUS_DEBUG")),
            "unknown options ignored: {}".format(", ".join(unknown)),
        )
    # An explicit dsn="" turns the SDK off; only None (not passed) falls through.
    if kwargs.get("dsn") is None:
        o.dsn = _env("ARGUS_DSN", "SENTRY_DSN")
    if kwargs.get("release") is None:
        o.release = _env("ARGUS_RELEASE", "SENTRY_RELEASE")
    if kwargs.get("environment") is None:
        o.environment = _env("ARGUS_ENVIRONMENT", "SENTRY_ENVIRONMENT") or "production"
    if kwargs.get("service_name") is None:
        o.service_name = _env("ARGUS_SERVICE_NAME", "OTEL_SERVICE_NAME") or "app"
    if kwargs.get("debug") is None:
        o.debug = (_env("ARGUS_DEBUG") or "").lower() in ("1", "true", "yes")
    return o
