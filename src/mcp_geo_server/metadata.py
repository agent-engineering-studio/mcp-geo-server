"""AI-written layer metadata: title, abstract, keywords from the data itself.

The chat resolver matches requests against each layer's title, abstract and
keywords — but layers published by the bootstrap or the upload page get the
folder/file name as title and no keywords, so the resolver sees names like
``frane_poly_emilia-romagna_opendata`` and nothing else. This module profiles a
layer (columns, types, sample values, geometry, extent), asks an LLM for
metadata with a JSON-Schema-constrained reply, validates it against the real
columns, and writes it to GeoServer.

Safety rules:

* It only overwrites metadata that is still the **automatic** kind (title empty
  or equal to the layer/folder name, no abstract, only GeoServer's default
  keywords) or that it generated itself before (keyword ``metadata:ai``).
  Hand-written metadata is left alone unless ``overwrite=True``.
* Generated metadata is tagged with the ``metadata:ai`` keyword, so it is
  recognizable (and re-generatable) later.
* Vector layers only for now: rasters have no attributes to describe.

Entry points: :func:`enrich_layer` (one layer), the bootstrap/upload hooks
(``GEO_INIT_AI_METADATA`` / ``GEO_UPLOAD_AI_METADATA``), and the CLI
``python -m mcp_geo_server.metadata`` for layers that are already published.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter
from typing import Any

logger = logging.getLogger("mcp_geo_server.metadata")

AI_KEYWORD = "metadata:ai"
# Keywords GeoServer adds by itself when publishing.
_DEFAULT_KEYWORDS = {"features"}
MAX_COLUMNS = 40
SAMPLE_FEATURES = 200
SAMPLE_VALUES = 6

INSTRUCTIONS = (
    "You write catalog metadata for a GIS layer from a profile of its data: "
    "the layer name, geometry type, extent, and its columns with sample "
    "values. The metadata is used by a search assistant to find this layer "
    "from a user's request, so describe WHAT the data is about in plain words "
    "a user would use.\n"
    "Rules:\n"
    "- title: short and human (max ~80 chars), in {language}; spell out "
    "abbreviations you are sure about; mention the area if the name or "
    "extent makes it clear.\n"
    "- abstract: 2-4 sentences in {language}: what each feature represents, "
    "the main attributes and what they classify.\n"
    "- keywords: 6-12 search terms, BOTH in Italian and English, singular and "
    "lower case, including synonyms a user might type.\n"
    "- columns: describe only columns whose meaning you can infer from the "
    "name and samples; use the EXACT column names.\n"
    "- theme_attribute: the categorical column best suited to colour the map "
    "by, or null.\n"
    "- Never invent facts not supported by the profile (no dates, sources or "
    "scales you cannot see). Reply with the JSON object only."
)


# ---- profiling ----------------------------------------------------------------------
def _short(v: Any, n: int = 40) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    return v


def summarize_samples(features: list[dict], columns: list[str]) -> dict[str, dict]:
    """Per column: up to SAMPLE_VALUES most common values + distinct count."""
    out = {}
    for col in columns:
        vals = [f.get("properties", {}).get(col) for f in features]
        vals = [v for v in vals if v not in (None, "")]
        counts = Counter(json.dumps(v, ensure_ascii=False) for v in vals)
        out[col] = {
            "samples": [_short(json.loads(k)) for k, _ in counts.most_common(SAMPLE_VALUES)],
            "distinct_in_sample": len(counts),
            "empty_in_sample": len(features) - len(vals),
        }
    return out


async def layer_profile(workspace: str, name: str, client=None) -> dict:
    """Read schema + a sample of features for one vector layer.

    Raises ``ValueError`` if the layer is not a vector feature type.
    """
    from .client import GeoServerError, get_client

    client = client or get_client()
    try:
        data = await client.get_json(f"workspaces/{workspace}/featuretypes/{name}.json")
    except GeoServerError as exc:
        raise ValueError(f"{workspace}:{name} is not a vector feature type ({exc})") from exc
    ft = data.get("featureType", {}) if isinstance(data, dict) else {}
    geometry, columns = None, []
    for a in (ft.get("attributes", {}) or {}).get("attribute", []) or []:
        binding = a.get("binding", "") or ""
        if "jts" in binding.lower():
            geometry = binding.rsplit(".", 1)[-1]
        elif a.get("name"):
            columns.append({"name": a["name"], "type": binding.rsplit(".", 1)[-1]})
    columns = columns[:MAX_COLUMNS]

    features: list[dict] = []
    try:
        resp = await client.ows(
            {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
             "typeNames": f"{workspace}:{name}", "count": str(SAMPLE_FEATURES),
             "propertyName": ",".join(c["name"] for c in columns),
             "outputFormat": "application/json"},
            base=client.settings.wfs_base)
        features = json.loads(resp.text).get("features", [])
    except (GeoServerError, ValueError) as exc:
        logger.warning("no sample for %s:%s: %s", workspace, name, exc)
    stats = summarize_samples(features, [c["name"] for c in columns])
    for c in columns:
        c.update(stats.get(c["name"], {}))

    kw = (ft.get("keywords") or {}).get("string") or []
    return {
        "workspace": workspace, "name": name,
        "title": ft.get("title") or "", "abstract": ft.get("abstract") or "",
        "keywords": [kw] if isinstance(kw, str) else list(kw),
        "native_name": ft.get("nativeName") or name,
        "geometry": geometry, "srs": ft.get("srs"),
        "bbox": ft.get("latLonBoundingBox"),
        "sampled_features": len(features), "columns": columns,
    }


# ---- deciding whether to write ----------------------------------------------------------
def _loose(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def is_automatic(profile: dict) -> bool:
    """True if the current metadata was not written by a person."""
    kws = {k.strip().lower() for k in profile.get("keywords", [])}
    if AI_KEYWORD in kws:
        return True
    names = {_loose(profile["name"]), _loose(profile.get("native_name", ""))}
    title_auto = not profile.get("title") or _loose(profile["title"]) in names
    keywords_auto = kws <= (_DEFAULT_KEYWORDS | {profile["name"].lower(),
                                                 profile.get("native_name", "").lower()})
    return title_auto and not profile.get("abstract") and keywords_auto


# ---- generation -----------------------------------------------------------------------------
def metadata_schema(profile: dict) -> dict:
    cols = [c["name"] for c in profile["columns"]]
    col_name = {"type": "string", "enum": cols} if cols else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "abstract": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "columns": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": col_name, "description": {"type": "string"}},
                "required": ["name", "description"], "additionalProperties": False}},
            "theme_attribute": {"anyOf": [col_name, {"type": "null"}]},
        },
        "required": ["title", "abstract", "keywords", "columns", "theme_attribute"],
        "additionalProperties": False,
    }


def build_prompt(profile: dict) -> str:
    view = {k: profile[k] for k in ("name", "native_name", "geometry", "srs", "bbox",
                                    "sampled_features", "columns")}
    return ("Layer profile:\n" + json.dumps(view, ensure_ascii=False, indent=1)
            + "\n\nJSON:")


def clean_metadata(reply: dict, profile: dict) -> dict:
    """Validate/normalize an LLM reply against the real profile."""
    title = " ".join(str(reply.get("title") or "").split())[:120]
    if len(title) < 3:
        raise ValueError("model returned no usable title")
    abstract = " ".join(str(reply.get("abstract") or "").split())[:1000]
    seen, keywords = set(), []
    for k in reply.get("keywords") or []:
        k = " ".join(str(k).split()).lower()[:40]
        if k and k not in seen and k != AI_KEYWORD:
            seen.add(k)
            keywords.append(k)
    keywords = keywords[:15]
    real = {c["name"] for c in profile["columns"]}
    columns = []
    for c in reply.get("columns") or []:
        if isinstance(c, dict) and c.get("name") in real and c.get("description"):
            columns.append({"name": c["name"],
                            "description": " ".join(str(c["description"]).split())[:200]})
    theme = reply.get("theme_attribute")
    return {"title": title, "abstract": abstract, "keywords": keywords,
            "columns": columns, "theme_attribute": theme if theme in real else None}


def full_abstract(meta: dict) -> str:
    """Abstract + a compact field glossary (the resolver reads the abstract)."""
    if not meta["columns"]:
        return meta["abstract"]
    fields = "; ".join(f"{c['name']}: {c['description']}" for c in meta["columns"][:12])
    return f"{meta['abstract']} Campi: {fields}."[:2000]


async def generate(agent: Any, profile: dict, *, provider: str) -> dict:
    """Ask the model for metadata (schema-constrained, with one plain retry)."""
    from .catalog import _extract_json
    from .resolver import response_format_for

    prompt = build_prompt(profile)
    try:
        result = await agent.run(prompt, options={
            "response_format": response_format_for(provider, metadata_schema(profile))})
    except Exception as exc:  # noqa: BLE001 — backend may not support schemas
        logger.warning("structured metadata call failed (%s); retrying without", exc)
        result = await agent.run(prompt)
    return clean_metadata(_extract_json(result.text), profile)


async def write_metadata(workspace: str, name: str, meta: dict, client=None) -> None:
    from .client import get_client

    client = client or get_client()
    body = {"featureType": {
        "title": meta["title"],
        "abstract": full_abstract(meta),
        "keywords": {"string": meta["keywords"] + [AI_KEYWORD]},
    }}
    await client.put(f"workspaces/{workspace}/featuretypes/{name}.json", json=body)


def build_agent(language: str | None = None):
    """Metadata writer agent on the configured LLM provider."""
    from .agent import _build_chat_client
    from .config import get_settings

    language = language or os.environ.get("GEO_AI_METADATA_LANGUAGE") or "Italian"
    settings = get_settings()
    client = _build_chat_client(settings)
    agent = client.as_agent(name="metadata-writer",
                            instructions=INSTRUCTIONS.format(language=language), tools=[])
    return agent, settings.llm_provider


async def enrich_layer(workspace: str, name: str, *, agent=None, provider: str | None = None,
                       overwrite: bool = False, dry_run: bool = False, client=None) -> dict:
    """Profile -> generate -> write one layer. Returns a report dict.

    ``status`` is one of ``written``, ``dry-run``, ``skipped`` (hand-written
    metadata kept) or ``not-vector``.
    """
    from . import telemetry

    with telemetry.span("geo.metadata", **{"geo.layer": f"{workspace}:{name}",
                                           "geo.dry_run": dry_run}) as sp:
        rep = await _enrich_layer(workspace, name, agent=agent, provider=provider,
                                  overwrite=overwrite, dry_run=dry_run, client=client)
        telemetry.set_attributes(sp, **{"geo.metadata.status": rep["status"],
                                        "geo.metadata.keywords": len(rep.get("keywords") or [])})
        return rep


async def _enrich_layer(workspace, name, *, agent, provider, overwrite, dry_run, client):
    try:
        profile = await layer_profile(workspace, name, client=client)
    except ValueError as exc:
        return {"layer": f"{workspace}:{name}", "status": "not-vector", "reason": str(exc)}
    if not overwrite and not is_automatic(profile):
        return {"layer": f"{workspace}:{name}", "status": "skipped",
                "reason": "hand-written metadata (use overwrite to replace it)",
                "title": profile["title"]}
    if agent is None:
        agent, provider = build_agent()
    meta = await generate(agent, profile, provider=provider or "ollama")
    if not dry_run:
        await write_metadata(workspace, name, meta, client=client)
    return {"layer": f"{workspace}:{name}", "status": "dry-run" if dry_run else "written",
            **meta}


class BatchEnricher:
    """Run :func:`enrich_layer` over many layers; give up after repeated failures.

    Used by the bootstrap, where an unreachable LLM must not turn into one
    timeout per layer: after ``max_failures`` consecutive errors it stops.
    """

    def __init__(self, *, max_failures: int = 3, **kwargs):
        self.kwargs = kwargs
        self.max_failures = max_failures
        self.failures = 0
        self.disabled = False
        self.reports: list[dict] = []

    async def __call__(self, workspace: str, name: str) -> dict | None:
        if self.disabled:
            return None
        try:
            if "agent" not in self.kwargs:
                self.kwargs["agent"], self.kwargs["provider"] = build_agent()
            rep = await enrich_layer(workspace, name, **self.kwargs)
            self.failures = 0
        except Exception as exc:  # noqa: BLE001 — metadata is best-effort
            self.failures += 1
            rep = {"layer": f"{workspace}:{name}", "status": "failed", "reason": str(exc)}
            logger.warning("AI metadata failed for %s:%s: %s", workspace, name, exc)
            if self.failures >= self.max_failures:
                self.disabled = True
                logger.error("AI metadata disabled for this run after %d consecutive "
                             "failures (is the LLM reachable?).", self.failures)
        self.reports.append(rep)
        return rep


# ---- CLI ----------------------------------------------------------------------------------------
async def _list_layers(workspace: str) -> list[str]:
    from .client import get_client

    data = await get_client().get_json(f"workspaces/{workspace}/featuretypes.json")
    fts = ((data or {}).get("featureTypes") or {})
    items = fts.get("featureType", []) if isinstance(fts, dict) else []
    return [i["name"] for i in items]


async def _main(args) -> int:
    from .config import get_settings

    ws = args.workspace or get_settings().default_workspace
    if not ws:
        print("error: pass --workspace or set GEOSERVER_DEFAULT_WORKSPACE", file=sys.stderr)
        return 2
    names = args.layer or await _list_layers(ws)
    if args.limit:
        names = names[:args.limit]
    run = BatchEnricher(overwrite=args.overwrite, dry_run=args.dry_run)
    for n in names:
        rep = await run(ws, n)
        if rep is None:
            break
        line = f"{rep['status']:<10} {rep['layer']}"
        if rep.get("title"):
            line += f"  — {rep['title']}"
        if rep.get("reason"):
            line += f"  ({rep['reason']})"
        print(line)
        if args.verbose and rep["status"] in ("written", "dry-run"):
            print(json.dumps({k: rep[k] for k in ("abstract", "keywords", "columns",
                                                  "theme_attribute")},
                             ensure_ascii=False, indent=1))
    counts = Counter(r["status"] for r in run.reports)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("failed") else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcp_geo_server.metadata",
                                 description="Write AI-generated metadata for published "
                                             "vector layers.")
    ap.add_argument("--workspace", help="default: GEOSERVER_DEFAULT_WORKSPACE")
    ap.add_argument("--layer", action="append", help="layer name (repeatable); default: all")
    ap.add_argument("--limit", type=int, help="process at most N layers")
    ap.add_argument("--dry-run", action="store_true", help="generate but do not write")
    ap.add_argument("--overwrite", action="store_true",
                    help="also replace hand-written metadata")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the generated fields")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("GEO_LOG_LEVEL", "WARNING").upper())
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
