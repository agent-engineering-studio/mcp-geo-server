"""Feature type publishing tools."""

from __future__ import annotations

from ..client import get_client
from ..formatting import extract
from . import resolve_srs, resolve_workspace


async def geo_list_featuretypes(datastore: str, workspace: str | None = None,
                                only_available: bool = False) -> list:
    """List feature types in a datastore.

    With ``only_available=True`` returns the database tables that exist but are
    *not yet* published (``?list=available``), so you can pick one to publish
    with ``geo_publish_featuretype``.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    params = {"list": "available"} if only_available else None
    data = await client.get_json(
        f"workspaces/{ws}/datastores/{datastore}/featuretypes.json",
        params=params,
    )
    if only_available:
        # {"list": {"string": ["table_a", "table_b"]}}
        return extract(data, "list", "string")
    return extract(data, "featureTypes", "featureType")


async def geo_publish_featuretype(
    datastore: str,
    table: str,
    workspace: str | None = None,
    name: str | None = None,
    srs: str | None = None,
    title: str | None = None,
    abstract: str | None = None,
) -> dict:
    """Publish a database table as a layer (feature type).

    POSTs with ``?recalculate=nativebbox,latlonbbox`` so GeoServer computes the
    bounding boxes from the data. ``name`` defaults to the table name.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    layer_name = name or table
    ft: dict = {
        "name": layer_name,
        "nativeName": table,
        "srs": resolve_srs(srs),
        "enabled": True,
    }
    if title:
        ft["title"] = title
    if abstract:
        ft["abstract"] = abstract
    await client.post(
        f"workspaces/{ws}/datastores/{datastore}/featuretypes.json",
        json={"featureType": ft},
        headers={"Content-Type": "application/json"},
        params={"recalculate": "nativebbox,latlonbbox"},
    )
    return {"published": layer_name, "table": table, "workspace": ws,
            "datastore": datastore, "srs": ft["srs"]}
