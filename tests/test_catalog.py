"""Unit tests for the layer-catalog parsing + NL-selection validation.

No network / no LLM: pure functions over layer names and a fake model reply.
"""

from __future__ import annotations

import pytest

from mcp_geo_server.catalog import (
    build_catalog,
    catalog_prompt,
    parse_layer_name,
    validate_selection,
)


@pytest.mark.parametrize("name,theme,geometry,region", [
    ("frane_line_molise_opendata", "frane", "line", "molise"),
    ("frane_poly_campania_opendata", "frane", "polygon", "campania"),
    ("frane_piff_molise_opendata", "frane", "point", "molise"),
    ("dgpv_poly_valle_d_aosta_opendata", "dgpv", "polygon", "valle_d_aosta"),
    ("aree_poly_friuli_venezia_giulia_opendata", "aree", "polygon",
     "friuli_venezia_giulia"),
    ("com01012023_g", "limiti", "polygon", None),
    ("reg01012023_g", "limiti", "polygon", None),
])
def test_parse_layer_name(name, theme, geometry, region):
    m = parse_layer_name(name, "ispra")
    assert m.theme == theme
    assert m.geometry == geometry
    assert m.region == region
    assert m.qualified == f"ispra:{name}"


def test_mosaicatura_prefix_wins_over_internal_aree_token():
    # "aree" appears inside the name but the theme is the prefix.
    m = parse_layer_name("mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai")
    assert m.theme == "mosaicatura"
    assert m.geometry == "polygon"
    assert "frana" in m.label


def test_validate_selection_keeps_known_drops_hallucinated():
    catalog = build_catalog([
        ("frane_line_molise_opendata", "ispra"),
        ("com01012023_g", "ispra"),
    ])
    reply = ('prose... {"layers": ["ispra:frane_line_molise_opendata", '
             '"ispra:does_not_exist", "com01012023_g"], "cql_filter": "  ", '
             '"explanation": "ok"} trailing text')
    sel = validate_selection(reply, catalog)
    # qualified + bare both resolve; hallucinated dropped; blank cql -> None.
    assert sel["layers"] == ["ispra:frane_line_molise_opendata", "ispra:com01012023_g"]
    assert sel["cql_filter"] is None
    assert sel["explanation"] == "ok"


def test_validate_selection_dedupes_and_accepts_string():
    catalog = build_catalog([("com01012023_g", "ispra")])
    reply = '{"layers": "com01012023_g", "explanation": ""}'
    sel = validate_selection(reply, catalog)
    assert sel["layers"] == ["ispra:com01012023_g"]


def test_validate_selection_raises_on_no_json():
    with pytest.raises(ValueError):
        validate_selection("no json here", [])


def test_catalog_prompt_lists_qualified_names():
    catalog = build_catalog([("frane_line_molise_opendata", "ispra")])
    prompt = catalog_prompt(catalog)
    assert "ispra:frane_line_molise_opendata" in prompt
    assert "theme=frane" in prompt
