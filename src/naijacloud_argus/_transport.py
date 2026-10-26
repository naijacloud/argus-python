"""Sending (spec section 6).

One queue per endpoint ("lane"), each drained by its own daemon thread, so a
slow or rate-limited endpoint never holds up another and nothing ever runs on
the caller's thread. The rules:

- network errors and 5xx back off 1 s, 2 s, 4 s … capped at 60 s, 5 tries;
- 429 waits `Retry-After` (or `X-Sentry-Rate-Limits`) and tries again without
  counting it, and nothing is sent on that lane until then;
- 401 and 403 stop the lane until the process restarts;
- any other 4xx drops the request;
- at most 100 requests and 1 MB per lane; when full, the oldest is dropped.

Nothing here raises into the app. Two process-lifecycle traps are handled:
after `fork()` (gunicorn `--preload`, multiprocessing) the parent's threads
do not exist in the child, so the lanes are rebuilt on the first send there;
and from Python 3.12 no thread can be started at interpreter shutdown, so a
lane without a worker is drained on the flushing thread instead.
"""

from __future__ import annotations

import email.utils
import http.client
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, Mapping, Optional

MAX_ITEMS = 100
MAX_BYTES = 1 << 20
MAX_TRIES = 5


def debug_log(enabled: bool, msg: str) -> None:
    if enabled:
        try:
            sys.stderr.write("[argus] {}\n".format(msg))
        except Exception:
            pass


class Request:
    __slots__ = ("url", "body", "headers")

    def __init__(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self.url = url
        self.body = body
        self.headers = dict(headers)


class Response:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes = b"") -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would replay the key to a host the response chose. Ingest
    # never redirects, so a 3xx is treated as a failed request.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_opener() -> urllib.request.OpenerDirector:
    # Proxies from HTTPS_PROXY / HTTP_PROXY / NO_PROXY only. urllib's default
    # also asks macOS SystemConfiguration, which segfaults in a child process
    # after fork() (gunicorn --preload, multiprocessing): the SDK would crash
    # the app it is watching.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def http_post(opener: urllib.request.OpenerDirector, req: Request, timeout: float) -> Response:
    """One POST. Returns the response for any status; raises only on network errors."""
    r = urllib.request.Request(req.url, data=req.body, headers=req.headers, method="POST")
    try:
        with opener.open(r, timeout=timeout) as res:
            return Response(res.status, dict(res.headers.items()), res.read(1 << 20))
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1 << 16)
        except Exception:
            body = b""
        return Response(e.code, dict(e.headers.items()) if e.headers else {}, body)


def retry_after(headers: Mapping[str, str]) -> float:
    """Seconds to wait after a 429: Retry-After, then X-Sentry-Rate-Limits; 1 s to 1 h."""
    seconds = 0.0
    ra = headers.get("retry-after")
    if ra:
        try:
            seconds = float(ra)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(ra)
                seconds = when.timestamp() - time.time()
            except Exception:
                seconds = 0.0
    if seconds <= 0:
        rl = headers.get("x-sentry-rate-limits")
        if rl:
            try:
                seconds = float(rl.split(",")[0].split(":")[0])
            except ValueError:
                seconds = 0.0
    if seconds <= 0:
        seconds = 60.0
    return min(3600.0, max(1.0, seconds))


class _Lane:
    __slots__ = ("name", "queue", "bytes", "worker", "busy", "blocked_until", "dead", "warned")

    def __init__(self, name: str) -> None:
        self.name = name
        self.queue: Deque[Request] = deque()
        self.bytes = 0
        self.worker: Optional[threading.Thread] = None
        self.busy = False
        self.blocked_until = 0.0
        self.dead = False
        self.warned = False


