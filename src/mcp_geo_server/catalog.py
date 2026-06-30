"""Layer-catalog parsing + natural-language selection helpers.

The published ISPRA layers have highly structured names that encode theme,
geometry and region, e.g. ``frane_line_molise_opendata``. This module:

* parses those names into ``{theme, geometry, region, label}`` (best effort) so
  the UI can group/search them, and
* builds a compact catalog the LLM resolver reads to map an Italian request
  ("mostrami le frane lineari del Molise") to exact layer names, and
* validates the LLM's JSON reply against the real catalog (dropping anything it
  hallucinated).

Pure functions, no I/O — unit-testable without a running GeoServer or LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Region tokens as they appear (sanitized) inside layer names.
REGIONS = {
    "abruzzo", "basilicata", "calabria", "campania", "emilia_romagna",
    "friuli_venezia_giulia", "lazio", "liguria", "lombardia", "marche",
    "molise", "piemonte", "puglia", "sardegna", "sicilia", "toscana",
    "umbria", "valle_d_aosta", "veneto", "bolzano", "trento",
}

# Human labels for the dataset themes.
THEMES = {
    "frane": "Frane (IFFI)",
    "dgpv": "DGPV — deformazioni gravitative profonde di versante",
    "aree": "Aree in frana",
    "mosaicatura": "Mosaicatura pericolosità (PAI)",
}

# Geometry hints found in names.
_GEOMETRY = {"line": "line", "poly": "polygon", "piff": "point"}

# Administrative-boundary prefixes (ISTAT Limiti).
_LIMITI = {
    "com": "Comuni", "provcm": "Province / Città metropolitane",
    "reg": "Regioni", "ripgeo": "Ripartizioni geografiche",
}


@dataclass(frozen=True)
class LayerMeta:
    name: str            # bare layer name, e.g. "frane_line_molise_opendata"
    workspace: str       # e.g. "ispra"
    theme: str | None
    geometry: str | None
    region: str | None
    label: str           # human-friendly label

    @property
    def qualified(self) -> str:
        return f"{self.workspace}:{self.name}" if self.workspace else self.name


def parse_layer_name(name: str, workspace: str = "") -> LayerMeta:
    """Best-effort parse of a layer name into structured metadata."""
    tokens = name.lower().split("_")
    token_set = set(tokens)

    region = next((r for r in REGIONS if r in name.lower()), None)
    geometry = next((g for t, g in _GEOMETRY.items() if t in token_set), None)

    # The theme is the name's PREFIX (tokens[0]); fall back to any matching token
    # so e.g. "mosaicatura_..._aree_..." is mosaicatura, not aree.
    theme = tokens[0] if tokens and tokens[0] in THEMES else \
        next((t for t in THEMES if t in token_set), None)
    limiti = next((p for p in _LIMITI if name.lower().startswith(p)), None)

    # Build a readable label.
    if limiti:
        theme = theme or "limiti"
        geometry = geometry or "polygon"
        label = f"Limiti — {_LIMITI[limiti]}"
    elif theme == "mosaicatura":
        geometry = geometry or "polygon"
        kind = "frana" if "frana" in token_set else \
            ("idraulica" if "idraulica" in token_set else "")
        label = THEMES[theme] + (f" — {kind}" if kind else "")
    elif theme:
        parts = [THEMES[theme]]
        if region:
            parts.append(region.replace("_", " ").title())
        label = " — ".join(parts)
    else:
        label = name

    return LayerMeta(name=name, workspace=workspace, theme=theme,
                     geometry=geometry, region=region, label=label)


def build_catalog(layers: list[tuple[str, str]]) -> list[LayerMeta]:
    """Parse a list of ``(name, workspace)`` pairs into ``LayerMeta`` records."""
    return [parse_layer_name(name, ws) for name, ws in layers]


CATALOG_LEGEND = (
    "Naming convention of the layers:\n"
    "- prefix = theme: frane (landslides/IFFI), dgpv (deep-seated slope "
    "deformations), aree (landslide areas), mosaicatura (PAI hazard mosaic), "
    "com/provcm/reg/ripgeo (ISTAT boundaries: municipalities/provinces/regions/"
    "geographic-areas).\n"
    "- geometry token: line (lines), poly (polygons), piff (points).\n"
    "- region token: the Italian region, e.g. molise, veneto, valle_d_aosta.\n"
)


def catalog_prompt(catalog: list[LayerMeta]) -> str:
    """Render the catalog as a compact table for the LLM resolver."""
    lines = [f"{m.qualified}\t[theme={m.theme or '?'} geom={m.geometry or '?'} "
             f"region={m.region or '-'}]" for m in catalog]
    return CATALOG_LEGEND + "\nAvailable layers (use the EXACT qualified name):\n" \
        + "\n".join(lines)


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

    # Accept either qualified ("ws:name") or bare ("name") references.
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
