"""DestructiveGuard middleware: blocks destructive tools unless enabled."""

from __future__ import annotations

import types

from mcp_geo_server import config
from mcp_geo_server.middleware import DESTRUCTIVE_TOOLS, DestructiveGuard


class _Ctx:
    def __init__(self, name: str):
        self.function = types.SimpleNamespace(name=name)
        self.result = None


async def _run(name: str):
    ctx = _Ctx(name)
    called = {"v": False}

    async def call_next():
        called["v"] = True
        ctx.result = {"executed": True}

    await DestructiveGuard().process(ctx, call_next)
    return ctx, called["v"]


async def test_blocks_destructive_by_default():
    ctx, called = await _run("geo_delete_workspace")
    assert called is False
    assert ctx.result["refused"] is True
    assert "GEO_ALLOW_DESTRUCTIVE" in ctx.result["reason"]


async def test_read_tool_passes_through():
    ctx, called = await _run("geo_list_workspaces")
    assert called is True
    assert ctx.result == {"executed": True}


async def test_allows_destructive_when_enabled(monkeypatch):
    monkeypatch.setenv("GEO_ALLOW_DESTRUCTIVE", "true")
    config.reset_settings()
    ctx, called = await _run("geo_delete_layer")
    assert called is True
    assert ctx.result == {"executed": True}


def test_wfs_transaction_is_guarded():
    assert "geo_wfs_transaction" in DESTRUCTIVE_TOOLS
    assert {"geo_delete_workspace", "geo_delete_datastore",
            "geo_delete_layer", "geo_delete_style"} <= DESTRUCTIVE_TOOLS
