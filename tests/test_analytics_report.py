"""Exact WFS counts, counting in the ask pipeline, area reports and fact-checking."""

from __future__ import annotations

import json
import re

import pytest

from mcp_geo_server import analytics, report
from mcp_geo_server.catalog import build_catalog
from mcp_geo_server.client import OwsResponse
from mcp_geo_server.resolver import Tier

FRANE = "ispra:frane_poly_puglia_opendata"
PAI = "ispra:mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai"


class HitsClient:
    """Answers WFS hits: the count depends on the cql_filter (records them)."""

    def __init__(self, table):
        from mcp_geo_server import config
        self.settings = config.get_settings()
        self.table = table          # function(layer, cql) -> int
        self.calls = []

    async def ows(self, params, base=None):
        assert params["resultType"] == "hits"
        self.calls.append((params["typeNames"], params.get("cql_filter")))
        n = self.table(params["typeNames"], params.get("cql_filter"))
        return OwsResponse(200, f'<wfs:FeatureCollection numberMatched="{n}" '
                                f'numberReturned="0"/>', b"", {})


def tipo_table(layer, cql):
    by_class = {"1": 30, "2": 50, "4": 0}
    m = re.search(r"tipo_movim = '(\d+)'", cql or "")
    return by_class.get(m.group(1), 0) if m else 80


# ---- analytics -----------------------------------------------------------------------
def test_combine_and_equals():
    assert analytics.combine(None, "") is None
    assert analytics.combine("a = 1") == "a = 1"
    assert analytics.combine("a = 1", "INTERSECTS(geom, P)") == "(a = 1) AND (INTERSECTS(geom, P))"
    assert analytics.equals("nome", "d'Aosta") == "nome = 'd''Aosta'"


async def test_count_and_count_by():
    client = HitsClient(tipo_table)
    assert await analytics.count(FRANE, None, client) == 80
    groups = await analytics.count_by(FRANE, "tipo_movim",
                                      [("1", "Crollo"), ("2", "Scivolamento"), ("4", "Colata")],
                                      "INTERSECTS(geom, P)", client)
    assert groups == [{"value": "2", "label": "Scivolamento", "count": 50},
                      {"value": "1", "label": "Crollo", "count": 30}]   # zero dropped
    assert client.calls[-1][1].startswith("(INTERSECTS(geom, P)) AND (tipo_movim = ")


def test_count_intent_and_clean_aggregate():
    assert analytics.count_intent("quante frane ci sono in Puglia?")
    assert not analytics.count_intent("frane in Puglia")
    assert analytics.clean_aggregate({"kind": "count_by", "attribute": "tipo_movim"}) == \
        {"kind": "count_by", "attribute": "tipo_movim"}
    assert analytics.clean_aggregate({"kind": "count_by", "attribute": None})["kind"] == "count"
    assert analytics.clean_aggregate({"kind": "sum"})["kind"] == "none"
    assert analytics.clean_aggregate(None)["kind"] == "none"


def test_fmt_and_describe():
    assert analytics.fmt(1234567) == "1.234.567" and analytics.fmt(None) == "n/d"
    text = analytics.describe(FRANE, 80, [{"label": "Crollo", "count": 30}], "tipo_movim")
    assert text == "frane_poly_puglia_opendata: 80 elementi — per tipo_movim: Crollo 30"


# ---- counting inside the ask pipeline ---------------------------------------------------
class _R:
    def __init__(self, t):
        self.text = t


class Agent:
    def __init__(self, reply):
        self.reply = json.dumps(reply)

    async def run(self, prompt, options=None):
        return _R(self.reply)


