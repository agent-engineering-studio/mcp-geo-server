"""Remote MCP servers as agent tools (issue #8): config, probing, mounting."""

from __future__ import annotations

import asyncio
import socket

import pytest

from mcp_geo_server import remote_mcp
from mcp_geo_server.remote_mcp import RemoteServer, parse_servers

pytestmark = pytest.mark.filterwarnings("ignore")


# ---- config -----------------------------------------------------------------------
def test_parse_servers():
    got = parse_servers(" idrogeo=http://idrogeo-mcp:8767/mcp , Meteo=https://m.example/mcp ,")
    assert got == [RemoteServer("idrogeo", "http://idrogeo-mcp:8767/mcp"),
                   RemoteServer("meteo", "https://m.example/mcp")]
    assert parse_servers("") == [] and parse_servers(None) == []


@pytest.mark.parametrize("raw, msg", [
    ("idrogeo", "expected name=url"),
    ("=http://x/mcp", "expected name=url"),
    ("9x=http://x/mcp", "invalid name"),
    ("a-b=http://x/mcp", "invalid name"),
    ("a=ftp://x/mcp", "must be http"),
    ("a=http://x/mcp,a=http://y/mcp", "duplicate"),
])
def test_parse_servers_rejects(raw, msg):
    with pytest.raises(ValueError, match=msg):
        parse_servers(raw)


def test_malformed_env_is_logged_not_fatal(monkeypatch, caplog):
    monkeypatch.setenv("GEO_EXTRA_MCP_SERVERS", "broken")
    assert remote_mcp.servers_from_env() == []
    assert "no remote MCP server will be used" in caplog.text


def test_instructions(monkeypatch):
    r = remote_mcp.RemoteTools([], {"idrogeo": ["idrogeo_area_risk", "idrogeo_webcams"]}, [])
    monkeypatch.setenv("GEO_EXTRA_MCP_INSTRUCTIONS", "Use idrogeo for landslides.")
    text = remote_mcp.instructions_for(r)
    assert "- idrogeo: idrogeo_area_risk, idrogeo_webcams" in text
    assert text.endswith("Use idrogeo for landslides.")
    assert remote_mcp.instructions_for(remote_mcp.RemoteTools([], {}, [])) == ""


# ---- probing against a real MCP server over streamable HTTP --------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def remote_url():
    """A tiny real MCP v2 server (two tools) on a local port."""
    import uvicorn
    from mcp.server import MCPServer

    srv = MCPServer("fake-idrogeo")

    @srv.tool()
    def area_risk(area: str) -> str:
        """Risk indicators of an area."""
        return f"risk of {area}"

    @srv.tool()
    def webcams() -> str:
        """Webcams."""
        return "none"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(srv.streamable_http_app(stateless_http=True),
                                           host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


async def test_reachable_and_unreachable(remote_url):
    dead = f"http://127.0.0.1:{_free_port()}/mcp"
    r = await remote_mcp.reachable_tools(
        [RemoteServer("idrogeo", remote_url), RemoteServer("spento", dead)], timeout=10)
    assert r.servers == {"idrogeo": ["idrogeo_area_risk", "idrogeo_webcams"]}
    assert r.unreachable == ["spento"]
    (tool,) = r.tools
    assert tool.name == "idrogeo" and not tool.is_connected     # fresh, for the agent's loop


async def test_remote_tool_is_callable(remote_url):
    (s,) = parse_servers(f"idrogeo={remote_url}")
    async with remote_mcp.make_tool(s) as tool:
        fn = {f.name: f for f in tool.functions}["idrogeo_area_risk"]
        res = await fn.invoke(arguments={"area": "Sarno"})
        assert "risk of Sarno" in getattr(res[0], "text", str(res[0]))


async def test_probe_timeout():
    # A port that accepts TCP but never speaks HTTP: the probe must give up on time.
    async def silent(reader, writer):
        await reader.read()                     # accept, never answer

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await remote_mcp.probe(RemoteServer("muto", f"http://127.0.0.1:{port}/mcp"),
                                 timeout=0.5)
    elapsed = loop.time() - t0
    srv.close()           # no wait_closed(): the abandoned probe may keep its socket
    assert got is None and elapsed < 2


def test_no_servers_means_no_probing(monkeypatch):
    monkeypatch.delenv("GEO_EXTRA_MCP_SERVERS", raising=False)
    r = remote_mcp.reachable_tools_sync()
    assert r.tools == [] and r.servers == {}


async def test_sync_probe_inside_a_loop_mounts_nothing(caplog):
    r = remote_mcp.reachable_tools_sync([RemoteServer("x", "http://127.0.0.1:1/mcp")])
    assert r.tools == [] and "running event loop" in caplog.text


# ---- the agent ----------------------------------------------------------------------------------
@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setenv("GEO_LLM_PROVIDER", "ollama")


def test_agent_without_remote_is_unchanged(ollama):
    from mcp_geo_server.agent import INSTRUCTIONS, build_agent

    agent = build_agent()
    assert agent.mcp_tools == [] and "Remote MCP" not in (agent.default_options.get(
        "instructions") or INSTRUCTIONS)


async def test_agent_gets_remote_tools_and_instructions(ollama, remote_url):
    from mcp_geo_server.agent import build_agent

    r = await remote_mcp.reachable_tools([RemoteServer("idrogeo", remote_url)], timeout=10)
    agent = build_agent(r)
    assert [t.name for t in agent.mcp_tools] == ["idrogeo"]
    assert "idrogeo: idrogeo_area_risk" in agent.default_options["instructions"]


async def test_geoserver_agent_rebuilds_after_a_failure(ollama, monkeypatch):
    from mcp_geo_server import server

    built = []

    class Boom:
        async def run(self, task):
            raise ConnectionError("remote went away")

    def fake_build(remote=None):
        built.append(remote)
        return Boom()

    async def no_remote():
        return remote_mcp.RemoteTools([], {}, [])

    monkeypatch.setattr("mcp_geo_server.agent.build_agent", fake_build)
    monkeypatch.setattr(remote_mcp, "reachable_tools", no_remote)
    monkeypatch.setattr(server, "_AGENT", None)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await server.geoserver_agent("x")
    assert len(built) == 2                      # re-probed and rebuilt on the 2nd call
