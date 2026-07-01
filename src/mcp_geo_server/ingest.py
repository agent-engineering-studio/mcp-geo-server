"""Reusable shapefile-ingestion core.

Shared by the batch bootstrap (``bootstrap.py`` over ``/data``) and the web UI's
ad-hoc upload endpoint. One place owns the "shapefile -> PostGIS -> GeoServer
layer" pipeline: ``ogr2ogr`` load (reprojection + encoding) plus idempotent
workspace / datastore / feature-type creation via the REST tools.

External tools required at runtime: ``ogr2ogr`` (GDAL) and ``psql`` — present in
the Docker ``bootstrap`` image stage.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .client import GeoServerError, get_client
from .formatting import extract
from .tools.coveragestores import geo_create_coveragestore_geotiff
from .tools.datastores import geo_create_datastore_postgis
from .tools.featuretypes import geo_publish_featuretype
from .tools.workspaces import geo_create_workspace

logger = logging.getLogger("mcp_geo_server.ingest")


def sanitize(name: str) -> str:
    """Turn a name into a safe, unquoted Postgres/GeoServer identifier.

    Lowercases and replaces every run of non ``[a-z0-9_]`` characters with a
    single underscore, e.g. ``frane_poly_valle-d-aosta_opendata`` ->
    ``frane_poly_valle_d_aosta_opendata``.
    """
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned or "layer"


@dataclass(frozen=True)
class PgConn:
    """PostGIS connection details (reachable from the container AND GeoServer)."""

    host: str = "postgis"
    port: int = 5432
    database: str = "gis"
    user: str = "gis"
    password: str = "gis"
    schema: str = "public"

    @property
    def ogr(self) -> str:
        """The ``PG:`` connection string ogr2ogr expects."""
        return (
            f"PG:host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password} active_schema={self.schema}"
        )

    def psql_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for ``psql`` (it reads the standard PG* variables)."""
        env = dict(base or {})
        env.update(
            PGHOST=self.host,
            PGPORT=str(self.port),
            PGDATABASE=self.database,
            PGUSER=self.user,
            PGPASSWORD=self.password,
        )
        return env


# --------------------------------------------------------------------------
# GeoServer catalog helpers (idempotent)
# --------------------------------------------------------------------------
async def resource_exists(path: str) -> bool:
    """Return True if a REST resource exists (GET succeeds), False on 404."""
    client = get_client()
    try:
        await client.get_json(path)
        return True
    except GeoServerError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


async def ensure_workspace(workspace: str) -> bool:
    """Create the workspace if missing. Returns True if it was created."""
    if await resource_exists(f"workspaces/{workspace}.json"):
        return False
    await geo_create_workspace(workspace)
    logger.info("Created workspace '%s'.", workspace)
    return True


async def ensure_datastore(workspace: str, datastore: str, conn: PgConn) -> bool:
    """Create the PostGIS datastore if missing. Returns True if it was created."""
    path = f"workspaces/{workspace}/datastores/{datastore}.json"
    if await resource_exists(path):
        return False
    await geo_create_datastore_postgis(
        name=datastore,
        host=conn.host,
        database=conn.database,
        user=conn.user,
        password=conn.password,
        port=conn.port,
        db_schema=conn.schema,
        workspace=workspace,
    )
    logger.info("Created PostGIS datastore '%s' -> %s/%s.",
                datastore, conn.host, conn.database)
    return True


async def list_featuretype_names(workspace: str, datastore: str) -> set[str]:
    """Names of feature types already published in a datastore (empty if none).

    Used as the idempotency check instead of a per-layer GET: a missing layer
    GET returns 404, which GeoServer logs as an ERROR with a full stack trace —
    listing once avoids that noise on a fresh catalog.
    """
    client = get_client()
    try:
        data = await client.get_json(
            f"workspaces/{workspace}/datastores/{datastore}/featuretypes.json")
    except GeoServerError:
        return set()
    return {ft.get("name") for ft in extract(data, "featureTypes", "featureType")
            if isinstance(ft, dict) and ft.get("name")}


