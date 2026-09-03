"""LLM provider selection for the intelligent agent (no network at build)."""

from __future__ import annotations

import dataclasses

import pytest

from mcp_geo_server import config
from mcp_geo_server.agent import _build_chat_client


def _settings(**overrides):
    return dataclasses.replace(config.get_settings(), **overrides)


def test_default_provider_is_local_ollama():
    client = _build_chat_client(_settings(llm_provider="ollama"))
    assert type(client).__name__ == "OllamaChatClient"


def test_ollama_cloud_requires_api_key():
    with pytest.raises(RuntimeError):
        _build_chat_client(_settings(llm_provider="ollama-cloud", ollama_api_key=""))


def test_ollama_cloud_builds_with_key():
    client = _build_chat_client(
        _settings(llm_provider="ollama-cloud", ollama_api_key="key",
                  ollama_cloud_host="https://ollama.com", ollama_model="gpt-oss:120b")
    )
    assert type(client).__name__ == "OllamaChatClient"


def test_anthropic_requires_api_key():
    with pytest.raises(RuntimeError):
        _build_chat_client(_settings(llm_provider="anthropic", anthropic_api_key=""))


def test_anthropic_builds_with_key():
    client = _build_chat_client(
        _settings(llm_provider="anthropic", anthropic_api_key="sk-test",
                  anthropic_model="claude-sonnet-4-6")
    )
    assert "Anthropic" in type(client).__name__


def test_openai_requires_model():
    """Behind a gateway the model name is a routing key with no sensible
    default, so an unset one must fail loudly instead of 404-ing later."""
    with pytest.raises(RuntimeError):
        _build_chat_client(_settings(llm_provider="openai", openai_model=""))


def test_openai_builds_against_a_gateway_without_a_key():
    """A self-hosted gateway with no master key must still be usable: the
    OpenAI SDK refuses to construct without a credential, so the provider
    supplies a placeholder."""
    client = _build_chat_client(
        _settings(
            llm_provider="openai",
            openai_model="fast",
            openai_base_url="http://host.docker.internal:8091/v1",
            openai_api_key="",
        )
    )
    assert type(client).__name__ == "OpenAIChatClient"


def test_openai_builds_with_a_key():
    client = _build_chat_client(
        _settings(llm_provider="openai", openai_model="chat", openai_api_key="sk-test")
    )
    assert type(client).__name__ == "OpenAIChatClient"


def test_unknown_provider_raises():
    with pytest.raises(RuntimeError):
        _build_chat_client(_settings(llm_provider="bogus"))
