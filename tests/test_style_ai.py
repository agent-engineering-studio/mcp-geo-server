"""Styles from a sentence: two constrained steps, real values only, valid SLD."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
import yaml

from mcp_geo_server import style_ai
from mcp_geo_server.client import GeoServerError, OwsResponse

LAYER = "ispra:frane_line_molise_opendata"
FT = {"featureType": {
    "name": "frane_line_molise_opendata", "title": "", "keywords": {"string": []},
    "attributes": {"attribute": [
        {"name": "geom", "binding": "org.locationtech.jts.geom.MultiLineString"},
        {"name": "id_frana", "binding": "java.lang.String"},
        {"name": "tipo_movim", "binding": "java.lang.Integer"},
    ]},
}}
# 10 x class 1, 5 x class 4, 2 x class 0
FEATURES = ([{"properties": {"id_frana": f"a{i}", "tipo_movim": 1}} for i in range(10)]
            + [{"properties": {"id_frana": f"b{i}", "tipo_movim": 4}} for i in range(5)]
            + [{"properties": {"id_frana": f"c{i}", "tipo_movim": 0}} for i in range(2)])


class FakeClient:
    def __init__(self, features=FEATURES):
        from mcp_geo_server import config
        self.settings = config.get_settings()
        self.features = features
        self.ows_calls = []

    async def get_json(self, path, params=None):
        if path.endswith("/featuretypes/frane_line_molise_opendata.json"):
            return json.loads(json.dumps(FT))
        raise GeoServerError("404")

    async def ows(self, params, base=None):
        self.ows_calls.append(params)
        props = params["propertyName"].split(",")
        feats = [{"properties": {k: f["properties"].get(k) for k in props}}
                 for f in self.features]
        return OwsResponse(200, json.dumps({"features": feats}), b"", {})


class _R:
    def __init__(self, t):
        self.text = t


class Agent:
    def __init__(self, reply):
        self.reply, self.prompts, self.options = reply, [], []

    async def run(self, prompt, options=None):
        self.prompts.append(prompt)
        self.options.append(options)
        return _R(json.dumps(self.reply))


PICK = {"attribute": "tipo_movim", "reason": "tipo di movimento"}
LEGEND = {"title": "Tipo di movimento", "else_color": "#CCCCCC", "classes": [
    {"value": "1", "label": "Crollo", "color": "#E41A1C"},
    {"value": "4", "label": "Colamento lento", "color": "#ff7f00"},
    {"value": "0", "label": "Non determinato", "color": "#999999"},
    {"value": "99", "label": "inventato", "color": "#000000"},     # not a real value
    {"value": "1", "label": "duplicato", "color": "#111111"},      # duplicate
]}


async def test_propose_end_to_end():
    pick, legend, client = Agent(PICK), Agent(LEGEND), FakeClient()
    p = await style_ai.propose(LAYER, "colora per tipo di movimento, toni caldi",
                               agents=(pick, legend), provider="ollama", client=client)
    assert p["name"] == "ai_frane_line_molise_opendata" and p["attribute"] == "tipo_movim"
    spec = p["spec"]
    assert spec["kind"] == "line"                                 # from the geometry
    assert [c["value"] for c in spec["classes"]] == ["1", "4", "0"]
    assert spec["classes"][0]["color"] == "#e41a1c" and spec["else_color"] == "#cccccc"
    # step 1 is constrained to the real columns, step 2 to the real values
    assert pick.options[0]["response_format"]["properties"]["attribute"]["enum"] == \
        ["id_frana", "tipo_movim"]
    values_enum = legend.options[0]["response_format"]["properties"]["classes"]["items"][
        "properties"]["value"]["enum"]
    assert values_enum == ["1", "4", "0"]                         # most frequent first
    assert "- 1: 10" in legend.prompts[0]
    # the SLD is well-formed XML with one rule per class + the else rule
    root = ET.fromstring(p["sld"])
    rules = [e for e in root.iter() if e.tag.endswith("Rule")]
    assert len(rules) == 4
    # the YAML snippet round-trips into the styles.yml shape
    snippet = yaml.safe_load(p["yaml"])
    assert snippet["styles"][p["name"]] == spec
    assert snippet["assign"] == [{"name_matches": r"^frane_line_molise_opendata$",
                                  "style": p["name"]}]


async def test_given_attribute_skips_step_one():
    pick, legend = Agent(PICK), Agent(LEGEND)
    p = await style_ai.propose(LAYER, "x", attribute="tipo_movim", agents=(pick, legend),
                               provider="ollama", client=FakeClient())
    assert pick.prompts == [] and p["reason"] == "given by the caller"


async def test_unknown_attribute_is_rejected():
    with pytest.raises(ValueError, match="not a column"):
        await style_ai.propose(LAYER, "x", attribute="nope", agents=(Agent(PICK), Agent(LEGEND)),
                               client=FakeClient())


async def test_too_many_values_is_rejected():
    feats = [{"properties": {"id_frana": f"x{i}", "tipo_movim": i}} for i in range(40)]
    with pytest.raises(ValueError, match="too many"):
        await style_ai.propose(LAYER, "x", attribute="tipo_movim",
                               agents=(Agent(PICK), Agent(LEGEND)), client=FakeClient(feats))


def test_clean_classes_rejects_all_invalid():
    with pytest.raises(ValueError, match="no valid class"):
        style_ai.clean_classes({"classes": [{"value": "1", "label": "a", "color": "red"}]}, ["1"])


@pytest.mark.parametrize("geometry, kind", [("MultiPolygon", "polygon"),
                                            ("MultiLineString", "line"),
                                            ("Point", "point"), (None, "polygon")])
def test_kind_for_geometry(geometry, kind):
    assert style_ai.kind_for_geometry(geometry) == kind


async def test_apply_publishes_and_assigns(monkeypatch):
    from mcp_geo_server import styling
    from mcp_geo_server.tools import styles as style_tools

    calls = []

    async def ensure(styles):
        calls.append(("ensure", sorted(styles)))

    async def assign(layer, style, workspace=None, default=True):
        calls.append(("assign", layer, style, workspace, default))

    monkeypatch.setattr(styling, "ensure_styles", ensure)
    monkeypatch.setattr(style_tools, "geo_assign_style_to_layer", assign)
    await style_ai.apply({"name": "ai_x", "layer": "ispra:x", "sld": "<sld/>"})
    assert calls == [("ensure", ["ai_x"]), ("assign", "x", "ai_x", "ispra", True)]


async def test_style_endpoint(monkeypatch):
    import httpx

    from webui import app as webui_app

    async def fake_propose(layer, instruction, attribute=None):
        return {"name": "ai_x", "layer": layer, "attribute": "tipo_movim", "reason": "r",
                "spec": {"kind": "line"}, "yaml": "styles: {}\n", "sld": "<sld/>"}

    applied = []

    async def fake_apply(p):
        applied.append(p["name"])

    monkeypatch.setattr(style_ai, "propose", fake_propose)
    monkeypatch.setattr(style_ai, "apply", fake_apply)
    transport = httpx.ASGITransport(app=webui_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/style", json={"layer": "ispra:x", "instruction": "colora"})
        assert r.status_code == 200 and r.json()["applied"] is False and applied == []
        r = await c.post("/api/style", json={"layer": "ispra:x", "instruction": "colora",
                                              "apply": True})
        assert r.json()["applied"] is True and applied == ["ai_x"]
        r = await c.post("/api/style", json={"layer": "ispra:x", "instruction": " "})
        assert r.status_code == 400
