"""OpenTelemetry, opt-in: traces + metrics for the LLM calls and the resolver.

Enabled when ``GEO_OTEL_ENABLE=true`` or a standard OTLP endpoint is set
(``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``).
Agent Framework then emits its own spans and ``gen_ai.*`` metrics (model,
tokens, duration) for every chat call; this module adds ``geo.*`` spans around
the resolver and the metadata writer with the outcome the evaluation measures
offline (layers kept/dropped, CQL rejected, structured fallback, confidence).

Prompts and model replies are NOT exported unless ``ENABLE_SENSITIVE_DATA=true``
(Agent Framework's own switch): user queries may contain personal data.
Without configuration every call here is a cheap no-op.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator

logger = logging.getLogger("mcp_geo_server.telemetry")

_CONFIGURED = False
_TRUE = {"1", "true", "yes", "on"}


def enabled_by_env() -> bool:
    if (os.environ.get("GEO_OTEL_ENABLE") or "").strip().lower() in _TRUE:
        return True
    return any(os.environ.get(v) for v in ("OTEL_EXPORTER_OTLP_ENDPOINT",
                                          "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


def setup(service_name: str) -> bool:
    """Configure exporters once per process. Returns True if telemetry is on.

    Never raises: a broken telemetry config must not take the service down.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return True
    if not enabled_by_env():
        return False
    try:
        from agent_framework.observability import configure_otel_providers

        from . import __version__
        configure_otel_providers(
            service_name=os.environ.get("OTEL_SERVICE_NAME") or service_name,
            service_version=__version__,
            # The OTLP/HTTP exporter is the one this package depends on; OTel's
            # own default (gRPC) would need an extra install.
            otlp_protocol=os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf")
        _CONFIGURED = True
        logger.info("OpenTelemetry enabled for %s.", service_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry setup failed (%s); continuing without.", exc)
    return _CONFIGURED


def _tracer():
    from opentelemetry import trace
    return trace.get_tracer("mcp_geo_server")


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A ``geo.*`` span; a no-op span when no provider is configured.

    ``None`` attribute values are skipped (OTel rejects them). Exceptions are
    recorded on the span and re-raised.
    """
    try:
        tracer = _tracer()
    except Exception:  # noqa: BLE001 — opentelemetry missing: no-op
        yield _NoSpan()
        return
    with tracer.start_as_current_span(name) as sp:
        set_attributes(sp, **attributes)
        yield sp


def event(name: str, **attributes: Any) -> None:
    """Add an event to the current span (no-op without one)."""
    try:
        from opentelemetry import trace
        trace.get_current_span().add_event(
            name, {k: v for k, v in attributes.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass


def set_attributes(sp: Any, **attributes: Any) -> None:
    for k, v in attributes.items():
        if v is None:
            continue
        try:
            sp.set_attribute(k, v)
        except Exception:  # noqa: BLE001
            pass


class _NoSpan:
    def set_attribute(self, *a, **k):
        pass

    def record_exception(self, *a, **k):
        pass
