# Changelog

All notable changes to `naijacloud-argus` are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-26

First release, implementing the Argus SDK contract `argus.sdk/1`.

### Added

- **`init()`**, configured from options, then `ARGUS_*`, then `SENTRY_*`
  environment variables. Without a DSN it does nothing.
  - Uncaught exceptions are reported from `sys.excepthook` and
    `threading.excepthook`; Python's own output and exit are unchanged.
  - `logging` records become breadcrumbs; `capture_logs` also sends them as
    logs, and `ArgusHandler` does it for chosen loggers.
  - Outgoing requests through `http.client` (`urllib`, `requests`, `urllib3`)
    leave breadcrumbs and, inside a span, become child spans carrying
    `traceparent`.
  - Queued data is flushed at exit.
- **Integrations**, each importing its framework only when used:
  - Django middleware (WSGI and ASGI, sync and async views);
  - Flask `init_app()`;
  - FastAPI and Starlette `instrument()`;
  - generic WSGI and ASGI middleware.
  Each traces requests by route template with `http.route`, continues an
  incoming `traceparent`, reports unhandled errors with the request, and gives
  every request its own scope.
- **Errors:** `capture_exception()`, `capture_message()`, `set_user()`,
  `set_tag()`, `set_tags()`, `set_context()`, `set_extra()`,
  `add_breadcrumb()`, `new_scope()`, and `before_send` / `before_breadcrumb`.
  Exception causes and source context are sent; the same exception is sent once.
- **Logs, metrics and traces** over OTLP/HTTP JSON:
  - `log.info()` and the other levels;
  - `metrics.count()`, `metrics.gauge()` and `metrics.distribution()`;
  - `trace()` as a context manager or decorator (sync and async),
    `start_span()`, `get_traceparent()`.
- **Cron check-ins by monitor slug:** `cron.check_in()` and `cron.monitor()`,
  as a context manager or decorator.
- **Feature flags:** `flags.is_on()`, `flags.variant()`, `flags.all()`, and
  their `*_async` forms.
- **Privacy defaults on:** sensitive keys, card numbers, tokens and JWTs are
  filtered, query strings, cookies and request bodies are not sent, and IP
  addresses are not sent.
- **Transport:** a queue and a thread per endpoint, retries with backoff,
  respects `Retry-After` and 429s, stops on a refused key, never raises, and is
  safe across `fork()`.

[Unreleased]: https://github.com/naijacloud/argus-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/naijacloud/argus-python/releases/tag/v0.1.0
