"""SLD style management tools."""

from __future__ import annotations

from pathlib import Path

from ..client import get_client
from ..formatting import extract
from . import resolve_workspace

SLD_CONTENT_TYPE = "application/vnd.ogc.sld+xml"


def styles_path(workspace: str | None) -> str:
    """Return the REST path prefix for styles (workspace-scoped or global)."""
    if workspace:
        return f"workspaces/{workspace}/styles"
    return "styles"


def load_sld(sld: str | None, sld_file: str | None) -> str:
    """Resolve the SLD body from an inline string or a file path."""
    if sld and sld_file:
        raise ValueError("Provide either 'sld' or 'sld_file', not both.")
    if sld:
        return sld
    if sld_file:
        return Path(sld_file).read_text(encoding="utf-8")
    raise ValueError("An SLD body is required: pass 'sld' or 'sld_file'.")


async def geo_list_styles(workspace: str | None = None) -> list:
    """List styles (global, or within a workspace if given)."""
    client = get_client()
    data = await client.get_json(f"{styles_path(workspace)}.json")
    return extract(data, "styles", "style")


async def geo_get_style(name: str, workspace: str | None = None) -> dict:
    """Get a style's SLD body as text."""
    client = get_client()
    sld = await client.get_text(f"{styles_path(workspace)}/{name}.sld",
                                accept=SLD_CONTENT_TYPE)
    return {"name": name, "workspace": workspace, "sld": sld}


async def geo_create_style(name: str, sld: str | None = None,
                           sld_file: str | None = None,
                           workspace: str | None = None) -> dict:
    """Create a style from an inline SLD string or an SLD file path."""
    client = get_client()
    body = load_sld(sld, sld_file)
    await client.post(
        styles_path(workspace),
        content=body.encode("utf-8"),
        headers={"Content-Type": SLD_CONTENT_TYPE},
        params={"name": name},
    )
    return {"created": name, "workspace": workspace}


async def geo_update_style(name: str, sld: str | None = None,
                           sld_file: str | None = None,
                           workspace: str | None = None) -> dict:
    """Replace a style's SLD body (PUT)."""
    client = get_client()
    body = load_sld(sld, sld_file)
    await client.put(
        f"{styles_path(workspace)}/{name}",
        content=body.encode("utf-8"),
        headers={"Content-Type": SLD_CONTENT_TYPE},
    )
    return {"updated": name, "workspace": workspace}


async def geo_assign_style_to_layer(layer: str, style: str,
                                    workspace: str | None = None,
                                    default: bool = True) -> dict:
    """Assign a style to a layer.

    With ``default=True`` sets it as the layer's default style (PUT); with
    ``default=False`` adds it to the layer's additional styles (POST).
    """
    client = get_client()
    ws = resolve_workspace(workspace)
    if default:
        await client.put(
            f"workspaces/{ws}/layers/{layer}.json",
            json={"layer": {"defaultStyle": {"name": style}}},
            headers={"Content-Type": "application/json"},
        )
    else:
        await client.post(
            f"workspaces/{ws}/layers/{layer}/styles.json",
            json={"style": {"name": style}},
            headers={"Content-Type": "application/json"},
        )
    return {"layer": layer, "style": style, "workspace": ws, "default": default}


async def geo_delete_style(name: str, workspace: str | None = None,
                           purge: bool = True) -> dict:
    """Delete a style (``purge=True`` also removes the SLD file on disk)."""
    client = get_client()
    params = {"purge": "true"} if purge else None
    await client.delete(f"{styles_path(workspace)}/{name}", params=params)
    return {"deleted": name, "workspace": workspace, "purge": purge}
