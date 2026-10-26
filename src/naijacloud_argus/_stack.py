"""Exceptions to Sentry-shaped `exception.values` (spec section 4)."""

from __future__ import annotations

import linecache
import os
import sys
import sysconfig
from types import TracebackType
from typing import Any, Dict, List, Optional, Tuple

MAX_FRAMES = 100
MAX_CHAIN = 5
CONTEXT_LINES = 5
MAX_LINE = 300
MAX_VALUE = 8192


def _library_roots() -> Tuple[str, ...]:
    roots = set()
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            p = sysconfig.get_path(name)
        except Exception:
            p = None
        if p:
            roots.add(os.path.realpath(p))
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            roots.add(os.path.realpath(p))
    return tuple(sorted(roots, key=len, reverse=True))


_LIB_ROOTS = _library_roots()


def _in_app(abs_path: str, module: Optional[str]) -> bool:
    if module and (module == "naijacloud_argus" or module.startswith("naijacloud_argus.")):
        return False
    if not abs_path or abs_path.startswith("<"):
        return False
    if "site-packages" in abs_path or "dist-packages" in abs_path:
        return False
    real = os.path.realpath(abs_path)
    return not any(real.startswith(root + os.sep) for root in _LIB_ROOTS)


def _filename(abs_path: str) -> str:
    """The path relative to the sys.path entry it was imported from."""
    best = ""
    for p in sys.path:
        if p and abs_path.startswith(p.rstrip(os.sep) + os.sep) and len(p) > len(best):
            best = p.rstrip(os.sep)
    return abs_path[len(best) + 1 :] if best else abs_path


def _clip(line: str) -> str:
    line = line.rstrip("\r\n")
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + "…"


def _frame(tb: TracebackType) -> Dict[str, Any]:
    f = tb.tb_frame
    code = f.f_code
    abs_path = (
        os.path.abspath(code.co_filename)
        if not code.co_filename.startswith("<")
        else code.co_filename
    )
    module = f.f_globals.get("__name__") if isinstance(f.f_globals.get("__name__"), str) else None
    lineno = tb.tb_lineno
    out: Dict[str, Any] = {
        "filename": _filename(abs_path),
        "abs_path": abs_path,
        "function": getattr(code, "co_qualname", code.co_name),
        "lineno": lineno,
        "in_app": _in_app(abs_path, module),
    }
    if module:
        out["module"] = module
    # Column offsets exist from 3.11; the frame is still useful without one.
    positions = getattr(code, "co_positions", None)
    if positions is not None and tb.tb_lasti >= 0:
        try:
            pos = list(positions())[tb.tb_lasti // 2]
            if pos[2] is not None:
                out["colno"] = pos[2] + 1
        except Exception:
            pass
    if not abs_path.startswith("<") and lineno:
        try:
            line = linecache.getline(abs_path, lineno, f.f_globals)
            if line:
                out["context_line"] = _clip(line)
                out["pre_context"] = [
                    _clip(linecache.getline(abs_path, n, f.f_globals))
                    for n in range(max(1, lineno - CONTEXT_LINES), lineno)
                ]
                post = []
                for n in range(lineno + 1, lineno + 1 + CONTEXT_LINES):
                    nxt = linecache.getline(abs_path, n, f.f_globals)
                    if not nxt:
                        break
                    post.append(_clip(nxt))
                out["post_context"] = post
        except Exception:
            pass
    return out


def frames(tb: Optional[TracebackType]) -> List[Dict[str, Any]]:
    """Frames oldest first, as Argus wants. Keeps the newest 100."""
    tbs: List[TracebackType] = []
    while tb is not None:
        tbs.append(tb)
        tb = tb.tb_next
    out = []
    for t in tbs[-MAX_FRAMES:]:
        try:
            out.append(_frame(t))
        except Exception:
            continue
    return out


def _value(exc: BaseException) -> str:
    try:
        text = str(exc)
    except Exception:
        text = "<unprintable {}>".format(type(exc).__name__)
    return text[:MAX_VALUE]


def exception_chain(exc: BaseException) -> List[Dict[str, Any]]:
    """The error and its causes, the deepest cause first, the error itself last."""
    chain: List[BaseException] = []
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and len(chain) < MAX_CHAIN and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif not cur.__suppress_context__:
            cur = cur.__context__
        else:
            cur = None
    values = []
    for e in reversed(chain):
        cls = type(e)
        v: Dict[str, Any] = {"type": cls.__name__, "value": _value(e)}
        if cls.__module__ not in ("builtins", "__builtin__"):
            v["module"] = cls.__module__
        fr = frames(e.__traceback__)
        if fr:
            v["stacktrace"] = {"frames": fr}
        values.append(v)
    return values
