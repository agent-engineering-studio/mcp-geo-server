"""Thin FastAPI backend for the test UI.

It imports the SAME core (``GeoServerClient`` + tool helpers) the MCP server
uses — it does NOT go through an MCP client. Each endpoint maps to one core
operation and returns JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp_geo_server.client import GeoServerError, get_client
from mcp_geo_server.formatting import extract
from mcp_geo_server.tools.datastores import _connection_parameters
from mcp_geo_server.tools.ogc import qualified_name, wfs_getfeature_params
from mcp_geo_server.tools.styles import SLD_CONTENT_TYPE, styles_path
from mcp_geo_server.tools.map import render_map, _bounds_from_bbox

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="mcp-geo-server test UI")


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
    html = render_map(title=title, wms_base=client.settings.wms_base, layers=qlayers,
                      bounds=bounds)
    out_dir = Path(client.settings.map_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "webui_map.html"
    path.write_text(html, encoding="utf-8")
    return {"saved": str(path), "layers": qlayers}


@app.get("/api/config")
async def config() -> dict:
    """Expose the bits the browser needs (e.g. the WMS base URL for Leaflet)."""
    client = get_client()
    return {
        "wms_base": client.settings.wms_base,
        "default_workspace": client.settings.default_workspace,
        "default_srs": client.settings.default_srs,
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
