"""Idempotent data bootstrap for the Docker stack.

Scans ``GEO_INIT_DATA_DIR`` (``/data`` in the container) for **every** shapefile,
loads each one into PostGIS and publishes it as a GeoServer layer. It is fully
*transversal*: it makes no assumption about how the folders are named or nested —
whatever ``*.shp`` it finds, it loads.

The actual "shapefile -> PostGIS -> layer" pipeline lives in :mod:`ingest`, which
the web UI's upload endpoint shares. This module is just the batch driver:
GeoServer-readiness wait, discovery, naming, and the loop.

Idempotency (delegated to :mod:`ingest`):
    * workspace / datastore created only when missing;
    * a table that already exists is skipped (unless ``GEO_INIT_FORCE``);
    * a feature type already published is skipped.

Run as a one-shot container (compose service ``geo-init``)::

    python -m mcp_geo_server.bootstrap
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from .client import GeoServerError, get_client
from .config import get_settings
from .ingest import (
    PgConn,
    ensure_datastore,
    ensure_workspace,
    list_coveragestore_names,
    list_featuretype_names,
    load_shapefile,
    preprocess_geotiff,
    publish,
    publish_geotiff,
    sanitize,
)

logger = logging.getLogger("mcp_geo_server.bootstrap")

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in _TRUE


class BootstrapConfig:
    """Configuration for the init job, all from the environment."""

    def __init__(self) -> None:
        self.enable = _env_bool("GEO_INIT_ENABLE", True)
        self.data_dir = Path(os.environ.get("GEO_INIT_DATA_DIR") or "/data")
        self.workspace = (os.environ.get("GEO_INIT_WORKSPACE") or "ispra").strip()
        self.datastore = (os.environ.get("GEO_INIT_DATASTORE") or "ispra_pg").strip()
        self.target_srs = (os.environ.get("GEO_INIT_TARGET_SRS") or "EPSG:4326").strip()
        # Optional fallback source SRS for shapefiles that ship without a .prj.
        self.source_srs = (os.environ.get("GEO_INIT_SOURCE_SRS") or "").strip()
        self.force = _env_bool("GEO_INIT_FORCE", False)
        # Raster (GeoTIFF / DTM) publishing. Rasters are registered as external
        # coverage stores (zero-copy) in this workspace (defaults to the same as
        # the vectors, so everything shows up under one workspace in the UI).
        self.raster_enable = _env_bool("GEO_INIT_RASTER_ENABLE", True)
        self.raster_workspace = (os.environ.get("GEO_INIT_RASTER_WORKSPACE")
                                 or self.workspace).strip()
        # Optional Cloud-Optimized-GeoTIFF preprocessing (adds overviews so a
        # large DTM serves fast over WMS). Off by default: it rewrites each
        # raster (time + disk), which is only worthwhile for big rasters. The
        # output dir must be writable AND readable by GeoServer (a shared volume,
        # since /data is mounted read-only).
        self.raster_preprocess = _env_bool("GEO_INIT_RASTER_PREPROCESS", False)
        self.raster_processed_dir = Path(
            os.environ.get("GEO_INIT_RASTER_PROCESSED_DIR") or "/data-processed")
        # Apply the thematic SLD styles after publishing.
        self.styles = _env_bool("GEO_INIT_STYLES", True)
        # Shapefile attribute encoding. These ISPRA datasets ship a .cst file
        # (ISO-8859-1) which GDAL ignores (it only reads .cpg), so force it here
        # to keep Italian accents intact. Set empty to let GDAL auto-detect.
        self.shape_encoding = (os.environ.get("GEO_INIT_SHAPE_ENCODING")
                               if "GEO_INIT_SHAPE_ENCODING" in os.environ
                               else "ISO-8859-1").strip()
        # GeoServer-readiness polling.
        self.ready_timeout = float(os.environ.get("GEO_INIT_READY_TIMEOUT") or 180)
        self.ready_interval = float(os.environ.get("GEO_INIT_READY_INTERVAL") or 3)
        self.conn = PgConn(
            host=(os.environ.get("POSTGIS_HOST") or "postgis").strip(),
            port=int(os.environ.get("POSTGIS_PORT") or 5432),
            database=(os.environ.get("POSTGIS_DB") or "gis").strip(),
            user=(os.environ.get("POSTGIS_USER") or "gis").strip(),
            password=(os.environ.get("POSTGIS_PASSWORD") or "gis").strip(),
            schema=(os.environ.get("POSTGIS_SCHEMA") or "public").strip(),
        )


async def wait_for_geoserver(cfg: BootstrapConfig) -> None:
    """Block until the GeoServer REST API answers, or raise on timeout."""
    client = get_client()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.ready_timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            await client.get_json("about/version.json")
            logger.info("GeoServer REST is ready (after %d attempt(s)).", attempt)
            return
        except GeoServerError as exc:
            if loop.time() >= deadline:
                raise GeoServerError(
                    f"GeoServer not ready after {cfg.ready_timeout:.0f}s: {exc}"
                ) from exc
            logger.info("Waiting for GeoServer REST… (attempt %d)", attempt)
            await asyncio.sleep(cfg.ready_interval)


def discover_shapefiles(data_dir: Path) -> list[Path]:
    """Return every ``*.shp`` under ``data_dir`` (recursively), sorted."""
    return sorted(p for p in data_dir.rglob("*.shp") if p.is_file())


def discover_rasters(data_dir: Path) -> list[Path]:
    """Return every GeoTIFF (``*.tif`` / ``*.tiff``) under ``data_dir``, sorted.

    Case-insensitive, so ``.TIF`` from Windows-exported DTMs is found too.
    """
    return sorted(p for p in data_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in (".tif", ".tiff"))


def layer_name_for(shp: Path) -> str:
    """Derive a unique, sanitized layer/table name for a shapefile.

    The meaningful identifier lives in the *folder* name (e.g. the region is in
    ``frane_line_molise_opendata/``, while the file is just
    ``frane_line_opendata.shp`` and repeats across regions). So the immediate
    parent folder is the base name. When a folder holds more than one shapefile
    (e.g. ``Limiti01012023_g/`` split into Reg/Prov/Com subfolders each with one
    shp — but also any flat multi-shp folder) the file stem is appended to keep
    names unique.
    """
    siblings = [p for p in shp.parent.glob("*.shp") if p.is_file()]
    base = shp.parent.name if len(siblings) == 1 else f"{shp.parent.name}_{shp.stem}"
    return sanitize(base)


def raster_name_for(tif: Path, data_dir: Path | None = None) -> str:
    """Derive a unique, sanitized coverage/layer name for a GeoTIFF.

    A GeoTIFF is self-contained (no sidecar bundle like a shapefile), so its own
    file stem is usually the meaningful name (e.g. ``HRDTM5m.tif`` -> ``hrdtm5m``).
    Only when it sits in a dedicated *subfolder* do we fold the folder name in —
    that mirrors the region-per-folder convention used for the vector data
    (``.../lombardia/dtm.tif`` -> ``lombardia_dtm``). A raster in the data-dir
    root keeps just its stem (the root folder name, e.g. ``data``, is meaningless).
    """
    if data_dir is not None and tif.parent.resolve() == data_dir.resolve():
        return sanitize(tif.stem)
    siblings = [p for p in tif.parent.glob("*")
                if p.is_file() and p.suffix.lower() in (".tif", ".tiff")]
    base = tif.parent.name if len(siblings) == 1 else f"{tif.parent.name}_{tif.stem}"
    return sanitize(base)


async def run() -> int:
    cfg = BootstrapConfig()
    if not cfg.enable:
        logger.info("GEO_INIT_ENABLE is false — bootstrap disabled, nothing to do.")
        return 0

    # Fail fast if the external tools are missing from the image.
    for tool in ("ogr2ogr", "psql"):
        if shutil.which(tool) is None:
            logger.error("Required tool '%s' not found on PATH.", tool)
            return 2

    if not cfg.data_dir.is_dir():
        logger.warning("Data dir '%s' does not exist — nothing to load.", cfg.data_dir)
        return 0

    shapefiles = discover_shapefiles(cfg.data_dir)
    if not shapefiles:
        logger.warning("No shapefiles (*.shp) found under '%s' — nothing to load.",
                       cfg.data_dir)
        # Still ensure workspace/datastore so the stack is usable.

    await wait_for_geoserver(cfg)
    await ensure_workspace(cfg.workspace)
    await ensure_datastore(cfg.workspace, cfg.datastore, cfg.conn)
    # Pre-fetch published layer names once (avoids a per-layer GET that would
    # 404 on a fresh catalog and spam GeoServer's log with ERROR stack traces).
    existing = await list_featuretype_names(cfg.workspace, cfg.datastore)

    loaded = published = failed = 0
    seen: dict[str, Path] = {}
    for shp in shapefiles:
        table = layer_name_for(shp)
        if table in seen:
            logger.warning("Duplicate layer name '%s' from %s (already from %s) — "
                           "skipping.", table, shp, seen[table])
            continue
        seen[table] = shp
        logger.info("• %s -> %s", shp.relative_to(cfg.data_dir), table)
        try:
            if load_shapefile(shp, table, conn=cfg.conn, target_srs=cfg.target_srs,
                              source_srs=cfg.source_srs,
                              shape_encoding=cfg.shape_encoding, force=cfg.force):
                loaded += 1
            if await publish(cfg.workspace, cfg.datastore, table,
                             srs=cfg.target_srs, title=shp.parent.name,
                             existing=existing):
                existing.add(table)
                published += 1
        except Exception as exc:  # noqa: BLE001 — keep going on a bad dataset
            failed += 1
            logger.error("  FAILED %s: %s", shp.name, exc)

    # ---- rasters (GeoTIFF / DTM) -> external coverage stores --------------
    rasters = discover_rasters(cfg.data_dir) if cfg.raster_enable else []
    if rasters:
        await ensure_workspace(cfg.raster_workspace)
        existing_cov = await list_coveragestore_names(cfg.raster_workspace)
        seen_cov: dict[str, Path] = {}
        for tif in rasters:
            store = raster_name_for(tif, cfg.data_dir)
            if store in seen_cov:
                logger.warning("Duplicate coverage name '%s' from %s (already "
                               "from %s) — skipping.", store, tif, seen_cov[store])
                continue
            seen_cov[store] = tif
            logger.info("• %s -> coverage %s", tif.relative_to(cfg.data_dir), store)
            try:
                source = tif
                if cfg.raster_preprocess and store not in existing_cov:
                    source = preprocess_geotiff(tif, cfg.raster_processed_dir,
                                                force=cfg.force)
                if await publish_geotiff(cfg.raster_workspace, store, source,
                                         title=tif.stem,
                                         existing=existing_cov):
                    existing_cov.add(store)
                    published += 1
            except Exception as exc:  # noqa: BLE001 — keep going on a bad raster
                failed += 1
                logger.error("  FAILED %s: %s", tif.name, exc)

    if cfg.styles:
        from .styling import apply as apply_styles
        # Style each workspace we published into (vectors + rasters may share one
        # workspace, or the raster workspace may differ).
        workspaces = [cfg.workspace]
        if cfg.raster_enable and cfg.raster_workspace not in workspaces:
            workspaces.append(cfg.raster_workspace)
        for ws in workspaces:
            try:
                styled = await apply_styles(ws)
                logger.info("Thematic styles assigned to %d layer(s) in '%s'.",
                            styled, ws)
            except Exception as exc:  # noqa: BLE001 — styling is non-fatal
                logger.error("Thematic styling failed for '%s': %s", ws, exc)

    logger.info(
        "Bootstrap done: %d shapefile(s) + %d raster(s) found, %d loaded, "
        "%d published, %d failed.",
        len(shapefiles), len(rasters), loaded, published, failed)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GEO_INIT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Validate core GeoServer settings early (raises with a clear message).
    get_settings()
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
