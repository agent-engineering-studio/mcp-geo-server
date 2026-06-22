"""Integration tests talk to a REAL GeoServer, so the fake-env fixture from the
parent conftest is overridden here to leave the real environment untouched."""

from __future__ import annotations

import pytest

from mcp_geo_server import client as client_module
from mcp_geo_server import config


@pytest.fixture(autouse=True)
def env():
    """Override the parent fake-env fixture: use the real environment as-is."""
    config.reset_settings()
    client_module.reset_client()
    yield
    config.reset_settings()
    client_module.reset_client()
