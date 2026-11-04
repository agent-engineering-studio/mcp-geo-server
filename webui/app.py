"""Thin FastAPI backend for the test UI.

It imports the SAME core (``GeoServerClient`` + tool helpers) the MCP server
uses — it does NOT go through an MCP client. Each endpoint maps to one core
operation and returns JSON.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp_geo_server import cql as _cql
from mcp_geo_server import places as _places
from mcp_geo_server import resolver as _resolver
from mcp_geo_server import retrieval as _retrieval
from mcp_geo_server.catalog import build_catalog, parse_wms_capabilities
from mcp_geo_server.styling import load_config as _load_style_config
from mcp_geo_server.client import GeoServerError, get_client
from mcp_geo_server.config import get_settings
from mcp_geo_server.formatting import extract
from mcp_geo_server.ingest import (
    PgConn,
    ensure_datastore,
    ensure_workspace,
    featuretype_bbox,
    layer_bbox,
    load_shapefile,
    publish,
    sanitize,
)
from mcp_geo_server.tools.datastores import _connection_parameters
from mcp_geo_server.tools.ogc import qualified_name, wfs_getfeature_params
from mcp_geo_server.tools.styles import SLD_CONTENT_TYPE, styles_path
from mcp_geo_server.tools.map import render_map, _bounds_from_bbox

from webui.security import BasicAuthMiddleware, RateLimitMiddleware

_STATIC = Path(__file__).resolve().parent / "static"

# Target for UI shapefile uploads (own workspace/datastore, separate from the
# curated `ispra` data).
_UPLOAD_WS = (os.environ.get("GEO_UPLOAD_WORKSPACE") or "uploads").strip()
# Let the LLM write title/abstract/keywords for an uploaded layer, so the chat
# can find it by subject instead of by file name (mcp_geo_server.metadata).
_UPLOAD_AI_METADATA = (os.environ.get("GEO_UPLOAD_AI_METADATA") or "false"
                       ).strip().lower() in {"1", "true", "yes", "on"}
_UPLOAD_DS = (os.environ.get("GEO_UPLOAD_DATASTORE") or "uploads_pg").strip()
_UPLOAD_PG = PgConn(
    host=(os.environ.get("POSTGIS_HOST") or "postgis").strip(),
    port=int(os.environ.get("POSTGIS_PORT") or 5432),
    database=(os.environ.get("POSTGIS_DB") or "gis").strip(),
    user=(os.environ.get("POSTGIS_USER") or "gis").strip(),
    password=(os.environ.get("POSTGIS_PASSWORD") or "gis").strip(),
    schema=(os.environ.get("POSTGIS_SCHEMA") or "public").strip(),
)

from mcp_geo_server import telemetry as _telemetry

_telemetry.setup("mcp-geo-server-webui")
app = FastAPI(title="mcp-geo-server test UI")
# Starlette runs the LAST-added middleware first: auth, then the rate limit, so
# anonymous traffic is rejected before it can consume someone's quota.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BasicAuthMiddleware)

# Lazily-built LLM resolver (no GeoServer tools — it only maps NL -> layer names).
# Resolver tiers (primary model + optional fallback) and the catalog
# retriever, built lazily on the first request.
_RESOLVER_TIERS: list | None = None
_RETRIEVER = _retrieval.retriever_from_env()
_MIN_CONFIDENCE = _resolver.min_confidence_from_env()
_RESOLVER_INSTRUCTIONS = _resolver.INSTRUCTIONS
# Schema-constrained replies (GEO_RESOLVER_STRUCTURED=false to turn off, e.g.
# for a backend that rejects JSON-schema output; there is also an automatic
# one-shot fallback when the constrained call fails).
_RESOLVER_STRUCTURED = (os.environ.get("GEO_RESOLVER_STRUCTURED") or "true"
                        ).strip().lower() not in {"0", "false", "no", "off"}


def _get_resolver_tiers() -> list:
    global _RESOLVER_TIERS
    if _RESOLVER_TIERS is None:
        _RESOLVER_TIERS = _resolver.tiers_from_settings(_RESOLVER_INSTRUCTIONS)
    return _RESOLVER_TIERS


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except (GeoServerError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- models ---------------------------------------------------------------
class WorkspaceIn(BaseModel):
    name: str
    default: bool = False


class DatastoreIn(BaseModel):
    name: str
    host: str
    database: str
    user: str
    password: str
    port: int = 5432
    schema_: str = "public"
    workspace: str


class PublishIn(BaseModel):
    datastore: str
    table: str
    workspace: str
    name: str | None = None
    srs: str | None = None
    title: str | None = None


class StyleIn(BaseModel):
    name: str
    sld: str
    workspace: str | None = None


class AssignStyleIn(BaseModel):
    layer: str
    style: str
    workspace: str
    default: bool = True


# ---- endpoints ------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict:
    client = get_client()
    try:
        data = await client.get_json("about/version.json")
    except GeoServerError as exc:
        return {"connected": False, "error": str(exc), "url": client.settings.url}
    about = data.get("about", {}) if isinstance(data, dict) else {}
    res = about.get("resource", [])
    return {"connected": True, "url": client.settings.url, "components": res}


@app.get("/api/workspaces")
async def list_workspaces() -> list:
    client = get_client()
    data = await _guard(client.get_json("workspaces.json"))
    return extract(data, "workspaces", "workspace")


@app.post("/api/workspaces")
async def create_workspace(body: WorkspaceIn) -> dict:
    client = get_client()
    params = {"default": "true"} if body.default else None
    await _guard(client.post("workspaces.json", json={"workspace": {"name": body.name}},
                             headers={"Content-Type": "application/json"}, params=params))
    return {"created": body.name}


@app.get("/api/datastores")
async def list_datastores(workspace: str) -> list:
    client = get_client()
    data = await _guard(client.get_json(f"workspaces/{workspace}/datastores.json"))
    return extract(data, "dataStores", "dataStore")


@app.post("/api/datastores")
async def create_datastore(body: DatastoreIn) -> dict:
    client = get_client()
    payload = {
        "dataStore": {
            "name": body.name,
            "connectionParameters": _connection_parameters(
                body.host, body.port, body.database, body.user, body.password,
                body.schema_,
            ),
        }
    }
    await _guard(client.post(f"workspaces/{body.workspace}/datastores.json",
                             json=payload, headers={"Content-Type": "application/json"}))
    return {"created": body.name, "workspace": body.workspace}


@app.get("/api/featuretypes")
async def list_featuretypes(workspace: str, datastore: str,
                            only_available: bool = False) -> list:
    client = get_client()
    params = {"list": "available"} if only_available else None
    data = await _guard(client.get_json(
        f"workspaces/{workspace}/datastores/{datastore}/featuretypes.json", params=params))
    if only_available:
        return extract(data, "list", "string")
    return extract(data, "featureTypes", "featureType")


@app.post("/api/featuretypes")
async def publish_featuretype(body: PublishIn) -> dict:
    client = get_client()
    ft: dict = {"name": body.name or body.table, "nativeName": body.table,
                "srs": body.srs or client.settings.default_srs, "enabled": True}
    if body.title:
        ft["title"] = body.title
    await _guard(client.post(
        f"workspaces/{body.workspace}/datastores/{body.datastore}/featuretypes.json",
        json={"featureType": ft}, headers={"Content-Type": "application/json"},
        params={"recalculate": "nativebbox,latlonbbox"}))
    return {"published": ft["name"], "workspace": body.workspace}


@app.get("/api/styles")
async def list_styles(workspace: str | None = None) -> list:
    client = get_client()
    data = await _guard(client.get_json(f"{styles_path(workspace)}.json"))
    return extract(data, "styles", "style")


@app.post("/api/styles")
async def create_style(body: StyleIn) -> dict:
    client = get_client()
    await _guard(client.post(styles_path(body.workspace),
                             content=body.sld.encode("utf-8"),
                             headers={"Content-Type": SLD_CONTENT_TYPE},
                             params={"name": body.name}))
    return {"created": body.name}


@app.post("/api/styles/assign")
async def assign_style(body: AssignStyleIn) -> dict:
    client = get_client()
    await _guard(client.put(
        f"workspaces/{body.workspace}/layers/{body.layer}.json",
        json={"layer": {"defaultStyle": {"name": body.style}}},
        headers={"Content-Type": "application/json"}))
    return {"layer": body.layer, "style": body.style}


@app.get("/api/wfs")
async def wfs_get_feature(type_name: str, workspace: str | None = None,
                          count: int = 50, cql_filter: str | None = None) -> dict:
    client = get_client()
    qname = qualified_name(type_name, workspace)
    params = wfs_getfeature_params(qname, count, None, cql_filter, None, "EPSG:4326")
    resp = await _guard(client.ows(params))
    import json as _json
    return _json.loads(resp.text)


@app.post("/api/map")
async def build_map(layers: str, workspace: str | None = None,
                    title: str = "GeoServer map") -> dict:
    client = get_client()
    layer_list = [l.strip() for l in layers.split(",") if l.strip()]
    qlayers = [qualified_name(l, workspace) for l in layer_list]
    bounds = None
    ws = workspace or client.settings.default_workspace
    if layer_list and ws:
        try:
            data = await client.get_json(f"workspaces/{ws}/featuretypes/{layer_list[0]}.json")
            ft = data.get("featureType", {}) if isinstance(data, dict) else {}
            bounds = _bounds_from_bbox(ft.get("latLonBoundingBox"))
        except GeoServerError:
            bounds = None
    html = render_map(title=title, wms_base=client.settings.public_wms_base,
                      layers=qlayers, bounds=bounds)
    out_dir = Path(client.settings.map_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "webui_map.html"
    path.write_text(html, encoding="utf-8")
    return {"saved": str(path), "layers": qlayers}


@app.get("/wms")
async def wms_proxy(request: Request) -> Response:
    """Proxy WMS (GetMap/GetLegendGraphic) to GeoServer over the internal network.

    The browser only ever talks to this same-origin endpoint, so it never needs
    to reach the in-container GeoServer hostname — no host/port juggling, no
    cross-origin issues. Query params are forwarded verbatim.

    Retries on 429/503: a tiled Leaflet layer fires many concurrent tile
    requests and GeoServer can transiently throttle some; a short retry makes
    the map load reliably instead of leaving blank tiles.
    """
    client = get_client()
    params = dict(request.query_params)
    upstream = None
    for attempt in range(4):
        upstream = await client._client.get(client.settings.wms_base, params=params)
        if upstream.status_code not in (429, 503):
            break
        await asyncio.sleep(0.15 * (attempt + 1))
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type",
                                                    "application/octet-stream"))


_GEOM_LABEL = [("Polygon", "poligoni"), ("Line", "linee"), ("Point", "punti")]


async def _layer_info(workspace: str, name: str) -> dict | None:
    """Generic, domain-agnostic description of a layer's data type.

    Geometry kind + attribute names (from the feature type schema) + feature
    count (WFS hits). No assumption about the domain.
    """
    client = get_client()
    try:
        data = await client.get_json(
            f"workspaces/{workspace}/featuretypes/{name}.json")
    except GeoServerError:
        # Not a vector feature type — try a raster coverage (DTM/GeoTIFF).
        try:
            cdata = await client.get_json(
                f"workspaces/{workspace}/coverages/{name}.json")
        except GeoServerError:
            return None
        cov = cdata.get("coverage", {}) if isinstance(cdata, dict) else {}
        return {"geometry": "raster", "fields": [], "count": None,
                "title": cov.get("title") or name,
                "abstract": cov.get("abstract") or ""}
    ft = data.get("featureType", {}) if isinstance(data, dict) else {}
    atts = ft.get("attributes", {}).get("attribute", []) or []
    geometry, geom_col, fields = None, None, None
    fields = []
    for a in atts:
        binding = a.get("binding", "")
        if "jts" in binding.lower() or "geom" in binding.lower():
            geometry = next((lbl for key, lbl in _GEOM_LABEL if key in binding),
                            "geometrie")
            geom_col = a.get("name") or geom_col
        elif a.get("name"):
            fields.append(a["name"])
    count = None
    try:
        resp = await client.ows(
            {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
             "typeNames": f"{workspace}:{name}", "resultType": "hits"},
            base=client.settings.wfs_base)
        m = re.search(r'numberMatched="(\d+)"', resp.text)
        if m:
            count = int(m.group(1))
    except (GeoServerError, ValueError):
        pass
    return {"geometry": geometry, "geometry_column": geom_col, "fields": fields,
            "count": count, "title": ft.get("title") or name,
            "abstract": ft.get("abstract") or ""}


@app.get("/api/config")
async def config() -> dict:
    """Expose the bits the browser needs (e.g. the WMS base URL for Leaflet)."""
    client = get_client()
    return {
        # Same-origin WMS proxy (see /wms) — robust regardless of how/where
        # GeoServer is exposed; the browser never hits the container hostname.
        "wms_base": "/wms",
        "default_workspace": client.settings.default_workspace,
        "default_srs": client.settings.default_srs,
    }


# ---- shapefile upload (ingestion) ----------------------------------------
def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip, refusing absolute paths or traversal outside ``dest``."""
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not str(target).startswith(str(dest)):
            raise HTTPException(status_code=400,
                                detail=f"unsafe path in archive: {member}")
    zf.extractall(dest)


