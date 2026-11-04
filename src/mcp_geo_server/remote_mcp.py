"""Remote MCP servers as extra tools for the GeoServer agent.

``GEO_EXTRA_MCP_SERVERS`` lists streamable-HTTP MCP servers whose tools the
agent may use next to the local ``geo_*`` ones, as comma-separated
``name=url`` pairs::

    GEO_EXTRA_MCP_SERVERS=idrogeo=http://idrogeo-mcp:8767/mcp

Each server becomes an Agent Framework ``MCPStreamableHTTPTool`` whose tools
are prefixed with the server name (``idrogeo_area_risk``), so they cannot
clash with the local ones or with each other.

Agent Framework connects MCP tools lazily, at every ``run()``: a server that
is down would make the WHOLE answer fail. So each server is probed first, with
a timeout (``GEO_EXTRA_MCP_TIMEOUT``, default 10 s); an unreachable one is
logged and left out, and the agent starts with what is reachable — with no
servers configured, exactly as before. The probe connects, lists the tools
and closes, in its own thread and event loop with a hard deadline; the agent
then gets FRESH, unconnected tool objects, which it connects inside its own
event loop (a connection opened in one loop cannot be used from another).

``GEO_EXTRA_MCP_INSTRUCTIONS`` (optional) is appended to the agent's
instructions, to say when to use which server (e.g. "use the idrogeo_* tools
for landslides, exposed population and monitoring").
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger("mcp_geo_server.remote_mcp")

DEFAULT_TIMEOUT = 10.0
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


@dataclass(frozen=True)
class RemoteServer:
    name: str
    url: str


def parse_servers(raw: str | None) -> list[RemoteServer]:
    """``"a=http://x/mcp, b=https://y/mcp"`` -> servers. Raises ``ValueError``.

    Names are lower-case identifiers (they become tool-name prefixes); URLs must
    be http(s); names must be unique.
    """
    out: list[RemoteServer] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        name, url = name.strip().lower(), url.strip()
        if not sep or not name or not url:
            raise ValueError(f"GEO_EXTRA_MCP_SERVERS: expected name=url, got {item!r}")
        if not _NAME.match(name):
            raise ValueError(f"GEO_EXTRA_MCP_SERVERS: invalid name {name!r} "
                             "(lower-case letters, digits, underscore; start with a letter)")
        if not re.match(r"^https?://", url):
            raise ValueError(f"GEO_EXTRA_MCP_SERVERS: {name}: URL must be http(s), got {url!r}")
        if any(s.name == name for s in out):
            raise ValueError(f"GEO_EXTRA_MCP_SERVERS: duplicate name {name!r}")
        out.append(RemoteServer(name, url))
    return out


def servers_from_env() -> list[RemoteServer]:
    """Configured servers; a malformed value is logged and ignored (never fatal)."""
    try:
        return parse_servers(os.environ.get("GEO_EXTRA_MCP_SERVERS"))
    except ValueError as exc:
        logger.error("%s — no remote MCP server will be used.", exc)
        return []


def timeout_from_env() -> float:
    raw = (os.environ.get("GEO_EXTRA_MCP_TIMEOUT") or "").strip()
    return float(raw) if raw else DEFAULT_TIMEOUT


def make_tool(server: RemoteServer):
    """A fresh, unconnected Agent Framework MCP tool for ``server``."""
    from agent_framework import MCPStreamableHTTPTool

    return MCPStreamableHTTPTool(
        name=server.name, url=server.url, tool_name_prefix=server.name,
        description=f"Tools of the remote MCP server '{server.name}'.")


async def _connect_and_list(server: RemoteServer) -> list[str]:
    tool = make_tool(server)
    try:
        await tool.connect()
        return [f.name for f in tool.functions]
    finally:
        try:
            await tool.close()
        except Exception:  # noqa: BLE001
            pass


def probe_blocking(server: RemoteServer, timeout: float = DEFAULT_TIMEOUT) -> list[str] | None:
    """Probe in a daemon thread with its own event loop, with a HARD deadline.

    Cancelling an MCP connect does not always unwind promptly (a server that
    accepts TCP but never answers keeps it waiting), so a plain ``wait_for``
    can overrun its timeout. A thread that misses the deadline is abandoned:
    it can block neither startup nor the caller's event-loop shutdown.
    """
    out: dict = {}

    def target() -> None:
        try:
            out["names"] = asyncio.run(_connect_and_list(server))
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    t = threading.Thread(target=target, name=f"mcp-probe-{server.name}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        reason = f"no answer within {timeout:g}s"
    elif "error" in out:
        reason = str(out["error"]) or type(out["error"]).__name__
    else:
        logger.info("Remote MCP server '%s': %d tool(s).", server.name, len(out["names"]))
        return out["names"]
    logger.warning("Remote MCP server '%s' (%s) unreachable: %s — starting without it.",
                   server.name, server.url, reason)
    return None


async def probe(server: RemoteServer, timeout: float = DEFAULT_TIMEOUT) -> list[str] | None:
    """:func:`probe_blocking` without blocking the running event loop."""
    return await asyncio.get_running_loop().run_in_executor(
        None, probe_blocking, server, timeout)


@dataclass
class RemoteTools:
    tools: list                       # fresh MCPStreamableHTTPTool objects
    servers: dict[str, list[str]]     # reachable server -> its tool names
    unreachable: list[str]


async def reachable_tools(servers: list[RemoteServer] | None = None,
                          timeout: float | None = None) -> RemoteTools:
    """Probe all servers concurrently; tools only for the reachable ones."""
    servers = servers_from_env() if servers is None else servers
    timeout = timeout_from_env() if timeout is None else timeout
    if not servers:
        return RemoteTools([], {}, [])
    results = await asyncio.gather(*(probe(s, timeout) for s in servers))
    ok = {s.name: names for s, names in zip(servers, results) if names is not None}
    return RemoteTools([make_tool(s) for s in servers if s.name in ok], ok,
                       [s.name for s in servers if s.name not in ok])


def reachable_tools_sync(servers: list[RemoteServer] | None = None,
                         timeout: float | None = None) -> RemoteTools:
    """:func:`reachable_tools` from synchronous code with no running event loop.

    Inside a running loop it cannot block on the probes: it then mounts nothing
    (with a warning) — callers in async code use :func:`reachable_tools`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        logger.warning("Remote MCP servers not probed: called inside a running event loop.")
        return RemoteTools([], {}, [])
    servers = servers_from_env() if servers is None else servers
    timeout = timeout_from_env() if timeout is None else timeout
    results: list = [None] * len(servers)

    def run(i: int) -> None:
        results[i] = probe_blocking(servers[i], timeout)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(servers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 1)
    ok = {s.name: n for s, n in zip(servers, results) if n is not None}
    return RemoteTools([make_tool(s) for s in servers if s.name in ok], ok,
                       [s.name for s in servers if s.name not in ok])


def instructions_for(remote: RemoteTools) -> str:
    """Extra agent instructions describing the mounted servers."""
    if not remote.servers:
        return ""
    lines = ["", "Remote MCP servers are also available. Their tools are prefixed with "
                 "the server name:"]
    for name, tools in remote.servers.items():
        shown = ", ".join(tools[:12]) + (", …" if len(tools) > 12 else "")
        lines.append(f"- {name}: {shown}")
    lines.append("Use them for questions the GeoServer data cannot answer, and say "
                 "which source each fact comes from.")
    extra = (os.environ.get("GEO_EXTRA_MCP_INSTRUCTIONS") or "").strip()
    if extra:
        lines.append(extra)
    return "\n".join(lines)
