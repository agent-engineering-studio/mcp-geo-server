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

from mcp_geo_server.agent import _build_chat_client
from mcp_geo_server.catalog import (
    build_catalog,
    catalog_prompt,
    parse_wms_capabilities,
    validate_selection,
)
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

_STATIC = Path(__file__).resolve().parent / "static"

# Target for UI shapefile uploads (own workspace/datastore, separate from the
# curated `ispra` data).
_UPLOAD_WS = (os.environ.get("GEO_UPLOAD_WORKSPACE") or "uploads").strip()
_UPLOAD_DS = (os.environ.get("GEO_UPLOAD_DATASTORE") or "uploads_pg").strip()
_UPLOAD_PG = PgConn(
    host=(os.environ.get("POSTGIS_HOST") or "postgis").strip(),
    port=int(os.environ.get("POSTGIS_PORT") or 5432),
    database=(os.environ.get("POSTGIS_DB") or "gis").strip(),
    user=(os.environ.get("POSTGIS_USER") or "gis").strip(),
    password=(os.environ.get("POSTGIS_PASSWORD") or "gis").strip(),
    schema=(os.environ.get("POSTGIS_SCHEMA") or "public").strip(),
)

app = FastAPI(title="mcp-geo-server test UI")

# Lazily-built LLM resolver (no GeoServer tools — it only maps NL -> layer names).
_RESOLVER = None
_RESOLVER_INSTRUCTIONS = (
    "You map an Italian (or English) natural-language map request to GeoServer "
    "layers chosen from a catalog the user gives you. Reply with ONLY a single "
    "JSON object, no prose, no code fences:\n"
    '{"layers": ["<exact qualified name from the catalog>", ...], '
    '"cql_filter": null, "explanation": "<one short sentence, in the user\'s '
    'language>"}\n'
    "Rules:\n"
    "- pick ONLY names that appear verbatim in the catalog; match the request "
    "against each layer's SUBJECT (its name, title and keywords).\n"
    "- A place name (region/province/comune, e.g. \"Valle d'Aosta\", \"Bari\") "
    "only restricts the AREA shown — the area clip is applied automatically "
    "afterwards. Use the place to choose a layer ONLY when the requested subject "
    "has per-place variants (e.g. landslide layers exist per region); otherwise "
    "ignore the place when selecting. NEVER add a layer of a DIFFERENT subject "
    "just because its name contains the place. Example: \"DTM della Valle "
    "d'Aosta\" -> the DTM / elevation layer ONLY (subject = DTM), NOT the "
    "landslide layers of Valle d'Aosta.\n"
    "- return as FEW layers as possible — only those whose subject was asked "
    "for; include several only when the request is genuinely ambiguous.\n"
    "- cql_filter: when the user asks for a SUBSET by an attribute value (e.g. "
    "'alta/elevata pericolosità'), build a CQL using ONLY the attributes and "
    "EXACT values listed under 'Filterable attributes'; otherwise null. Never "
    "invent layer, column or value names."
)


def _get_resolver():
    global _RESOLVER
    if _RESOLVER is None:
        client = _build_chat_client(get_settings())
        _RESOLVER = client.as_agent(name="layer-resolver",
                                    instructions=_RESOLVER_INSTRUCTIONS, tools=[])
    return _RESOLVER


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
    geometry, fields = None, []
    for a in atts:
        binding = a.get("binding", "")
        if "jts" in binding.lower() or "geom" in binding.lower():
            geometry = next((lbl for key, lbl in _GEOM_LABEL if key in binding),
                            "geometrie")
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
    return {"geometry": geometry, "fields": fields, "count": count,
            "title": ft.get("title") or name, "abstract": ft.get("abstract") or ""}


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

    return {"workspace": _UPLOAD_WS, "datastore": _UPLOAD_DS, "layer": table,
            "qualified": f"{_UPLOAD_WS}:{table}", "bbox": bbox}


