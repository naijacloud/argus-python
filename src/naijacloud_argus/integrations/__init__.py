"""Optional integrations. Each one imports its framework only when used:

- `naijacloud_argus.integrations.wsgi.ArgusWSGIMiddleware`
- `naijacloud_argus.integrations.asgi.ArgusASGIMiddleware`
- `naijacloud_argus.integrations.flask.init_app`
- `naijacloud_argus.integrations.django.ArgusMiddleware`
- `naijacloud_argus.integrations.fastapi.instrument`
- `naijacloud_argus.integrations.logging.ArgusHandler`
"""
