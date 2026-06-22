"""Round-trip integration tests against a real GeoServer.

Skipped unless GEO_RUN_INTEGRATION=1. Requires GEOSERVER_URL / GEOSERVER_USER /
GEOSERVER_PASSWORD pointing at a running instance (see docker-compose.yml).
"""

from __future__ import annotations

import os

import pytest

from mcp_geo_server.client import GeoServerError, get_client
from mcp_geo_server.formatting import extract

pytestmark = pytest.mark.skipif(
    os.environ.get("GEO_RUN_INTEGRATION") != "1",
    reason="set GEO_RUN_INTEGRATION=1 to run live GeoServer tests",
)

_TEST_WS = "mcp_geo_it_ws"


async def test_status_live():
    client = get_client()
    data = await client.get_json("about/version.json")
    assert "about" in data


async def test_workspace_roundtrip_live():
    client = get_client()
    # Clean any leftover from a previous failed run.
    try:
        await client.delete(f"workspaces/{_TEST_WS}.json", params={"recurse": "true"})
    except GeoServerError:
        pass

    await client.post("workspaces.json", json={"workspace": {"name": _TEST_WS}},
                      headers={"Content-Type": "application/json"})

    listing = extract(await client.get_json("workspaces.json"), "workspaces", "workspace")
    assert any(w.get("name") == _TEST_WS for w in listing)

    await client.delete(f"workspaces/{_TEST_WS}.json", params={"recurse": "true"})

    listing = extract(await client.get_json("workspaces.json"), "workspaces", "workspace")
    assert not any(w.get("name") == _TEST_WS for w in listing)
