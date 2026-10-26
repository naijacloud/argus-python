<p align="center">
  <a href="https://www.naijacloud.com/argus">
    <img alt="Naijacloud Argus, Python SDK" src="https://raw.githubusercontent.com/naijacloud/argus-python/main/.github/assets/banner.png" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/naijacloud-argus/"><img alt="pypi" src="https://img.shields.io/badge/pypi-naijacloud--argus-008751?style=flat-square&labelColor=0A0E0C"></a>
  <img alt="python" src="https://img.shields.io/badge/python-%3E%3D_3.9-4B8BBE?style=flat-square&labelColor=0A0E0C">
  <img alt="dependencies" src="https://img.shields.io/badge/dependencies-0-46C98A?style=flat-square&labelColor=0A0E0C">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-8A988F?style=flat-square&labelColor=0A0E0C"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#django">Django</a> ·
  <a href="#flask">Flask</a> ·
  <a href="#fastapi-and-starlette">FastAPI</a> ·
  <a href="#errors">Errors</a> ·
  <a href="#logs-metrics-and-traces">Logs, metrics, traces</a> ·
  <a href="#cron-check-ins">Crons</a> ·
  <a href="#feature-flags">Flags</a> ·
  <a href="#privacy">Privacy</a>
</p>

# naijacloud-argus