async def publish(workspace: str, datastore: str, table: str, *,
                  srs: str, title: str | None = None,
                  existing: set[str] | None = None) -> bool:
    """Publish a PostGIS table as a layer. Returns True if newly published.

    Pass ``existing`` (a set of already-published names, e.g. from
    :func:`list_featuretype_names`) to skip the per-layer GET existence probe —
    that probe 404s on a fresh catalog and GeoServer logs each 404 as an ERROR.
    """
    if existing is not None:
        if table in existing:
            return False
    else:
        path = (f"workspaces/{workspace}/datastores/{datastore}"
                f"/featuretypes/{table}.json")
        if await resource_exists(path):
            return False
    await geo_publish_featuretype(
        datastore=datastore, table=table, workspace=workspace,
        srs=srs, title=title,
    )
    logger.info("Published layer '%s:%s'.", workspace, table)
    return True


async def featuretype_bbox(workspace: str, name: str) -> dict | None:
    """Return a published layer's lat/lon bounding box, or None.

    Uses the datastore-less feature-type path so callers only need the
    workspace + layer name.
    """
    client = get_client()
    try:
        data = await client.get_json(f"workspaces/{workspace}/featuretypes/{name}.json")
    except GeoServerError:
        return None
    ft = data.get("featureType", {}) if isinstance(data, dict) else {}
    return ft.get("latLonBoundingBox")


async def coverage_bbox(workspace: str, name: str) -> dict | None:
    """Return a published *coverage* (raster) lat/lon bounding box, or None.

    The raster counterpart of :func:`featuretype_bbox` — coverages live under a
    different REST path (``coverages`` not ``featuretypes``).
    """
    client = get_client()
    try:
        data = await client.get_json(f"workspaces/{workspace}/coverages/{name}.json")
    except GeoServerError:
        return None
    cov = data.get("coverage", {}) if isinstance(data, dict) else {}
    return cov.get("latLonBoundingBox")


async def layer_bbox(workspace: str, name: str) -> dict | None:
    """Lat/lon bbox of a layer regardless of kind (vector OR raster).

    Tries the feature-type path first, then falls back to the coverage path, so
    UI callers do not need to know whether a layer is a shapefile or a GeoTIFF.
    """
    return (await featuretype_bbox(workspace, name)
            or await coverage_bbox(workspace, name))


# --------------------------------------------------------------------------
# Raster (GeoTIFF / DTM) publish — external coverage store, zero-copy
# --------------------------------------------------------------------------
def file_url_for(path: Path) -> str:
    """Filesystem location GeoServer uses to read a raster on its own FS.

    A plain **absolute path**, not a ``file://`` URL: GeoServer's external
    coverage-store endpoint rejects ``file://`` URLs on some distributions
    (kartoza 2.28 returns HTTP 400 "Failed to locate the input file"), while the
    bare path is accepted everywhere. Both the init container and GeoServer
    mount the data dir at the same path (``/data``), so the absolute path is
    identical on both sides — no translation needed.
    """
    return str(path.resolve())


async def list_coveragestore_names(workspace: str) -> set[str]:
    """Names of coverage stores already present in a workspace (empty if none).

    The raster idempotency check, mirroring
    :func:`list_featuretype_names` for vectors.
    """
    client = get_client()
    try:
        data = await client.get_json(f"workspaces/{workspace}/coveragestores.json")
    except GeoServerError:
        return set()
    return {cs.get("name")
            for cs in extract(data, "coverageStores", "coverageStore")
            if isinstance(cs, dict) and cs.get("name")}


