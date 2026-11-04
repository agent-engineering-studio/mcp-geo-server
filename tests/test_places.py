"""Admin-area scoping: config parsing, name matching, GeoServer-backed lookup."""

from __future__ import annotations

import json

import pytest

from mcp_geo_server import places
from mcp_geo_server.client import OwsResponse
from mcp_geo_server.styling import load_config

CFG = places.admin_config(load_config())
LV = {lv.level: lv for lv in CFG.levels}
NAMES = {
    LV["comune"]: ["Bari", "Aosta", "AgliÃ¨", "Re", "Campobasso", "Massa"],
    LV["provincia"]: ["Bari", "Campobasso", "Massa-Carrara"],
    LV["regione"]: ["Puglia", "Valle d'Aosta/Vallée d'Aoste", "Valle d'Aosta",
                    "Toscana", "Molise"],
}
INDEX = places.build_index(NAMES)


def test_default_config_has_istat_levels():
    assert [lv.level for lv in CFG.levels] == ["comune", "provincia", "regione"]
    assert CFG.hints["provincia"] == "provincia" and CFG.enabled


def test_missing_section_falls_back_to_packaged_default(caplog):
    cfg = places.admin_config({"styles": {}})
    assert cfg.enabled and "no 'admin' section" in caplog.text


def test_explicit_empty_section_disables():
    assert not places.admin_config({"admin": {}}).enabled


def test_custom_levels_and_validation(monkeypatch):
    monkeypatch.setenv("GEO_ADMIN_WORKSPACE", "boundaries")
    cfg = places.admin_config({"admin": {
        "levels": [{"level": "county", "layer": "counties", "attr": "name"}],
        "level_hints": {"County": "county"}}})
    assert cfg.levels[0] == places.AdminLevel("county", "counties", "name")
    assert cfg.hints == {"county": "county"} and cfg.workspace == "boundaries"
    with pytest.raises(ValueError, match="missing"):
        places.admin_config({"admin": {"levels": [{"level": "x"}]}})
    with pytest.raises(ValueError, match="unknown level"):
        places.admin_config({"admin": {"levels": [], "level_hints": {"x": "nope"}}})


def test_norm_and_demojibake():
    assert places.demojibake("AgliÃ¨") == "Agliè"
    assert places.norm("Valle d'Aosta/Vallée d'Aoste") == "valle d aosta vallee d aoste"
    assert places.norm("AgliÃ¨") == "aglie"


def test_short_names_are_not_indexed():
    assert all(n != "re" for n, _, _ in INDEX)


@pytest.mark.parametrize("query, expected", [
    # a comune and a provincia share the name: most specific level wins…
    ("frane a Bari", ("comune", "Bari")),
    # …unless the request names the level
    ("frane in provincia di Bari", ("provincia", "Bari")),
    ("frane nella città metropolitana di Bari", ("provincia", "Bari")),
    ("pericolosità nella regione Puglia", ("regione", "Puglia")),
    # the longest phrase beats a contained shorter name ("aosta")
    ("DTM della Valle d'Aosta", ("regione", "Valle d'Aosta")),
    ("frane ad Aosta", ("comune", "Aosta")),
    # accents and mojibake in the stored name
    ("frane ad Agliè", ("comune", "AgliÃ¨")),
    ("frane ad aglie", ("comune", "AgliÃ¨")),
    ("FRANE IN TOSCANA", ("regione", "Toscana")),
    ("frane in provincia di Massa-Carrara", ("provincia", "Massa-Carrara")),
    # whole words only: "molisano" is not "Molise", "re" is not indexed
    ("dati del territorio molisano", None),
    ("mostrami le frane", None),
])
def test_match(query, expected):
    got = places.match(query, INDEX, CFG)
    assert ((got[0].level, got[1]) if got else None) == expected


# ---- GeoServer-backed ---------------------------------------------------------------
class FakeWfs:
    """Answers the two WFS reads AdminAreas makes; counts calls."""

    def __init__(self, settings):
        self.settings = settings
        self.calls = []

    async def ows(self, params, base=None):
        self.calls.append(params)
        type_name = params["typeNames"].split(":")[1]
        if "cql_filter" in params:
            ring = [[16.7, 41.0], [17.0, 41.0], [17.0, 41.2], [16.7, 41.2], [16.7, 41.0]]
            feats = [{"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}}]
        else:
            level = next(lv for lv in NAMES if lv.layer == type_name)
            feats = [{"properties": {level.attr: n}} for n in NAMES[level]]
        return OwsResponse(status_code=200, headers={"content-type": "application/json"},
                           text=json.dumps({"features": feats}), content=b"")


@pytest.fixture
def fake_wfs():
    from mcp_geo_server import config
    fake = FakeWfs(config.get_settings())
    return fake


async def test_admin_areas_detect_and_scope(fake_wfs):
    pytest.importorskip("shapely")
    areas = places.AdminAreas(CFG, client_getter=lambda: fake_wfs)
    hit = await areas.detect("frane in provincia di Bari")
    assert (hit[0].level, hit[1]) == ("provincia", "Bari")
    assert len(fake_wfs.calls) == 3                     # one names read per level
    assert fake_wfs.calls[0]["typeNames"] == "demo:com01012023_g"   # default ws

    scope = await areas.scope(*hit)
    assert scope["level"] == "provincia" and scope["name"] == "Bari"
    assert scope["bbox"] == {"minx": 16.7, "miny": 41.0, "maxx": 17.0, "maxy": 41.2}
    assert scope["clip"].startswith("SRID=4326;POLYGON")
    assert "den_uts='Bari'" == fake_wfs.calls[-1]["cql_filter"]

    await areas.detect("frane a Bari")
    await areas.scope(*hit)
    assert len(fake_wfs.calls) == 4                     # index + scope cached


async def test_disabled_config_makes_no_calls(fake_wfs):
    areas = places.AdminAreas(places.AdminConfig(), client_getter=lambda: fake_wfs)
    assert await areas.detect("frane a Bari") is None and fake_wfs.calls == []


def test_clip_wkt_fits_the_limit():
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import Point
    big = Point(12, 42).buffer(1.0, quad_segs=2000)      # ~8000 vertices
    assert len(big.wkt) > places.MAX_CLIP_WKT
    wkt = places.clip_wkt(big)
    assert len(wkt) <= places.MAX_CLIP_WKT and shapely.from_wkt(wkt).is_valid
