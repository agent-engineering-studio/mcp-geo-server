"""AI-written layer metadata: profiling, the overwrite rule, validation, writing."""

from __future__ import annotations

import json

import pytest

from mcp_geo_server import metadata
from mcp_geo_server.catalog import build_catalog, catalog_prompt
from mcp_geo_server.client import GeoServerError, OwsResponse

FT = {"featureType": {
    "name": "frane_poly_molise_opendata",
    "nativeName": "frane_poly_molise_opendata",
    "title": "frane_poly_molise_opendata",
    "keywords": {"string": ["features", "frane_poly_molise_opendata"]},
    "srs": "EPSG:4326",
    "latLonBoundingBox": {"minx": 13.9, "miny": 41.3, "maxx": 15.2, "maxy": 42.1},
    "attributes": {"attribute": [
        {"name": "geom", "binding": "org.locationtech.jts.geom.MultiPolygon"},
        {"name": "id_frana", "binding": "java.lang.String"},
        {"name": "tipo_movim", "binding": "java.lang.Integer"},
        {"name": "nome_tipo", "binding": "java.lang.String"},
    ]},
}}
FEATURES = [{"properties": {"id_frana": f"070{i:04d}", "tipo_movim": (1 if i % 3 else 4),
                            "nome_tipo": ("Crollo" if i % 3 else "Colamento lento")}}
            for i in range(30)]
REPLY = {
    "title": "Frane poligonali del Molise (IFFI)",
    "abstract": "Poligoni delle frane censite nel Molise, classificate per tipo di movimento.",
    "keywords": ["Frana", "landslide", "frana", "molise", "  dissesto  ", "metadata:ai"],
    "columns": [{"name": "tipo_movim", "description": "codice del tipo di movimento"},
                {"name": "made_up", "description": "x"}],
    "theme_attribute": "tipo_movim",
}


class FakeClient:
    def __init__(self, ft=FT, features=FEATURES):
        from mcp_geo_server import config
        self.settings = config.get_settings()
        self.ft = ft
        self.features = features
        self.puts = []
        self.ows_params = []

    async def get_json(self, path, params=None):
        if path.endswith("/featuretypes/frane_poly_molise_opendata.json") and self.ft:
            return json.loads(json.dumps(self.ft))
        raise GeoServerError("404 not found")

    async def ows(self, params, base=None):
        self.ows_params.append(params)
        return OwsResponse(200, json.dumps({"features": self.features}), b"", {})

    async def put(self, path, json=None, **kw):
        self.puts.append((path, json))
        return ""


class _Result:
    def __init__(self, text):
        self.text = text


class FakeAgent:
    def __init__(self, reply=REPLY, fail_structured=False):
        self.reply = reply
        self.fail_structured = fail_structured
        self.calls = []

    async def run(self, prompt, options=None):
        self.calls.append(options)
        if options and self.fail_structured:
            raise RuntimeError("no json_schema")
        return _Result(json.dumps(self.reply))


# ---- profiling ------------------------------------------------------------------------
async def test_profile():
    client = FakeClient()
    p = await metadata.layer_profile("ispra", "frane_poly_molise_opendata", client=client)
    assert p["geometry"] == "MultiPolygon"
    assert [c["name"] for c in p["columns"]] == ["id_frana", "tipo_movim", "nome_tipo"]
    tipo = p["columns"][1]
    assert tipo["samples"] == [1, 4] and tipo["distinct_in_sample"] == 2
    assert p["sampled_features"] == 30
    assert client.ows_params[0]["propertyName"] == "id_frana,tipo_movim,nome_tipo"
    assert len(p["columns"][0]["samples"]) == metadata.SAMPLE_VALUES


async def test_profile_of_a_raster_is_refused():
    with pytest.raises(ValueError, match="not a vector"):
        await metadata.layer_profile("ispra", "dtm", client=FakeClient())


def test_samples_truncate_long_strings():
    s = metadata.summarize_samples([{"properties": {"t": "x" * 100}}, {"properties": {}}], ["t"])
    assert s["t"]["samples"][0].endswith("…") and len(s["t"]["samples"][0]) == 41
    assert s["t"]["empty_in_sample"] == 1


# ---- overwrite rule -----------------------------------------------------------------------
def _profile(**kw):
    base = {"name": "frane_poly_emilia_romagna_opendata",
            "native_name": "frane_poly_emilia_romagna_opendata",
            "title": "frane_poly_emilia-romagna_opendata", "abstract": "",
            "keywords": ["features", "frane_poly_emilia_romagna_opendata"], "columns": []}
    return {**base, **kw}


def test_bootstrap_style_metadata_is_automatic():
    assert metadata.is_automatic(_profile())          # folder-name title, dashes
    assert metadata.is_automatic(_profile(title="", keywords=[]))


