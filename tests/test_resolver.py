"""Resolver core: schema, provider formats, structured call + fallback, CQL guard."""

from __future__ import annotations

import json

import pytest

from mcp_geo_server import resolver
from mcp_geo_server.catalog import build_catalog
from mcp_geo_server.styling import load_config

CATALOG = build_catalog([
    {"name": "frane_poly_molise_opendata", "workspace": "ispra"},
    {"name": "mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai", "workspace": "ispra"},
    {"name": "dtm", "workspace": "ispra", "title": "HRDTM5m"},
])
PAI = "ispra:mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai"


@pytest.fixture(scope="module")
def spec():
    return resolver.filter_spec(load_config())


class _Result:
    def __init__(self, text):
        self.text = text


class FakeAgent:
    """Returns `reply`; optionally fails when called with a response_format."""

    def __init__(self, reply, fail_structured=False):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.fail_structured = fail_structured
        self.calls = []

    async def run(self, prompt, options=None):
        self.calls.append({"prompt": prompt, "options": options})
        if options and self.fail_structured:
            raise RuntimeError("json_schema not supported")
        return _Result(self.reply)


# ---- filter spec ------------------------------------------------------------------
def test_filter_spec_from_default_config(spec):
    assert "Elevata P3" in spec.allowed_values["per_fr_ita"]
    assert "1" in spec.allowed_values["tipo_movim"]
    assert spec.attributes_for_layer(PAI.split(":")[1]) == {"per_fr_ita"}
    assert spec.attributes_for_layer("frane_poly_molise_opendata") == {"tipo_movim"}
    assert spec.attributes_for_layer("dtm") == set()
    assert "per_fr_ita" in spec.hints() and "'Elevata P3'" in spec.hints()


def test_filter_spec_empty_config():
    spec = resolver.filter_spec({})
    assert spec.hints() == "" and spec.allowed_values == {}


# ---- schema / provider formats -------------------------------------------------------
def test_schema_restricts_layers_to_catalog():
    schema = resolver.selection_schema(CATALOG)
    assert schema["properties"]["layers"]["items"]["enum"] == [m.qualified for m in CATALOG]
    assert set(schema["required"]) == {"layers", "cql_filter", "explanation", "confidence",
                                       "mode", "metrics", "place"}
    assert schema["properties"]["metrics"]["items"]["enum"] == list(resolver.METRICS)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("provider", ["ollama", "ollama-cloud", "anthropic"])
def test_bare_schema_providers(provider):
    schema = resolver.selection_schema(CATALOG)
    assert resolver.response_format_for(provider, schema) is schema


def test_openai_envelope():
    schema = resolver.selection_schema(CATALOG)
    fmt = resolver.response_format_for("openai", schema)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] is schema and fmt["json_schema"]["strict"]


def test_prompt_is_static_first(spec):
    prompt = resolver.build_prompt(CATALOG, spec.hints(), "frane in Molise")
    assert prompt.index("Available layers") < prompt.index("Filterable") \
        < prompt.index("Request: frane in Molise")
    assert prompt.endswith("JSON:")


# ---- resolve() --------------------------------------------------------------------------
async def test_resolve_structured(spec):
    agent = FakeAgent({"layers": [PAI], "cql_filter": "per_fr_ita='Elevata P3'",
                       "explanation": "ok", "confidence": 0.9})
    sel = await resolver.resolve(agent, "alta pericolosità", CATALOG, spec, provider="ollama")
    assert sel["layers"] == [PAI]
    assert sel["cql_filter"] == "per_fr_ita = 'Elevata P3'"     # canonical form
    assert sel["cql_rejected"] is None
    assert sel["confidence"] == 0.9 and sel["structured"] is True
    fmt = agent.calls[0]["options"]["response_format"]
    assert fmt["properties"]["layers"]["items"]["enum"][1] == PAI


async def test_resolve_falls_back_when_schema_rejected(spec):
    agent = FakeAgent('Sure! {"layers": ["ispra:dtm"], "cql_filter": null, '
                      '"explanation": "DTM", "confidence": 2}', fail_structured=True)
    sel = await resolver.resolve(agent, "dtm", CATALOG, spec, provider="openai")
    assert sel["layers"] == ["ispra:dtm"]
    assert sel["structured"] is False
    assert sel["confidence"] == 1.0                     # clamped
    assert len(agent.calls) == 2 and agent.calls[1]["options"] is None


async def test_resolve_unstructured_mode(spec):
    agent = FakeAgent({"layers": ["ispra:dtm"], "cql_filter": None, "explanation": ""})
    sel = await resolver.resolve(agent, "dtm", CATALOG, spec, provider="ollama",
                                 structured=False)
    assert agent.calls[0]["options"] is None
    assert sel["confidence"] is None


async def test_resolve_drops_hallucinated_layers(spec):
    agent = FakeAgent({"layers": ["ispra:nope", "ispra:dtm"], "cql_filter": None,
                       "explanation": "", "confidence": 0.5})
    sel = await resolver.resolve(agent, "x", CATALOG, spec, provider="ollama")
    assert sel["layers"] == ["ispra:dtm"]


async def test_resolve_unparseable_raises(spec):
    with pytest.raises(ValueError):
        await resolver.resolve(FakeAgent("no json here"), "x", CATALOG, spec,
                               provider="ollama")


# ---- CQL guard ----------------------------------------------------------------------------
@pytest.mark.parametrize("layers, cql, reason", [
    # attribute exists only on the PAI layer, not on the inventory one
    (["ispra:frane_poly_molise_opendata"], "per_fr_ita = 'Elevata P3'", "unknown attribute"),
    # made-up class value
    ([PAI], "per_fr_ita = 'Altissima'", "not valid"),
    # outside the subset
    ([PAI], "INTERSECTS(geom, POINT(1 2))", "functions"),
    # no layer selected -> nothing can be filtered
    ([], "tipo_movim = '1'", "unknown attribute"),
])
def test_guard_rejects(spec, layers, cql, reason):
    sel = resolver.guard_cql({"layers": layers, "cql_filter": cql}, spec)
    assert sel["cql_filter"] is None
    assert sel["cql_rejected"]["filter"] == cql
    assert reason in sel["cql_rejected"]["reason"]


def test_guard_accepts_real_columns_from_layer_fields(spec):
    sel = {"layers": ["ispra:dtm"], "cql_filter": "quota > 1000"}
    assert resolver.guard_cql(dict(sel), spec)["cql_filter"] is None
    ok = resolver.guard_cql(dict(sel), spec, {"ispra:dtm": {"quota"}})
    assert ok["cql_filter"] == "quota > 1000" and ok["cql_rejected"] is None


def test_guard_no_filter(spec):
    sel = resolver.guard_cql({"layers": [PAI], "cql_filter": None}, spec)
    assert sel["cql_rejected"] is None
