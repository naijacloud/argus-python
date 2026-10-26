"""A `logging.Handler` that ships records to Argus Logs.

`init(capture_logs=True)` does this for every logger without a handler. Use
the handler instead when you want Argus on some loggers only::

    from naijacloud_argus.integrations.logging import ArgusHandler
    logging.getLogger("payments").addHandler(ArgusHandler(logging.INFO))

A record is shipped once even when both are on.
"""

from __future__ import annotations

import logging

from .._hooks import log_severity, record_attributes
from .._hub import active_client


class ArgusHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "_argus_shipped", False):
            return
        c = active_client()
        if c is None:
            return
        try:
            c.log(log_severity(record.levelno), record.getMessage(), record_attributes(record))  # type: ignore[arg-type]
            record._argus_shipped = True
        except Exception:
            # logging's own handleError prints a traceback to stderr; a
            # monitoring handler failing should be silent instead.
            pass


__all__ = ["ArgusHandler"]
