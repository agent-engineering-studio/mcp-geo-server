"""Natural-language request -> layers, as one reusable core.

This is the resolver behind the chat (``POST /api/ask``), moved out of the web
UI so the same code path serves the UI, the evaluation runner
(``tests/evals/resolver``) and, later, an MCP tool.

What it adds over a plain "reply with JSON" prompt:

* **Structured output** — the model is constrained by a JSON Schema built per
  request, with the layer names as an ``enum`` of the real catalog, so it
  cannot produce a name that does not exist. Each provider takes the schema in
  its own shape (:func:`response_format_for`). If a backend rejects the schema
  (some OpenAI-compatible servers do), the call is retried once unconstrained
  and the reply goes through the same validation.
* **A confidence score** in the reply, for model routing later on.
* **CQL guardrails** (:mod:`.cql`) — the filter is parsed, checked against the
  attributes and exact values the style config declares, and re-serialized; a
  rejected filter is dropped with a reason instead of breaking the render.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import cql as cqlmod
from .catalog import LayerMeta, _extract_json, catalog_prompt, validate_selection

logger = logging.getLogger("mcp_geo_server.resolver")

INSTRUCTIONS = (
    "You map an Italian (or English) natural-language map request to GeoServer "
    "layers chosen from a catalog the user gives you. Reply with ONLY a single "
    "JSON object, no prose, no code fences:\n"
    '{"layers": ["<exact qualified name from the catalog>", ...], '
    '"cql_filter": null, "explanation": "<one short sentence, in the user\'s '
    'language>", "confidence": <0.0-1.0>}\n'
    "Rules:\n"
    "- pick ONLY names that appear verbatim in the catalog; match the request "
    "against each layer's SUBJECT (its name, title and keywords).\n"
    "- A place name (region/province/comune, e.g. \"Valle d'Aosta\", \"Bari\") "
    "only restricts the AREA shown — the area clip is applied automatically "
    "afterwards. Use the place to choose a layer ONLY when the requested subject "
    "has per-place variants (e.g. landslide layers exist per region); otherwise "
    "ignore the place when selecting. NEVER add a layer of a DIFFERENT subject "
    "just because its name contains the place. Example: \"DTM della Valle "
    "d'Aosta\" -> the DTM / elevation layer ONLY (subject = DTM), NOT the "
    "landslide layers of Valle d'Aosta.\n"
    "- return as FEW layers as possible — only those whose subject was asked "
    "for; include several only when the request is genuinely ambiguous.\n"
    "- cql_filter: when the user asks for a SUBSET by an attribute value (e.g. "
    "'alta/elevata pericolosità'), build a CQL using ONLY the attributes and "
    "EXACT values listed under 'Filterable attributes'; otherwise null. Never "
    "invent layer, column or value names. Use only =, <>, IN (...), AND, OR, "
    "NOT and parentheses — no functions, no spatial predicates.\n"
    "- an attribute exists ONLY on the layers matching its pattern in "
    "'Filterable attributes'. If you build a cql_filter on an attribute, you "
    "MUST select a layer whose name matches that attribute's pattern — e.g. a "
    "hazard-class filter goes on the hazard layer, NOT on a landslide-inventory "
    "layer that lacks that column.\n"
    "- confidence: how sure you are that the layers (and filter) are what the "
    "user meant — low when the request is vague or nothing fits well. If "
    "nothing in the catalog fits, return an empty layers list."
)


# ---- filterable attributes (from the style config) -------------------------
@dataclass(frozen=True)
class FilterSpec:
    """Categorical attributes the resolver may filter on, from the style config."""

    # attribute -> [(value, label), ...] in config order
    values: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # attribute -> layer-name patterns (from the `assign` rules) it lives on
    patterns: dict[str, set[str]] = field(default_factory=dict)

    @property
    def allowed_values(self) -> dict[str, set[str]]:
        return {a: {v for v, _ in vs} for a, vs in self.values.items()}

    def attributes_for_layer(self, layer_name: str) -> set[str]:
        """Attributes whose assign pattern matches ``layer_name`` (bare name)."""
        out = set()
        for attr, pats in self.patterns.items():
            for p in pats:
                if _pattern_matches(p, layer_name):
                    out.add(attr)
                    break
        return out

    def hints(self) -> str:
        """Prompt section listing the attributes, their values and layers."""
        if not self.values:
            return ""
        lines = ["Filterable attributes — each exists ONLY on layers whose "
                 "name matches the given pattern; to filter by it you MUST "
                 "pick such a layer (use EXACT values in cql_filter):"]
        for a, vs in self.values.items():
            vals = ", ".join(f"'{v}' ({lbl})" if lbl else f"'{v}'" for v, lbl in vs)
            pats = " | ".join(sorted(self.patterns.get(a, ()))) or "(any)"
            lines.append(f"- {a} (layers matching: {pats}): {vals}")
        return "\n".join(lines)


def _pattern_matches(pattern: str, name: str) -> bool:
    """Same semantics as the style `assign` rules: regex, else substring."""
    try:
        return re.search(pattern, name, re.I) is not None
    except re.error:
        return pattern.lower() in name.lower()


def filter_spec(style_config: dict) -> FilterSpec:
    """Derive the :class:`FilterSpec` from a styles.yml-shaped dict."""
    styles = style_config.get("styles", {}) or {}
    assign = style_config.get("assign", []) or []
    values: dict[str, list[tuple[str, str]]] = {}
    patterns: dict[str, set[str]] = {}
    style_attr: dict[str, str] = {}
    for sname, spec in styles.items():
        attr, classes = spec.get("attribute"), spec.get("classes")
        if not (attr and classes):
            continue
        style_attr[sname] = attr
        vs = values.setdefault(attr, [])
        patterns.setdefault(attr, set())
        for c in classes:
            pair = (str(c.get("value")), str(c.get("label", "")))
            if pair not in vs:
                vs.append(pair)
    for rule in assign:
        attr = style_attr.get(rule.get("style"))
        pat = rule.get("name_contains") or rule.get("name_matches")
        if attr and pat:
            patterns[attr].add(str(pat))
    return FilterSpec(values, patterns)


# ---- structured output ---------------------------------------------------------
def selection_schema(catalog: list[LayerMeta]) -> dict:
    """JSON Schema for the reply, with layer names restricted to the catalog."""
    names = [m.qualified for m in catalog]
    return {
        "type": "object",
        "properties": {
            "layers": {"type": "array",
                       "items": {"type": "string", "enum": names} if names
                       else {"type": "string"}},
            "cql_filter": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "explanation": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["layers", "cql_filter", "explanation", "confidence"],
        "additionalProperties": False,
    }


def response_format_for(provider: str, schema: dict) -> dict:
    """Wrap ``schema`` in the shape each Agent Framework chat client expects.

    Ollama passes the value straight to its ``format`` field (a bare schema);
    the OpenAI client wants the ``{"type": "json_schema", ...}`` envelope; the
    Anthropic client accepts either and adds ``additionalProperties`` itself.
    """
    if provider == "openai":
        return {"type": "json_schema",
                "json_schema": {"name": "layer_selection", "schema": schema,
                                "strict": True}}
    return schema


def build_prompt(catalog: list[LayerMeta], hints: str, query: str) -> str:
    """Static-first ordering: catalog and hints change rarely, the query always."""
    parts = [catalog_prompt(catalog)]
    if hints:
        parts.append(hints)
    parts.append(f"Request: {query}\n\nJSON:")
    return "\n\n".join(parts)


# ---- resolution -------------------------------------------------------------------
def _confidence(reply: dict) -> float | None:
    try:
        c = float(reply.get("confidence"))
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, c))


def guard_cql(selection: dict, spec: FilterSpec,
              layer_fields: dict[str, set[str]] | None = None) -> dict:
    """Validate ``selection["cql_filter"]`` in place; drop it with a reason if bad.

    Allowed attributes: those the style config places on the SELECTED layers,
    plus — when ``layer_fields`` (qualified name -> real columns) is given — the
    real columns of the selected layers. Categorical values must be the exact
    configured ones. On success the filter is replaced by its canonical form.
    """
    raw = selection.get("cql_filter")
    selection["cql_rejected"] = None
    if not raw:
        return selection
    layers = selection.get("layers", [])
    allowed: set[str] = set()
    for q in layers:
        allowed |= spec.attributes_for_layer(q.split(":")[-1])
        allowed |= set((layer_fields or {}).get(q, ()))
    try:
        selection["cql_filter"] = cqlmod.sanitize(
            raw, allowed_attributes=allowed, allowed_values=spec.allowed_values)
    except cqlmod.CQLError as exc:
        logger.info("cql filter rejected (%s): %s", exc, raw)
        selection["cql_filter"] = None
        selection["cql_rejected"] = {"filter": raw, "reason": str(exc)}
    return selection


async def resolve(agent: Any, query: str, catalog: list[LayerMeta], spec: FilterSpec,
                  *, provider: str, structured: bool = True, guard: bool = True) -> dict:
    """Run the resolver agent on ``query`` and return a validated selection.

    Result keys: ``layers``, ``cql_filter``, ``explanation``, ``confidence``,
    ``cql_rejected`` (None or {filter, reason}), ``structured`` (whether the
    schema-constrained call was used), ``raw`` (model text, for evals/debug).
    Raises ``ValueError`` if the reply cannot be parsed at all.

    ``guard=False`` skips :func:`guard_cql`, for callers that know the layers'
    real columns and run it themselves (the web UI does).
    """
    prompt = build_prompt(catalog, spec.hints(), query)
    used_structured = False
    result = None
    if structured:
        try:
            fmt = response_format_for(provider, selection_schema(catalog))
            result = await agent.run(prompt, options={"response_format": fmt})
            used_structured = True
        except Exception as exc:  # noqa: BLE001 — backend may not support schemas
            logger.warning("structured resolver call failed (%s); retrying "
                           "without a schema", exc)
    if result is None:
        result = await agent.run(prompt)

    text = result.text
    selection = validate_selection(text, catalog)
    selection["confidence"] = _confidence(_extract_json(text))
    selection["structured"] = used_structured
    selection["raw"] = text
    selection.setdefault("cql_rejected", None)
    return guard_cql(selection, spec) if guard else selection