def _assistant(monkeypatch, reply, admin=None):
    from mcp_geo_server import answer, places
    from mcp_geo_server.styling import load_config

    a = answer.MapAssistant()
    a.tiers = [Tier(Agent(reply), "ollama")]
    cat = build_catalog([{"name": FRANE.split(":")[1], "workspace": "ispra"}])
    levels = {lv.level: lv for lv in places.admin_config(load_config()).levels}

    async def load():
        return cat

    async def info(ws, name):
        return {"geometry": "poligoni", "geometry_column": "geom",
                "fields": ["tipo_movim", "id"], "count": 999, "title": name, "abstract": ""}

    async def bbox(ws, name):
        return {"minx": 15, "miny": 39, "maxx": 19, "maxy": 42}

    async def detect(text):
        return (levels["provincia"], "Bari") if admin and "bari" in text.lower() else None

    async def scope(level, raw):
        return {"level": level.level, "name": raw, "clip": "SRID=4326;POINT(16.8 41.1)",
                "bbox": {"minx": 16, "miny": 40.5, "maxx": 17.5, "maxy": 41.5}}

    for k, v in dict(load_catalog=load, layer_info=info, layer_bbox=bbox,
                     detect_admin=detect, admin_scope=scope).items():
        monkeypatch.setattr(a, k, v)
    client = HitsClient(tipo_table)
    monkeypatch.setattr("mcp_geo_server.client.get_client", lambda: client)
    return a, client


def _reply(aggregate, layers=(FRANE,), cql=None):
    return {"layers": list(layers), "cql_filter": cql, "explanation": "", "confidence": 0.9,
            "mode": "new", "metrics": [], "place": None, "aggregate": aggregate}


async def test_count_by_uses_the_maps_own_filters(monkeypatch):
    a, client = _assistant(monkeypatch, _reply({"kind": "count_by", "attribute": "tipo_movim"}),
                           admin=True)
    sel = await a.ask("quante frane per tipo in provincia di Bari?")
    an = sel["analysis"]
    assert an["kind"] == "count_by" and an["title"] == "Conteggio — Bari"
    row = an["layers"][0]
    assert row["total"] == 80
    assert [g["label"] for g in row["groups"]][:2] == \
        ["Scivolamento rotazionale/traslativo", "Crollo / Ribaltamento"]
    # every count carried the area INTERSECTS the map applies
    assert client.calls and all("INTERSECTS(geom, POINT(16.8 41.1))" in c
                                for _, c in client.calls)


async def test_keyword_safety_net_turns_on_counting(monkeypatch):
    a, _ = _assistant(monkeypatch, _reply({"kind": "none", "attribute": None}))
    sel = await a.ask("quante frane ci sono?")
    assert sel["aggregate"]["kind"] == "count" and sel["analysis"]["layers"][0]["total"] == 80


async def test_no_counting_unless_asked(monkeypatch):
    a, client = _assistant(monkeypatch, _reply({"kind": "none", "attribute": None}))
    sel = await a.ask("frane in Puglia")
    assert "analysis" not in sel and client.calls == []


async def test_group_by_unknown_attribute_is_noted(monkeypatch):
    a, _ = _assistant(monkeypatch, _reply({"kind": "count_by", "attribute": "per_fr_ita"}))
    sel = await a.ask("quante frane per pericolosità?")
    row = sel["analysis"]["layers"][0]
    assert row["groups"] is None and "non è un attributo" in row["note"]


# ---- report ----------------------------------------------------------------------------------
def test_numbers_and_fact_check():
    assert report._numbers("1.234 frane, 12,5% e 7") == {"1234", "12.5", "7"}
    facts = {"layers": [{"total": 1234, "groups": [{"count": 30}]}]}
    assert report.fact_check("Ci sono 1.234 frane, di cui 30 crolli.", facts) == []
    assert report.fact_check("Ci sono 1.234 frane, il 2% del totale.", facts) == ["2"]


