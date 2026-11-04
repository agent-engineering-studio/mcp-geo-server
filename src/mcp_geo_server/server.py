"""The MCP server: GeoServer as typed tools, plus the map assistant.

Two modes (``GEO_MCP_MODE``):

* ``tools`` (default) — an ``MCPServer`` (mcp SDK v2, protocol 2026-07-28)
  exposing directly:

  - the 33 ``geo_*`` operations, each with its own input schema and
    ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint`` annotations.
    Destructive ones stay blocked unless ``GEO_ALLOW_DESTRUCTIVE=true``;
  - the map assistant's high-level tools: ``geo_search_layers`` (catalog
    search, no LLM), ``geo_describe_layer``, ``geo_admin_scope`` (bbox + clip of
    a comune/provincia/regione) and ``geo_resolve_request`` (the chat's full
    answer: layers in draw order, per-layer CQL, area clip, terrain metrics);
  - ``geoserver-agent``, the natural-language agent of the previous versions
    (``GEO_MCP_AGENT_TOOL=false`` hides it);
  - resources ``geo://catalog`` and ``geo://layer/{workspace}/{name}``, and
    prompts for common map tasks.

  The client's own model picks the operation, so most calls need no LLM on
  the server at all.

* ``agent`` — the previous behaviour: one opaque tool backed by the server-side
  agent (``agent.as_mcp_server()``).

Transport (``GEO_MCP_TRANSPORT``): ``stdio`` or ``http`` (streamable HTTP at
``/mcp`` on ``GEO_MCP_HOST``:``GEO_MCP_PORT``, stateless).
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os

from . import __version__
from .config import get_settings
from .retrieval import match_score as search_score

logger = logging.getLogger("mcp_geo_server")

MCP_SERVER_NAME = "mcp-geo-server"
AGENT_INSTRUCTIONS = (
    "Single intelligent tool to administer and query a GeoServer instance. "
    "Send a natural-language request (e.g. 'how many features in topp:states?', "
    "'create workspace demo', 'publish table roads from datastore pg')."
)
TOOLS_INSTRUCTIONS = (
    "Tools to explore, query and administer a GeoServer instance.\n"
    "- To SHOW data for a request in plain language (\"frane in Toscana\", "
    "\"pericolosità elevata a Bari\"), call geo_resolve_request: it returns the "
    "layers in draw order, the per-layer CQL filters, the zoom bbox and the area "
    "clip, ready for WMS. Pass back its map state as `context` to refine it.\n"
    "- To FIND layers, use geo_search_layers or read the geo://catalog resource; "
    "geo_describe_layer gives geometry, fields and feature count.\n"
    "- geo_admin_scope gives the bbox and boundary of a comune/provincia/regione.\n"
    "- The geo_* tools are the raw GeoServer operations (REST, WMS, WFS, styles). "
    "Destructive ones (delete, WFS-T) are refused unless the server allows them."
)

# ---- tool classification ------------------------------------------------------------
WRITE_TOOLS = frozenset({
    "geo_create_workspace", "geo_create_datastore_postgis",
    "geo_create_coveragestore_geotiff", "geo_publish_featuretype", "geo_create_style",
})
IDEMPOTENT_TOOLS = frozenset({
    "geo_update_layer", "geo_update_style", "geo_assign_style_to_layer",
})


def annotations_for(name: str):
    """MCP tool annotations from the tool's kind (read / write / idempotent / destructive)."""
    from mcp.types import ToolAnnotations

    from .middleware import DESTRUCTIVE_TOOLS

    destructive = name in DESTRUCTIVE_TOOLS
    mutating = destructive or name in WRITE_TOOLS or name in IDEMPOTENT_TOOLS
    return ToolAnnotations(
        title=name.removeprefix("geo_").replace("_", " ").capitalize(),
        read_only_hint=not mutating,
        destructive_hint=destructive,
        idempotent_hint=(not mutating) or name in IDEMPOTENT_TOOLS,
        open_world_hint=False,
    )


def guarded(fn):
    """Refuse a destructive tool unless GEO_ALLOW_DESTRUCTIVE (same rule as the agent)."""
    from .middleware import DESTRUCTIVE_TOOLS, _refusal

    if fn.__name__ not in DESTRUCTIVE_TOOLS:
        return fn

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not get_settings().allow_destructive:
            return _refusal(fn.__name__)
        return await fn(*args, **kwargs)

    return wrapper


# ---- high-level tools -------------------------------------------------------------------
def _qualified(layer: str) -> tuple[str, str]:
    ws, _, name = layer.partition(":")
    if not name:
        ws, name = get_settings().default_workspace or "", ws
    if not ws:
        raise ValueError("pass a qualified layer name (workspace:name)")
    return ws, name




