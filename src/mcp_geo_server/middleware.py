"""Safety middleware for the GeoServer agent (Microsoft Agent Framework).

``DestructiveGuard`` is a function-invocation middleware that blocks destructive
``geo_*`` tools unless ``GEO_ALLOW_DESTRUCTIVE`` is enabled. Because the agent is
exposed as a (non-interactive) MCP server, we cannot pause for human approval
mid-run; instead the guard short-circuits the call and returns an actionable
refusal that the agent reports back to the caller.
"""

from __future__ import annotations

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from .config import get_settings

# Tools that create no data but remove or mutate it irreversibly.
DESTRUCTIVE_TOOLS = frozenset({
    "geo_delete_workspace",
    "geo_delete_datastore",
    "geo_delete_layer",
    "geo_delete_style",
    "geo_wfs_transaction",  # delete/update/raw all mutate feature data
})


def _refusal(name: str) -> dict:
    return {
        "refused": True,
        "tool": name,
        "reason": (
            f"'{name}' is a destructive operation and is blocked by default. "
            "Set GEO_ALLOW_DESTRUCTIVE=true to allow delete/transaction tools."
        ),
    }


class DestructiveGuard(FunctionMiddleware):
    """Block destructive tools unless explicitly allowed via configuration."""

    async def process(self, context: FunctionInvocationContext, call_next):
        name = getattr(context.function, "name", "") or ""
        if name in DESTRUCTIVE_TOOLS and not get_settings().allow_destructive:
            context.result = _refusal(name)
            return  # short-circuit: do not invoke the underlying tool
        await call_next()
