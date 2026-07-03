"""Unit tests for the config-driven thematic SLD engine.

No network: loads the packaged default (ISPRA) config, validates the generated
SLD is well-formed XML and that layers map to the expected style.
"""

from __future__ import annotations

import xml.dom.minidom as minidom

import pytest

from mcp_geo_server.styling import (
    build_style,
    build_styles,
    load_config,
    style_for_layer,
)


@pytest.fixture(scope="module")
def cfg():
    return load_config()  # packaged ISPRA default


@pytest.fixture(scope="module")
def styles(cfg):
    return build_styles(cfg)


def test_default_config_has_expected_ispra_styles(styles):
    assert set(styles) == {
        "frana_tipo_poly", "frana_tipo_line", "frana_tipo_point",
        "pericolosita_pai", "pericolosita_idraulica", "limiti_outline",
        "dtm_elevation",
    }


def test_raster_style_builds_colormap():
    sld = build_style("dtm", {
        "kind": "raster",
        "opacity": 0.9,
        "color_map_type": "ramp",
        "entries": [
            {"quantity": 0, "color": "#1a9850", "label": "0 m"},
            {"quantity": 3000, "color": "#ffffff"},
        ],
    })
    import xml.dom.minidom as minidom
    minidom.parseString(sld)  # well-formed
    assert "<RasterSymbolizer>" in sld
    assert '<ColorMap type="ramp">' in sld
    assert sld.count("<ColorMapEntry") == 2
    assert 'quantity="3000"' in sld
    assert "<Opacity>0.9</Opacity>" in sld


def test_raster_style_requires_entries():
    with pytest.raises(ValueError):
        build_style("bad", {"kind": "raster"})


def test_default_dtm_style_maps_by_name(cfg):
    assert style_for_layer("lombardia_dtm", cfg["assign"]) == "dtm_elevation"
    assert style_for_layer("dem_5m_trento", cfg["assign"]) == "dtm_elevation"


def test_all_styles_are_well_formed_xml(styles):
    for name, sld in styles.items():
        minidom.parseString(sld)  # raises on malformed XML
        assert f"<Name>{name}</Name>" in sld


def test_categorical_style_has_a_rule_per_class_plus_else(cfg, styles):
    n_classes = len(cfg["styles"]["frana_tipo_poly"]["classes"])
    sld = styles["frana_tipo_poly"]
    assert sld.count("<ogc:PropertyIsEqualTo>") == n_classes
    assert sld.count("<ElseFilter/>") == 1
    assert "tipo_movim" in sld


def test_pai_style_has_no_stroke(styles):
    # PAI is configured stroke:false → no polygon Stroke element.
    sld = styles["pericolosita_pai"]
    assert "per_fr_ita" in sld
    assert "<Stroke>" not in sld


def test_flat_and_outline_styles(styles):
    assert "PolygonSymbolizer" in styles["pericolosita_idraulica"]
    # outline = stroke only, no Fill.
    assert "<Fill>" not in styles["limiti_outline"]
    assert "<Stroke>" in styles["limiti_outline"]


def test_build_style_rejects_categorical_without_classes():
    with pytest.raises(ValueError):
        build_style("bad", {"kind": "polygon", "attribute": "x"})


@pytest.mark.parametrize("name,expected", [
    ("frane_line_molise_opendata", "frana_tipo_line"),
    ("frane_piff_lazio_opendata", "frana_tipo_point"),
    ("frane_poly_lazio_opendata", "frana_tipo_poly"),
    ("aree_poly_campania_opendata", "frana_tipo_poly"),
    ("dgpv_poly_lombardia_opendata", "frana_tipo_poly"),
    ("com01012023_g", "limiti_outline"),
    ("mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai", "pericolosita_pai"),
    ("mosaicatura_ispra_2020_aree_pericolosita_idraulica", "pericolosita_idraulica"),
    ("states", None),  # unknown / default sample layer
])
def test_style_for_layer_uses_name_rules_from_config(cfg, name, expected):
    # Matching is purely name-based (config assign rules), no hardcoded vocab.
    assert style_for_layer(name, cfg["assign"]) == expected


def test_style_for_layer_arbitrary_domain():
    assign = [
        {"name_contains": "co2", "style": "air_quality"},
        {"name_matches": "^road_", "style": "roads"},
    ]
    assert style_for_layer("sensor_co2_2024", assign) == "air_quality"
    assert style_for_layer("road_primary", assign) == "roads"
    assert style_for_layer("sensor_pm10_2024", assign) is None

