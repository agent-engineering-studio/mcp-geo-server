"""Thin FastAPI backend for the test UI.

It imports the SAME core (``GeoServerClient`` + tool helpers) the MCP server
uses — it does NOT go through an MCP client. Each endpoint maps to one core
operation and returns JSON.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp_geo_server.answer import AskError, get_assistant
from mcp_geo_server.client import GeoServerError, get_client
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
# The pipeline lives in mcp_geo_server.answer (shared with the MCP server's
# geo_resolve_request); these are thin views onto the process-wide assistant.
_ASSISTANT = get_assistant()


async def _load_catalog() -> list:
    return await _guard(_ASSISTANT.load_catalog())


def _invalidate_catalog() -> None:
    _ASSISTANT.invalidate_catalog()


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
    # The current map state from the previous answer, for follow-up turns
    # ("solo quelle ad alta pericolosità"): {layers, cql_filter, admin}. The
    # client keeps it; the server holds no conversation state.
    context: dict | None = None


@app.post("/api/ask")
async def ask(body: AskIn) -> dict:
    """Map a natural-language request to a ready-to-render answer.

    Layers in draw order, per-layer CQL (attribute filter + area INTERSECTS),
    zoom bbox, raster clip, layer descriptions and optional terrain metrics —
    see :class:`mcp_geo_server.answer.MapAssistant`.
    """
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="empty query.")
    try:
        return await _ASSISTANT.ask(query, body.context)
    except AskError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except GeoServerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


# ---- thematic style from a sentence (mcp_geo_server.style_ai) --------------
class StyleAskIn(BaseModel):
    layer: str                        # qualified, e.g. ispra:frane_poly_molise_opendata
    instruction: str                  # "colora per tipo di movimento, toni caldi"
    attribute: str | None = None      # skip the model's column choice
    apply: bool = False               # publish + set as the layer's default style


@app.post("/api/style")
async def style_from_text(body: StyleAskIn) -> dict:
    """Design a categorical style from an instruction; optionally apply it.

    Returns the styles.yml entry (``spec`` + ``yaml`` to paste into
    data/styles.yml) and whether it was applied. The model only labels and
    colours the attribute's REAL values; the SLD is generated by the config
    engine, never written by the model.
    """
    from mcp_geo_server import style_ai
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="empty instruction.")
    try:
        p = await style_ai.propose(body.layer, body.instruction, attribute=body.attribute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — LLM/transport errors -> 502
        raise HTTPException(status_code=502, detail=f"style design failed: {exc}") from exc
    if body.apply:
        await _guard(style_ai.apply(p))
    return {"name": p["name"], "layer": p["layer"], "attribute": p["attribute"],
            "reason": p["reason"], "spec": p["spec"], "yaml": p["yaml"],
            "applied": body.apply}


# ---- area report (mcp_geo_server.report) -----------------------------------
@app.get("/api/report")
async def area_report(place: str, subject: str | None = None, narrative: bool = False,
                      format: str = "html") -> Response:
    """Fact sheet of the data in an administrative area, with exact counts.

    ``format``: ``html`` (a printable page), ``md`` or ``json``. ``narrative``
    adds an LLM synthesis that is discarded if it quotes a number not in the
    facts.
    """
    from mcp_geo_server import report
    try:
        rep = await report.area_report(place, subject, assistant=_ASSISTANT,
                                       narrative=narrative)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GeoServerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if format == "md":
        return Response(rep["markdown"], media_type="text/markdown; charset=utf-8")
    if format == "json":
        import json as _json
        body = {k: rep[k] for k in ("facts", "summary", "narrative", "narrative_rejected")}
        return Response(_json.dumps(body, ensure_ascii=False),
                        media_type="application/json")
    return Response(rep["html"], media_type="text/html; charset=utf-8")


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
