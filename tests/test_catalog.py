"""Unit tests for the domain-agnostic catalog (WMS-capabilities parsing +
LLM-selection validation). No network / no LLM."""

from __future__ import annotations

import pytest

from mcp_geo_server.catalog import (
    build_catalog,
    catalog_prompt,
    parse_wms_capabilities,
    validate_selection,
)

_CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Title>GeoServer Web Map Service</Title>
      <Layer queryable="1">
        <Name>ispra:frane_line_molise_opendata</Name>
        <Title>Frane lineari Molise</Title>
        <Abstract>Inventario frane</Abstract>
        <KeywordList><Keyword>frane</Keyword><Keyword>molise</Keyword></KeywordList>
      </Layer>
      <Layer queryable="1">
        <Name>topp:states</Name>
        <Title>USA Population</Title>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>"""


def test_parse_wms_capabilities_extracts_named_layers():
    layers = parse_wms_capabilities(_CAPS)
    # the container Layer (no Name) is skipped → exactly 2 named layers.
    assert len(layers) == 2
    first = layers[0]
    assert first["workspace"] == "ispra"
    assert first["name"] == "frane_line_molise_opendata"
    assert first["title"] == "Frane lineari Molise"
    assert first["keywords"] == ["frane", "molise"]


def test_parse_wms_capabilities_bad_xml_is_empty():
    assert parse_wms_capabilities("not xml <<<") == []


def test_catalog_prompt_includes_qualified_name_and_metadata():
    catalog = build_catalog(parse_wms_capabilities(_CAPS))
    prompt = catalog_prompt(catalog)
    assert "ispra:frane_line_molise_opendata" in prompt
    assert "Frane lineari Molise" in prompt
    assert "topp:states" in prompt
    assert "kw: frane, molise" in prompt


def test_validate_selection_keeps_known_drops_hallucinated():
    catalog = build_catalog([
        {"name": "frane_line_molise_opendata", "workspace": "ispra"},
        {"name": "states", "workspace": "topp"},
    ])
    reply = ('prose {"layers": ["ispra:frane_line_molise_opendata", '
             '"ispra:nope", "states"], "cql_filter": "  ", "explanation": "ok"} x')
    sel = validate_selection(reply, catalog)
    assert sel["layers"] == ["ispra:frane_line_molise_opendata", "topp:states"]
    assert sel["cql_filter"] is None
    assert sel["explanation"] == "ok"


def test_validate_selection_keeps_cql_when_present():
    catalog = build_catalog([{"name": "mosaic", "workspace": "ispra"}])
    reply = ('{"layers": ["ispra:mosaic"], '
             '"cql_filter": "per_fr_ita IN (\'Elevata P3\',\'Molto elevata P4\')", '
             '"explanation": "alta pericolosità"}')
    sel = validate_selection(reply, catalog)
    assert sel["layers"] == ["ispra:mosaic"]
    assert "per_fr_ita" in sel["cql_filter"]


def test_validate_selection_raises_on_no_json():
    with pytest.raises(ValueError):
        validate_selection("no json here", [])
