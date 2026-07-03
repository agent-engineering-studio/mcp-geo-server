"""Behavioural tests: drive the registered tools with a FakeClient and assert
on the requests they build (bodies, params, XML) — no network involved."""

from __future__ import annotations

import dataclasses
import json

import pytest

from mcp_geo_server.client import OwsResponse


def test_all_tools_registered(tools):
    registry, _ = tools
    assert len(registry) == 33, sorted(registry)
    assert "geo_get_status" in registry
    assert "geo_build_web_map" in registry
    # Raster (coverage store) tools are registered too.
    assert "geo_create_coveragestore_geotiff" in registry
    assert "geo_delete_coveragestore" in registry
    # Terrain enrichment tool.
    assert "geo_enrich_from_dtm" in registry


async def test_status(tools):
    registry, fake = tools
    fake.json_responses["about/version.json"] = {
        "about": {"resource": [{"@name": "GeoServer", "Version": "2.28.0"}]}
    }
    result = await registry["geo_get_status"]()
    assert result["connected"] is True
    assert result["components"][0]["Version"] == "2.28.0"


async def test_list_workspaces_normalizes(tools):
    registry, fake = tools
    fake.json_responses["workspaces.json"] = {
        "workspaces": {"workspace": [{"name": "topp"}, {"name": "demo"}]}
    }
    result = await registry["geo_list_workspaces"]()
    assert [w["name"] for w in result] == ["topp", "demo"]


async def test_create_workspace_body(tools):
    registry, fake = tools
    await registry["geo_create_workspace"]("demo", default=True)
    call = fake.last("POST")
    assert call["path"] == "workspaces.json"
    assert call["json"] == {"workspace": {"name": "demo"}}
    assert call["params"] == {"default": "true"}


async def test_create_datastore_postgis_connection_params(tools):
    registry, fake = tools
    await registry["geo_create_datastore_postgis"](
        "pg", host="postgis", database="gis", user="gis", password="gis")
    call = fake.last("POST")
    assert call["path"] == "workspaces/demo/datastores.json"
    entries = call["json"]["dataStore"]["connectionParameters"]["entry"]
    kv = {e["@key"]: e["$"] for e in entries}
    assert kv["dbtype"] == "postgis"
    assert kv["host"] == "postgis"
    assert kv["database"] == "gis"
    assert kv["port"] == "5432"


async def test_publish_featuretype_recalculate(tools):
    registry, fake = tools
    await registry["geo_publish_featuretype"]("pg", "cities", name="city_pts")
    call = fake.last("POST")
    assert call["path"] == "workspaces/demo/datastores/pg/featuretypes.json"
    assert call["params"] == {"recalculate": "nativebbox,latlonbbox"}
    ft = call["json"]["featureType"]
    assert ft["name"] == "city_pts"
    assert ft["nativeName"] == "cities"
    assert ft["srs"] == "EPSG:4326"


async def test_list_available_featuretypes(tools):
    registry, fake = tools
    fake.json_responses["workspaces/demo/datastores/pg/featuretypes.json"] = {
        "list": {"string": ["table_a", "table_b"]}
    }
    result = await registry["geo_list_featuretypes"]("pg", only_available=True)
    assert result == ["table_a", "table_b"]
    assert fake.last("GET")["params"] == {"list": "available"}


async def test_get_layer_bbox(tools):
    registry, fake = tools
    fake.json_responses["workspaces/demo/featuretypes/states.json"] = {
        "featureType": {"srs": "EPSG:4326",
                        "latLonBoundingBox": {"minx": -130, "miny": 24,
                                              "maxx": -66, "maxy": 50}}
    }
    result = await registry["geo_get_layer_bbox"]("states")
    assert result["srs"] == "EPSG:4326"
    assert result["latLonBoundingBox"]["maxy"] == 50


async def test_update_layer_default_style(tools):
    registry, fake = tools
    await registry["geo_update_layer"]("states", default_style="polygon", enabled=True)
    call = fake.last("PUT")
    assert call["json"] == {"layer": {"defaultStyle": {"name": "polygon"}, "enabled": True}}


async def test_update_layer_requires_something(tools):
    registry, _ = tools
    with pytest.raises(ValueError):
        await registry["geo_update_layer"]("states")


async def test_delete_layer_with_datastore_cleans_featuretype(tools):
    registry, fake = tools
    await registry["geo_delete_layer"]("states", datastore="pg")
    deletes = [c for c in fake.calls if c["verb"] == "DELETE"]
    paths = [c["path"] for c in deletes]
    assert "workspaces/demo/layers/states.json" in paths
    assert "workspaces/demo/datastores/pg/featuretypes/states.json" in paths


