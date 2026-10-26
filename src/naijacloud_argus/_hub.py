"""The one client `init()` made. Hooks look it up on every call, so a re-init
(or `close()`) takes effect everywhere without re-installing anything."""

from __future__ import annotations

from typing import Optional

from ._client import Client

_client: Optional[Client] = None


def get_client() -> Optional[Client]:
    """The client `init()` made, if any."""
    return _client


def set_client(client: Optional[Client]) -> None:
    global _client
    _client = client


def active_client() -> Optional[Client]:
    """The client, when it will send something."""
    c = _client
    return c if c is not None and c.enabled else None
