"""Workspace management tools."""

from __future__ import annotations

from ..client import get_client
from ..formatting import extract, unwrap


async def geo_list_workspaces() -> list:
    """List all workspaces, returning their names and hrefs."""
    client = get_client()
    data = await client.get_json("workspaces.json")
    return extract(data, "workspaces", "workspace")


async def geo_get_workspace(name: str) -> dict:
    """Get a single workspace by name."""
    client = get_client()
    data = await client.get_json(f"workspaces/{name}.json")
    return unwrap(data, "workspace")


async def geo_create_workspace(name: str, default: bool = False) -> dict:
    """Create a workspace.

    Set ``default=True`` to also make it the catalog default workspace.
    """
    client = get_client()
    params = {"default": "true"} if default else None
    await client.post(
        "workspaces.json",
        json={"workspace": {"name": name}},
        headers={"Content-Type": "application/json"},
        params=params,
    )
    return {"created": name, "default": default}


async def geo_delete_workspace(name: str, recurse: bool = False) -> dict:
    """Delete a workspace.

    With ``recurse=True`` GeoServer also deletes the stores/layers it contains;
    otherwise deletion fails if the workspace is not empty.
    """
    client = get_client()
    params = {"recurse": "true"} if recurse else None
    await client.delete(f"workspaces/{name}.json", params=params)
    return {"deleted": name, "recurse": recurse}
