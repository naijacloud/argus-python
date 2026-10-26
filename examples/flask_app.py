"""Flask: `ARGUS_DSN=… flask --app examples/flask_app run`."""

from flask import Flask

import naijacloud_argus as argus
from naijacloud_argus.integrations.flask import init_app

argus.init()
app = init_app(Flask(__name__))


@app.route("/orders/<int:order_id>")
def order(order_id: int) -> dict:
    argus.log.info("order viewed", order_id=order_id)
    return {"id": order_id}


@app.route("/boom")
def boom() -> str:
    raise RuntimeError("reported to Argus, then Flask's 500 page")