async def geo_search_layers(query: str, limit: int = 20) -> dict:
    """Search the published layers by subject or place (no LLM call).

    Matches the words of ``query`` against each layer's name, title, keywords
    and abstract. Returns up to ``limit`` layers, best first, each with its
    qualified name, title, keywords and the start of its abstract. Use the
    qualified names with the other tools.
    """
    from .answer import get_assistant

    catalog = await get_assistant().load_catalog()
    scored = []
    for i, m in enumerate(catalog):
        s = search_score(query, " ".join([m.name, m.title, " ".join(m.keywords), m.abstract]))
        if s:
            scored.append((-s, i, m))
    scored.sort()
    hits = [{"layer": m.qualified, "title": m.title or m.name,
             "keywords": [k for k in m.keywords if k.lower() not in ("features", "metadata:ai")],
             "abstract": m.abstract[:200], "score": -s}
            for s, _, m in scored[:max(1, min(limit, 200))]]
    return {"query": query, "total_layers": len(catalog), "results": hits}


async def geo_describe_layer(layer: str) -> dict:
    """Describe one layer: geometry kind (or raster), fields, feature count, bbox.

    ``layer`` is ``workspace:name``.
    """
    from .answer import get_assistant

    ws, name = _qualified(layer)
    a = get_assistant()
    info = await a.layer_info(ws, name)
    if info is None:
        return {"layer": f"{ws}:{name}", "found": False}
    return {"layer": f"{ws}:{name}", "found": True, **info,
            "bbox": await a.layer_bbox(ws, name)}


async def geo_admin_scope(place: str) -> dict:
    """Find an administrative unit named in ``place`` and return its scope.

    E.g. "provincia di Bari", "Valle d'Aosta". Returns the level, name, the
    lat/lon ``bbox`` to zoom to and ``clip`` (an EWKT polygon, EPSG:4326, already
    simplified to fit a WMS URL): use it as WMS ``clip`` for rasters or inside a
    CQL ``INTERSECTS(geom, <wkt>)`` for vectors. ``found: false`` if no unit
    matches.
    """
    from .answer import get_assistant

    a = get_assistant()
    hit = await a.detect_admin(place)
    if not hit:
        return {"place": place, "found": False}
    scope = await a.admin_scope(*hit)
    if not scope:
        return {"place": place, "found": False, "level": hit[0].level, "name": hit[1]}
    return {"place": place, "found": True, **scope}


async def geo_resolve_request(query: str, context: dict | None = None) -> dict:
    """Turn a map request in plain language into a ready-to-render answer.

    Same pipeline as the web chat (uses the server's LLM to pick layers):
    ``layers`` in draw order (bottom to top), ``cql_by_layer`` (attribute
    filter + area INTERSECTS, per layer), ``bbox`` to zoom to, ``clip`` for
    rasters, ``info`` (geometry, count, fields per layer), ``explanation``,
    ``confidence``, optional ``enrichment`` (terrain metrics) and
    ``cql_rejected`` when a filter was unsafe. For a follow-up ("solo i
    crolli"), pass ``context`` = {"layers", "cql_filter", "admin"} from the
    previous answer; the reply then has ``mode`` "refine".
    """
    from .answer import AskError, get_assistant

    if not query.strip():
        raise ValueError("empty query")
    try:
        return await get_assistant().ask(query.strip(), context)
    except AskError as exc:
        return {"query": query, "error": str(exc), "layers": []}


