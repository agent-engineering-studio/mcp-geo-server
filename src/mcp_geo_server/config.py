"""Settings loaded from environment variables.

No secrets are hardcoded: everything comes from the environment (see
``.env.example``). ``get_settings()`` is cached; tests can call
``reset_settings()`` after mutating ``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

_TRUE = {"1", "true", "yes", "on"}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in _TRUE


def _as_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    """Immutable view of the GeoServer / web-UI configuration."""

    url: str
    user: str
    password: str
    default_workspace: str | None
    default_srs: str
    timeout: float
    retries: int
    retry_backoff: float
    verify_tls: bool
    map_output_dir: str
    webui_port: int
    # Intelligent MCP agent (Microsoft Agent Framework)
    llm_provider: str            # "ollama" | "ollama-cloud" | "anthropic"
    ollama_host: str
    ollama_model: str
    ollama_cloud_host: str
    ollama_api_key: str
    anthropic_api_key: str
    anthropic_model: str
    mcp_transport: str
    mcp_host: str
    mcp_port: int

    @property
    def rest_base(self) -> str:
        """Base URL of the GeoServer REST API (no trailing slash)."""
        return f"{self.url}/rest"

    @property
    def ows_base(self) -> str:
        """Base URL of the generic OGC endpoint (``/ows``)."""
        return f"{self.url}/ows"

    @property
    def wms_base(self) -> str:
        """Base URL of the WMS endpoint (used by Leaflet ``L.tileLayer.wms``)."""
        return f"{self.url}/wms"

    @property
    def wfs_base(self) -> str:
        """Base URL of the WFS endpoint."""
        return f"{self.url}/wfs"


def _load() -> Settings:
    url = (os.environ.get("GEOSERVER_URL") or "").strip().rstrip("/")
    user = os.environ.get("GEOSERVER_USER") or ""
    password = os.environ.get("GEOSERVER_PASSWORD") or ""

    missing = [
        name
        for name, value in (
            ("GEOSERVER_URL", url),
            ("GEOSERVER_USER", user),
            ("GEOSERVER_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example and export them before running."
        )

    default_ws = (os.environ.get("GEOSERVER_DEFAULT_WORKSPACE") or "").strip() or None

    return Settings(
        url=url,
        user=user,
        password=password,
        default_workspace=default_ws,
        default_srs=(os.environ.get("GEOSERVER_DEFAULT_SRS") or "EPSG:4326").strip(),
        timeout=_as_float(os.environ.get("GEOSERVER_TIMEOUT"), 30.0),
        retries=_as_int(os.environ.get("GEOSERVER_RETRIES"), 2),
        retry_backoff=_as_float(os.environ.get("GEOSERVER_RETRY_BACKOFF"), 0.5),
        verify_tls=_as_bool(os.environ.get("GEOSERVER_VERIFY_TLS"), True),
        map_output_dir=(os.environ.get("GEO_MAP_OUTPUT_DIR") or "./maps").strip(),
        webui_port=_as_int(os.environ.get("WEBUI_PORT"), 8000),
        llm_provider=(os.environ.get("GEO_LLM_PROVIDER") or "ollama").strip().lower(),
        ollama_host=(os.environ.get("OLLAMA_HOST") or "http://localhost:11434").strip(),
        ollama_model=(os.environ.get("OLLAMA_MODEL") or "qwen2.5").strip(),
        ollama_cloud_host=(os.environ.get("OLLAMA_CLOUD_HOST") or "https://ollama.com").strip(),
        ollama_api_key=(os.environ.get("OLLAMA_API_KEY") or "").strip(),
        anthropic_api_key=(os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        anthropic_model=(os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip(),
        mcp_transport=(os.environ.get("GEO_MCP_TRANSPORT") or "stdio").strip().lower(),
        mcp_host=(os.environ.get("GEO_MCP_HOST") or "0.0.0.0").strip(),
        mcp_port=_as_int(os.environ.get("GEO_MCP_PORT"), 9000),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings parsed from the environment."""
    return _load()


def reset_settings() -> None:
    """Clear the settings cache (used by tests after changing env vars)."""
    get_settings.cache_clear()
