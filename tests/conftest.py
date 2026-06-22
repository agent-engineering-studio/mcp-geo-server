"""Shared test fixtures: env setup, a network-free FakeClient, and ToolCapture."""

from __future__ import annotations

import os

import pytest

from mcp_geo_server import client as client_module
from mcp_geo_server import config
from mcp_geo_server.client import OwsResponse
from mcp_geo_server.tools import collect_tools


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Provide required env vars and reset cached settings/client per test."""
    monkeypatch.setenv("GEOSERVER_URL", "http://geoserver.test:8080/geoserver")
    monkeypatch.setenv("GEOSERVER_USER", "admin")
    monkeypatch.setenv("GEOSERVER_PASSWORD", "secret")
    monkeypatch.setenv("GEOSERVER_DEFAULT_WORKSPACE", "demo")
    monkeypatch.setenv("GEOSERVER_DEFAULT_SRS", "EPSG:4326")
    monkeypatch.setenv("GEO_MAP_OUTPUT_DIR", "./_test_maps")
    config.reset_settings()
    client_module.reset_client()
    yield
    config.reset_settings()
    client_module.reset_client()


class FakeClient:
    """Drop-in for GeoServerClient that records calls and returns canned data.

    Tests inspect ``self.calls`` to assert on the request bodies / params / XML
    the tools build — no network is involved.
    """

    def __init__(self):
        self.settings = config.get_settings()
        self.calls: list[dict] = []
        # Canned responses keyed by (verb, path); ``ows_response`` for OGC.
        self.json_responses: dict = {}
        self.text_responses: dict = {}
        self.ows_response = OwsResponse(200, "{}", b"{}", {"content-type": "application/json"})

    def _record(self, **kw):
        self.calls.append(kw)

    async def get_json(self, path, params=None):
        self._record(verb="GET", path=path, params=params)
        return self.json_responses.get(path, {})

    async def get_text(self, path, params=None, accept=None):
        self._record(verb="GET", path=path, params=params, accept=accept)
        return self.text_responses.get(path, "")

    async def post(self, path, *, json=None, content=None, headers=None, params=None):
        self._record(verb="POST", path=path, json=json, content=content,
                     headers=headers, params=params)
        return ""

    async def put(self, path, *, json=None, content=None, headers=None, params=None):
        self._record(verb="PUT", path=path, json=json, content=content,
                     headers=headers, params=params)
        return ""

    async def delete(self, path, params=None):
        self._record(verb="DELETE", path=path, params=params)
        return ""

    def build_ows_url(self, params, *, base=None):
        import urllib.parse
        root = base or self.settings.ows_base
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        return f"{root}?{urllib.parse.urlencode(clean)}"

    async def ows(self, params=None, *, method="GET", content=None, headers=None, base=None):
        self._record(verb="OWS", method=method, params=params, content=content,
                     headers=headers, base=base)
        return self.ows_response

    # helper for tests
    def last(self, verb=None):
        calls = [c for c in self.calls if verb is None or c.get("verb") == verb]
        return calls[-1] if calls else None


@pytest.fixture
def fake_client():
    fake = FakeClient()
    client_module.set_client(fake)
    yield fake
    client_module.reset_client()


@pytest.fixture
def tools(fake_client):
    """Return (tools_by_name, fake_client) for the geo_* tool functions."""
    registry = {fn.__name__: fn for fn in collect_tools()}
    return registry, fake_client