async def geo_count_features(layer: str, cql_filter: str | None = None,
                             place: str | None = None, group_by: str | None = None) -> dict:
    """Exact feature count of a layer, optionally inside an area and per class.

    ``cql_filter``: attribute filter in simple CQL (=, <>, <, >, IN, LIKE,
    BETWEEN, IS NULL, AND/OR/NOT). ``place``: an administrative unit ("provincia
    di Bari") to count inside. ``group_by``: an attribute to break the count
    down by (configured classes, else its real values). Counts come from WFS
    ``resultType=hits``: exact, no sampling.
    """
    from . import analytics
    from . import cql as cqlmod
    from .answer import get_assistant

    ws, name = _qualified(layer)
    q = f"{ws}:{name}"
    a = get_assistant()
    info = await a.layer_info(ws, name)
    if not info or info.get("geometry") == "raster":
        return {"layer": q, "error": "not a vector layer"}
    fields = set(info.get("fields") or [])
    attr_filter = None
    if cql_filter:
        try:
            attr_filter = cqlmod.sanitize(cql_filter, allowed_attributes=fields)
        except cqlmod.CQLError as exc:
            return {"layer": q, "error": f"cql_filter rejected: {exc}"}
    area = None
    area_filter = None
    if place:
        scope = await geo_admin_scope(place)
        if not scope.get("found"):
            return {"layer": q, "error": f"no administrative unit found in {place!r}"}
        area = {"level": scope["level"], "name": scope["name"]}
        wkt = scope["clip"].split(";", 1)[-1]
        area_filter = f"INTERSECTS({info.get('geometry_column') or 'geom'}, {wkt})"
    flt = analytics.combine(attr_filter, area_filter)
    out = {"layer": q, "cql_filter": attr_filter, "area": area,
           "total": await analytics.count(q, flt)}
    if group_by:
        if group_by not in fields:
            out["error"] = f"{group_by} is not an attribute of {q}"
            return out
        values = await a.group_values(q, group_by, a.filter_spec())
        if len(values) > analytics.MAX_GROUPS:
            out["error"] = f"{group_by} has too many distinct values to group by"
            return out
        out["group_by"] = group_by
        out["groups"] = await analytics.count_by(q, group_by, values, flt)
    return out


async def geo_area_report(place: str, subject: str | None = None,
                          narrative: bool = False) -> dict:
    """Fact sheet of the data in an administrative area, with exact counts.

    For ``place`` (e.g. "provincia di Bari") and an optional ``subject``
    ("frane"): which vector layers have features inside the area, how many,
    and the breakdown per configured class; rasters covering it. Returns the
    facts, a summary and a Markdown report. ``narrative`` adds a short LLM
    synthesis, kept only if every number in it appears in the facts.
    """
    from . import report

    rep = await report.area_report(place, subject, narrative=narrative)
    return {k: rep[k] for k in ("facts", "summary", "narrative", "narrative_rejected",
                                "markdown")}


_AGENT = None


async def geoserver_agent(task: str) -> str:
    """Do a GeoServer task described in natural language, with the server's agent.

    The server-side agent picks and chains the geo_* operations itself. Prefer
    calling those tools directly when you know which one you need.
    """
    global _AGENT
    if _AGENT is None:
        from .agent import build_agent
        from .remote_mcp import reachable_tools
        _AGENT = build_agent(await reachable_tools())
    try:
        result = await _AGENT.run(task)
    except Exception:
        # e.g. a remote MCP server went away mid-life: rebuild (and re-probe)
        # on the next call instead of failing forever.
        _AGENT = None
        raise
    return result.text


HIGH_LEVEL_TOOLS = (geo_search_layers, geo_describe_layer, geo_admin_scope,
                    geo_resolve_request, geo_count_features, geo_area_report)


# ---- server ------------------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw in (None, "") else raw.strip().lower() in {"1", "true", "yes", "on"}


def build_tools_server():
    """The ``tools``-mode MCPServer (mcp SDK v2)."""
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    from .tools import collect_tools

    srv = MCPServer(MCP_SERVER_NAME, title="GeoServer", version=__version__,
                    instructions=TOOLS_INSTRUCTIONS)

    for fn in HIGH_LEVEL_TOOLS:
        srv.add_tool(fn, name=fn.__name__, annotations=ToolAnnotations(
            title=fn.__name__.removeprefix("geo_").replace("_", " ").capitalize(),
            read_only_hint=True, destructive_hint=False, idempotent_hint=True,
            open_world_hint=False))
    for fn in collect_tools():
        srv.add_tool(guarded(fn), name=fn.__name__, annotations=annotations_for(fn.__name__))
    if _env_bool("GEO_MCP_AGENT_TOOL", True):
        srv.add_tool(geoserver_agent, name="geoserver-agent",
                     annotations=ToolAnnotations(title="GeoServer agent",
                                                 read_only_hint=False,
                                                 destructive_hint=False,
                                                 open_world_hint=False))

    @srv.resource("geo://catalog", name="catalog", mime_type="application/json",
                  description="Every published layer: qualified name, title, keywords, "
                              "abstract (from the WMS capabilities).")
    async def catalog_resource() -> str:
        from .answer import get_assistant
        cat = await get_assistant().load_catalog()
        return json.dumps([{"layer": m.qualified, "title": m.title or m.name,
                            "keywords": list(m.keywords), "abstract": m.abstract}
                           for m in cat], ensure_ascii=False)

    @srv.resource("geo://layer/{workspace}/{name}", name="layer",
                  mime_type="application/json",
                  description="One layer's geometry, fields, feature count and bbox.")
    async def layer_resource(workspace: str, name: str) -> str:
        return json.dumps(await geo_describe_layer(f"{workspace}:{name}"),
                          ensure_ascii=False)

    @srv.prompt(name="show-on-map", title="Show data on a map",
                description="Find and display the data for a request in plain language.")
    def show_on_map(request: str) -> str:
        return (f"Show on a map: {request}\n\n"
                "Call geo_resolve_request with this request. Then describe what is "
                "shown (layers, filter, area, feature counts from `info`), and "
                "mention any `cql_rejected` filter or empty layer. If the user then "
                "narrows it down, call geo_resolve_request again with `context` set "
                "to the previous answer's layers, cql_filter and admin.")

    @srv.prompt(name="area-overview", title="Overview of an area",
                description="Summarize the available data for an administrative area.")
    def area_overview(place: str, subject: str = "") -> str:
        topic = f" about {subject}" if subject else ""
        return (f"Give an overview of the data{topic} for {place}.\n\n"
                f"1. geo_admin_scope('{place}') for the area's bbox and boundary.\n"
                f"2. geo_search_layers to find the relevant layers{topic}.\n"
                "3. geo_describe_layer on the most relevant ones; geo_wfs_get_feature "
                "with the area's INTERSECTS filter to count features inside the area.\n"
                "4. Summarize: what data exists, how much falls in the area, and "
                "what a map would show. Say clearly when a count is approximate.")

    return srv


