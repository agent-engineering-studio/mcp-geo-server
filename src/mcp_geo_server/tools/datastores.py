"""PostGIS datastore management tools."""

from __future__ import annotations

from ..client import get_client
from ..formatting import extract, unwrap
from . import resolve_workspace


def _connection_parameters(host: str, port: int, database: str, user: str,
                           password: str, schema: str) -> dict:
    """Build the GeoServer ``connectionParameters`` block for PostGIS.

    GeoServer expects the entries as an array of ``{"@key": k, "$": v}`` objects.
    """
    entries = {
        "host": host,
        "port": str(port),
        "database": database,
        "user": user,
        "passwd": password,
        "schema": schema,
        "dbtype": "postgis",
    }
    return {"entry": [{"@key": k, "$": v} for k, v in entries.items()]}


async def geo_list_datastores(workspace: str | None = None) -> list:
    """List datastores in a workspace (defaults to the configured workspace)."""
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/datastores.json")
    return extract(data, "dataStores", "dataStore")


async def geo_get_datastore(name: str, workspace: str | None = None) -> dict:
    """Get a single datastore by name."""
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/datastores/{name}.json")
    return unwrap(data, "dataStore")


async def geo_create_datastore_postgis(
    name: str,
    host: str,
    database: str,
    user: str,
    password: str,
    port: int = 5432,
    db_schema: str = "public",
    workspace: str | None = None,
) -> dict:
    """Create a PostGIS datastore in a workspace.

    Builds the ``connectionParameters`` (``dbtype=postgis``) and POSTs the new
    store. ``db_schema`` is the PostgreSQL schema (default ``public``). The
    PostGIS server must be reachable from GeoServer.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    body = {
        "dataStore": {
            "name": name,
            "connectionParameters": _connection_parameters(
                host, port, database, user, password, db_schema
            ),
        }
    }
    await client.post(
        f"workspaces/{ws}/datastores.json",
        json=body,
        headers={"Content-Type": "application/json"},
    )
    return {"created": name, "workspace": ws, "host": host, "database": database}


async def geo_delete_datastore(name: str, workspace: str | None = None,
                               recurse: bool = False) -> dict:
    """Delete a datastore (use ``recurse=True`` to drop its feature types too)."""
    client = get_client()
    ws = resolve_workspace(workspace)
    params = {"recurse": "true"} if recurse else None
    await client.delete(f"workspaces/{ws}/datastores/{name}.json", params=params)
    return {"deleted": name, "workspace": ws, "recurse": recurse}
