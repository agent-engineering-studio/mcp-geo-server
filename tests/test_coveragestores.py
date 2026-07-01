"""Behavioural tests for the raster (coverage store / GeoTIFF) tools and the
ingest-layer raster helpers — all network-free via the FakeClient."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_geo_server import ingest


async def test_create_coveragestore_geotiff_external_put(tools):
    registry, fake = tools
    await registry["geo_create_coveragestore_geotiff"](
        "lombardia_dtm", "/data/dtm/lombardia.tif")
    put = fake.last("PUT")
    assert put["path"] == ("workspaces/demo/coveragestores/lombardia_dtm/"
                           "external.geotiff")
    assert put["params"] == {"configure": "first",
                             "coverageName": "lombardia_dtm"}
    assert put["headers"]["Content-Type"] == "text/plain"
    # A bare absolute path (GeoServer rejects file:// on some distros).
    assert put["content"] == "/data/dtm/lombardia.tif"


async def test_create_coveragestore_sets_title_on_coverage(tools):
    registry, fake = tools
    await registry["geo_create_coveragestore_geotiff"](
        "dtm5m", "file:///data/dtm5m.tif", title="DTM 5m")
    puts = [c for c in fake.calls if c["verb"] == "PUT"]
    # second PUT updates the coverage title on the store-qualified path
    assert puts[-1]["path"] == \
        "workspaces/demo/coveragestores/dtm5m/coverages/dtm5m.json"
    assert puts[-1]["json"] == {"coverage": {"title": "DTM 5m"}}


async def test_delete_coveragestore_recurse(tools):
    registry, fake = tools
    await registry["geo_delete_coveragestore"]("dtm5m", recurse=True)
    call = fake.last("DELETE")
    assert call["path"] == "workspaces/demo/coveragestores/dtm5m.json"
    assert call["params"] == {"recurse": "true"}


async def test_list_coveragestores_normalizes(tools):
    registry, fake = tools
    fake.json_responses["workspaces/demo/coveragestores.json"] = {
        "coverageStores": {"coverageStore": [{"name": "a"}, {"name": "b"}]}
    }
    result = await registry["geo_list_coveragestores"]()
    assert [c["name"] for c in result] == ["a", "b"]


def test_file_url_for_is_absolute_path():
    # Bare absolute path (not a file:// URL) — GeoServer external stores want it.
    assert ingest.file_url_for(Path("/data/dtm/lombardia.tif")) == \
        "/data/dtm/lombardia.tif"


def test_raster_name_uses_stem_at_data_root(tmp_path):
    from mcp_geo_server.bootstrap import raster_name_for
    tif = tmp_path / "HRDTM5m.tif"
    tif.touch()
    # A raster in the data-dir root keeps its stem (root folder name is noise).
    assert raster_name_for(tif, tmp_path) == "hrdtm5m"


def test_raster_name_uses_folder_in_subfolder(tmp_path):
    from mcp_geo_server.bootstrap import raster_name_for
    sub = tmp_path / "lombardia"
    sub.mkdir()
    tif = sub / "dtm.tif"
    tif.touch()
    # A dedicated subfolder carries the meaning (region-per-folder convention).
    assert raster_name_for(tif, tmp_path) == "lombardia"


async def test_publish_geotiff_skips_when_store_exists(fake_client):
    # existing set contains the store -> no PUT is issued.
    created = await ingest.publish_geotiff(
        "demo", "dtm5m", Path("/data/dtm5m.tif"), existing={"dtm5m"})
    assert created is False
    assert fake_client.last("PUT") is None


async def test_publish_geotiff_registers_external(fake_client):
    created = await ingest.publish_geotiff(
        "demo", "dtm5m", Path("/data/dtm5m.tif"), existing=set())
    assert created is True
    put = fake_client.last("PUT")
    assert put["path"].endswith("coveragestores/dtm5m/external.geotiff")
    assert put["content"] == "/data/dtm5m.tif"


def test_preprocess_geotiff_skips_when_output_exists(tmp_path):
    src = tmp_path / "HRDTM5m.tif"
    src.touch()
    out_dir = tmp_path / "proc"
    out_dir.mkdir()
    (out_dir / "hrdtm5m.tif").write_bytes(b"cog")  # pretend already built
    # Existing output -> returned as-is, gdal_translate never invoked.
    result = ingest.preprocess_geotiff(src, out_dir)
    assert result == out_dir / "hrdtm5m.tif"
    assert result.read_bytes() == b"cog"


async def test_coverage_bbox_reads_coverage_path(fake_client):
    fake_client.json_responses["workspaces/demo/coverages/dtm5m.json"] = {
        "coverage": {"latLonBoundingBox": {"minx": 8, "miny": 44,
                                           "maxx": 11, "maxy": 46}}
    }
    bbox = await ingest.coverage_bbox("demo", "dtm5m")
    assert bbox["maxy"] == 46


async def test_layer_bbox_falls_back_to_coverage(fake_client):
    # No feature type (404-empty) but a coverage exists -> coverage bbox used.
    fake_client.json_responses["workspaces/demo/coverages/dtm5m.json"] = {
        "coverage": {"latLonBoundingBox": {"minx": 8, "miny": 44,
                                           "maxx": 11, "maxy": 46}}
    }
    bbox = await ingest.layer_bbox("demo", "dtm5m")
    assert bbox["minx"] == 8