def build_agent_server():
    """The ``agent``-mode low-level Server: one tool backed by the server's agent."""
    from .agent import build_agent
    from .remote_mcp import reachable_tools_sync, servers_from_env

    # No event loop runs yet here (the transport starts it), so probe the
    # remote servers synchronously; only reachable ones are mounted.
    remote = reachable_tools_sync() if servers_from_env() else None
    return build_agent(remote).as_mcp_server(server_name=MCP_SERVER_NAME, version=__version__,
                                             instructions=AGENT_INSTRUCTIONS)


def mcp_mode() -> str:
    mode = (os.environ.get("GEO_MCP_MODE") or "tools").strip().lower()
    if mode not in ("tools", "agent"):
        raise RuntimeError(f"GEO_MCP_MODE must be 'tools' or 'agent', not '{mode}'")
    return mode


def build_mcp_server():
    """The server for the configured ``GEO_MCP_MODE``."""
    return build_tools_server() if mcp_mode() == "tools" else build_agent_server()


# ---- transports ---------------------------------------------------------------------------------
def _csv_env(name: str) -> list[str]:
    return [p.strip() for p in (os.environ.get(name) or "").split(",") if p.strip()]


def transport_security(host: str):
    """DNS-rebinding protection: the SDK enables it on loopback; on an open bind
    it applies when ``GEO_MCP_ALLOWED_HOSTS`` / ``GEO_MCP_ALLOWED_ORIGINS`` are set."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts, origins = _csv_env("GEO_MCP_ALLOWED_HOSTS"), _csv_env("GEO_MCP_ALLOWED_ORIGINS")
    if not (hosts or origins):
        return None
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


def build_http_app(server):
    """Starlette app serving ``server`` over streamable HTTP at ``/mcp``."""
    settings = get_settings()
    if hasattr(server, "streamable_http_app") and hasattr(server, "add_tool"):
        return server.streamable_http_app(
            stateless_http=True, host=settings.mcp_host,
            transport_security=transport_security(settings.mcp_host))
    return _legacy_http_app(server)


def _legacy_http_app(server):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


async def _serve_stdio_legacy(server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _configure_logging() -> None:
    level = os.environ.get("GEO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO))


def run() -> None:
    """Run the MCP server over the configured transport."""
    _configure_logging()
    from .telemetry import setup as setup_telemetry
    setup_telemetry("mcp-geo-server-mcp")
    settings = get_settings()
    server = build_mcp_server()
    mode = mcp_mode()

    if settings.mcp_transport == "http":
        import uvicorn

        logger.info("Serving MCP (%s mode) over streamable-HTTP at http://%s:%d/mcp",
                    mode, settings.mcp_host, settings.mcp_port)
        uvicorn.run(build_http_app(server), host=settings.mcp_host, port=settings.mcp_port)
    elif mode == "tools":
        logger.info("Serving MCP (tools mode) over stdio")
        server.run("stdio")
    else:
        import anyio

        logger.info("Serving MCP (agent mode) over stdio")
        anyio.run(_serve_stdio_legacy, server)


def main() -> None:
    """Console-script entry point (``mcp-geo-server``)."""
    run()


if __name__ == "__main__":
    main()