The official Python SDK for [Argus](https://www.naijacloud.com/argus),
Naijacloud's monitoring. One package sends errors, logs, metrics, traces, cron
check-ins and feature-flag checks, with one key: your app's **DSN**.

It has no dependencies, supports Python 3.9 and newer, implements the Argus SDK
contract `argus.sdk/1`, and works for apps hosted anywhere, not only on
Naijacloud.

**Already using Sentry's library?** Keep it. Argus accepts it: change the `dsn`
line and you're done. Switch to this SDK when you want logs, metrics, cron
check-ins and flags through the same setup.

## Install

```sh
pip install naijacloud-argus
```

Copy the DSN (your app's key) from the source in Argus. It looks like
`https://<key>@argus-in.naijacloud.com/<number>`.

```python
import naijacloud_argus as argus

argus.init(dsn="https://<key>@argus-in.naijacloud.com/<id>")
```

Call it once, as early as you can: at the top of your entry file, `settings.py`,
`wsgi.py` or `asgi.py`. That's it. You get:

- **Uncaught exceptions,** on the main thread and in other threads. Python
  still prints the traceback and exits the way it would without Argus.
- **Logging records as breadcrumbs** on the next error. Your logging setup is
  untouched: no handler is added, so a later `logging.basicConfig()` still works.
- **Outgoing HTTP** made through `urllib`, `requests` or `urllib3` as
  breadcrumbs, and inside a span, as child spans with a `traceparent` header.
- **A final flush at exit,** so a short script's data is not lost.

`init()` reads `ARGUS_DSN`, `ARGUS_RELEASE` and `ARGUS_ENVIRONMENT`, falling
back to `SENTRY_DSN`, `SENTRY_RELEASE` and `SENTRY_ENVIRONMENT`. Without a DSN
it does nothing. Apps hosted on Naijacloud get these variables set when the
Argus tab is on, so there `argus.init()` with no arguments is enough.

Web frameworks catch your errors themselves, so add their integration too.

## Django

```python
# settings.py
import naijacloud_argus as argus
argus.init()

MIDDLEWARE = [
    "naijacloud_argus.integrations.django.ArgusMiddleware",  # first
    # ...
]
```

Every request is traced and named after its URL pattern
(`GET /orders/<int:pk>/`). View errors are reported before Django turns them
into a 500 page. It works under WSGI and ASGI, with sync and async views.

## Flask

```python
import naijacloud_argus as argus
from naijacloud_argus.integrations.flask import init_app

argus.init()
app = init_app(Flask(__name__))
```

Requests are traced and named after the URL rule. Unhandled view errors are
reported; errors your own `@app.errorhandler` handles are not.

## FastAPI and Starlette

```python
import naijacloud_argus as argus
from naijacloud_argus.integrations.fastapi import instrument

argus.init()
app = instrument(FastAPI())
```

Requests are traced and named after the route (`GET /items/{item_id}`), with
`http.route` set. Unhandled errors are reported; `HTTPException`s are answers,
not errors, and are not.

**Any other WSGI or ASGI app** (Pyramid, Bottle, Falcon, Quart, Litestar):

```python
from naijacloud_argus.integrations.wsgi import ArgusWSGIMiddleware
from naijacloud_argus.integrations.asgi import ArgusASGIMiddleware

app = ArgusWSGIMiddleware(app)   # or ArgusASGIMiddleware(app)
```

In every integration a `traceparent` header from the caller continues its
trace, and each request gets its own scope: `set_user()` inside one request
never leaks into another, on threads or on an event loop. None of them imports
its framework until you use it.

## Errors

```python
import naijacloud_argus as argus

try:
    charge_card(order)
except PaystackError as err:
    argus.capture_exception(err, tags={"provider": "paystack"}, extra={"order_id": order.id})

argus.capture_message("Webhook signature mismatch", "warning")
argus.set_user({"id": user.id})
argus.set_tag("region", "af-west")
argus.add_breadcrumb(category="auth", message="signed in")
```

- **The handled exception:** `capture_exception()` with no argument, inside an
  `except` block, sends the one being handled.
- **Causes:** `raise … from …` and the implicit context are sent too, with
  stack frames and the source lines around each.
- **Duplicates:** the same exception object is sent once, however many
  handlers see it.
- **`before_send(event, hint)`** lets you change an event, or return `None` to
  drop it. `hint["exc_info"]` is the original exception.
- **Your own scope:** `with argus.new_scope():` keeps a user or tags you set
  inside it from leaking out, for background jobs and queue workers.

## Logs, metrics and traces

```python
import naijacloud_argus as argus

argus.log.info("order placed", order_id="ord_81", total=18500)
argus.log.error("paystack timed out", attempt=3)

argus.metrics.count("orders.placed", 1, {"method": "card"})
argus.metrics.gauge("queue.depth", len(jobs))
argus.metrics.distribution("checkout.ms", elapsed, unit="ms")

with argus.trace("charge card", {"provider": "paystack"}) as span:
    span.set_attribute("amount", 18500)
    paystack.charge(order)

@argus.trace("nightly report")      # or bare @argus.trace, named after the function
async def build_report(): ...
```

- **Logs** are sent in batches every 5 seconds. A log written inside a request
  or span carries its trace id. Attribute names that aren't Python identifiers
  go in a dict: `argus.log.info("slow", {"http.route": "/pay"})`.
- **Ship your `logging` records** as Argus logs with `init(capture_logs=True)`
  (INFO and up), or a level: `capture_logs=logging.WARNING`. For some loggers
  only, add `naijacloud_argus.ArgusHandler()` to them instead.
- **Metrics** are added up in your process and sent every 10 seconds.
- **Spans** join the request or span they run in, and are marked failed when
  the block raises (the exception still propagates). `argus.start_span()` gives
  you one to end yourself; pass `parent=` a `traceparent` string to continue a
  trace from a queue message, and `argus.get_traceparent()` to send one on.
- **Sample traces** with `traces_sample_rate` (0–1, default 1).

## Cron check-ins

Create a cron monitor in Argus first, then report runs by its slug:

```python
import naijacloud_argus as argus

@argus.cron.monitor("nightly-invoices")
def send_invoices(): ...

# or as a block:
with argus.cron.monitor("nightly-invoices"):
    send_invoices()

# or by hand:
argus.cron.check_in("nightly-invoices", "start")
argus.cron.check_in("nightly-invoices", "ok")   # or "error", or an exit code
```

`monitor` reports the start, then success or failure, and re-raises your
job's error. A `sys.exit(code)` inside it reports that exit code. Async
functions work too.

## Feature flags

```python
import naijacloud_argus as argus

if argus.flags.is_on("new-checkout", user.id):
    ...
theme = argus.flags.variant("theme", user.id, "light")

# in async code:
if await argus.flags.is_on_async("new-checkout", user.id):
    ...
```

- **Caching:** answers are cached for 30 seconds per user.
- **Fallback:** if Argus can't be reached within `flags_timeout` (2 seconds),
  or the flag doesn't exist, you get the default you passed.
- **Blocking:** `is_on` and `variant` wait for Argus on a cache miss. In async
  code use `is_on_async` and `variant_async`, which don't block the event loop.

## Privacy

On by default:

- The values of keys such as `password`, `token`, `secret`, `authorization`,
  `cookie`, `bvn` and `card` are replaced with `[Filtered]`, at any depth.
- Card numbers, bearer tokens, JWTs and `password=…` pairs are filtered inside
  strings, including exception messages and log lines.
- Query strings are removed from URLs in breadcrumbs, requests and spans.
- Request bodies and cookies are never sent.
- The user's IP address is not sent unless you set `send_default_pii=True`.

Argus filters again on arrival.

## Options

| Option | Default | |
| -- | -- | -- |
| `dsn` | `ARGUS_DSN`, then `SENTRY_DSN` | Without one, nothing is sent. `dsn=""` turns the SDK off |
| `release` | `ARGUS_RELEASE`, then `SENTRY_RELEASE` | Your app's version, like `1.4.2` or a git commit |
| `environment` | `ARGUS_ENVIRONMENT`, then `SENTRY_ENVIRONMENT`, else `production` | |
| `service_name` | `ARGUS_SERVICE_NAME`, then `OTEL_SERVICE_NAME`, else `app` | Shown on logs, metrics and traces |
| `server_name` | the host name | |
| `sample_rate` | `1` | Share of errors sent |
| `traces_sample_rate` | `1` | Share of traces sent |
| `send_default_pii` | `False` | Send the user's IP address |
| `max_breadcrumbs` | `100` | |
| `before_send` / `before_breadcrumb` | | `(event, hint)`: change or drop an event or crumb |
| `capture_logs` | `False` | Also send `logging` records as logs: `True` (INFO+) or a level |
| `breadcrumb_level` | `logging.INFO` | `logging` records from this level become breadcrumbs |
| `default_integrations` | `True` | `False` installs no hooks (excepthooks, logging, http.client, atexit) |
| `flags_timeout` | `2.0` | Seconds a flag evaluation may wait |
| `shutdown_timeout` | `2.0` | Seconds the exit flush may take |
| `debug` | `False` | Print what the SDK does to stderr (or `ARGUS_DEBUG=1`) |
| `enabled` | `True` | Turn the SDK off without removing it |

`argus.flush(timeout=2.0)` sends what's queued and returns whether it all went
out; `argus.close()` flushes and stops. Long-running servers need neither.

## How it sends

Nothing is sent on your thread. Each endpoint has its own queue (at most 100
requests and 1 MB) and its own background thread, using only the standard
library's `urllib`. Network errors and 5xx are retried with backoff, a 429
waits for `Retry-After`, and a refused key stops that endpoint until restart.
SDK code never raises into yours.

It is safe under `fork()` (gunicorn `--preload`, multiprocessing): a child
process rebuilds its queues on first use. Proxies come from `HTTPS_PROXY`,
`HTTP_PROXY` and `NO_PROXY`; system proxy settings are not read, because on
macOS that lookup crashes a forked child.

## Not in this version

These are planned, and not done yet:

- automatic spans for database clients (`psycopg`, SQLAlchemy, the Django ORM),
  `httpx` and `aiohttp` (only `http.client`-based requests are traced);
- Celery and RQ task tracing (use `cron.monitor` or `trace` in the task);
- reporting `logging.error()` calls as error events (they are breadcrumbs, and
  with `capture_logs`, logs);
- local variables in stack frames.

## License

MIT