@app.post("/api/upload")
async def upload_shapefile(file: UploadFile = File(...),
                           name: str | None = Form(None)) -> dict:
    """Ingest an uploaded zipped shapefile into PostGIS and publish it.

    The browser uploads a ``.zip`` containing the shapefile sidecars
    (``.shp/.shx/.dbf`` and ideally ``.prj``). Loaded into the ``uploads``
    workspace/datastore, reprojected to EPSG:4326, then published.
    """
    filename = file.filename or "upload.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400,
                            detail="upload a .zip containing the shapefile "
                                   "(.shp/.shx/.dbf/.prj).")
    payload = await file.read()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                _safe_extract(zf, tmp_dir)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail=f"invalid zip: {exc}") from exc

        shps = sorted(tmp_dir.rglob("*.shp"))
        if not shps:
            raise HTTPException(status_code=400,
                                detail="no .shp found inside the archive.")
        if len(shps) > 1 and not name:
            raise HTTPException(
                status_code=400,
                detail=f"archive has {len(shps)} shapefiles; upload one at a "
                       "time or pass an explicit 'name'.")
        shp = shps[0]
        table = sanitize(name or shp.stem)

        await ensure_workspace(_UPLOAD_WS)
        await ensure_datastore(_UPLOAD_WS, _UPLOAD_DS, _UPLOAD_PG)
        srs = get_client().settings.default_srs
        try:
            await asyncio.to_thread(load_shapefile, shp, table, conn=_UPLOAD_PG,
                                    target_srs=srs, force=False)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _guard(publish(_UPLOAD_WS, _UPLOAD_DS, table, srs=srs, title=shp.stem))
        bbox = await featuretype_bbox(_UPLOAD_WS, table)

    metadata = None
    if _UPLOAD_AI_METADATA:
        from mcp_geo_server import metadata as _metadata
        try:
            rep = await _metadata.enrich_layer(_UPLOAD_WS, table)
            metadata = {k: rep.get(k) for k in ("status", "title", "keywords", "reason")}
        except Exception as exc:  # noqa: BLE001 — metadata is best-effort
            metadata = {"status": "failed", "reason": str(exc)}
    _invalidate_catalog()  # the new layer must be askable right away
    return {"metadata": metadata,
            "workspace": _UPLOAD_WS, "datastore": _UPLOAD_DS, "layer": table,
            "qualified": f"{_UPLOAD_WS}:{table}", "bbox": bbox}


