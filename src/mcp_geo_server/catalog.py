"""Domain-agnostic layer catalog + natural-language selection helpers.

NO hardcoded vocabulary: the catalog is built from each layer's real GeoServer
metadata (name, title, abstract, keywords — read from the WMS capabilities), and
the LLM resolver matches a request against that metadata. This works for ANY
GeoServer / domain, not just the ISPRA landslide data.

Pure functions, no I/O — unit-testable without a running GeoServer or LLM.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(frozen=True)
class LayerMeta:
    name: str                       # bare layer name, e.g. "frane_line_molise_opendata"
    workspace: str = ""             # e.g. "ispra"
    title: str = ""
    abstract: str = ""
    keywords: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.workspace}:{self.name}" if self.workspace else self.name

    @property
    def label(self) -> str:
        return self.title or self.name


def build_catalog(entries: list[dict]) -> list[LayerMeta]:
    """Build ``LayerMeta`` records from ``{name, workspace, title, ...}`` dicts."""
    out = []
    for e in entries:
        out.append(LayerMeta(
            name=e.get("name", ""),
            workspace=e.get("workspace", ""),
            title=e.get("title", "") or "",
            abstract=e.get("abstract", "") or "",
            keywords=tuple(e.get("keywords", []) or ()),
        ))
    return out


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag, e.g. '{...}Layer' -> 'Layer'."""
    return tag.rsplit("}", 1)[-1]


def parse_wms_capabilities(xml_text: str) -> list[dict]:
    """Parse a WMS GetCapabilities document into layer metadata dicts.

    Returns one dict per *named* layer: ``{name, workspace, title, abstract,
    keywords}``. The qualified name ``ws:layer`` is split into workspace + name.
    Namespace-agnostic (works for WMS 1.1.1 and 1.3.0).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    layers: list[dict] = []
    for el in root.iter():
        if _local(el.tag) != "Layer":
            continue
        children = list(el)
        name = title = abstract = ""
        keywords: list[str] = []
        for c in children:
            lt = _local(c.tag)
            if lt == "Name":
                name = (c.text or "").strip()
            elif lt == "Title":
                title = (c.text or "").strip()
            elif lt == "Abstract":
                abstract = (c.text or "").strip()
            elif lt == "KeywordList":
                keywords = [(k.text or "").strip() for k in c
                            if _local(k.tag) == "Keyword" and (k.text or "").strip()]
        if not name:
            continue  # container layer (no Name) — skip
        ws, _, bare = name.partition(":") if ":" in name else ("", "", name)
        layers.append({"name": bare, "workspace": ws, "title": title,
                       "abstract": abstract, "keywords": keywords})
    return layers


def catalog_prompt(catalog: list[LayerMeta]) -> str:
    """Render the catalog as a compact table for the LLM resolver.

    Each line gives the EXACT qualified name plus its human title and keywords,
    so the model can match a natural-language request semantically.
    """
    lines = []
    for m in catalog:
        extra = m.title if m.title and m.title != m.name else ""
        if m.keywords:
            extra = (extra + " | " if extra else "") + "kw: " + ", ".join(m.keywords)
        lines.append(f"{m.qualified}" + (f"\t{extra}" if extra else ""))
    return ("Available layers (use the EXACT qualified name on the left):\n"
            + "\n".join(lines))


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerates prose/fences)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model reply")
    return json.loads(text[start:end + 1])


def validate_selection(reply_text: str, catalog: list[LayerMeta]) -> dict:
    """Parse + validate the resolver's JSON reply against the real catalog.

    Returns ``{"layers": [qualified...], "cql_filter": str|None,
    "explanation": str}``. Layer names not present in the catalog are dropped.
    Raises ``ValueError`` if the reply is unparseable.
    """
    data = _extract_json(reply_text)

    by_qualified = {m.qualified: m for m in catalog}
    by_name = {m.name: m for m in catalog}

    requested = data.get("layers") or []
    if isinstance(requested, str):
        requested = [requested]

    valid: list[str] = []
    for ref in requested:
        ref = str(ref).strip()
        meta = by_qualified.get(ref) or by_name.get(ref) \
            or by_name.get(ref.split(":")[-1])
        if meta and meta.qualified not in valid:
            valid.append(meta.qualified)

    cql = data.get("cql_filter")
    cql = cql.strip() if isinstance(cql, str) and cql.strip() else None

    return {
        "layers": valid,
        "cql_filter": cql,
        "explanation": str(data.get("explanation") or "").strip(),
    }
