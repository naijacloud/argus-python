"""FastAPI and Starlette.

::

    import naijacloud_argus as argus
    from naijacloud_argus.integrations.fastapi import instrument

    argus.init()
    app = instrument(FastAPI())

`instrument(app)` is `app.add_middleware(ArgusASGIMiddleware)`. Requests are
traced and named after the route template (`GET /items/{item_id}`); errors
that reach Starlette's 500 handler are reported. `HTTPException`s are answers,
not errors, and are not reported.
"""

from __future__ import annotations

from typing import Any

from .asgi import ArgusASGIMiddleware


def instrument(app: Any) -> Any:
    """Adds the Argus middleware to a FastAPI or Starlette app and returns it."""
    if not getattr(app, "_argus_instrumented", False):
        app.add_middleware(ArgusASGIMiddleware)
        app._argus_instrumented = True
    return app


__all__ = ["ArgusASGIMiddleware", "instrument"]
