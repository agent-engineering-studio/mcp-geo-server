"""POST /api/ask wiring: core resolver + CQL guard, with GeoServer and LLM faked."""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_geo_server.catalog import build_catalog
from mcp_geo_server.resolver import Tier

PAI = "ispra:mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai"
FRANE = "ispra:frane_poly_molise_opendata"
CATALOG = build_catalog([
    {"name": PAI.split(":")[1], "workspace": "ispra"},
    {"name": FRANE.split(":")[1], "workspace": "ispra"},
])
FIELDS = {PAI: ["per_fr_ita", "id"], FRANE: ["tipo_movim", "id"]}


class _Result:
    def __init__(self, text):
        self.text = text


class FakeAgent:
    def __init__(self, reply):
        self.reply = json.dumps(reply)

    async def run(self, prompt, options=None):
        return _Result(self.reply)


@pytest.fixture
def ask(monkeypatch):
    from webui import app as webui_app

    async def load_catalog():
        return CATALOG

    async def layer_bbox(ws, name):
        return {"minx": 14.0, "miny": 41.0, "maxx": 15.0, "maxy": 42.0}

    async def layer_info(ws, name):
        return {"geometry": "poligoni", "geometry_column": "geom",
                "fields": FIELDS[f"{ws}:{name}"], "count": 10, "title": name,
                "abstract": ""}

    async def no_admin(query):
        return None

    monkeypatch.setattr(webui_app, "_load_catalog", load_catalog)
    monkeypatch.setattr(webui_app, "layer_bbox", layer_bbox)
    monkeypatch.setattr(webui_app, "_layer_info", layer_info)
    monkeypatch.setattr(webui_app, "_detect_admin", no_admin)
    monkeypatch.setattr(webui_app, "_FILTER_SPEC", None)

    async def call(reply, query="q"):
        monkeypatch.setattr(webui_app, "_RESOLVER_TIERS", [Tier(FakeAgent(reply), "ollama")])
        transport = httpx.ASGITransport(app=webui_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/api/ask", json={"query": query})
    return call


async def test_valid_filter_is_canonicalized_and_applied(ask):
    r = await ask({"layers": [PAI], "cql_filter": "per_fr_ita='Elevata P3'",
                   "explanation": "ok", "confidence": 0.8})
    body = r.json()
    assert r.status_code == 200
    assert body["cql_filter"] == "per_fr_ita = 'Elevata P3'"
    assert body["cql_rejected"] is None and body["cql_applicable"] is True
    assert body["cql_by_layer"] == {PAI: "per_fr_ita = 'Elevata P3'"}
    assert body["confidence"] == 0.8
    assert "raw" not in body


async def test_filter_on_wrong_layer_is_rejected(ask):
    r = await ask({"layers": [FRANE], "cql_filter": "per_fr_ita = 'Elevata P3'",
                   "explanation": "", "confidence": 0.5})
    body = r.json()
    assert body["cql_filter"] is None and body["cql_by_layer"] == {}
    assert "unknown attribute" in body["cql_rejected"]["reason"]


async def test_made_up_value_is_rejected(ask):
    body = (await ask({"layers": [PAI], "cql_filter": "per_fr_ita = 'Altissima'",
                       "explanation": "", "confidence": 0.5})).json()
    assert body["cql_filter"] is None
    assert "not valid" in body["cql_rejected"]["reason"]


async def test_real_column_not_in_style_config_is_allowed(ask):
    body = (await ask({"layers": [FRANE], "cql_filter": "id > 3",
                       "explanation": "", "confidence": 0.5})).json()
    assert body["cql_filter"] == "id > 3"
    assert body["cql_by_layer"] == {FRANE: "id > 3"}


async def test_unparseable_reply_is_502(ask, monkeypatch):
    from webui import app as webui_app

    class Broken:
        async def run(self, prompt, options=None):
            return _Result("no json")

    async def call():
        monkeypatch.setattr(webui_app, "_RESOLVER_TIERS", [Tier(Broken(), "ollama")])
        transport = httpx.ASGITransport(app=webui_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/api/ask", json={"query": "q"})
    assert (await call()).status_code == 502