class Transport:
    #: Seconds; tests shrink these so the retry paths run in milliseconds.
    backoff_base = 1.0
    backoff_cap = 60.0

    def __init__(self, debug: bool = False, timeout: float = 10.0) -> None:
        self.debug = debug
        self.timeout = timeout
        self._opener = build_opener()
        self._reset()

    def _reset(self) -> None:
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._lanes: Dict[str, _Lane] = {}
        self._closed = False

    def _check_fork(self) -> None:
        if os.getpid() != self._pid:
            # The parent's worker threads and lock state did not survive the
            # fork. Its queued requests are the parent's to send.
            self._reset()

    def _log(self, msg: str) -> None:
        debug_log(self.debug, msg)

    # -- the app's side ---------------------------------------------------

    def send(self, lane_name: str, req: Request) -> bool:
        """Queues a request. False when it was dropped. Never raises."""
        try:
            self._check_fork()
            with self._cond:
                if self._closed:
                    return False
                lane = self._lanes.get(lane_name)
                if lane is None:
                    lane = self._lanes[lane_name] = _Lane(lane_name)
                if lane.dead:
                    return False
                lane.queue.append(req)
                lane.bytes += len(req.body)
                # Drop the oldest, but never the request in flight, and always
                # keep the newest so an oversize one still gets its chance.
                start = 1 if lane.busy else 0
                while (len(lane.queue) > MAX_ITEMS or lane.bytes > MAX_BYTES) and len(
                    lane.queue
                ) > start + 1:
                    old = lane.queue[start]
                    del lane.queue[start]
                    lane.bytes -= len(old.body)
                    self._log("{}: queue full, dropped the oldest request".format(lane_name))
                self._ensure_worker(lane)
                self._cond.notify_all()
            return True
        except Exception as e:  # never into the app
            self._log("send failed: {!r}".format(e))
            return False

    def _ensure_worker(self, lane: _Lane) -> None:
        if lane.worker is not None and lane.worker.is_alive():
            return
        t = threading.Thread(target=self._run, args=(lane,), name="argus-" + lane.name, daemon=True)
        try:
            t.start()
            lane.worker = t
        except RuntimeError:
            # Interpreter shutdown (3.12+) refuses new threads. flush() drains
            # this lane on its own thread instead.
            lane.worker = None

    def flush(self, timeout: float = 2.0) -> bool:
        """Waits until every lane is empty. True when all of it went out in time."""
        try:
            self._check_fork()
            deadline = time.monotonic() + max(0.0, timeout)
            with self._cond:
                orphans = [
                    lane
                    for lane in self._lanes.values()
                    if lane.queue and (lane.worker is None or not lane.worker.is_alive())
                ]
            for lane in orphans:
                while time.monotonic() < deadline:
                    if not self._process_one(lane, deadline):
                        break
            with self._cond:
                while True:
                    pending = any(lane.queue or lane.busy for lane in self._lanes.values())
                    if not pending:
                        return True
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cond.wait(min(left, 0.05))
        except Exception as e:
            self._log("flush failed: {!r}".format(e))
            return False

    def close(self, timeout: float = 2.0) -> bool:
        ok = self.flush(timeout)
        with self._cond:
            self._closed = True
            self._stop.set()
            self._cond.notify_all()
        return ok

    # -- the worker's side ------------------------------------------------

    def _run(self, lane: _Lane) -> None:
        while True:
            with self._cond:
                while not lane.queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
            try:
                sent = self._process_one(lane, None)
            except Exception as e:  # pragma: no cover - belt and braces
                self._log("{}: worker error {!r}".format(lane.name, e))
                sent = False
            if lane.dead:
                return
            if not sent:
                # Another thread (a flush draining inline) holds the head.
                with self._cond:
                    self._cond.wait(0.05)

    def _process_one(self, lane: _Lane, deadline: Optional[float]) -> bool:
        """Sends the request at the head of the lane. False when nothing was sent."""
        with self._cond:
            if not lane.queue or lane.dead or lane.busy:
                return False
            req = lane.queue[0]
            lane.busy = True
        try:
            self._attempt(lane, req, deadline)
        finally:
            with self._cond:
                if lane.queue and lane.queue[0] is req:
                    lane.queue.popleft()
                    lane.bytes -= len(req.body)
                lane.busy = False
                self._cond.notify_all()
        return True

    def _wait(self, seconds: float, deadline: Optional[float]) -> bool:
        """Sleeps; False when the transport is closing or the inline deadline passed."""
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= seconds:
                return False
        return not self._stop.wait(seconds)

    def _attempt(self, lane: _Lane, req: Request, deadline: Optional[float]) -> None:
        tries = 0
        while tries < MAX_TRIES:
            wait = lane.blocked_until - time.time()
            if wait > 0 and not self._wait(wait, deadline):
                return
            try:
                res = http_post(self._opener, req, self.timeout)
            except (OSError, http.client.HTTPException) as e:
                tries += 1
                self._log("{}: network error ({!r}), try {}".format(lane.name, e, tries))
                if tries >= MAX_TRIES or not self._wait(self._backoff(tries), deadline):
                    return
                continue
            except Exception as e:
                # Not the network: a request that can never succeed. Drop it.
                self._log("{}: dropped, {!r}".format(lane.name, e))
                return
            if 200 <= res.status < 300:
                if res.headers.get("x-argus-dropped-series") and not lane.warned:
                    lane.warned = True
                    self._log(
                        "{}: workspace hit its series limit; new series dropped".format(lane.name)
                    )
                return
            if res.status == 429:
                secs = retry_after(res.headers)
                lane.blocked_until = time.time() + secs
                self._log("{}: 429, waiting {:.0f} s".format(lane.name, secs))
                continue  # not counted as a try
            if res.status in (401, 403):
                self._log(
                    "{}: {}, the key was refused; stopping this endpoint until restart".format(
                        lane.name, res.status
                    )
                )
                with self._cond:
                    lane.dead = True
                    lane.queue.clear()
                    lane.queue.append(req)  # popped by _process_one
                    lane.bytes = len(req.body)
                return
            if 300 <= res.status < 500:
                self._log("{}: {}, dropped".format(lane.name, res.status))
                return
            tries += 1
            self._log("{}: {}, try {}".format(lane.name, res.status, tries))
            if tries >= MAX_TRIES or not self._wait(self._backoff(tries), deadline):
                return

    def _backoff(self, tries: int) -> float:
        return float(min(self.backoff_cap, self.backoff_base * (2 ** (tries - 1))))

    # -- introspection (tests, debug) --------------------------------------

    def lane_state(self, name: str) -> Dict[str, Any]:
        with self._cond:
            lane = self._lanes.get(name)
            if lane is None:
                return {}
            return {
                "queued": len(lane.queue),
                "dead": lane.dead,
                "blocked_until": lane.blocked_until,
            }
