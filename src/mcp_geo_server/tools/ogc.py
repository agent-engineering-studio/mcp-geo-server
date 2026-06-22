"""OGC service tools: WMS GetMap, WFS GetFeature, WFS-T transactions.

All requests hit the generic ``/ows`` endpoint. The pure URL/XML builders are
module-level functions so they can be unit-tested without any network.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..client import get_client
from . import resolve_srs, resolve_workspace

WFST_NS = {
    "wfs": "http://www.opengis.net/wfs",
    "ogc": "http://www.opengis.net/ogc",
}


def qualified_name(name: str, workspace: str | None) -> str:
    """Return ``workspace:name`` if a workspace is given, else ``name``."""
    if workspace and ":" not in name:
        return f"{workspace}:{name}"
    return name


def wms_getmap_params(layers: str, bbox: str, width: int, height: int, srs: str,
                      image_format: str, styles: str, transparent: bool) -> dict:
    """Build WMS 1.1.1 GetMap query parameters."""
    return {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": layers,
        "styles": styles or "",
        "bbox": bbox,
        "width": width,
        "height": height,
        "srs": srs,
        "format": image_format,
        "transparent": "true" if transparent else "false",
    }


def wfs_getfeature_params(type_name: str, count: int, bbox: str | None,
                          cql_filter: str | None, property_names: str | None,
                          srs_name: str | None) -> dict:
    """Build WFS 2.0.0 GetFeature query parameters (GeoJSON output)."""
    if bbox and cql_filter:
        raise ValueError("Pass either 'bbox' or 'cql_filter', not both.")
    params: dict = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "count": count,
        "outputFormat": "application/json",
    }
    if bbox:
        params["bbox"] = bbox
    if cql_filter:
        params["cql_filter"] = cql_filter
    if property_names:
        params["propertyName"] = property_names
    if srs_name:
        params["srsName"] = srs_name
    return params


def build_wfst_xml(operation: str, type_name: str, ns_prefix: str, ns_uri: str,
                   feature_ids: list[str] | None = None,
                   properties: dict[str, str] | None = None) -> str:
    """Build a WFS-T 1.1.0 Transaction document for ``delete`` or ``update``.

    ``type_name`` must be unqualified; it is prefixed with ``ns_prefix`` and the
    namespace is declared on the root element.
    """
    feature_ids = feature_ids or []
    qname = f"{ns_prefix}:{type_name}"
    filt = "".join(f'<ogc:FeatureId fid={quoteattr(fid)}/>' for fid in feature_ids)
    filter_block = f"<ogc:Filter>{filt}</ogc:Filter>" if filt else ""

    if operation == "delete":
        inner = f'<wfs:Delete typeName="{qname}">{filter_block}</wfs:Delete>'
    elif operation == "update":
        props = "".join(
            f"<wfs:Property><wfs:Name>{escape(k)}</wfs:Name>"
            f"<wfs:Value>{escape(str(v))}</wfs:Value></wfs:Property>"
            for k, v in (properties or {}).items()
        )
        inner = f'<wfs:Update typeName="{qname}">{props}{filter_block}</wfs:Update>'
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"Unsupported WFS-T operation for XML builder: {operation}")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<wfs:Transaction service="WFS" version="1.1.0" '
        f'xmlns:wfs="{WFST_NS["wfs"]}" xmlns:ogc="{WFST_NS["ogc"]}" '
        f'xmlns:{ns_prefix}={quoteattr(ns_uri)}>'
        f"{inner}"
        "</wfs:Transaction>"
    )


async def _resolve_namespace(client, workspace: str) -> tuple[str, str]:
    data = await client.get_json(f"namespaces/{workspace}.json")
    ns = data.get("namespace", {}) if isinstance(data, dict) else {}
    prefix = ns.get("prefix") or workspace
    uri = ns.get("uri") or f"http://{workspace}"
    return prefix, uri


async def _resolve_feature_ids(client, type_name: str, cql_filter: str) -> list[str]:
    params = wfs_getfeature_params(type_name, count=10000, bbox=None,
                                   cql_filter=cql_filter, property_names=None,
                                   srs_name=None)
    resp = await client.ows(params)
    payload = json.loads(resp.text)
    return [f["id"] for f in payload.get("features", []) if f.get("id")]


async def geo_wms_get_capabilities(workspace: str | None = None) -> dict:
    """Fetch the WMS GetCapabilities document (returned as raw XML text)."""
    client = get_client()
    params = {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}
    base = f"{client.settings.url}/{workspace}/ows" if workspace else None
    resp = await client.ows(params, base=base)
    return {"xml": resp.text}


async def geo_wms_get_map(layers: str, bbox: str, workspace: str | None = None,
                          width: int = 768, height: int = 512,
                          srs: str | None = None,
                          image_format: str = "image/png",
                          styles: str = "", transparent: bool = True,
                          download: bool = False,
                          filename: str = "map.png") -> dict:
    """Build a WMS 1.1.1 GetMap URL (and optionally download the PNG).

    ``layers`` is a comma-separated list; each is qualified with ``workspace``
    when given. ``bbox`` is ``minx,miny,maxx,maxy``. With ``download=True`` the
    image is fetched and saved under ``GEO_MAP_OUTPUT_DIR``.
    """
    client = get_client()
    ws = workspace or client.settings.default_workspace
    qlayers = ",".join(qualified_name(l.strip(), ws) for l in layers.split(","))
    params = wms_getmap_params(qlayers, bbox, width, height,
                               resolve_srs(srs), image_format, styles, transparent)
    url = client.build_ows_url(params, base=client.settings.wms_base)
    result = {"url": url, "layers": qlayers}
    if download:
        resp = await client.ows(params, base=client.settings.wms_base)
        out_dir = Path(client.settings.map_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(resp.content)
        result["saved"] = str(path)
    return result


async def geo_wfs_get_capabilities(workspace: str | None = None) -> dict:
    """Fetch the WFS GetCapabilities document (returned as raw XML text)."""
    client = get_client()
    params = {"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"}
    base = f"{client.settings.url}/{workspace}/ows" if workspace else None
    resp = await client.ows(params, base=base)
    return {"xml": resp.text}


async def geo_wfs_get_feature(type_name: str, workspace: str | None = None,
                              count: int = 50, bbox: str | None = None,
                              cql_filter: str | None = None,
                              property_names: str | None = None,
                              srs_name: str | None = None) -> dict:
    """Query features via WFS 2.0.0 and return GeoJSON.

    ``type_name`` is qualified with ``workspace`` when given. Pass either
    ``bbox`` or ``cql_filter`` (not both).
    """
    client = get_client()
    ws = workspace or client.settings.default_workspace
    qname = qualified_name(type_name, ws)
    params = wfs_getfeature_params(qname, count, bbox, cql_filter,
                                   property_names, srs_name)
    resp = await client.ows(params)
    return json.loads(resp.text)


async def geo_wfs_transaction(operation: str, type_name: str,
                              workspace: str | None = None,
                              feature_ids: list[str] | None = None,
                              cql_filter: str | None = None,
                              properties: dict | None = None,
                              raw_xml: str | None = None) -> dict:
    """Run a WFS-T 1.1.0 transaction: ``delete``, ``update`` or ``raw``.

    - ``delete`` / ``update``: target features by ``feature_ids`` or by resolving
      ``cql_filter`` to ids (via GetFeature). ``update`` also needs
      ``properties`` (name → value). The namespace prefix/URI is resolved from
      ``/rest/namespaces/{workspace}``.
    - ``raw``: POST the caller-supplied ``raw_xml`` verbatim.
    """
    client = get_client()
    if operation == "raw":
        if not raw_xml:
            raise ValueError("operation='raw' requires raw_xml.")
        xml = raw_xml
    elif operation in ("delete", "update"):
        ws = resolve_workspace(workspace)
        prefix, uri = await _resolve_namespace(client, ws)
        qname = qualified_name(type_name, ws)
        ids = list(feature_ids or [])
        if not ids and cql_filter:
            ids = await _resolve_feature_ids(client, qname, cql_filter)
        if not ids:
            raise ValueError(
                "No target features: provide feature_ids or a cql_filter that "
                "matches at least one feature."
            )
        if operation == "update" and not properties:
            raise ValueError("operation='update' requires properties.")
        xml = build_wfst_xml(operation, type_name, prefix, uri,
                             feature_ids=ids, properties=properties)
    else:
        raise ValueError(
            f"Unknown operation '{operation}'. Use delete, update or raw."
        )

    resp = await client.ows(method="POST", content=xml.encode("utf-8"),
                            headers={"Content-Type": "text/xml; charset=UTF-8"})
    return {"operation": operation, "request_xml": xml, "response": resp.text}
