"""Terrain-analysis tools: enrich vector layers with DTM-derived metrics."""

from __future__ import annotations

from ..enrich import enrich_layer


async def geo_enrich_from_dtm(
    layer: str,
    metrics: list[str] | None = None,
    dtm: str | None = None,
    bbox: str | None = None,
    limit: int = 500,
    include_features: bool = False,
) -> dict:
    """Enrich a vector layer with terrain metrics sampled from a DTM.

    Computes ``quota`` (elevation), ``slope`` (°), ``aspect`` (°) and
    ``curvature`` for the features of ``layer`` from a DTM raster — points get a
    value per metric, lines/polygons get min/max/mean. Reads the DTM file
    directly (not WMS); ``dtm`` is a raster **coverage layer name** (auto-picked
    from the workspace if omitted). ``metrics`` selects a subset; ``bbox``
    (``minx,miny,maxx,maxy`` in EPSG:4326) and ``limit`` bound large layers.

    Returns a dataset-level summary (min/max/mean per metric) + count; set
    ``include_features=True`` to also get every per-feature value (can be large).
    """
    result = await enrich_layer(layer, metrics=metrics, dtm=dtm, bbox=bbox,
                                limit=limit)
    if include_features:
        return result
    # LLM-friendly by default: drop the (potentially huge) per-feature list,
    # keep a small sample alongside the summary.
    sample = (result.get("features") or [])[:5]
    return {k: v for k, v in result.items() if k != "features"} | {"sample": sample}