# ---- layer catalog + natural-language command -----------------------------
async def _load_catalog() -> list:
    """Build the catalog from the WMS capabilities (name + title + keywords).

    Domain-agnostic: one GetCapabilities call gives every published layer's real
    metadata, so the resolver matches requests semantically without any
    hardcoded vocabulary.
    """
    client = get_client()
    resp = await _guard(client.ows(
        {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
        base=client.settings.wms_base))
    return build_catalog(parse_wms_capabilities(resp.text))


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


_FILTER_HINTS: str | None = None


def _filter_hints() -> str:
    """Filterable attributes + allowed values, derived from the style config.

    Lets the LLM build a valid ``cql_filter`` (e.g. high hazard classes) using
    real column names and exact values — generic, no domain hardcoding.
    """
    global _FILTER_HINTS
    if _FILTER_HINTS is None:
        attrs: dict[str, list[tuple[str, str]]] = {}
        for spec in _load_style_config().get("styles", {}).values():
            attr, classes = spec.get("attribute"), spec.get("classes")
            if not (attr and classes):
                continue
            for c in classes:
                pair = (str(c.get("value")), str(c.get("label", "")))
                attrs.setdefault(attr, [])
                if pair not in attrs[attr]:
                    attrs[attr].append(pair)
        if not attrs:
            _FILTER_HINTS = ""
        else:
            lines = ["Filterable attributes (use EXACT values in cql_filter):"]
            for a, pairs in attrs.items():
                vals = ", ".join(f"'{v}' ({lbl})" if lbl else f"'{v}'"
                                 for v, lbl in pairs)
                lines.append(f"- {a}: {vals}")
            _FILTER_HINTS = "\n".join(lines)
    return _FILTER_HINTS


# ---- admin-area (comune / provincia / regione) spatial scoping -----------
# When a chat request names an administrative unit, zoom to it and CLIP the
# rendered layers to its exact boundary. ISTAT boundary layers, most specific
# first (a comune match beats a regione match). Workspace defaults to the
# catalog workspace; override with GEO_ADMIN_WORKSPACE.
_ADMIN_WS = (os.environ.get("GEO_ADMIN_WORKSPACE") or "").strip()
_ADMIN_LEVELS = [
    {"level": "comune",    "layer": "com01012023_g",    "attr": "comune"},
    {"level": "provincia", "layer": "provcm01012023_g", "attr": "den_uts"},
    {"level": "regione",   "layer": "reg01012023_g",    "attr": "den_reg"},
]
_ADMIN_INDEX: list[tuple[str, dict, str]] | None = None
_ADMIN_SCOPE_CACHE: dict[tuple[str, str], dict | None] = {}


def _admin_ws() -> str:
    return _ADMIN_WS or (get_client().settings.default_workspace or "")


def _demojibake(s: str) -> str:
    """Repair classic double-encoded latin-1/UTF-8 text ('AgliÃ¨' -> 'Agliè').

    Some boundary layers were loaded with the wrong encoding, so the DB holds
    the mojibake. Used only for MATCHING against user text and for display — the
    raw stored value is still used in the CQL filter that fetches the geometry.
    """
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _norm_admin(s: str) -> str:
    """Accent/case/punctuation-insensitive key for matching place names."""
    import unicodedata
    s = _demojibake(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


async def _admin_names(level: dict) -> list[str]:
    """Raw stored names for one boundary layer (geometry excluded)."""
    client = get_client()
    try:
        resp = await client.ows(
            {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
             "typeNames": f"{_admin_ws()}:{level['layer']}",
             "propertyName": level["attr"], "count": "20000",
             "outputFormat": "application/json"},
            base=client.settings.wfs_base)
        import json as _json
        data = _json.loads(resp.text)
        return [f.get("properties", {}).get(level["attr"])
                for f in data.get("features", [])
                if f.get("properties", {}).get(level["attr"])]
    except (GeoServerError, ValueError):
        return []


async def _admin_index() -> list[tuple[str, dict, str]]:
    """Build ``(normalized_name, level, raw_name)`` once (cached process-wide)."""
    global _ADMIN_INDEX
    if _ADMIN_INDEX is None:
        idx: list[tuple[str, dict, str]] = []
        for level in _ADMIN_LEVELS:
            for raw in await _admin_names(level):
                n = _norm_admin(raw)
                if len(n) >= 3:  # skip 2-letter names (false-positive prone)
                    idx.append((n, level, raw))
        _ADMIN_INDEX = idx
    return _ADMIN_INDEX


# Level keywords in the query force that admin level (e.g. 'provincia di Bari'
# should scope to the province, not the comune that shares the name).
_LEVEL_HINTS = {
    "regione": "regione", "regionale": "regione",
    "provincia": "provincia", "provincie": "provincia", "prov": "provincia",
    "metropolitana": "provincia",  # 'città metropolitana di ...'
    "comune": "comune",
}


def _level_hint(q_norm: str) -> str | None:
    """The admin level explicitly named in the (normalized) query, if any."""
    tokens = set(q_norm.split())
    for kw, lvl in _LEVEL_HINTS.items():
        if kw in tokens:
            return lvl
    return None


async def _detect_admin(query: str) -> tuple[dict, str] | None:
    """The admin unit named in the query, if any.

    Matches whole normalized phrases (space-padded) so 'regione' won't spuriously
    match a 2-letter comune. If the query names a level ('provincia di Bari'),
    that level wins; otherwise the most specific (then longest) match wins.
    """
    q_norm = _norm_admin(query)
    q = f" {q_norm} "
    hint = _level_hint(q_norm)
    order = {lvl["level"]: i for i, lvl in enumerate(_ADMIN_LEVELS)}
    best: tuple[dict, str] | None = None
    best_key = (99, 99, 0)
    for n, level, raw in await _admin_index():
        if f" {n} " in q:
            lvl = level["level"]
            # 0 if it matches the level the user named, else 1 (hint wins).
            hint_rank = 0 if (hint and lvl == hint) else 1
            # Then the LONGEST phrase match (a fuller name like "valle d aosta"
            # beats the substring "aosta"), then the most specific level.
            key = (hint_rank, -len(n), order[lvl])
            if key < best_key:
                best_key, best = key, (level, raw)
    return best


async def _admin_scope(level: dict, raw_name: str) -> dict | None:
    """Zoom bbox + clip WKT (EPSG:4326) for one admin unit. Cached."""
    ck = (level["layer"], raw_name)
    if ck in _ADMIN_SCOPE_CACHE:
        return _ADMIN_SCOPE_CACHE[ck]
    client = get_client()
    scope = None
    try:
        cql = f"{level['attr']}='{raw_name.replace(chr(39), chr(39) * 2)}'"
        resp = await client.ows(
            {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
             "typeNames": f"{_admin_ws()}:{level['layer']}", "cql_filter": cql,
             "count": "1", "srsName": "EPSG:4326",
             "outputFormat": "application/json"},
            base=client.settings.wfs_base)
        import json as _json
        feats = _json.loads(resp.text).get("features", [])
        geom_json = feats[0].get("geometry") if feats else None
        if geom_json:
            from shapely.geometry import shape
            geom = shape(geom_json)
            minx, miny, maxx, maxy = geom.bounds
            # Simplify enough that the clip WKT fits in the WMS tile GET URL
            # (Tomcat caps the request line at ~8 KB).
            tol = 0.001
            wkt = geom.simplify(tol, preserve_topology=True).wkt
            while len(wkt) > 3500 and tol < 1:
                tol *= 2
                wkt = geom.simplify(tol, preserve_topology=True).wkt
            scope = {
                "level": level["level"],
                "name": _demojibake(raw_name),
                "bbox": {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
                "clip": f"SRID=4326;{wkt}",
            }
    except Exception:  # noqa: BLE001 — scoping is best-effort, never fail the ask
        scope = None
    _ADMIN_SCOPE_CACHE[ck] = scope
    return scope


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
    prompt = (f"{catalog_prompt(catalog)}\n\n{_filter_hints()}\n\n"
              f"Request: {query}\n\nJSON:")
    try:
        resolver = _get_resolver()
        result = await resolver.run(prompt)
        selection = validate_selection(result.text, catalog)
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

    # Admin-area scoping: if the request names a comune/provincia/regione, zoom
    # to that unit and clip the rendered layers to its exact boundary (so e.g.
    # "il DTM della Puglia" shows only Puglia, not the whole national raster).
    admin = await _detect_admin(query)
    if admin:
        scope = await _admin_scope(*admin)
        if scope:
            selection["admin"] = {"level": scope["level"], "name": scope["name"]}
            selection["bbox"] = scope["bbox"]   # override: zoom to the unit
            selection["clip"] = scope["clip"]   # frontend passes to the WMS
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/upload")
async def upload_page() -> FileResponse:
    """Dedicated shapefile-upload page (separate from the chat/map)."""
    return FileResponse(str(_STATIC / "upload.html"))


app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
