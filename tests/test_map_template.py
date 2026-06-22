from mcp_geo_server.tools.map import _bounds_from_bbox, render_map


def test_render_map_has_osm_and_wms():
    html = render_map("My map", "http://gs.test/geoserver/wms", ["topp:states"])
    assert "tile.openstreetmap.org" in html
    assert "L.tileLayer.wms" in html
    assert "topp:states" in html
    assert "OpenStreetMap" in html  # attribution present


def test_render_map_with_bounds_uses_fitbounds():
    html = render_map("M", "http://gs/wms", ["a:b"], bounds=[[24.0, -130.0], [50.0, -66.0]])
    assert "fitBounds" in html
    assert "setView" not in html


def test_render_map_without_bounds_uses_setview():
    html = render_map("M", "http://gs/wms", ["a:b"], center=(45.0, 9.0), zoom=8)
    assert "setView" in html
    assert "fitBounds" not in html


def test_render_map_embeds_geojson():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "x.1",
         "properties": {"name": "Foo"},
         "geometry": {"type": "Point", "coordinates": [9, 45]}}]}
    html = render_map("M", "http://gs/wms", ["a:b"], geojson=gj, geojson_name="a:b")
    assert "L.geoJSON" in html
    assert "FeatureCollection" in html
    assert "bindPopup" in html


def test_bounds_from_bbox():
    bbox = {"minx": -130, "miny": 24, "maxx": -66, "maxy": 50}
    assert _bounds_from_bbox(bbox) == [[24.0, -130.0], [50.0, -66.0]]
    assert _bounds_from_bbox(None) is None
    assert _bounds_from_bbox({"minx": "x"}) is None