def preprocess_geotiff(src: Path, out_dir: Path, *, force: bool = False,
                       resampling: str = "average",
                       compress: str = "DEFLATE") -> Path:
    """Rewrite a GeoTIFF as a Cloud-Optimized GeoTIFF (tiled + overviews).

    A large DTM (national 5 m coverage = tens of GB) served without overviews
    makes WMS painfully slow at small scales: GeoServer must read the full-res
    tiles for a zoomed-out view. A COG carries internal overviews so GeoServer
    reads a downsampled level instead. This writes the COG to ``out_dir`` (a
    writable location GeoServer can also read — the source ``/data`` mount is
    read-only) and returns its path. Idempotent: skips when the output exists.

    Requires ``gdal_translate`` (GDAL) on PATH — present in the Docker bootstrap
    image. ``resampling`` is the overview resampling (``average`` suits
    continuous elevation data).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sanitize(src.stem)}.tif"
    if not force and out.exists():
        logger.info("Processed COG '%s' already exists — skip.", out.name)
        return out
    cmd = [
        "gdal_translate", str(src), str(out),
        "-of", "COG",
        "-co", f"COMPRESS={compress}",
        "-co", "BIGTIFF=YES",
        "-co", "NUM_THREADS=ALL_CPUS",
        "-co", f"RESAMPLING={resampling}",
    ]
    logger.info("Building COG %s -> %s (overviews, %s)…", src.name, out.name,
                resampling)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # gdal_translate can exit 0 even when it fails to create the file (e.g. a
    # permission error on the output dir prints "ERROR 4" but returns 0), so
    # verify the output actually exists rather than trusting the return code.
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            f"gdal_translate (COG) failed for {src} (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout).strip()}")
    return out


async def publish_geotiff(workspace: str, store: str, tif: Path, *,
                          title: str | None = None,
                          existing: set[str] | None = None) -> bool:
    """Register a GeoTIFF as an external coverage store + publish it. True if new.

    Idempotent: skips when the coverage store already exists. The coverage
    (published layer) is named after the store, so the layer name is
    deterministic and matches the vector naming convention.
    """
    if existing is not None:
        if store in existing:
            return False
    else:
        path = f"workspaces/{workspace}/coveragestores/{store}.json"
        if await resource_exists(path):
            return False
    await geo_create_coveragestore_geotiff(
        name=store, file_url=file_url_for(tif), workspace=workspace,
        coverage_name=store, title=title,
    )
    logger.info("Published coverage '%s:%s' from %s.", workspace, store, tif.name)
    return True


# --------------------------------------------------------------------------
# PostGIS load (ogr2ogr) + idempotency probe (psql)
# --------------------------------------------------------------------------
def table_exists(conn: PgConn, table: str) -> bool:
    """True if ``table`` already exists in PostGIS.

    Existence (not row count) is the right idempotency signal: some shapefiles
    are empty (0 features), so a row-count check would re-load them every run.
    Once the table is created — even empty — there is nothing to re-load.
    """
    sql = f"SELECT to_regclass('{conn.schema}.{table}') IS NOT NULL;"
    proc = subprocess.run(
        ["psql", "-tAqc", sql],
        env=conn.psql_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # If the probe itself failed, assume absent so we attempt the load.
        return False
    return proc.stdout.strip() == "t"


def load_shapefile(shp: Path, table: str, *, conn: PgConn,
                   target_srs: str = "EPSG:4326", source_srs: str = "",
                   shape_encoding: str = "ISO-8859-1", force: bool = False) -> bool:
    """Load one shapefile into PostGIS with ogr2ogr. Returns True if loaded.

    Skips (returns False) when the table already exists and ``force`` is False.
    Raises ``RuntimeError`` on an ogr2ogr failure.
    """
    if not force and table_exists(conn, table):
        logger.info("Table '%s' already exists — skip load.", table)
        return False

    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        conn.ogr,
        str(shp),
        "-nln", table,
        "-nlt", "PROMOTE_TO_MULTI",
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "FID=fid",
        "-lco", "PRECISION=NO",
        "--config", "PG_USE_COPY", "YES",
        "-skipfailures",
    ]
    if shape_encoding:
        cmd += ["--config", "SHAPE_ENCODING", shape_encoding]
    if force:
        cmd.append("-overwrite")

    if shp.with_suffix(".prj").exists():
        cmd += ["-t_srs", target_srs]
    elif source_srs:
        cmd += ["-s_srs", source_srs, "-t_srs", target_srs]
    else:
        logger.warning(
            "'%s' has no .prj and no source SRS given — loading without "
            "reprojection; declared SRS may be wrong.", shp.name)

    logger.info("Loading %s -> table '%s'%s", shp.name, table,
                " (force/overwrite)" if force else "")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ogr2ogr failed for {shp} (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return True
