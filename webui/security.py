"""Access control for the web UI: optional HTTP Basic auth + a rate limiter.

Both are opt-in through the environment so the local, single-user stack keeps
working unchanged:

* ``WEBUI_USER`` + ``WEBUI_PASSWORD`` — when BOTH are set, every route (pages,
  static files, ``/api/*``, the ``/wms`` proxy — all but ``/healthz``)
  requires HTTP Basic auth. The
  browser shows its native login prompt and then resends the credentials on
  every request, including the WMS tiles, so the UI needs no changes.
* ``WEBUI_RATE_LIMIT`` — max requests per minute, per client IP, on the routes
  that spend LLM tokens or heavy compute (``/api/ask``, ``/api/enrich``,
  ``/api/upload``, ``/api/style``). Default ``30``; ``0`` disables it.

Pure Starlette, no I/O: unit-testable without GeoServer or an LLM.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Liveness probe — must answer without credentials (container healthcheck).
AUTH_EXEMPT_PATHS = frozenset({"/healthz"})

# Routes that cost LLM tokens or heavy compute — the ones worth throttling.
RATE_LIMITED_PATHS = frozenset({"/api/ask", "/api/enrich", "/api/upload", "/api/style",
                                "/api/report"})

_REALM = 'Basic realm="mcp-geo-server", charset="UTF-8"'


def _credentials_from_env() -> tuple[str, str] | None:
    user = (os.environ.get("WEBUI_USER") or "").strip()
    password = os.environ.get("WEBUI_PASSWORD") or ""
    if user and password:
        return user, password
    return None


def _parse_basic(header: str) -> tuple[str, str] | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


class BasicAuthMiddleware:
    """Require HTTP Basic auth on every HTTP request when credentials are set.

    ``credentials=None`` reads ``WEBUI_USER``/``WEBUI_PASSWORD``; if either is
    missing the middleware is a pass-through.
    """

    def __init__(self, app: ASGIApp, credentials: tuple[str, str] | None = None,
                 exempt: frozenset[str] = AUTH_EXEMPT_PATHS):
        self.app = app
        self.credentials = credentials if credentials is not None else _credentials_from_env()
        self.exempt = exempt

    @property
    def enabled(self) -> bool:
        return self.credentials is not None

    def _authorized(self, header: str) -> bool:
        given = _parse_basic(header)
        if given is None or self.credentials is None:
            return False
        user, password = self.credentials
        # Compare both fields in constant time, and always both (no early exit).
        ok_user = secrets.compare_digest(given[0].encode(), user.encode())
        ok_pass = secrets.compare_digest(given[1].encode(), password.encode())
        return ok_user and ok_pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or not self.enabled
                or scope.get("path") in self.exempt):
            await self.app(scope, receive, send)
            return
        header = Request(scope).headers.get("authorization", "")
        if not self._authorized(header):
            response = PlainTextResponse(
                "Authentication required.", status_code=401,
                headers={"WWW-Authenticate": _REALM})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


@dataclass
class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window`` seconds per key.

    In-memory and per process — enough for the single-container web UI. Behind
    several replicas, put the limit in the reverse proxy instead.
    """

    limit: int
    window: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def hit(self, key: str, now: float | None = None) -> float | None:
        """Record a hit. Return ``None`` if allowed, else seconds until retry."""
        if self.limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return max(0.0, self.window - (now - q[0]))
        q.append(now)
        # Drop idle keys so the map can't grow without bound.
        if len(self._hits) > 10_000:
            for k in [k for k, v in self._hits.items() if not v]:
                del self._hits[k]
        return None


def _rate_limit_from_env() -> int:
    raw = (os.environ.get("WEBUI_RATE_LIMIT") or "").strip()
    return int(raw) if raw else 30


class RateLimitMiddleware:
    """Apply a :class:`RateLimiter` to :data:`RATE_LIMITED_PATHS`, keyed by client IP."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None,
                 paths: frozenset[str] = RATE_LIMITED_PATHS):
        self.app = app
        self.limiter = limiter if limiter is not None else RateLimiter(_rate_limit_from_env())
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = client[0] if client else "unknown"
        retry = self.limiter.hit(key)
        if retry is not None:
            response = JSONResponse(
                {"detail": "Too many requests — slow down and retry shortly."},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(retry)))})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
