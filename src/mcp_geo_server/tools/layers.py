"""Layer management tools."""

from __future__ import annotations

from ..client import get_client
from ..formatting import extract, unwrap
from . import resolve_workspace


async def _featuretype(client, ws: str, name: str) -> dict:
    data = await client.get_json(f"workspaces/{ws}/featuretypes/{name}.json")
    return unwrap(data, "featureType")


async def geo_list_layers(workspace: str | None = None) -> list:
    """List published layers in a workspace (defaults to the configured one)."""
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/layers.json")
    return extract(data, "layers", "layer")


async def geo_get_layer(name: str, workspace: str | None = None) -> dict:
    """Get a layer's definition (default style, enabled flag, resource)."""
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/layers/{name}.json")
    return unwrap(data, "layer")


async def geo_get_layer_bbox(name: str, workspace: str | None = None) -> dict:
    """Return a layer's bounding boxes and SRS.

    Reads the underlying feature type and returns ``latLonBoundingBox``,
    ``nativeBoundingBox`` and ``srs`` — handy for centering a map.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    ft = await _featuretype(client, ws, name)
    return {
        "srs": ft.get("srs"),
        "latLonBoundingBox": ft.get("latLonBoundingBox"),
        "nativeBoundingBox": ft.get("nativeBoundingBox"),
    }


async def geo_update_layer(name: str, workspace: str | None = None,
                           default_style: str | None = None,
                           enabled: bool | None = None) -> dict:
    """Update a layer's default style and/or enabled flag."""
    client = get_client()
    ws = resolve_workspace(workspace)
    layer: dict = {}
    if default_style is not None:
        layer["defaultStyle"] = {"name": default_style}
    if enabled is not None:
        layer["enabled"] = enabled
    if not layer:
        raise ValueError("Nothing to update: set default_style and/or enabled.")
    await client.put(
        f"workspaces/{ws}/layers/{name}.json",
        json={"layer": layer},
        headers={"Content-Type": "application/json"},
    )
    return {"updated": name, "workspace": ws,
            "default_style": default_style, "enabled": enabled}


async def geo_delete_layer(name: str, workspace: str | None = None,
                           datastore: str | None = None,
                           recurse: bool = True) -> dict:
    """Delete a layer.

    If ``datastore`` is given, also delete the backing feature type so the
    publication is fully cleaned up.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    params = {"recurse": "true"} if recurse else None
    await client.delete(f"workspaces/{ws}/layers/{name}.json", params=params)
    deleted = {"deleted_layer": name, "workspace": ws}
    if datastore:
        await client.delete(
            f"workspaces/{ws}/datastores/{datastore}/featuretypes/{name}.json",
            params={"recurse": "true"},
        )
        deleted["deleted_featuretype"] = name
        deleted["datastore"] = datastore
    return deleted
