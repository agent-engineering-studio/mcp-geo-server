"""The cached prompt reaches the Anthropic API in the right shape (no network).

The real Agent Framework Anthropic client is used with a mock HTTP transport,
so this checks the actual request body: the cacheable block must be the LAST
system block (``cache_control`` caches the prefix up to it), after the agent's
instructions, and the per-request text must be the user message.
"""

from __future__ import annotations

import json

import pytest

# The Anthropic SDK ships its own fork of httpx; its mock transport is needed here.
httpx = pytest.importorskip("httpx2")

anthropic = pytest.importorskip("anthropic")
pytest.importorskip("agent_framework_anthropic")

from agent_framework.anthropic import AnthropicClient  # noqa: E402

from mcp_geo_server import resolver  # noqa: E402
from mcp_geo_server.catalog import build_catalog  # noqa: E402
from mcp_geo_server.styling import load_config  # noqa: E402

CATALOG = build_catalog([{"name": f"layer_{i}", "workspace": "ws"} for i in range(5)])
REPLY = {"layers": ["ws:layer_1"], "cql_filter": None, "explanation": "ok", "confidence": 0.9}


def _agent(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": json.dumps(REPLY)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}})

    sdk = anthropic.AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    client = AnthropicClient(model="claude-sonnet-5-5", anthropic_client=sdk)
    return client.as_agent(name="layer-resolver", instructions=resolver.INSTRUCTIONS, tools=[])


@pytest.mark.filterwarnings("ignore")
async def test_cached_system_block_shape():
    captured: list = []
    sel = await resolver.resolve(_agent(captured), "mostra layer 1", CATALOG,
                                 resolver.filter_spec(load_config()), provider="anthropic",
                                 prompt_cache=True)
    assert sel["layers"] == ["ws:layer_1"]
    body = captured[0]
    system = body["system"]
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"].startswith("You map an Italian")           # agent instructions
    assert system[-1]["cache_control"] == {"type": "ephemeral"}          # cached prefix end
    assert "ws:layer_4" in system[-1]["text"]                            # static catalog
    user = body["messages"][0]["content"]
    user_text = user if isinstance(user, str) else user[0]["text"]
    assert user_text.startswith("Request: mostra layer 1")
    assert "ws:layer_4" not in user_text
    assert body["output_config"]["format"]["type"] == "json_schema"      # structured too


@pytest.mark.filterwarnings("ignore")
async def test_no_cache_by_default():
    captured: list = []
    await resolver.resolve(_agent(captured), "x", CATALOG,
                           resolver.filter_spec(load_config()), provider="anthropic")
    system = captured[0]["system"]
    blocks = system if isinstance(system, list) else [{"text": system}]
    assert not any("cache_control" in b for b in blocks)
