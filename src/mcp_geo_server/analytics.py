"""Quantitative answers from exact WFS counts — no SQL, no model arithmetic.

"Quante frane per tipo in Campania?" is answered by GeoServer itself: one WFS
``GetFeature`` with ``resultType=hits`` per group, with the same filter the map
uses (attribute CQL AND area INTERSECTS). The model only says WHAT to count
(``aggregate`` in the resolver reply); the numbers never pass through it.

This is the safe first level of analytics (roadmap §4.4): exact, read-only,
bounded (at most ``MAX_GROUPS`` requests per layer, ``CONCURRENCY`` at a time),
and it works for any WFS layer. Arbitrary spatial questions (distances, joins)
would need SQL and are deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from . import cql as cqlmod

logger = logging.getLogger("mcp_geo_server.analytics")

MAX_GROUPS = 30
CONCURRENCY = 4
AGGREGATES = ("none", "count", "count_by")

# Keyword safety net for "how many" next to the model's `aggregate`.
_COUNT_WORDS = {"quante", "quanti", "quanto", "numero", "conta", "conteggio", "contare",
                "totale", "many", "count", "number"}


def count_intent(query: str) -> bool:
    from .places import norm
    return bool(set(norm(query).split()) & _COUNT_WORDS)


def combine(*filters: str | None) -> str | None:
    """AND the non-empty filters together (each parenthesized)."""
    parts = [f for f in filters if f]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else " AND ".join(f"({p})" for p in parts)


def equals(attribute: str, value: Any) -> str:
    """``attribute = <literal>`` with correct quoting (via the CQL serializer)."""
    return cqlmod.to_cql(cqlmod.Compare(attribute, "=", value))


async def count(layer: str, cql_filter: str | None = None, client=None) -> int | None:
    """Exact number of features of ``layer`` matching ``cql_filter`` (WFS hits)."""
    from .client import GeoServerError, get_client

    client = client or get_client()
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
              "typeNames": layer, "resultType": "hits"}
    if cql_filter:
        params["cql_filter"] = cql_filter
    try:
        resp = await client.ows(params, base=client.settings.wfs_base)
    except (GeoServerError, ValueError) as exc:
        logger.warning("count failed for %s: %s", layer, exc)
        return None
    m = re.search(r'numberMatched="(\d+)"', resp.text)
    return int(m.group(1)) if m else None


async def count_by(layer: str, attribute: str, values: list[tuple[Any, str]],
                   cql_filter: str | None = None, client=None) -> list[dict]:
    """Per-value counts: ``[{value, label, count}]``, largest first, zeros dropped."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(value, label):
        async with sem:
            n = await count(layer, combine(cql_filter, equals(attribute, value)), client)
        return {"value": value, "label": label or str(value), "count": n}

    rows = await asyncio.gather(*(one(v, lbl) for v, lbl in values[:MAX_GROUPS]))
    return sorted((r for r in rows if r["count"]), key=lambda r: -r["count"])


def fmt(n: int | None) -> str:
    """Italian thousands separator: 12345 -> '12.345'."""
    return "n/d" if n is None else f"{n:,}".replace(",", ".")


def describe(layer: str, total: int | None, groups: list[dict] | None,
             attribute: str | None = None, label: str | None = None) -> str:
    name = label or layer.split(":")[-1]
    text = f"{name}: {fmt(total)} elementi"
    if groups:
        shown = groups[:8]
        rest = sum(g["count"] for g in groups[8:])
        parts = [f"{g['label']} {fmt(g['count'])}" for g in shown]
        if rest:
            parts.append(f"altro {fmt(rest)}")
        text += f" — per {attribute}: " + ", ".join(parts)
    return text


def clean_aggregate(reply: Any) -> dict:
    """The resolver's ``aggregate`` field, validated: {kind, attribute}."""
    if not isinstance(reply, dict):
        return {"kind": "none", "attribute": None}
    kind = reply.get("kind") if reply.get("kind") in AGGREGATES else "none"
    attr = reply.get("attribute")
    attr = attr.strip() if isinstance(attr, str) and attr.strip() else None
    if kind == "count_by" and not attr:
        kind = "count"
    return {"kind": kind, "attribute": attr if kind == "count_by" else None}
