from mcp_geo_server.ingest import PG_IDENTIFIER_MAX, sanitize


def test_short_names_unchanged() -> None:
    assert sanitize("frane_poly_valle-d-aosta_opendata") == "frane_poly_valle_d_aosta_opendata"


def test_long_name_matches_gdal_pg_laundering() -> None:
    # Table name ogr2ogr actually created for the ISPRA HPH mosaic.
    name = (
        "Mosaicatura_ISPRA_2020_aree_pericolosita_idraulica_"
        "HPH_Mosaicatura_ISPRA_2020_pericolosita_idraulica_elevata"
    )
    assert sanitize(name) == "mosaicatura_ispra_2020_aree_pericolosita_idraulica_hph_4c7713bb"


def test_long_name_fits_postgres_and_is_idempotent() -> None:
    name = sanitize("x" * 200)
    assert len(name) == PG_IDENTIFIER_MAX
    assert sanitize(name) == name
