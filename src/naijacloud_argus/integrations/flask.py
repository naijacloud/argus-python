"""Flask.

::

    import naijacloud_argus as argus
    from naijacloud_argus.integrations.flask import init_app

    argus.init()
    app = Flask(__name__)
    init_app(app)

Requests are traced and named after the URL rule (`GET /orders/<int:id>`).
Flask turns an unhandled view error into a 500 page itself, so the error never
reaches WSGI middleware; it is reported from Flask's `got_request_exception`
signal instead (or from `teardown_request` where signals are unavailable).
Errors your own `@app.errorhandler` handles are not reported.

Flask is imported only when `init_app` is called.
"""

from __future__ import annotations

from typing import Any, Optional

from .._hub import active_client
from ._http import set_route
from .wsgi import ENVIRON_SCOPE, ENVIRON_SPAN, ArgusWSGIMiddleware


def _on_exception(sender: Any, exception: Optional[BaseException] = None, **extra: Any) -> None:
    c = active_client()
    if c is None or exception is None:
        return
    try:
        c.capture_exception(exception, tags={"mechanism": "flask"})
    except Exception:
        pass


def init_app(app: Any) -> Any:
    """Instruments a Flask app in place and returns it."""
    if getattr(app, "_argus_instrumented", False):
        return app
    app._argus_instrumented = True
    app.wsgi_app = ArgusWSGIMiddleware(app.wsgi_app)

    from flask import request

    def _argus_route() -> None:
        try:
            rule = request.url_rule
            env = request.environ
            scope = env.get(ENVIRON_SCOPE)
            if rule is not None and scope is not None:
                set_route(env.get(ENVIRON_SPAN), scope, request.method, rule.rule)
        except Exception:
            pass

    app.before_request(_argus_route)

    try:
        from flask.signals import got_request_exception

        got_request_exception.connect(_on_exception, app, weak=False)
    except Exception:

        def _argus_teardown(exc: Optional[BaseException]) -> None:
            if exc is not None:
                _on_exception(app, exc)

        app.teardown_request(_argus_teardown)

    return app


class Argus:
    """Extension-style spelling: ``Argus(app)`` or ``Argus().init_app(app)``."""

    def __init__(self, app: Any = None) -> None:
        if app is not None:
            init_app(app)

    def init_app(self, app: Any) -> Any:
        return init_app(app)
