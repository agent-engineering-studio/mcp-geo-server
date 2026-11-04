"""The map assistant: a natural-language request -> a ready-to-render answer.

This is the whole pipeline behind the chat (``POST /api/ask``), in the core so
the web UI and the MCP server (``geo_resolve_request``) give the SAME answer:

1. catalog (WMS capabilities, cached) -> resolver (retrieval, structured
   output, model routing, conversation context);
2. per-layer bbox and description (geometry, count, fields);
3. server-side draw order (rasters below vectors, broadest raster lowest);
4. CQL guard with the layers' real columns, and per-layer applicability;
5. admin-area scoping (this request, the model's ``place``, or the previous
   turn's area on a refinement): zoom bbox, raster ``clip``, vector
   ``INTERSECTS``;
6. on-demand terrain enrichment from a DTM when metrics are asked for.

:class:`MapAssistant` keeps the caches (catalog, filter spec, admin index,
resolver tiers); one instance per process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from . import cql as cqlmod
from . import places, resolver
from .catalog import LayerMeta, build_catalog, parse_wms_capabilities

logger = logging.getLogger("mcp_geo_server.answer")

_TRUE = {"1", "true", "yes", "on"}
_GEOM_LABEL = [("Polygon", "poligoni"), ("Line", "linee"), ("Point", "punti")]

# Terrain-metric words: the keyword safety net next to the model's `metrics`.
METRIC_HINTS = {
    "quota": "quota", "quote": "quota", "altimetria": "quota",
    "altitudine": "quota", "elevazione": "quota", "altezza": "quota",
    "pendenza": "slope", "pendenze": "slope", "acclivita": "slope",
    "slope": "slope",
    "esposizione": "aspect", "orientamento": "aspect", "aspect": "aspect",
    "curvatura": "curvature", "curvature": "curvature",
}


def metric_intent(query: str) -> list[str]:
    """Terrain metrics named in the query (normalized, accent-insensitive)."""
    toks = set(places.norm(query).split())
    return sorted({v for k, v in METRIC_HINTS.items() if k in toks})


class AskError(RuntimeError):
    """The resolver could not produce an answer (LLM down, unparseable reply)."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw in (None, "") else raw.strip().lower() in _TRUE


