"""Public shapes, as TypedDicts so plain dicts type-check."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Callable, Dict, Literal, Optional, Tuple, Type, TypedDict, Union

Level = Literal["fatal", "error", "warning", "info", "debug"]
LogSeverity = Literal["trace", "debug", "info", "warn", "error", "fatal"]
CheckInStatus = Union[Literal["start", "ok", "error"], int]

ExcInfo = Tuple[Type[BaseException], BaseException, Optional[TracebackType]]


class User(TypedDict, total=False):
    id: str
    email: str
    username: str
    #: Dropped unless `send_default_pii=True`.
    ip_address: str


class Breadcrumb(TypedDict, total=False):
    timestamp: float
    type: str
    category: str
    message: str
    level: str
    data: Dict[str, Any]


class Hint(TypedDict, total=False):
    #: The exception being reported, for `capture_exception` and the hooks.
    exc_info: ExcInfo
    #: The `logging.LogRecord`, for breadcrumbs made from logging.
    log_record: Any


#: An event as sent to Argus (spec section 4). A plain dict.
Event = Dict[str, Any]

BeforeSend = Callable[[Event, Hint], Optional[Event]]
BeforeBreadcrumb = Callable[[Breadcrumb, Hint], Optional[Breadcrumb]]

__all__ = [
    "BeforeBreadcrumb",
    "BeforeSend",
    "Breadcrumb",
    "CheckInStatus",
    "Event",
    "ExcInfo",
    "Hint",
    "Level",
    "LogSeverity",
    "User",
]
