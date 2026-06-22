"""Intelligent GeoServer agent (Microsoft Agent Framework).

The agent is given the GeoServer ``geo_*`` functions as tools and a pluggable LLM
backend. It reasons over natural-language requests and calls the right GeoServer
operations. ``server.py`` then exposes this agent as an MCP server, so any MCP
client talks to a single "intelligent" GeoServer tool.

Supported LLM providers (``GEO_LLM_PROVIDER``):

* ``ollama``        — local Ollama server (default, no API key)
* ``ollama-cloud``  — Ollama Cloud (https://ollama.com), needs ``OLLAMA_API_KEY``
* ``anthropic``     — Claude via the Anthropic API, needs ``ANTHROPIC_API_KEY``
"""

from __future__ import annotations

from agent_framework import Agent

from .config import Settings, get_settings
from .tools import collect_tools

AGENT_NAME = "geoserver-agent"
AGENT_DESCRIPTION = (
    "Intelligent assistant that administers and queries a GeoServer instance: "
    "workspaces, PostGIS datastores, feature types, layers, SLD styles, and OGC "
    "WMS/WFS/WFS-T operations."
)

INSTRUCTIONS = """\
You are a GeoServer expert assistant. You manage and query a single GeoServer
instance by calling the provided geo_* tools — never invent REST calls yourself.

Guidelines:
- Pick the most specific tool for the request and pass only the arguments you are
  sure about. If a workspace is not specified, omit it so the configured default
  workspace is used.
- For read questions (counts, listings, bounding boxes, feature attributes) use
  the list/get/WFS tools. topp:states is a common sample layer.
- For changes (create/publish/assign/delete) confirm the target names from the
  user's request; destructive operations (delete_*) remove data, so only run them
  when the user clearly asked.
- WFS GetFeature accepts EITHER a bbox OR a cql_filter, never both.
- When you finish, summarize what you did and surface the key values (names,
  counts, URLs, file paths) from the tool results in clear language.
"""


def _build_chat_client(settings: Settings):
    """Create the Agent Framework chat client for the configured provider."""
    provider = settings.llm_provider

    if provider == "anthropic":
        from agent_framework.anthropic import AnthropicClient

        if not settings.anthropic_api_key:
            raise RuntimeError(
                "GEO_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY."
            )
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
        )

    if provider == "ollama-cloud":
        import ollama
        from agent_framework.ollama import OllamaChatClient

        if not settings.ollama_api_key:
            raise RuntimeError(
                "GEO_LLM_PROVIDER=ollama-cloud requires OLLAMA_API_KEY "
                "(create one at https://ollama.com)."
            )
        cloud = ollama.AsyncClient(
            host=settings.ollama_cloud_host,
            headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
        )
        return OllamaChatClient(client=cloud, model=settings.ollama_model)

    if provider == "ollama":
        from agent_framework.ollama import OllamaChatClient

        return OllamaChatClient(host=settings.ollama_host, model=settings.ollama_model)

    raise RuntimeError(
        f"Unknown GEO_LLM_PROVIDER '{provider}'. "
        "Use 'ollama', 'ollama-cloud' or 'anthropic'."
    )


def build_agent() -> Agent:
    """Build the GeoServer agent backed by the configured LLM provider."""
    settings = get_settings()
    client = _build_chat_client(settings)
    return client.as_agent(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        instructions=INSTRUCTIONS,
        tools=collect_tools(),
    )
