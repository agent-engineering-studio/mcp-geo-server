"""Intelligent GeoServer agent (Microsoft Agent Framework).

The agent is given the GeoServer ``geo_*`` functions as tools and a pluggable LLM
backend. It reasons over natural-language requests and calls the right GeoServer
operations. ``server.py`` then exposes this agent as an MCP server, so any MCP
client talks to a single "intelligent" GeoServer tool.

Supported LLM providers (``GEO_LLM_PROVIDER``):

* ``ollama``        — local Ollama server (default, no API key)
* ``ollama-cloud``  — Ollama Cloud (https://ollama.com), needs ``OLLAMA_API_KEY``
* ``anthropic``     — Claude via the Anthropic API, needs ``ANTHROPIC_API_KEY``
* ``openai``        — any OpenAI-compatible ``/v1`` endpoint, needs
  ``OPENAI_LLM_MODEL`` (and ``OPENAI_BASE_URL`` for anything that is not
  OpenAI itself)

The ``openai`` provider is what a self-hosted inference gateway needs: the
``ollama`` providers speak Ollama's own ``/api/chat`` protocol, so pointing
``OLLAMA_HOST`` at a gateway that exposes ``/v1/chat/completions`` fails on the
URL, not on the model.
"""

from __future__ import annotations

from agent_framework import Agent

from .config import Settings, get_settings
from .middleware import DestructiveGuard
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

    if provider == "openai":
        from agent_framework.openai import OpenAIChatClient

        if not settings.openai_model:
            raise RuntimeError(
                "GEO_LLM_PROVIDER=openai requires OPENAI_LLM_MODEL (behind a "
                "gateway this is a routing key in its model list, e.g. 'fast')."
            )
        return OpenAIChatClient(
            model=settings.openai_model,
            # The OpenAI SDK refuses to construct without a credential, while a
            # self-hosted gateway with no master key ignores whatever it gets.
            # Send a placeholder rather than make keyless gateways unusable.
            api_key=settings.openai_api_key or "sk-no-key-required",
            base_url=settings.openai_base_url or None,
        )

    raise RuntimeError(
        f"Unknown GEO_LLM_PROVIDER '{provider}'. "
        "Use 'ollama', 'ollama-cloud', 'anthropic' or 'openai'."
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
        middleware=[DestructiveGuard()],
    )
