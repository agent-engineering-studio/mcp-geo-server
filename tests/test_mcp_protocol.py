"""MCP surface over the real SDK, in memory — no GeoServer, no LLM call."""

from __future__ import annotations

import json

import pytest

mcp = pytest.importorskip("mcp")

from mcp_geo_server import server as srv_mod  # noqa: E402
from mcp_geo_server.catalog import build_catalog  # noqa: E402
from mcp_geo_server.middleware import DESTRUCTIVE_TOOLS  # noqa: E402
from mcp_geo_server.tools import collect_tools  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HIGH_LEVEL = ["geo_search_layers", "geo_describe_layer", "geo_admin_scope",
              "geo_resolve_request", "geo_count_features", "geo_area_report"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GEO_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("GEO_MCP_MODE", raising=False)
    monkeypatch.delenv("GEO_MCP_AGENT_TOOL", raising=False)


async def _tools(server):
    async with mcp.Client(server) as client:
        assert client.protocol_version == "2026-07-28"
        result = await client.list_tools()
        return getattr(result, "tools", result)


# ---- tools mode (default) -------------------------------------------------------------
async def test_tools_mode_lists_typed_tools_in_stable_order():
    names = [t.name for t in await _tools(srv_mod.build_mcp_server())]
    geo = [f.__name__ for f in collect_tools()]
    assert names == HIGH_LEVEL + geo + ["geoserver-agent"]
    assert names == [t.name for t in await _tools(srv_mod.build_mcp_server())]  # deterministic


async def test_annotations_follow_tool_kind():
    tools = {t.name: t for t in await _tools(srv_mod.build_mcp_server())}
    for name in DESTRUCTIVE_TOOLS:
        a = tools[name].annotations
        assert a.destructive_hint is True and a.read_only_hint is False
    assert tools["geo_list_layers"].annotations.read_only_hint is True
    assert tools["geo_create_workspace"].annotations.read_only_hint is False
    assert tools["geo_create_workspace"].annotations.destructive_hint is False
    assert tools["geo_assign_style_to_layer"].annotations.idempotent_hint is True
    for name in HIGH_LEVEL:
        assert tools[name].annotations.read_only_hint is True


async def test_every_tool_has_an_input_schema_and_description():
    for t in await _tools(srv_mod.build_mcp_server()):
        assert t.input_schema and t.input_schema.get("type") == "object", t.name
        assert (t.description or "").strip(), t.name


async def test_destructive_tool_is_refused_by_default(monkeypatch):
    monkeypatch.delenv("GEO_ALLOW_DESTRUCTIVE", raising=False)
    from mcp_geo_server import config
    config.reset_settings()
    async with mcp.Client(srv_mod.build_mcp_server()) as client:
        r = await client.call_tool("geo_delete_layer", {"name": "x"})
        body = json.loads(r.content[0].text)
        assert body["refused"] is True and body["tool"] == "geo_delete_layer"


async def test_agent_tool_can_be_hidden(monkeypatch):
    monkeypatch.setenv("GEO_MCP_AGENT_TOOL", "false")
    names = [t.name for t in await _tools(srv_mod.build_mcp_server())]
    assert "geoserver-agent" not in names


async def test_search_and_resources(monkeypatch):
    from mcp_geo_server import answer

    cat = build_catalog([
        {"name": "frane_poly_toscana_opendata", "workspace": "ispra",
         "title": "Frane della Toscana", "keywords": ["frana", "landslide"]},
        {"name": "dtm", "workspace": "ispra", "title": "Modello digitale del terreno"},
    ])
    assistant = answer.MapAssistant()

    async def load():
        return cat

    async def info(ws, name):
        return {"geometry": "raster", "fields": [], "count": None, "title": name,
                "abstract": ""}

    async def bbox(ws, name):
        return {"minx": 6.0, "miny": 36.0, "maxx": 19.0, "maxy": 47.0}

    monkeypatch.setattr(assistant, "load_catalog", load)
    monkeypatch.setattr(assistant, "layer_info", info)
    monkeypatch.setattr(assistant, "layer_bbox", bbox)
    monkeypatch.setattr(answer, "_ASSISTANT", assistant)

    async with mcp.Client(srv_mod.build_mcp_server()) as client:
        r = await client.call_tool("geo_search_layers", {"query": "frane toscane"})
        res = json.loads(r.content[0].text)
        assert [h["layer"] for h in res["results"]] == ["ispra:frane_poly_toscana_opendata"]
        assert res["total_layers"] == 2

        listed = await client.list_resources()
        uris = [str(x.uri) for x in getattr(listed, "resources", listed)]
        assert "geo://catalog" in uris
        read = await client.read_resource("geo://catalog")
        contents = getattr(read, "contents", read)
        data = json.loads(contents[0].text)
        assert {d["layer"] for d in data} == {"ispra:frane_poly_toscana_opendata", "ispra:dtm"}

        read = await client.read_resource("geo://layer/ispra/dtm")
        layer = json.loads(getattr(read, "contents", read)[0].text)
        assert layer["found"] and layer["geometry"] == "raster" and layer["bbox"]["minx"] == 6.0


async def test_prompts():
    async with mcp.Client(srv_mod.build_mcp_server()) as client:
        listed = await client.list_prompts()
        names = {p.name for p in getattr(listed, "prompts", listed)}
        assert names == {"show-on-map", "area-overview"}
        got = await client.get_prompt("area-overview", {"place": "Bari", "subject": "frane"})
        text = got.messages[0].content.text
        assert "geo_admin_scope('Bari')" in text and "about frane" in text


def test_search_score():
    assert srv_mod.search_score("frane in Toscana", "frane_poly_toscana_opendata") == 2
    assert srv_mod.search_score("landslides", "frane_poly_toscana_opendata") == 0


# ---- agent mode (previous behaviour) -----------------------------------------------------
async def test_agent_mode_is_the_single_tool(monkeypatch):
    monkeypatch.setenv("GEO_MCP_MODE", "agent")
    tools = await _tools(srv_mod.build_mcp_server())
    assert [t.name for t in tools] == ["geoserver-agent"]


def test_bad_mode(monkeypatch):
    monkeypatch.setenv("GEO_MCP_MODE", "nope")
    with pytest.raises(RuntimeError, match="GEO_MCP_MODE"):
        srv_mod.build_mcp_server()


def test_transport_security_from_env(monkeypatch):
    assert srv_mod.transport_security("0.0.0.0") is None
    monkeypatch.setenv("GEO_MCP_ALLOWED_HOSTS", "localhost:9000, mcp:9000")
    ts = srv_mod.transport_security("0.0.0.0")
    assert ts.allowed_hosts == ["localhost:9000", "mcp:9000"]
    assert ts.enable_dns_rebinding_protection
