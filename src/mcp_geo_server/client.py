"""Async GeoServer HTTP client: Basic auth, retry/backoff, logging, OGC helper.

A single shared client is reused across all tools and the web UI
(``get_client()`` returns a process-wide singleton). The same instance is used
by ``webui/app.py`` so there is exactly one place that talks to GeoServer.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings, get_settings

logger = logging.getLogger("mcp_geo_server")

# HTTP statuses worth retrying (transient gateway/availability problems).
_RETRY_STATUS = {502, 503, 504}


class GeoServerError(RuntimeError):
    """Actionable error raised when GeoServer returns a failure."""


# Human-readable suggestions per HTTP status, appended to error messages.
_HINTS = {
    401: "check GEOSERVER_USER / GEOSERVER_PASSWORD.",
    403: "the user is authenticated but lacks permission for this operation.",
    404: "not found — verify the name (workspace/store/layer) or create it first.",
    405: "method not allowed — the resource may not support this verb (wrong path?).",
    409: "conflict — the resource already exists or is still referenced (try recurse=True).",
    500: "GeoServer internal error — check the server logs.",
}


@dataclass
class OwsResponse:
    """Lightweight view of an OGC response (text + raw bytes)."""

    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]


def _raise_for_status(method: str, url: str, resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    hint = _HINTS.get(resp.status_code, "see response body below.")
    body = resp.text.strip()
    snippet = (body[:500] + "…") if len(body) > 500 else body
    raise GeoServerError(
        f"{method} {url} -> HTTP {resp.status_code}: {hint}\n{snippet}"
    )


def _check_service_exception(url: str, text: str) -> None:
    """OGC services report errors with HTTP 200 + a ServiceExceptionReport body."""
    if not text:
        return
    head = text.lstrip()[:4096]
    if "ServiceExceptionReport" in head or "ServiceException" in head or "<ows:ExceptionReport" in head:
        snippet = (text[:800] + "…") if len(text) > 800 else text
        raise GeoServerError(f"OGC service exception from {url}:\n{snippet.strip()}")


class GeoServerClient:
    """Thin async wrapper over httpx with auth, retry and error translation."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            auth=(self.settings.user, self.settings.password),
            timeout=self.settings.timeout,
            verify=self.settings.verify_tls,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- low level ---------------------------------------------------------
    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = self.settings.retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                logger.debug("%s %s (attempt %d/%d)", method, url, attempt, attempts)
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.warning("%s %s failed: %s", method, url, exc)
                if attempt >= attempts:
                    raise GeoServerError(
                        f"{method} {url} failed after {attempts} attempts: {exc}"
                    ) from exc
            else:
                if resp.status_code in _RETRY_STATUS and attempt < attempts:
                    logger.warning(
                        "%s %s -> HTTP %d, retrying", method, url, resp.status_code
                    )
                else:
                    return resp
            await asyncio.sleep(self.settings.retry_backoff * attempt)
        # Unreachable, but keeps type-checkers happy.
        raise GeoServerError(f"{method} {url} failed: {last_exc}")

    # -- REST --------------------------------------------------------------
    def _rest_url(self, path: str) -> str:
        return f"{self.settings.rest_base}/{path.lstrip('/')}"

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a REST resource and parse the JSON body."""
        url = self._rest_url(path)
        resp = await self._send("GET", url, params=params, headers={"Accept": "application/json"})
        _raise_for_status("GET", url, resp)
        if not resp.content:
            return {}
        return resp.json()

    async def get_text(self, path: str, params: dict[str, Any] | None = None,
                       accept: str | None = None) -> str:
        """GET a REST resource and return the raw text (e.g. an ``.sld`` body)."""
        url = self._rest_url(path)
        headers = {"Accept": accept} if accept else None
        resp = await self._send("GET", url, params=params, headers=headers)
        _raise_for_status("GET", url, resp)
        return resp.text

    async def post(self, path: str, *, json: Any = None, content: str | bytes | None = None,
                   headers: dict[str, str] | None = None,
                   params: dict[str, Any] | None = None) -> str:
        url = self._rest_url(path)
        resp = await self._send("POST", url, json=json, content=content,
                                headers=headers, params=params)
        _raise_for_status("POST", url, resp)
        return resp.text

    async def put(self, path: str, *, json: Any = None, content: str | bytes | None = None,
                  headers: dict[str, str] | None = None,
                  params: dict[str, Any] | None = None) -> str:
        url = self._rest_url(path)
        resp = await self._send("PUT", url, json=json, content=content,
                                headers=headers, params=params)
        _raise_for_status("PUT", url, resp)
        return resp.text

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self._rest_url(path)
        resp = await self._send("DELETE", url, params=params)
        _raise_for_status("DELETE", url, resp)
        return resp.text

    # -- OGC / OWS ---------------------------------------------------------
    def build_ows_url(self, params: dict[str, Any], *, base: str | None = None) -> str:
        """Build a fully-qualified OWS/WMS/WFS URL (no request issued)."""
        root = base or self.settings.ows_base
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        return f"{root}?{urllib.parse.urlencode(clean)}"

    async def ows(self, params: dict[str, Any] | None = None, *, method: str = "GET",
                  content: str | bytes | None = None, headers: dict[str, str] | None = None,
                  base: str | None = None) -> OwsResponse:
        """Issue an OGC request and surface ``ServiceExceptionReport`` as errors."""
        root = base or self.settings.ows_base
        if method.upper() == "GET":
            url = self.build_ows_url(params or {}, base=root)
            resp = await self._send("GET", url)
        else:
            url = root
            resp = await self._send(method, url, content=content, headers=headers,
                                    params=params)
        _raise_for_status(method.upper(), url, resp)
        ctype = resp.headers.get("content-type", "")
        # XML/text payloads may carry an OGC exception with HTTP 200.
        if "xml" in ctype or "text" in ctype or "json" in ctype:
            _check_service_exception(url, resp.text)
        return OwsResponse(
            status_code=resp.status_code,
            text=resp.text,
            content=resp.content,
            headers=dict(resp.headers),
        )


_client: GeoServerClient | None = None


def get_client() -> GeoServerClient:
    """Return the process-wide shared client, creating it on first use."""
    global _client
    if _client is None:
        _client = GeoServerClient()
    return _client


def set_client(client: GeoServerClient | None) -> None:
    """Override the shared client (used by tests to inject a fake)."""
    global _client
    _client = client


def reset_client() -> None:
    set_client(None)
