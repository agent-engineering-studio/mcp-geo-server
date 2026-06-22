"""Expose the GeoServer agent as an MCP server (Microsoft Agent Framework).

The agent (LLM + geo_* tools) is wrapped with ``agent.as_mcp_server()`` and
served over a transport selected by ``GEO_MCP_TRANSPORT``:

* ``stdio`` (default) — for MCP clients that launch the process (Claude Desktop…)
* ``http``           — streamable-HTTP on ``GEO_MCP_HOST``:``GEO_MCP_PORT``,
                       so it runs as a long-lived, visible container.
"""

from __future__ import annotations

import contextlib
import logging
import os

from . import __version__
from .agent import build_agent
from .config import get_settings

logger = logging.getLogger("mcp_geo_server")

MCP_SERVER_NAME = "mcp-geo-server"
MCP_INSTRUCTIONS = (
    "Single intelligent tool to administer and query a GeoServer instance. "
    "Send a natural-language request (e.g. 'how many features in topp:states?', "
    "'create workspace demo', 'publish table roads from datastore pg')."
)


def build_mcp_server():
    """Build the low-level MCP ``Server`` that fronts the GeoServer agent."""
    agent = build_agent()
    return agent.as_mcp_server(
        server_name=MCP_SERVER_NAME,
        version=__version__,
        instructions=MCP_INSTRUCTIONS,
    )


async def _serve_stdio(server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def _build_http_app(server):
    """Wrap the MCP server in a Starlette app using streamable HTTP."""
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


def _configure_logging() -> None:
    level = os.environ.get("GEO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO))


def run() -> None:
    """Run the MCP server over the configured transport."""
    _configure_logging()
    settings = get_settings()
    server = build_mcp_server()

    if settings.mcp_transport == "http":
        import uvicorn

        logger.info("Serving MCP over streamable-HTTP at http://%s:%d/mcp",
                    settings.mcp_host, settings.mcp_port)
        app = _build_http_app(server)
        uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
    else:
        import anyio

        logger.info("Serving MCP over stdio")
        anyio.run(_serve_stdio, server)


def main() -> None:
    """Console-script entry point (``mcp-geo-server``)."""
    run()


if __name__ == "__main__":
    main()