class MapAssistant:
    def __init__(self) -> None:
        self.catalog_ttl = float(os.environ.get("GEO_CATALOG_TTL") or 60)
        self.structured = _env_bool("GEO_RESOLVER_STRUCTURED", True)
        self.min_confidence = resolver.min_confidence_from_env()
        self.prompt_cache = resolver.prompt_cache_from_env()
        from . import retrieval
        self.retriever = retrieval.retriever_from_env()
        self.tiers: list[resolver.Tier] | None = None
        self._spec: resolver.FilterSpec | None = None
        self._areas: places.AdminAreas | None = None
        self._catalog: tuple[float, list[LayerMeta]] | None = None
        self._catalog_lock = asyncio.Lock()

    # ---- config-derived ------------------------------------------------------
    def _domain_config(self) -> dict:
        from .styling import load_config
        return load_config()

    def filter_spec(self) -> resolver.FilterSpec:
        if self._spec is None:
            self._spec = resolver.filter_spec(self._domain_config())
        return self._spec

    def admin_areas(self) -> places.AdminAreas:
        if self._areas is None:
            self._areas = places.AdminAreas(places.admin_config(self._domain_config()))
        return self._areas

    def resolver_tiers(self) -> list[resolver.Tier]:
        if self.tiers is None:
            self.tiers = resolver.tiers_from_settings()
        return self.tiers

    # ---- catalog -------------------------------------------------------------
    def invalidate_catalog(self) -> None:
        self._catalog = None

    async def fetch_catalog(self) -> list[LayerMeta]:
        from .client import get_client
        client = get_client()
        resp = await client.ows({"service": "WMS", "version": "1.3.0",
                                 "request": "GetCapabilities"},
                                base=client.settings.wms_base)
        return build_catalog(parse_wms_capabilities(resp.text))

    async def load_catalog(self) -> list[LayerMeta]:
        """WMS-capabilities catalog, cached ``GEO_CATALOG_TTL`` s (0 = no cache)."""
        if self.catalog_ttl <= 0:
            return await self.fetch_catalog()
        async with self._catalog_lock:
            now = asyncio.get_running_loop().time()
            if self._catalog is None or now - self._catalog[0] >= self.catalog_ttl:
                self._catalog = (now, await self.fetch_catalog())
            return self._catalog[1]

    # ---- layers --------------------------------------------------------------
    async def layer_bbox(self, workspace: str, name: str) -> dict | None:
        from .ingest import layer_bbox
        return await layer_bbox(workspace, name)

    async def layer_info(self, workspace: str, name: str) -> dict | None:
        """Domain-agnostic description: geometry kind, fields, feature count."""
        from .client import GeoServerError, get_client
        client = get_client()
        try:
            data = await client.get_json(f"workspaces/{workspace}/featuretypes/{name}.json")
        except GeoServerError:
            try:
                cdata = await client.get_json(f"workspaces/{workspace}/coverages/{name}.json")
            except GeoServerError:
                return None
            cov = cdata.get("coverage", {}) if isinstance(cdata, dict) else {}
            return {"geometry": "raster", "fields": [], "count": None,
                    "title": cov.get("title") or name, "abstract": cov.get("abstract") or ""}
        ft = data.get("featureType", {}) if isinstance(data, dict) else {}
        geometry, geom_col, fields = None, None, []
        for a in (ft.get("attributes", {}) or {}).get("attribute", []) or []:
            binding = a.get("binding", "")
            if "jts" in binding.lower() or "geom" in binding.lower():
                geometry = next((lbl for key, lbl in _GEOM_LABEL if key in binding),
                                "geometrie")
                geom_col = a.get("name") or geom_col
            elif a.get("name"):
                fields.append(a["name"])
        count = None
        try:
            resp = await client.ows(
                {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                 "typeNames": f"{workspace}:{name}", "resultType": "hits"},
                base=client.settings.wfs_base)
            m = re.search(r'numberMatched="(\d+)"', resp.text)
            if m:
                count = int(m.group(1))
        except (GeoServerError, ValueError):
            pass
        return {"geometry": geometry, "geometry_column": geom_col, "fields": fields,
                "count": count, "title": ft.get("title") or name,
                "abstract": ft.get("abstract") or ""}

    # ---- places --------------------------------------------------------------
    async def detect_admin(self, text: str):
        return await self.admin_areas().detect(text)

    async def admin_scope(self, level, raw_name: str) -> dict | None:
        return await self.admin_areas().scope(level, raw_name)

    def admin_from_context(self, context: dict | None):
        area = (context or {}).get("admin") or {}
        level_name, raw = area.get("level"), area.get("raw")
        if not (level_name and raw):
            return None
        level = next((lv for lv in self.admin_areas().cfg.levels if lv.level == level_name),
                     None)
        return (level, raw) if level else None

    # ---- the pipeline ----------------------------------------------------------
    async def ask(self, query: str, context: dict | None = None) -> dict:
        """Resolve ``query`` (with an optional previous-turn ``context``).

        Raises :class:`AskError` when no model could answer.
        """
        catalog = await self.load_catalog()
        spec = self.filter_spec()
        try:
            selection = await resolver.resolve_routed(
                self.resolver_tiers(), query, catalog, spec,
                min_confidence=self.min_confidence, structured=self.structured,
                guard=False, retriever=self.retriever, prompt_cache=self.prompt_cache,
                context=context)
        except ValueError as exc:
            raise AskError(f"could not parse model reply: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — LLM/transport errors
            raise AskError(f"LLM resolver failed: {exc}") from exc
        selection.pop("raw", None)

        # Combined extent; skip degenerate boxes (empty layers report 0,0,0,0).
        bbox_by_q: dict[str, dict] = {}
        for q in selection["layers"]:
            ws, _, name = q.partition(":")
            b = await self.layer_bbox(ws, name)
            if b and b["maxx"] > b["minx"] and b["maxy"] > b["miny"]:
                bbox_by_q[q] = b
        boxes = list(bbox_by_q.values())
        selection["bbox"] = ({"minx": min(b["minx"] for b in boxes),
                              "miny": min(b["miny"] for b in boxes),
                              "maxx": max(b["maxx"] for b in boxes),
                              "maxy": max(b["maxy"] for b in boxes)} if boxes else None)

        info = []
        for q in selection["layers"]:
            ws, _, name = q.partition(":")
            li = await self.layer_info(ws, name)
            if li:
                kind = "raster" if li.get("geometry") == "raster" else "vector"
                info.append({"qualified": q, "kind": kind, "bbox": bbox_by_q.get(q), **li})

        # Draw order bottom -> top: rasters below vectors (an opaque raster must
        # not hide them), broadest raster lowest. Stable within each group.
        info_by_q = {e["qualified"]: e for e in info}

        def order_key(q: str) -> tuple:
            e = info_by_q.get(q, {})
            b = e.get("bbox")
            area = (b["maxx"] - b["minx"]) * (b["maxy"] - b["miny"]) if b else 0.0
            raster = e.get("kind") == "raster"
            return (0 if raster else 1, -area if raster else 0.0)

        selection["layers"] = sorted(selection["layers"], key=order_key)
        selection["info"] = sorted(info, key=lambda e: order_key(e["qualified"]))

        # CQL guard with the real columns, then per-layer applicability: a
        # filter on a layer lacking the column would fail the whole render.
        resolver.guard_cql(selection, spec,
                           {e["qualified"]: set(e.get("fields") or []) for e in info})
        cql = selection.get("cql_filter")
        cql_by_layer: dict[str, str] = {}
        if cql:
            attrs = cqlmod.attributes(cqlmod.parse(cql))
            for e in info:
                if attrs and attrs <= set(e.get("fields") or []):
                    cql_by_layer[e["qualified"]] = cql
        selection["cql_applicable"] = bool(cql_by_layer) if cql else None

        # Area: named in this request; else the model's `place`; else, on a
        # refinement, the previous turn's area. Rasters get the polygon `clip`;
        # vectors an INTERSECTS predicate (no JTS overlay on dense layers).
        admin = await self.detect_admin(query)
        if not admin and selection.get("place"):
            admin = await self.detect_admin(selection["place"])
        if not admin and selection.get("mode") == "refine":
            admin = self.admin_from_context(context)
        if admin:
            scope = await self.admin_scope(*admin)
            if scope:
                selection["admin"] = {"level": scope["level"], "name": scope["name"],
                                      "raw": admin[1]}
                selection["bbox"] = scope["bbox"]
                selection["clip"] = scope["clip"]
                wkt = scope["clip"].split(";", 1)[-1]
                for e in info:
                    if e.get("kind") != "vector":
                        continue
                    q = e["qualified"]
                    spatial = f"INTERSECTS({e.get('geometry_column') or 'geom'}, {wkt})"
                    base = cql_by_layer.get(q)
                    cql_by_layer[q] = f"({base}) AND {spatial}" if base else spatial
        selection["cql_by_layer"] = cql_by_layer

        vectors = [i["qualified"] for i in info if i.get("kind") == "vector"]

        # Counting: the model says WHAT to count; GeoServer does the counting,
        # with the very filters the map uses (so numbers match what is shown).
        from . import analytics
        agg = selection.get("aggregate") or {"kind": "none", "attribute": None}
        if agg["kind"] == "none" and analytics.count_intent(query):
            agg = {"kind": "count", "attribute": None}
        selection["aggregate"] = agg
        if agg["kind"] != "none" and vectors:
            selection["analysis"] = await self._analyse(
                agg, [e for e in info if e.get("kind") == "vector"], cql_by_layer, spec,
                (selection.get("admin") or {}).get("name"))

        metrics = sorted(set(metric_intent(query)) | set(selection.get("metrics") or []))
        if metrics and vectors:
            enrichment = await self._enrich(selection, info, vectors, metrics)
            if enrichment:
                selection["enrichment"] = enrichment
        return selection

    async def group_values(self, layer: str, attribute: str,
                           spec: resolver.FilterSpec) -> list[tuple]:
        """Values to group by: the configured classes, else the real distinct ones."""
        configured = spec.values.get(attribute)
        if configured:
            return list(configured)
        from .style_ai import distinct_values
        ws, _, name = layer.partition(":")
        try:
            return [(v, str(v)) for v, _ in await distinct_values(ws, name, attribute)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("distinct values of %s.%s unavailable: %s", layer, attribute, exc)
            return []

    async def _analyse(self, agg: dict, vectors: list[dict], cql_by_layer: dict,
                       spec: resolver.FilterSpec, area: str | None) -> dict:
        """Exact counts (and per-class counts) on up to 3 vector layers."""
        from . import analytics
        rows, lines = [], []
        attr = agg.get("attribute")
        for e in vectors[:3]:
            q = e["qualified"]
            total = await analytics.count(q, cql_by_layer.get(q))
            groups, note = None, None
            if agg["kind"] == "count_by" and attr:
                if attr in (e.get("fields") or []):
                    values = await self.group_values(q, attr, spec)
                    if len(values) > analytics.MAX_GROUPS:
                        note = f"{attr} ha troppi valori per un raggruppamento"
                    elif values:
                        groups = await analytics.count_by(q, attr, values, cql_by_layer.get(q))
                else:
                    note = f"{attr} non è un attributo di questo layer"
            rows.append({"layer": q, "total": total, "groups": groups, "note": note})
            line = analytics.describe(q, total, groups, attr, e.get("title"))
            lines.append(line + (f" ({note})" if note else ""))
        where = f" — {area}" if area else ""
        title = f"Conteggio{where}"
        return {"kind": agg["kind"], "attribute": attr, "layers": rows, "title": title,
                "text": title + ":\n" + "\n".join(f"· {t}" for t in lines)}

    async def _enrich(self, selection: dict, info: list[dict], vectors: list[str],
                      metrics: list[str]) -> dict | None:
        """DTM metrics on up to 3 vector layers within the current area. Best-effort."""
        from . import enrich
        rasters = [i["qualified"] for i in info if i.get("kind") == "raster"]
        b = selection.get("bbox")
        bbox = f'{b["minx"]},{b["miny"]},{b["maxx"]},{b["maxy"]}' if b else None
        labels = {m: enrich.METRICS.get(m, m) for m in metrics}
        out = []
        for vl in vectors[:3]:
            try:
                r = await enrich.enrich_layer(vl, metrics=metrics,
                                              dtm=rasters[0] if rasters else None,
                                              bbox=bbox, limit=1000)
            except Exception as exc:  # noqa: BLE001
                logger.warning("enrichment skipped for %s: %s", vl, exc)
                continue
            if not r.get("count"):
                continue
            parts = [f'{labels[m]} media {s["mean"]} (min {s["min"]}, max {s["max"]})'
                     for m in metrics if (s := r["summary"].get(m))]
            out.append({"layer": vl, "count": r["count"], "summary": r["summary"],
                        "text": f'{vl.split(":")[-1]} — {r["count"]} elementi: '
                                + "; ".join(parts)})
        if not out:
            return None
        title = "Analisi del terreno (DTM)"
        return {"metrics": metrics, "labels": labels, "title": title, "layers": out,
                "text": title + ":\n" + "\n".join(f"· {e['text']}" for e in out)}


_ASSISTANT: MapAssistant | None = None


def get_assistant() -> MapAssistant:
    """The process-wide assistant (shared by the web UI and the MCP server)."""
    global _ASSISTANT
    if _ASSISTANT is None:
        _ASSISTANT = MapAssistant()
    return _ASSISTANT