async def _report_assistant(monkeypatch):
    from mcp_geo_server import answer, places
    from mcp_geo_server.styling import load_config

    a = answer.MapAssistant()
    cat = build_catalog([
        {"name": "frane_poly_puglia_opendata", "workspace": "ispra", "bbox": [15, 39.8, 18.6, 42.2]},
        {"name": "frane_poly_sardegna_opendata", "workspace": "ispra", "bbox": [8, 38.8, 9.9, 41.3]},
        {"name": "frane_poly_basilicata_opendata", "workspace": "ispra",
         "bbox": [15.3, 39.8, 16.9, 41.2]},
        {"name": "dtm", "workspace": "ispra", "title": "HRDTM5m", "bbox": [6, 36, 19, 47]},
        {"name": "provcm01012023_g", "workspace": "ispra", "bbox": [6, 36, 19, 47]},
        {"name": "carta_geolitologica", "workspace": "ispra", "bbox": [6, 36, 19, 47]},
    ])
    levels = {lv.level: lv for lv in places.admin_config(load_config()).levels}

    async def load():
        return cat

    async def info(ws, name):
        if name == "dtm":
            return {"geometry": "raster", "fields": []}
        return {"geometry": "poligoni", "geometry_column": "geom",
                "fields": ["tipo_movim"] if name.startswith("frane") else ["sigla"]}

    async def detect(text):
        return (levels["provincia"], "Bari") if "bari" in text.lower() else None

    async def scope(level, raw):
        return {"level": level.level, "name": raw, "clip": "SRID=4326;POINT(16.8 41.1)",
                "bbox": {"minx": 15.9, "miny": 40.4, "maxx": 17.5, "maxy": 41.3}}

    for k, v in dict(load_catalog=load, layer_info=info, detect_admin=detect,
                     admin_scope=scope).items():
        monkeypatch.setattr(a, k, v)

    def table(layer, cql):
        if "basilicata" in layer:
            return 0                                   # overlaps the bbox, not the area
        if "geolitologica" in layer:
            return 12
        return tipo_table(layer, cql)

    client = HitsClient(table)
    monkeypatch.setattr("mcp_geo_server.client.get_client", lambda: client)
    return a, client


async def test_area_report(monkeypatch):
    a, client = await _report_assistant(monkeypatch)
    rep = await report.area_report("provincia di Bari", "frane", assistant=a)
    f = rep["facts"]
    assert f["area"]["name"] == "Bari" and f["subject"] == "frane"
    # sardegna: no bbox overlap; basilicata: overlaps but 0 inside; boundary layer excluded
    assert [lay["layer"] for lay in f["layers"]] == ["ispra:frane_poly_puglia_opendata"]
    assert f["layers"][0]["total"] == 80 and f["layers"][0]["attribute"] == "tipo_movim"
    assert not any("sardegna" in c[0] for c in client.calls)
    assert "| Elementi nell'area |" in rep["markdown"] and "<table>" in rep["html"]
    assert rep["narrative"] is None


async def test_area_report_without_subject_includes_other_layers(monkeypatch):
    a, _ = await _report_assistant(monkeypatch)
    rep = await report.area_report("Bari", assistant=a)
    names = [lay["layer"] for lay in rep["facts"]["layers"]]
    assert names == ["ispra:frane_poly_puglia_opendata", "ispra:carta_geolitologica"]
    assert rep["facts"]["rasters"] == ["HRDTM5m"]
    assert all("provcm" not in n for n in names)


async def test_narrative_kept_only_if_numbers_check_out(monkeypatch):
    a, _ = await _report_assistant(monkeypatch)
    good = TextAgent("Nella provincia di Bari ci sono 80 frane poligonali, "
                     "soprattutto scivolamenti (50).")
    rep = await report.area_report("Bari", "frane", assistant=a, narrative=True, agent=good)
    assert rep["narrative"].startswith("Nella provincia") and rep["narrative_rejected"] is None

    bad = TextAgent("Le frane sono 80, il 62% scivolamenti.")
    rep = await report.area_report("Bari", "frane", assistant=a, narrative=True, agent=bad)
    assert rep["narrative"] is None and "62" in rep["narrative_rejected"]
    assert "Sintesi automatica scartata" in rep["markdown"]


class TextAgent:
    def __init__(self, text):
        self.text = text

    async def run(self, prompt, options=None):
        assert "Facts:" in prompt
        return _R(self.text)


async def test_unknown_place(monkeypatch):
    a, _ = await _report_assistant(monkeypatch)
    with pytest.raises(ValueError, match="no administrative unit"):
        await report.area_report("Atlantide", assistant=a)