async def test_create_style_content_type(tools):
    registry, fake = tools
    sld = '<StyledLayerDescriptor version="1.0.0"/>'
    await registry["geo_create_style"]("red", sld=sld)
    call = fake.last("POST")
    assert call["path"] == "styles"
    assert call["headers"]["Content-Type"] == "application/vnd.ogc.sld+xml"
    assert call["params"] == {"name": "red"}
    assert call["content"] == sld.encode("utf-8")


async def test_assign_style_default(tools):
    registry, fake = tools
    await registry["geo_assign_style_to_layer"]("states", "red", default=True)
    call = fake.last("PUT")
    assert call["json"] == {"layer": {"defaultStyle": {"name": "red"}}}


async def test_wms_get_map_builds_url(tools):
    registry, fake = tools
    result = await registry["geo_wms_get_map"]("states", "-130,24,-66,50")
    assert "request=GetMap" in result["url"]
    assert "layers=demo%3Astates" in result["url"]
    assert "geoserver/wms?" in result["url"]
    assert "saved" not in result  # download defaults to False


async def test_wms_get_map_download(tools, tmp_path):
    registry, fake = tools
    fake.settings = dataclasses.replace(fake.settings, map_output_dir=str(tmp_path))
    fake.ows_response = OwsResponse(200, "", b"PNGBYTES", {"content-type": "image/png"})
    result = await registry["geo_wms_get_map"]("states", "-130,24,-66,50",
                                               download=True, filename="m.png")
    assert (tmp_path / "m.png").read_bytes() == b"PNGBYTES"
    assert result["saved"].endswith("m.png")


async def test_wfs_get_feature_parses_geojson(tools):
    registry, fake = tools
    fc = {"type": "FeatureCollection", "features": [{"id": "states.1"}]}
    fake.ows_response = OwsResponse(200, json.dumps(fc), b"", {"content-type": "application/json"})
    result = await registry["geo_wfs_get_feature"]("states", count=10)
    assert result["features"][0]["id"] == "states.1"
    call = fake.last("OWS")
    assert call["params"]["typeNames"] == "demo:states"


async def test_wfs_transaction_delete_with_ids(tools):
    registry, fake = tools
    fake.json_responses["namespaces/demo.json"] = {
        "namespace": {"prefix": "demo", "uri": "http://demo.test"}}
    result = await registry["geo_wfs_transaction"](
        "delete", "states", feature_ids=["states.7"])
    xml = result["request_xml"]
    assert "<wfs:Delete" in xml
    assert 'typeName="demo:states"' in xml
    assert 'fid="states.7"' in xml
    post = fake.last("OWS")
    assert post["method"] == "POST"
    assert b"wfs:Transaction" in post["content"]


async def test_wfs_transaction_delete_resolves_cql(tools):
    registry, fake = tools
    fake.json_responses["namespaces/demo.json"] = {
        "namespace": {"prefix": "demo", "uri": "http://demo.test"}}
    fc = {"type": "FeatureCollection", "features": [{"id": "states.3"}]}
    fake.ows_response = OwsResponse(200, json.dumps(fc), b"", {"content-type": "application/json"})
    result = await registry["geo_wfs_transaction"](
        "delete", "states", cql_filter="STATE_NAME='Texas'")
    assert 'fid="states.3"' in result["request_xml"]


async def test_wfs_transaction_update_requires_properties(tools):
    registry, fake = tools
    fake.json_responses["namespaces/demo.json"] = {
        "namespace": {"prefix": "demo", "uri": "http://demo.test"}}
    with pytest.raises(ValueError):
        await registry["geo_wfs_transaction"]("update", "states", feature_ids=["states.1"])


async def test_wfs_transaction_raw_passthrough(tools):
    registry, fake = tools
    raw = "<wfs:Transaction>custom</wfs:Transaction>"
    result = await registry["geo_wfs_transaction"]("raw", "states", raw_xml=raw)
    assert result["request_xml"] == raw


async def test_build_web_map_writes_file(tools, tmp_path):
    registry, fake = tools
    fake.settings = dataclasses.replace(fake.settings, map_output_dir=str(tmp_path))
    fake.json_responses["workspaces/demo/featuretypes/states.json"] = {
        "featureType": {"latLonBoundingBox": {"minx": -130, "miny": 24,
                                              "maxx": -66, "maxy": 50}}}
    result = await registry["geo_build_web_map"]("states", title="States")
    saved = tmp_path / "map.html"
    assert saved.exists()
    html = saved.read_text()
    assert "tile.openstreetmap.org" in html
    assert "L.tileLayer.wms" in html
    assert "demo:states" in html
    assert result["centered_on_bbox"] is True
