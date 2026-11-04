"""MCP surface over the real SDK, in memory — no GeoServer, no LLM call.

Building the server only constructs the chat client; the LLM is reached lazily,
so listing tools exercises the protocol without any network.
"""

from __future__ import annotations

import pytest

mcp = pytest.importorskip("mcp")

from mcp_geo_server.server import MCP_SERVER_NAME, build_mcp_server  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setenv("GEO_LLM_PROVIDER", "ollama")


async def test_server_speaks_current_protocol(ollama):
    async with mcp.Client(build_mcp_server()) as client:
        assert client.protocol_version == "2026-07-28"


async def test_lists_the_single_agent_tool(ollama):
    async with mcp.Client(build_mcp_server()) as client:
        result = await client.list_tools()
        tools = getattr(result, "tools", result)
        assert [t.name for t in tools] == ["geoserver-agent"]
        schema = tools[0].input_schema
        assert schema and schema.get("type") == "object"


def test_server_name():
    assert MCP_SERVER_NAME == "mcp-geo-server"
