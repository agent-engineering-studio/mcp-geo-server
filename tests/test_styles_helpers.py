import pytest

from mcp_geo_server.tools.styles import load_sld, styles_path

SLD = '<StyledLayerDescriptor version="1.0.0"/>'


def test_styles_path_global_and_workspace():
    assert styles_path(None) == "styles"
    assert styles_path("demo") == "workspaces/demo/styles"


def test_load_sld_inline():
    assert load_sld(SLD, None) == SLD


def test_load_sld_from_file(tmp_path):
    f = tmp_path / "style.sld"
    f.write_text(SLD, encoding="utf-8")
    assert load_sld(None, str(f)) == SLD


def test_load_sld_requires_one_source():
    with pytest.raises(ValueError):
        load_sld(None, None)


def test_load_sld_rejects_both():
    with pytest.raises(ValueError):
        load_sld(SLD, "file.sld")
