import pytest

from mcp_geo_server.tools.ogc import (
    build_wfst_xml,
    qualified_name,
    wfs_getfeature_params,
    wms_getmap_params,
)


def test_qualified_name():
    assert qualified_name("states", "topp") == "topp:states"
    assert qualified_name("states", None) == "states"
    assert qualified_name("topp:states", "other") == "topp:states"


def test_wms_getmap_params():
    p = wms_getmap_params("topp:states", "-130,24,-66,50", 768, 512,
                          "EPSG:4326", "image/png", "", True)
    assert p["service"] == "WMS"
    assert p["version"] == "1.1.1"
    assert p["request"] == "GetMap"
    assert p["layers"] == "topp:states"
    assert p["transparent"] == "true"


def test_wfs_getfeature_params_json_output():
    p = wfs_getfeature_params("topp:states", 50, None, "STATE_NAME='Texas'", None, None)
    assert p["version"] == "2.0.0"
    assert p["outputFormat"] == "application/json"
    assert p["typeNames"] == "topp:states"
    assert p["count"] == 50
    assert p["cql_filter"] == "STATE_NAME='Texas'"


def test_wfs_getfeature_rejects_bbox_and_cql():
    with pytest.raises(ValueError):
        wfs_getfeature_params("topp:states", 50, "-1,-1,1,1", "x=1", None, None)


def test_build_wfst_delete_xml():
    xml = build_wfst_xml("delete", "states", "topp", "http://www.openplans.org/topp",
                         feature_ids=["states.1", "states.2"])
    assert "wfs:Transaction" in xml
    assert 'version="1.1.0"' in xml
    assert "<wfs:Delete" in xml
    assert 'typeName="topp:states"' in xml
    assert 'fid="states.1"' in xml
    assert 'fid="states.2"' in xml
    assert 'xmlns:topp="http://www.openplans.org/topp"' in xml


def test_build_wfst_update_xml_escapes_values():
    xml = build_wfst_xml("update", "states", "topp", "http://topp",
                         feature_ids=["states.1"],
                         properties={"name": "A & B"})
    assert "<wfs:Update" in xml
    assert "<wfs:Property>" in xml
    assert "<wfs:Name>name</wfs:Name>" in xml
    assert "A &amp; B" in xml
    assert 'fid="states.1"' in xml
