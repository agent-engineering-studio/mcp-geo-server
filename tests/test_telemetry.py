"""geo.* spans carry the resolver outcome; telemetry is off without config."""

from __future__ import annotations

import json

import pytest

from mcp_geo_server import resolver, telemetry
from mcp_geo_server.catalog import build_catalog
from mcp_geo_server.styling import load_config

sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    tp = sdk.TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(tp)
    yield


@pytest.fixture(autouse=True)
def clear():
    EXPORTER.clear()


PAI = "ispra:mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai"
CATALOG = build_catalog([{"name": PAI.split(":")[1], "workspace": "ispra"}])


class _R:
    def __init__(self, t):
        self.text = t


class Agent:
    def __init__(self, reply):
        self.reply = json.dumps(reply)

    async def run(self, prompt, options=None):
        return _R(self.reply)


async def test_resolve_span_attributes():
    agent = Agent({"layers": [PAI, "ispra:invented"], "cql_filter": "per_fr_ita = 'Alta'",
                   "explanation": "", "confidence": 0.3})
    await resolver.resolve(agent, "frane alta pericolosità", CATALOG,
                           resolver.filter_spec(load_config()), provider="ollama")
    (sp,) = [s for s in EXPORTER.get_finished_spans() if s.name == "geo.resolve"]
    a = sp.attributes
    assert a["gen_ai.provider.name"] == "ollama" and a["geo.catalog.size"] == 1
    assert a["geo.layers.count"] == 1 and a["geo.layers.dropped"] == 1
    assert a["geo.cql.rejected"] is True and a["geo.confidence"] == 0.3
    assert a["geo.structured.used"] is True
    assert "frane" not in json.dumps(dict(a))           # no query text exported
    assert [e.name for e in sp.events] == ["geo.cql.rejected"]


def test_disabled_without_env(monkeypatch):
    for v in ("GEO_OTEL_ENABLE", "OTEL_EXPORTER_OTLP_ENDPOINT",
              "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    assert not telemetry.enabled_by_env()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    assert telemetry.enabled_by_env()


def test_span_skips_none_attributes():
    with telemetry.span("geo.test", a=1, b=None):
        pass
    (sp,) = EXPORTER.get_finished_spans()
    assert dict(sp.attributes) == {"a": 1}
