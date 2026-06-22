"""Build a standalone Leaflet HTML map (OSM basemap + GeoServer WMS overlays)."""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..client import get_client
from .ogc import qualified_name, wfs_getfeature_params

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),
)


def render_map(title: str, wms_base: str, layers: list[str], *,
               bounds: list | None = None, center: tuple[float, float] = (0.0, 0.0),
               zoom: int = 6, geojson: dict | None = None,
               geojson_name: str | None = None) -> str:
    """Render the Leaflet HTML from already-resolved data (no network)."""
    template = _env.get_template("leaflet_map.html.j2")
    return template.render(
        title=title,
        wms_base=wms_base,
        layers=[{"qualified": q, "name": q.split(":")[-1]} for q in layers],
        bounds=bounds,
        center=list(center),
        zoom=zoom,
        geojson=geojson,
        geojson_json=json.dumps(geojson) if geojson else "null",
        geojson_name=geojson_name or "",
    )


def _bounds_from_bbox(bbox: dict | None) -> list | None:
    """Convert a GeoServer latLonBoundingBox into Leaflet [[s,w],[n,e]]."""
    if not bbox:
        return None
    try:
        minx, miny = float(bbox["minx"]), float(bbox["miny"])
        maxx, maxy = float(bbox["maxx"]), float(bbox["maxy"])
    except (KeyError, TypeError, ValueError):
        return None
    return [[miny, minx], [maxy, maxx]]


async def geo_build_web_map(layers: str, workspace: str | None = None,
                            title: str = "GeoServer map",
                            filename: str = "map.html",
                            center: list | None = None, zoom: int = 6,
                            geojson_layer: str | None = None,
                            geojson_count: int = 200) -> dict:
    """Generate a standalone Leaflet HTML map and save it to disk.

    Adds an OpenStreetMap basemap and one WMS overlay per layer (comma
    separated, each qualified with ``workspace``). The map is centered on the
    first layer's ``latLonBoundingBox`` via ``fitBounds`` when available,
    otherwise on ``center``/``zoom``. If ``geojson_layer`` is given, that layer
    is also fetched via WFS and embedded as clickable GeoJSON. Returns the saved
    file path.
    """
    client = get_client()
    ws = workspace or client.settings.default_workspace
    layer_list = [l.strip() for l in layers.split(",") if l.strip()]
    qlayers = [qualified_name(l, ws) for l in layer_list]

    # Try to center on the first layer's bounding box.
    bounds = None
    if layer_list and ws:
        try:
            data = await client.get_json(
                f"workspaces/{ws}/featuretypes/{layer_list[0]}.json"
            )
            ft = data.get("featureType", {}) if isinstance(data, dict) else {}
            bounds = _bounds_from_bbox(ft.get("latLonBoundingBox"))
        except Exception:  # noqa: BLE001 - centering is best-effort
            bounds = None

    geojson = None
    if geojson_layer:
        qname = qualified_name(geojson_layer, ws)
        params = wfs_getfeature_params(qname, geojson_count, None, None, None,
                                       "EPSG:4326")
        resp = await client.ows(params)
        geojson = json.loads(resp.text)

    html = render_map(
        title=title,
        wms_base=client.settings.wms_base,
        layers=qlayers,
        bounds=bounds,
        center=tuple(center) if center else (0.0, 0.0),
        zoom=zoom,
        geojson=geojson,
        geojson_name=geojson_layer,
    )

    out_dir = Path(client.settings.map_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(html, encoding="utf-8")
    return {"saved": str(path), "layers": qlayers,
            "centered_on_bbox": bounds is not None,
            "embedded_geojson": geojson is not None}
