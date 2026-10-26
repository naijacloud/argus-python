"""DSN parsing and endpoint URLs (spec sections 1 to 3)."""

from __future__ import annotations

import re
from typing import NamedTuple, Optional
from urllib.parse import quote, unquote, urlsplit


class Dsn(NamedTuple):
    """A parsed `https://<public_key>@<host>/<project_id>`."""

    public_key: str
    project_id: str
    #: `https://argus-in.naijacloud.com`, plus any path prefix before the project id.
    origin: str
    #: The host name alone, used to keep the SDK from tracing its own requests.
    hostname: str
    raw: str

    def envelope_url(self) -> str:
        return "{}/api/{}/envelope/".format(self.origin, self.project_id)

    def ingest_url(self, path: str) -> str:
        return self.origin + path

    def __repr__(self) -> str:
        # The key is public, but there is no reason for it to sit in tracebacks.
        return "Dsn(project_id={!r}, origin={!r})".format(self.project_id, self.origin)


_PROJECT = re.compile(r"^\d+$")


def parse_dsn(raw: Optional[str]) -> Optional[Dsn]:
    """Parses a DSN, or returns None for anything that is not one. Never raises."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    try:
        u = urlsplit(raw)
        port = u.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if u.scheme not in ("https", "http") or not u.hostname:
        return None
    key = unquote(u.username or "")
    parts = [p for p in u.path.split("/") if p]
    if not key or not parts or not _PROJECT.match(parts[-1]):
        return None
    project = parts.pop()
    prefix = "/" + "/".join(parts) if parts else ""
    host = u.hostname
    if ":" in host:  # IPv6 literal
        host = "[{}]".format(host)
    netloc = host if port is None else "{}:{}".format(host, port)
    return Dsn(key, project, "{}://{}{}".format(u.scheme, netloc, prefix), u.hostname, raw)


def cron_path(slug: str, suffix: str) -> str:
    return "/v1/cron/{}{}".format(quote(slug, safe=""), suffix)
