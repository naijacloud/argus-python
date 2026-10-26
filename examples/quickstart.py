"""Errors, logs, a metric, a span and a cron check-in from a script.

    ARGUS_DSN=https://<key>@argus-in.naijacloud.com/<id> python examples/quickstart.py
"""

import time

import naijacloud_argus as argus

argus.init(release="quickstart@1.0.0")  # reads ARGUS_DSN

argus.set_user({"id": "user-42"})
argus.set_tag("region", "af-west")


@argus.cron.monitor("nightly-invoices")  # create this monitor in Argus first
def send_invoices() -> None:
    with argus.trace("render invoices", {"count": 3}):
        time.sleep(0.05)
        argus.log.info("invoices rendered", count=3)
    argus.metrics.count("invoices.sent", 3)
    argus.metrics.distribution("invoices.render_ms", 50, unit="ms")


send_invoices()

try:
    {}["missing"]
except KeyError as err:
    print("event", argus.capture_exception(err, extra={"order_id": "ord_81"}))

if argus.flags.is_on("new-checkout", "user-42"):
    print("new checkout is on for user-42")

# Short scripts flush at exit on their own; this just makes it explicit.
argus.flush()
