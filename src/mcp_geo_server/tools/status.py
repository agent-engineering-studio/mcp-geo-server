"""Diagnostics: GeoServer version and connectivity."""

from __future__ import annotations

from ..client import get_client


async def geo_get_status() -> dict:
    """Report GeoServer connectivity and version.

    Reads ``/rest/about/version.json`` and returns the parsed component
    versions plus a ``connected`` flag. Use this first to confirm the server is
    reachable and the credentials are valid.
    """
    client = get_client()
    data = await client.get_json("about/version.json")
    about = data.get("about", {}) if isinstance(data, dict) else {}
    resources = about.get("resource", [])
    if isinstance(resources, dict):
        resources = [resources]
    return {
        "connected": True,
        "url": client.settings.url,
        "components": resources,
    }
