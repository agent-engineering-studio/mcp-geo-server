"""On-demand terrain enrichment from a DTM (domain-agnostic).

Given vector features and a DTM GeoTIFF, compute per-feature terrain metrics —
elevation (``quota``), ``slope``, ``aspect``, ``curvature`` — by reading the
raster window over the features' extent and sampling it. No database writes; it
works for ANY point / line / polygon layer, not just the ISPRA landslide data.

The DTM FILE is read directly (analysis reads the raster, not WMS). For a large
window the read is decimated through the COG overviews, so a whole-region
request stays fast (metrics are then computed at that coarser scale).

Requires ``rasterio`` + ``rasterstats`` (Docker ``bootstrap`` image).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("mcp_geo_server.enrich")

# Metric name -> human label. `quota` is elevation; the rest are derived.
METRICS: dict[str, str] = {
    "quota": "quota (m)",
    "slope": "pendenza (°)",
    "aspect": "esposizione (°)",
    "curvature": "curvatura",
}

# Cap the working array so a large area is read decimated (via overviews)
# instead of blowing up memory. ~2048² cells is plenty for terrain statistics.
_MAX_DIM = 2048


async def coverage_file_path(qualified: str) -> str | None:
    """Filesystem path of the GeoTIFF backing a published coverage layer.

    The DTM is referenced by its coverage layer name (``ws:name``), not a
    hardcoded path: this reads the coverage store's ``url`` from GeoServer and
    strips the ``file:`` scheme. The app and GeoServer mount the data dirs at
    the same paths, so the resolved path is valid here too. Returns None if the
    layer is not a (file-backed) coverage.
    """
    from .client import GeoServerError, get_client

    ws, _, name = (qualified.partition(":") if ":" in qualified
                   else ("", "", qualified))
    client = get_client()
    try:
        cov = await client.get_json(f"workspaces/{ws}/coverages/{name}.json")
        store_ref = (cov.get("coverage", {}).get("store", {}) or {}).get("name", "")
        store = store_ref.split(":")[-1] or name
        cs = await client.get_json(f"workspaces/{ws}/coveragestores/{store}.json")
        url = (cs.get("coverageStore", {}) or {}).get("url", "")
    except GeoServerError:
        return None
    if not url:
        return None
    # e.g. "file:/data-processed/hrdtm5m.tif" -> "/data-processed/hrdtm5m.tif"
    return url.split("file:", 1)[-1] if url.startswith("file:") else url


def normalize_metrics(metrics: Iterable[str] | None) -> list[str]:
    """Validate/normalize requested metric names (default: all)."""
    if not metrics:
        return list(METRICS)
    out = [m for m in metrics if m in METRICS]
    return out or list(METRICS)


def _derive(elev, xres: float, yres: float, metrics: list[str]) -> dict:
    """Compute requested derived-metric arrays from an elevation array.

    ``elev`` is a float array (NaN = nodata); ``xres``/``yres`` are the cell
    sizes in the DTM's (projected, metre) CRS. Slope in degrees; aspect in
    degrees (0–360, 0 = North, clockwise); curvature = Laplacian of elevation.
    """
    import numpy as np

    out: dict = {}
    need_grad = any(m in metrics for m in ("slope", "aspect", "curvature"))
    if not need_grad:
        return out
    # np.gradient axis order = (rows=Y, cols=X). Rows increase southward, so the
    # geographic north gradient is the negative of the row gradient.
    dz_drow, dz_dcol = np.gradient(elev, yres, xres)
    dz_dx = dz_dcol
    dz_dy = -dz_drow
    if "slope" in metrics:
        out["slope"] = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    if "aspect" in metrics:
        a = np.degrees(np.arctan2(dz_dy, -dz_dx))
        out["aspect"] = np.where(a < 0, a + 360.0, a)
    if "curvature" in metrics:
        d2z_drow, _ = np.gradient(dz_drow, yres, xres)
        _, d2z_dcol = np.gradient(dz_dcol, yres, xres)
        out["curvature"] = d2z_dcol + d2z_drow  # Laplacian
    return out


def enrich_features(features: list[dict], *, dtm_path: str,
                    metrics: Iterable[str] | None = None,
                    src_crs: str = "EPSG:4326") -> list[dict]:
    """Per-feature terrain metrics for GeoJSON-like ``features``.

    Each feature is ``{"geometry": <GeoJSON>, "id"?: ...}`` in ``src_crs``
    (default EPSG:4326). Returns one dict per feature: point geometries get a
    single value per metric, polygons/lines get ``{min,max,mean}``. Values are
    ``None`` where the geometry falls on nodata / outside the DTM.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform_geom
    from rasterio.windows import from_bounds
    from rasterio.transform import rowcol
    from rasterstats import zonal_stats
    from shapely.geometry import shape

    wanted = normalize_metrics(metrics)
    if not features:
        return []

    with rasterio.open(dtm_path) as ds:
        dst_crs = ds.crs
        # Reproject geometries to the DTM CRS once.
        geoms_dst = [transform_geom(src_crs, dst_crs, f["geometry"]) for f in features]
        shapes = [shape(g) for g in geoms_dst]
        minx = min(s.bounds[0] for s in shapes)
        miny = min(s.bounds[1] for s in shapes)
        maxx = max(s.bounds[2] for s in shapes)
        maxy = max(s.bounds[3] for s in shapes)
        xres, yres = abs(ds.res[0]), abs(ds.res[1])
        pad = xres * 2
        win = from_bounds(minx - pad, miny - pad, maxx + pad, maxy + pad,
                          ds.transform)
        # Decimate large windows through the overviews.
        wpx, hpx = int(round(win.width)), int(round(win.height))
        scale = max(1.0, wpx / _MAX_DIM, hpx / _MAX_DIM)
        out_w, out_h = max(1, int(wpx / scale)), max(1, int(hpx / scale))
        elev = ds.read(1, window=win, out_shape=(out_h, out_w),
                       boundless=True, fill_value=ds.nodata if ds.nodata is not None else -9999).astype("float64")
        wt = ds.window_transform(win)
        # Scale the transform to the decimated array.
        eff = wt * rasterio.Affine.scale(win.width / out_w, win.height / out_h)
        cell_x = abs(eff.a)
        cell_y = abs(eff.e)
        nodata = ds.nodata if ds.nodata is not None else -9999.0
        elev[elev == nodata] = np.nan

    derived = _derive(elev, cell_x, cell_y, wanted)
    arrays = {"quota": elev, **derived}

    def _pt(arr, x, y):
        try:
            r, c = rowcol(eff, x, y)
        except Exception:  # noqa: BLE001
            return None
        if 0 <= r < arr.shape[0] and 0 <= c < arr.shape[1]:
            v = arr[r, c]
            return None if np.isnan(v) else round(float(v), 3)
        return None

    def _zonal(arr, geom):
        safe = np.where(np.isnan(arr), -9999.0, arr)
        try:
            st = zonal_stats(geom, safe, affine=eff, nodata=-9999.0,
                             stats=["min", "max", "mean"], all_touched=True)[0]
        except Exception:  # noqa: BLE001
            return None
        if not st or st.get("mean") is None:
            return None
        return {k: round(float(v), 3) for k, v in st.items() if v is not None}

    results = []
    for feat, shp, gdst in zip(features, shapes, geoms_dst):
        is_point = shp.geom_type in ("Point", "MultiPoint")
        rep = shp.representative_point()
        entry: dict[str, Any] = {"id": feat.get("id")}
        for m in wanted:
            arr = arrays.get(m)
            if arr is None:
                continue
            entry[m] = _pt(arr, rep.x, rep.y) if is_point else _zonal(arr, gdst)
        results.append(entry)
    return results


def summarize(results: list[dict], metrics: Iterable[str] | None = None) -> dict:
    """Aggregate per-feature results into dataset-level stats per metric."""
    import statistics
    wanted = normalize_metrics(metrics)
    agg: dict = {"count": len(results)}
    for m in wanted:
        vals: list[float] = []
        for r in results:
            v = r.get(m)
            if isinstance(v, dict):
                v = v.get("mean")
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if vals:
            agg[m] = {"min": round(min(vals), 3), "max": round(max(vals), 3),
                      "mean": round(statistics.fmean(vals), 3), "n": len(vals)}
    return agg
