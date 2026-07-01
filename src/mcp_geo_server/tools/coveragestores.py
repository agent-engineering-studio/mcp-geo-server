"""Coverage store (raster / GeoTIFF) management tools.

The vector side of the stack goes shapefile -> PostGIS -> feature type. Rasters
have no equivalent "push over the network": a GeoServer *coverage store* is a
pointer to a file on GeoServer's own filesystem. So a DTM/DEM GeoTIFF is
registered ``external`` (zero-copy) with a ``file:`` URL that GeoServer can read
— the file lives in the shared ``/data`` mount (see docker-compose.yml).

``geo_create_coveragestore_geotiff`` uses ``configure=first`` so GeoServer both
creates the store AND publishes its first coverage in one call.
"""

from __future__ import annotations

from ..client import get_client
from ..formatting import extract, unwrap
from . import resolve_workspace


async def geo_list_coveragestores(workspace: str | None = None) -> list:
    """List coverage (raster) stores in a workspace (defaults to configured)."""
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/coveragestores.json")
    return extract(data, "coverageStores", "coverageStore")


async def geo_get_coverage(name: str, workspace: str | None = None) -> dict:
    """Get a published coverage (raster layer) by name — includes its bbox/SRS.

    Uses the datastore-less coverage path so only the workspace + coverage name
    are needed.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    data = await client.get_json(f"workspaces/{ws}/coverages/{name}.json")
    return unwrap(data, "coverage")


async def geo_create_coveragestore_geotiff(
    name: str,
    file_url: str,
    workspace: str | None = None,
    coverage_name: str | None = None,
    title: str | None = None,
) -> dict:
    """Register a GeoTIFF as an external coverage store and publish its coverage.

    ``file_url`` is the location GeoServer reads the raster from — an absolute
    filesystem path (e.g. ``/data/dtm/lombardia.tif``) reachable on GeoServer's
    own filesystem (shared ``/data`` mount). NB: use a bare path, not a
    ``file://`` URL — GeoServer's external endpoint rejects the URL form on some
    distributions. ``configure=first`` makes GeoServer create the store and
    publish the first coverage in one PUT. ``coverage_name`` (default = store
    name) fixes the published layer name so it is deterministic; ``title`` sets
    a human-readable title on the coverage.
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    cov = coverage_name or name
    await client.put(
        f"workspaces/{ws}/coveragestores/{name}/external.geotiff",
        content=file_url,
        headers={"Content-Type": "text/plain"},
        params={"configure": "first", "coverageName": cov},
    )
    if title:
        # external.geotiff has no title slot — set it on the coverage after.
        # NB: use the store-qualified coverage path; the short
        # ``workspaces/{ws}/coverages/{cov}`` path is GET-only on some
        # distributions (kartoza 2.28 returns HTTP 405 on PUT).
        await client.put(
            f"workspaces/{ws}/coveragestores/{name}/coverages/{cov}.json",
            json={"coverage": {"title": title}},
            headers={"Content-Type": "application/json"},
        )
    return {"created": name, "coverage": cov, "workspace": ws, "file": file_url}


async def geo_delete_coveragestore(name: str, workspace: str | None = None,
                                   recurse: bool = False) -> dict:
    """Delete a coverage store (``recurse=True`` also drops its coverages).

    Only unregisters the store from GeoServer; the underlying GeoTIFF on disk is
    left untouched (it is referenced ``external``).
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    params = {"recurse": "true"} if recurse else None
    await client.delete(f"workspaces/{ws}/coveragestores/{name}.json", params=params)
    return {"deleted": name, "workspace": ws, "recurse": recurse}