def test_hand_written_metadata_is_kept():
    assert not metadata.is_automatic(_profile(title="Frane dell'Emilia-Romagna"))
    assert not metadata.is_automatic(_profile(abstract="Scritto a mano."))
    assert not metadata.is_automatic(_profile(keywords=["features", "dissesto"]))


def test_previously_generated_metadata_is_regenerable():
    assert metadata.is_automatic(_profile(title="Frane", abstract="AI",
                                          keywords=["frana", metadata.AI_KEYWORD]))


# ---- validation ------------------------------------------------------------------------------
async def test_clean_metadata_against_real_columns():
    p = await metadata.layer_profile("ispra", "frane_poly_molise_opendata", client=FakeClient())
    m = metadata.clean_metadata(REPLY, p)
    assert m["keywords"] == ["frana", "landslide", "molise", "dissesto"]
    assert [c["name"] for c in m["columns"]] == ["tipo_movim"]     # made_up dropped
    assert m["theme_attribute"] == "tipo_movim"
    bad = metadata.clean_metadata({**REPLY, "theme_attribute": "nope"}, p)
    assert bad["theme_attribute"] is None
    with pytest.raises(ValueError, match="title"):
        metadata.clean_metadata({**REPLY, "title": ""}, p)


async def test_schema_enumerates_real_columns():
    p = await metadata.layer_profile("ispra", "frane_poly_molise_opendata", client=FakeClient())
    s = metadata.metadata_schema(p)
    col = s["properties"]["columns"]["items"]["properties"]["name"]
    assert col["enum"] == ["id_frana", "tipo_movim", "nome_tipo"]
    assert s["additionalProperties"] is False


# ---- end to end ---------------------------------------------------------------------------------
async def test_enrich_layer_writes_tagged_metadata():
    client, agent = FakeClient(), FakeAgent()
    rep = await metadata.enrich_layer("ispra", "frane_poly_molise_opendata",
                                      agent=agent, provider="ollama", client=client)
    assert rep["status"] == "written"
    path, body = client.puts[0]
    assert path == "workspaces/ispra/featuretypes/frane_poly_molise_opendata.json"
    ft = body["featureType"]
    assert ft["title"] == "Frane poligonali del Molise (IFFI)"
    assert ft["keywords"]["string"][-1] == metadata.AI_KEYWORD
    assert "Campi: tipo_movim: codice del tipo di movimento." in ft["abstract"]
    assert agent.calls[0]["response_format"]["required"]


async def test_enrich_layer_dry_run_and_skip():
    client = FakeClient()
    rep = await metadata.enrich_layer("ispra", "frane_poly_molise_opendata",
                                      agent=FakeAgent(), provider="ollama",
                                      client=client, dry_run=True)
    assert rep["status"] == "dry-run" and client.puts == []

    manual = json.loads(json.dumps(FT))
    manual["featureType"]["title"] = "Inventario frane Molise"
    client = FakeClient(ft=manual)
    rep = await metadata.enrich_layer("ispra", "frane_poly_molise_opendata",
                                      agent=FakeAgent(), provider="ollama", client=client)
    assert rep["status"] == "skipped" and client.puts == []
    rep = await metadata.enrich_layer("ispra", "frane_poly_molise_opendata",
                                      agent=FakeAgent(), provider="ollama", client=client,
                                      overwrite=True)
    assert rep["status"] == "written"


async def test_fallback_without_schema():
    agent = FakeAgent(fail_structured=True)
    rep = await metadata.enrich_layer("ispra", "frane_poly_molise_opendata", agent=agent,
                                      provider="openai", client=FakeClient(), dry_run=True)
    assert rep["status"] == "dry-run" and agent.calls == [agent.calls[0], None]


async def test_batch_gives_up_after_repeated_failures():
    class Down:
        async def run(self, prompt, options=None):
            raise ConnectionError("LLM down")

    batch = metadata.BatchEnricher(agent=Down(), provider="ollama", client=FakeClient(),
                                   max_failures=2)
    results = [await batch("ispra", "frane_poly_molise_opendata") for _ in range(4)]
    assert [r["status"] if r else None for r in results] == ["failed", "failed", None, None]
    assert batch.disabled


# ---- the resolver sees the generated metadata --------------------------------------------------
def test_catalog_prompt_uses_abstract_and_drops_noise_keywords():
    cat = build_catalog([{
        "name": "frane_poly_molise_opendata", "workspace": "ispra",
        "title": "Frane poligonali del Molise",
        "abstract": "Poligoni delle frane del Molise. " * 20,
        "keywords": ["features", "frane_poly_molise_opendata", "frana", metadata.AI_KEYWORD]}])
    line = catalog_prompt(cat).splitlines()[1]
    assert "kw: frana |" in line and "features" not in line and "metadata:ai" not in line
    assert "Poligoni delle frane del Molise." in line and line.endswith("…")
