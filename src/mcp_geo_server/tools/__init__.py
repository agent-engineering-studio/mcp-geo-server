"""GeoServer tool functions and shared helpers.

Every tool name is prefixed ``geo_``. The tools are plain async functions: they
are used as the *tools of the intelligent agent* (Microsoft Agent Framework),
which exposes itself as a single MCP server. ``collect_tools()`` returns the
full ordered list.
"""

from __future__ import annotations

from typing import Callable

from ..client import GeoServerClient, get_client
from ..config import get_settings


def resolve_workspace(workspace: str | None) -> str:
    """Return the given workspace or fall back to GEOSERVER_DEFAULT_WORKSPACE."""
    ws = workspace or get_settings().default_workspace
    if not ws:
        raise ValueError(
            "No workspace given and GEOSERVER_DEFAULT_WORKSPACE is not set."
        )
    return ws


def resolve_srs(srs: str | None) -> str:
    """Return the given SRS or fall back to GEOSERVER_DEFAULT_SRS."""
    return srs or get_settings().default_srs


def collect_tools() -> list[Callable]:
    """Return every ``geo_*`` tool function, in a stable catalogue order."""
    from . import (
        coveragestores,
        datastores,
        featuretypes,
        layers,
        map as map_module,
        ogc,
        status,
        styles,
        terrain,
        workspaces,
    )

    return [
        # diagnostics
        status.geo_get_status,
        # workspaces
        workspaces.geo_list_workspaces,
        workspaces.geo_get_workspace,
        workspaces.geo_create_workspace,
        workspaces.geo_delete_workspace,
        # datastores
        datastores.geo_list_datastores,
        datastores.geo_get_datastore,
        datastores.geo_create_datastore_postgis,
        datastores.geo_delete_datastore,
        # coverage stores (raster / GeoTIFF)
        coveragestores.geo_list_coveragestores,
        coveragestores.geo_get_coverage,
        coveragestores.geo_create_coveragestore_geotiff,
        coveragestores.geo_delete_coveragestore,
        # feature types
        featuretypes.geo_list_featuretypes,
        featuretypes.geo_publish_featuretype,
        # layers
        layers.geo_list_layers,
        layers.geo_get_layer,
        layers.geo_get_layer_bbox,
        layers.geo_update_layer,
        layers.geo_delete_layer,
        # styles
        styles.geo_list_styles,
        styles.geo_get_style,
        styles.geo_create_style,
        styles.geo_update_style,
        styles.geo_assign_style_to_layer,
        styles.geo_delete_style,
        # terrain analysis (DTM enrichment)
        terrain.geo_enrich_from_dtm,
        # OGC
        ogc.geo_wms_get_capabilities,
        ogc.geo_wms_get_map,
        ogc.geo_wfs_get_capabilities,
        ogc.geo_wfs_get_feature,
        ogc.geo_wfs_transaction,
        # map
        map_module.geo_build_web_map,
    ]


__all__ = [
    "GeoServerClient",
    "get_client",
    "collect_tools",
    "resolve_workspace",
    "resolve_srs",
]