# ---- layer catalog + natural-language command -----------------------------
# The catalog only changes when layers are published, so a full WMS
# GetCapabilities per chat request is wasted work. Cache it for
# GEO_CATALOG_TTL seconds (default 60, 0 = no cache); uploads invalidate it.
_CATALOG_TTL = float(os.environ.get("GEO_CATALOG_TTL") or 60)
_CATALOG_CACHE: tuple[float, list] | None = None
_CATALOG_LOCK = asyncio.Lock()


def _invalidate_catalog() -> None:
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


async def _fetch_catalog() -> list:
    client = get_client()
    resp = await _guard(client.ows(
        {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
        base=client.settings.wms_base))
    return build_catalog(parse_wms_capabilities(resp.text))


async def _load_catalog() -> list:
    """Build the catalog from the WMS capabilities (name + title + keywords).

    Domain-agnostic: one GetCapabilities call gives every published layer's real
    metadata, so the resolver matches requests semantically without any
    hardcoded vocabulary. Cached for ``GEO_CATALOG_TTL`` seconds; concurrent
    misses share a single fetch.
    """
    global _CATALOG_CACHE
    if _CATALOG_TTL <= 0:
        return await _fetch_catalog()
    async with _CATALOG_LOCK:
        now = asyncio.get_running_loop().time()
        if _CATALOG_CACHE is None or now - _CATALOG_CACHE[0] >= _CATALOG_TTL:
            _CATALOG_CACHE = (now, await _fetch_catalog())
        return _CATALOG_CACHE[1]


@app.get("/api/layers")
async def list_layers() -> list:
    """Return the catalog of all published layers (name + title + keywords)."""
    catalog = await _load_catalog()
    return [{"name": m.name, "workspace": m.workspace, "qualified": m.qualified,
             "label": m.label, "keywords": list(m.keywords)} for m in catalog]


@app.get("/api/bbox")
async def api_layer_bbox(workspace: str, name: str) -> dict:
    """Return a layer's lat/lon bounding box (for zoom-to-extent).

    NB the endpoint is deliberately NOT named ``layer_bbox`` — that would shadow
    the imported :func:`mcp_geo_server.ingest.layer_bbox` helper at module scope
    and make this call (and the one in ``/api/ask``) recurse infinitely.
    """
    bbox = await layer_bbox(workspace, name)
    if not bbox:
        raise HTTPException(status_code=404, detail="no bounding box for layer.")
    return bbox


class AskIn(BaseModel):
    query: str


_FILTER_SPEC: _resolver.FilterSpec | None = None


def _filter_spec() -> _resolver.FilterSpec:
    """Filterable attributes + allowed values, derived from the style config.

    Lets the LLM build a valid ``cql_filter`` (e.g. high hazard classes) using
    real column names and exact values, and lets the CQL guard reject anything
    else — generic, no domain hardcoding.
    """
    global _FILTER_SPEC
    if _FILTER_SPEC is None:
        _FILTER_SPEC = _resolver.filter_spec(_load_style_config())
    return _FILTER_SPEC


# ---- admin-area (comune / provincia / regione) spatial scoping -----------
# Which boundary layers exist is domain config (`admin:` in styles.yml), read
# by mcp_geo_server.places; the matching + geometry logic lives there too.
_ADMIN_AREAS: _places.AdminAreas | None = None


def _admin_areas() -> _places.AdminAreas:
    global _ADMIN_AREAS
    if _ADMIN_AREAS is None:
        _ADMIN_AREAS = _places.AdminAreas(_places.admin_config(_load_style_config()))
    return _ADMIN_AREAS


async def _detect_admin(query: str):
    """The admin unit named in the query, if any: ``(AdminLevel, raw_name)``."""
    return await _admin_areas().detect(query)


async def _admin_scope(level, raw_name: str) -> dict | None:
    """Zoom bbox + clip WKT (EPSG:4326) for one admin unit. Cached."""
    return await _admin_areas().scope(level, raw_name)


# Terrain-metric intent: words that mean the user wants a DTM-derived metric
# computed on the selected vector features (quota/slope/aspect/curvature).
_METRIC_HINTS = {
    "quota": "quota", "quote": "quota", "altimetria": "quota",
    "altitudine": "quota", "elevazione": "quota", "altezza": "quota",
    "pendenza": "slope", "pendenze": "slope", "acclivita": "slope",
    "slope": "slope",
    "esposizione": "aspect", "orientamento": "aspect", "aspect": "aspect",
    "curvatura": "curvature", "curvature": "curvature",
}


def _metric_intent(query: str) -> list[str]:
    """Terrain metrics named in the query (normalized, accent-insensitive)."""
    toks = set(_places.norm(query).split())
    return sorted({v for k, v in _METRIC_HINTS.items() if k in toks})


@app.post("/api/ask")
async def ask(body: AskIn) -> dict:
    """Map a natural-language request to layers via the LLM resolver.

    Returns the matched (qualified) layer names, an optional CQL filter, a short
    explanation, and the combined lat/lon bbox so the UI can render WMS + zoom.
    """
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="empty query.")

    catalog = await _load_catalog()
    spec = _filter_spec()
    try:
        selection = await _resolver.resolve_routed(
            _get_resolver_tiers(), query, catalog, spec,
            min_confidence=_MIN_CONFIDENCE, structured=_RESOLVER_STRUCTURED,
            guard=False, retriever=_RETRIEVER,
            prompt_cache=_resolver.prompt_cache_from_env())
    except ValueError as exc:
        raise HTTPException(status_code=502,
                            detail=f"could not parse model reply: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — LLM/transport errors -> 502
        raise HTTPException(status_code=502,
                            detail=f"LLM resolver failed: {exc}") from exc

    # Zoom to the combined extent of the selected layers (skip degenerate boxes —
    # empty layers report (0,0,0,0), which would drag the extent to the sea).
    bbox_by_q: dict[str, dict] = {}
    for qualified in selection["layers"]:
        ws, _, name = qualified.partition(":")
        bbox = await layer_bbox(ws, name)
        if bbox and bbox["maxx"] > bbox["minx"] and bbox["maxy"] > bbox["miny"]:
            bbox_by_q[qualified] = bbox
    boxes = list(bbox_by_q.values())
    combined = None
    if boxes:
        combined = {
            "minx": min(b["minx"] for b in boxes),
            "miny": min(b["miny"] for b in boxes),
            "maxx": max(b["maxx"] for b in boxes),
            "maxy": max(b["maxy"] for b in boxes),
        }
    selection["bbox"] = combined

    # Describe the data type of each selected layer (geometry, count, fields).
    # The per-layer bbox is attached so the UI can order overlapping rasters by
    # footprint (broadest at the bottom).
    info = []
    for qualified in selection["layers"]:
        ws, _, name = qualified.partition(":")
        li = await _layer_info(ws, name)
        if li:
            kind = "raster" if li.get("geometry") == "raster" else "vector"
            info.append({"qualified": qualified, "kind": kind,
                         "bbox": bbox_by_q.get(qualified), **li})

    # Server-side draw order (bottom -> top) so ANY map client can just render
    # `layers` in the given order — the stacking policy lives here, not in the
    # UI: rasters below all vectors (an opaque raster must not hide them), and
    # among rasters the broadest footprint sits at the bottom (a smaller/local
    # raster stays visible over a national one). Stable within each group.
    info_by_q = {e["qualified"]: e for e in info}

    def _order_key(qualified: str) -> tuple:
        e = info_by_q.get(qualified, {})
        b = e.get("bbox")
        area = (b["maxx"] - b["minx"]) * (b["maxy"] - b["miny"]) if b else 0.0
        is_raster = e.get("kind") == "raster"
        return (0 if is_raster else 1, -area if is_raster else 0.0)

    selection["layers"] = sorted(selection["layers"], key=_order_key)
    selection["info"] = sorted(info, key=lambda e: _order_key(e["qualified"]))

    # Per-layer CQL applicability: a single cql_filter can only apply to layers
    # that actually have the referenced attribute(s). Applying it to a layer
    # that lacks the column makes GeoServer fail the ENTIRE render (that is why
    # "pericolosità elevata" over a landslide-inventory layer showed nothing).
    # The server decides, per layer, whether the filter applies.
    # CQL guard: parse the model's filter, allow only the attributes the
    # selected layers really have (style config + actual columns) and the exact
    # configured values; otherwise drop it and say why (`cql_rejected`).
    selection.pop("raw", None)
    _resolver.guard_cql(selection, spec,
                        {e["qualified"]: set(e.get("fields") or []) for e in info})
    cql = selection.get("cql_filter")
    cql_by_layer: dict[str, str] = {}
    if cql:
        attrs = _cql.attributes(_cql.parse(cql))
        for e in info:
            fields = set(e.get("fields") or [])
            if attrs and attrs <= fields:
                cql_by_layer[e["qualified"]] = cql
    selection["cql_applicable"] = bool(cql_by_layer) if cql else None

    # Admin-area scoping: if the request names a comune/provincia/regione, zoom
    # to that unit and restrict the layers to its boundary. RASTERS use the exact
    # polygon `clip` (robust on coverages). VECTORS use a CQL INTERSECTS spatial
    # filter instead — a predicate (no geometry overlay), so it avoids the JTS
    # "non-noded intersection" crash that `clip` triggers on dense/imperfect
    # vector layers (e.g. the ~900k-polygon PAI mosaic).
    admin = await _detect_admin(query)
    if admin:
        scope = await _admin_scope(*admin)
        if scope:
            selection["admin"] = {"level": scope["level"], "name": scope["name"]}
            selection["bbox"] = scope["bbox"]   # override: zoom to the unit
            selection["clip"] = scope["clip"]   # client applies it to RASTERS
            wkt = scope["clip"].split(";", 1)[-1]  # bare WKT for CQL
            for e in info:
                if e.get("kind") != "vector":
                    continue
                q = e["qualified"]
                gcol = e.get("geometry_column") or "geom"
                spatial = f"INTERSECTS({gcol}, {wkt})"
                base = cql_by_layer.get(q)
                cql_by_layer[q] = f"({base}) AND {spatial}" if base else spatial
    selection["cql_by_layer"] = cql_by_layer

    # Terrain enrichment: if the request names a metric (quota/slope/aspect/
    # curvature), compute it on the selected VECTOR layers by sampling a DTM,
    # bounded to the current area. The raster is the sampling source (a DTM in
    # the selection, else auto-picked). Best-effort — never fails the ask.
    wanted_metrics = _metric_intent(query)
    vector_layers = [i["qualified"] for i in info if i.get("kind") == "vector"]
    if wanted_metrics and vector_layers:
        from mcp_geo_server import enrich as _enrich
        dtm_layers = [i["qualified"] for i in info if i.get("kind") == "raster"]
        dtm = dtm_layers[0] if dtm_layers else None
        b = selection.get("bbox")
        bbox_str = (f'{b["minx"]},{b["miny"]},{b["maxx"]},{b["maxy"]}'
                    if b else None)
        labels = {m: _enrich.METRICS.get(m, m) for m in wanted_metrics}
        enriched = []
        for vl in vector_layers[:3]:
            try:
                r = await _enrich.enrich_layer(vl, metrics=wanted_metrics,
                                               dtm=dtm, bbox=bbox_str, limit=1000)
                if not r.get("count"):
                    continue
                # Build a display-ready sentence server-side so any client can
                # just print it (no client-side formatting of the stats).
                parts = []
                for m in wanted_metrics:
                    s = r["summary"].get(m)
                    if s:
                        parts.append(f'{labels[m]} media {s["mean"]} '
                                     f'(min {s["min"]}, max {s["max"]})')
                text = (f'{vl.split(":")[-1]} — {r["count"]} elementi: '
                        + "; ".join(parts))
                enriched.append({"layer": vl, "count": r["count"],
                                 "summary": r["summary"], "text": text})
            except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
                print(f"enrichment skipped for {vl}: {exc}")  # webui logs
        if enriched:
            title = "Analisi del terreno (DTM)"
            selection["enrichment"] = {
                "metrics": wanted_metrics, "labels": labels, "title": title,
                "layers": enriched,
                "text": title + ":\n" + "\n".join(f"· {e['text']}"
                                                   for e in enriched),
            }
    return selection


# ---- terrain enrichment from a DTM (generic, on-demand) ------------------
class EnrichIn(BaseModel):
    layer: str                       # qualified vector layer to enrich
    dtm: str | None = None           # qualified DTM coverage (auto-picked if None)
    metrics: list[str] | None = None  # subset of quota/slope/aspect/curvature
    bbox: str | None = None          # optional "minx,miny,maxx,maxy" (EPSG:4326)
    limit: int = 500


@app.post("/api/enrich")
async def enrich_endpoint(body: EnrichIn) -> dict:
    """Enrich a vector layer's features with DTM terrain metrics (on-demand).

    Generic: works for any point/line/polygon layer and any DTM published as a
    coverage. Returns per-feature metrics + a dataset-level summary; writes
    nothing. Restrict the work with ``bbox`` and ``limit`` for large layers.
    Shares its core with the ``geo_enrich_from_dtm`` MCP tool.
    """
    from mcp_geo_server import enrich as _enrich
    try:
        return await _enrich.enrich_layer(
            body.layer, metrics=body.metrics, dtm=body.dtm,
            bbox=body.bbox, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surface enrichment errors
        raise HTTPException(status_code=400,
                            detail=f"enrichment failed: {exc}") from exc


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe: no GeoServer/LLM call, and exempt from Basic auth."""
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/upload")
async def upload_page() -> FileResponse:
    """Dedicated shapefile-upload page (separate from the chat/map)."""
    return FileResponse(str(_STATIC / "upload.html"))


app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
