"""FastAPI: `ARGUS_DSN=… uvicorn examples.fastapi_app:app`."""

from fastapi import FastAPI

import naijacloud_argus as argus
from naijacloud_argus.integrations.fastapi import instrument

argus.init()
app = instrument(FastAPI())


@app.get("/items/{item_id}")
async def item(item_id: int) -> dict:
    if await argus.flags.is_on_async("fast-items", str(item_id)):
        argus.metrics.count("items.fast")
    return {"id": item_id}


@app.get("/boom")
async def boom() -> None:
    raise RuntimeError("reported to Argus, then a 500")
