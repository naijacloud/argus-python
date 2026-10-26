"""Privacy defaults (spec section 7).

Argus scrubs again on arrival; the SDK does not rely on that. Everything that
leaves the process goes through `scrub()` (structures) or `scrub_string()`
(free text), which also turns arbitrary Python objects into JSON-safe values,
so a `datetime` or a model instance in `extra` can never make `json.dumps`
raise inside the SDK.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Match, Optional, Set

FILTERED = "[Filtered]"

_CONTAINS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "authorization",
    "credential",
    "cookie",
    "privatekey",
    "cardnumber",
    "creditcard",
    "bvn",
    "cvv",
    "cvc",
    "sessionid",
    "sessiontoken",
    "signature",
)
_EXACT = frozenset(("auth", "pwd", "pin", "otp", "session", "card", "sid"))

_NORMALISE = re.compile(r"[-_ ]")


def sensitive_key(key: str) -> bool:
    """Whether a field name's value should be filtered."""
    n = _NORMALISE.sub("", str(key).lower())
    if n in _EXACT:
        return True
    return any(c in n for c in _CONTAINS)


_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_BEARER = re.compile(r"\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
# The value of a sensitive pair, unless it is already filtered or is a
# `Bearer …` credential (the pattern above has filtered its secret part).
_VALUE = r"(?!\[Filtered\])(?!(?:bearer|basic|token)\s)([^\"'\s,;&})<>\[\]]+)"
# `key=value` and `key: value` where the key contains a sensitive word. The
# word list is the spec's `contains` list, spelled to allow `_`/`-` inside it.
_KV = re.compile(
    r"\b([a-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization"
    r"|credential|cookie|private[_-]?key|card[_-]?number|credit[_-]?card|bvn|cvv|cvc"
    r"|session[_-]?id|session[_-]?token|signature)[a-z0-9_-]*)([\"']?\s*[:=]\s*)([\"']?)" + _VALUE,
    re.IGNORECASE,
)
# The spec's `equals` keys. Only `=` counts: "pin: 1234" is rare, while
# "session: started" or "card: declined" in a log line are not secrets.
_KV_EXACT = re.compile(
    r"(?<![A-Za-z0-9_-])(auth|pwd|pin|otp|session|card|sid)([\"']?\s*=\s*)([\"']?)" + _VALUE,
    re.IGNORECASE,
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card(m: Match[str]) -> str:
    digits = re.sub(r"[ -]", "", m.group(0))
    return FILTERED if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def _kv(m: Match[str]) -> str:
    return "{}{}{}{}".format(m.group(1), m.group(2), m.group(3), FILTERED)


def scrub_string(s: str) -> str:
    """Filters card numbers, credentials, JWTs and sensitive key=value pairs in text."""
    if not s:
        return s
    s = _CARD.sub(_card, s)
    s = _BEARER.sub(lambda m: "{} {}".format(m.group(1), FILTERED), s)
    s = _JWT.sub(FILTERED, s)
    s = _KV.sub(_kv, s)
    return _KV_EXACT.sub(_kv, s)


MAX_DEPTH = 8
MAX_STRING = 16384
MAX_ITEMS = 500


def _repr(value: Any) -> str:
    try:
        text = repr(value)
    except Exception:  # a broken __repr__ must not break capture
        text = "<{} object>".format(type(value).__name__)
    return text[:1000]


def scrub(value: Any, depth: int = 0, _seen: Optional[Set[int]] = None) -> Any:
    """Deep-scrubs a value and returns a JSON-safe copy.

    Mappings become dicts with string keys, sequences and sets become lists,
    non-finite floats become strings, bytes and other objects become their
    repr. Cycles and anything deeper than 8 levels are cut.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return scrub_string(value[:MAX_STRING])
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    seen = _seen if _seen is not None else set()
    if depth > MAX_DEPTH or id(value) in seen:
        return "[…]"
    if isinstance(value, dict):
        seen.add(id(value))
        out: Dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                break
            key = k if isinstance(k, str) else _repr(k)
            if sensitive_key(key) and v is not None and v != "":
                out[key] = FILTERED
            else:
                out[key] = scrub(v, depth + 1, seen)
        seen.discard(id(value))
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(id(value))
        items: List[Any] = []
        for i, v in enumerate(value):
            if i >= MAX_ITEMS:
                break
            items.append(scrub(v, depth + 1, seen))
        seen.discard(id(value))
        return items
    if isinstance(value, (bytes, bytearray)):
        return scrub_string(_repr(bytes(value[:1000])))
    return scrub_string(_repr(value))


def strip_query(url: str) -> str:
    """Removes the query string and fragment from a URL (spec section 7)."""
    for i, ch in enumerate(url):
        if ch in "?#":
            return url[:i]
    return url
