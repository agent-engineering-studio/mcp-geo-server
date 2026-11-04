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
from . import telemetry
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
    "nothing in the catalog fits, return an empty layers list.\n"
    "- metrics: terrain metrics the user wants computed on the selected "
    "features — any of quota (elevation/altitude), slope (pendenza, how steep), "
    "aspect (esposizione/orientation), curvature; [] if none.\n"
    "- place: the administrative place the request is about, as written "
    "(e.g. \"provincia di Bari\"), or null.\n"
    "- mode: when a 'Current map' is given and the request REFINES it (\"solo "
    "le più pericolose\", \"aggiungi il DTM\", \"togli i crolli\"), mode is "
    "\"refine\" and layers/cql_filter are the COMPLETE new state: keep the "
    "current layers and filter unless the request changes them. A request about "
    "a different subject is mode \"new\" and ignores the current map."
)

METRICS = ("quota", "slope", "aspect", "curvature")


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
            "mode": {"type": "string", "enum": ["new", "refine"]},
            "metrics": {"type": "array", "items": {"type": "string", "enum": list(METRICS)}},
            "place": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["layers", "cql_filter", "explanation", "confidence",
                     "mode", "metrics", "place"],
        "additionalProperties": False,
    }


# ---- conversation context ---------------------------------------------------------------
def clean_context(context: Any, catalog: list[LayerMeta]) -> dict | None:
    """The map state the client sends back, validated; None if empty/invalid.

    ``{"layers": [...], "cql_filter": str|None, "admin": {...}|None}``. Unknown
    layers are dropped (the catalog may have changed between turns).
    """
    if not isinstance(context, dict):
        return None
    known = {m.qualified for m in catalog}
    layers = [str(q) for q in (context.get("layers") or []) if str(q) in known]
    if not layers:
        return None
    cql = context.get("cql_filter")
    admin = context.get("admin") if isinstance(context.get("admin"), dict) else None
    return {"layers": layers, "cql_filter": cql if isinstance(cql, str) and cql else None,
            "admin": admin}


def context_prompt(context: dict | None) -> str:
    if not context:
        return ""
    area = context.get("admin") or {}
    lines = ["Current map (the user may be refining it):",
             "- layers: " + ", ".join(context["layers"]),
             f"- cql_filter: {context.get('cql_filter') or 'null'}"]
    if area.get("name"):
        lines.append(f"- area: {area.get('level', '')} {area['name']}".rstrip())
    return "\n".join(lines)


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


def cached_prompt_parts(catalog: list[LayerMeta], hints: str, query: str, *,
                        catalog_is_static: bool,
                        context: dict | None = None) -> tuple[dict, str]:
    """Anthropic prompt caching: (cacheable system block, per-request message).

    The static part — the full catalog (when retrieval did not narrow it, so it
    is identical across requests) and the filter hints — goes in a system block
    marked ``cache_control``; Agent Framework appends it after the agent's
    instructions, so the cached prefix is instructions + catalog + hints. The
    request (and a narrowed, per-request catalog) goes in the user message.
    """
    static = [catalog_prompt(catalog)] if catalog_is_static else []
    if hints:
        static.append(hints)
    block = {"type": "text", "text": "\n\n".join(static) or "(no catalog context)",
             "cache_control": {"type": "ephemeral"}}
    dynamic = [] if catalog_is_static else [catalog_prompt(catalog)]
    if context:
        dynamic.append(context_prompt(context))
    dynamic.append(f"Request: {query}\n\nJSON:")
    return block, "\n\n".join(dynamic)


def prompt_cache_from_env() -> bool:
    import os
    return (os.environ.get("GEO_PROMPT_CACHE") or "false").strip().lower() in {
        "1", "true", "yes", "on"}


def build_prompt(catalog: list[LayerMeta], hints: str, query: str,
                 context: dict | None = None) -> str:
    """Static-first ordering: catalog and hints change rarely, the query always."""
    parts = [catalog_prompt(catalog)]
    if hints:
        parts.append(hints)
    if context:
        parts.append(context_prompt(context))
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
        telemetry.event("geo.cql.rejected", **{"geo.cql.reason": str(exc)})
        selection["cql_filter"] = None
        selection["cql_rejected"] = {"filter": raw, "reason": str(exc)}
    return selection


async def resolve(agent: Any, query: str, catalog: list[LayerMeta], spec: FilterSpec,
                  *, provider: str, structured: bool = True, guard: bool = True,
                  retriever: Any | None = None, prompt_cache: bool = False,
                  context: dict | None = None) -> dict:
    """Run the resolver agent on ``query`` and return a validated selection.

    Result keys: ``layers``, ``cql_filter``, ``explanation``, ``confidence``,
    ``cql_rejected`` (None or {filter, reason}), ``structured`` (whether the
    schema-constrained call was used), ``raw`` (model text, for evals/debug).
    Raises ``ValueError`` if the reply cannot be parsed at all.

    ``guard=False`` skips :func:`guard_cql`, for callers that know the layers'
    real columns and run it themselves (the web UI does).

    ``retriever`` (:class:`mcp_geo_server.retrieval.Retriever`) narrows the
    layers shown to the model; the reply is still validated against the FULL
    catalog. The result then carries ``candidates`` (how many were shown).

    ``prompt_cache`` (Anthropic only) sends the static part of the prompt as a
    cached system block (:func:`cached_prompt_parts`).

    ``context`` is the current map state for a follow-up turn (see
    :func:`clean_context`); the result then carries ``mode`` ``"refine"`` or
    ``"new"``. Also returned: ``metrics`` and ``place`` read by the model.
    """
    with telemetry.span("geo.resolve", **{
            "gen_ai.provider.name": provider, "geo.catalog.size": len(catalog),
            "geo.query.length": len(query), "geo.structured.requested": structured,
    }) as sp:
        context = clean_context(context, catalog)
        shown = catalog
        retrieval_reason = None
        narrowed = False
        if retriever is not None:
            r = retriever.retrieve(query, catalog)
            shown, retrieval_reason, narrowed = r.candidates, r.reason, r.applied
            if narrowed and context:
                # The current layers must stay selectable (the schema enum).
                keep = {m.qualified for m in shown} | set(context["layers"])
                shown = [m for m in catalog if m.qualified in keep]
        prompt = build_prompt(shown, spec.hints(), query, context)
        first_prompt, first_opts = prompt, {}
        use_cache = prompt_cache and provider == "anthropic"
        if use_cache:
            block, first_prompt = cached_prompt_parts(shown, spec.hints(), query,
                                                      catalog_is_static=not narrowed,
                                                      context=context)
            first_opts["instructions"] = [block]
        if structured:
            first_opts["response_format"] = response_format_for(provider,
                                                                selection_schema(shown))
        used_structured = False
        result = None
        if first_opts:
            try:
                result = await agent.run(first_prompt, options=first_opts)
                used_structured = structured
            except Exception as exc:  # noqa: BLE001 — schema/cache may be unsupported
                logger.warning("resolver call with %s failed (%s); retrying plain",
                               "+".join(sorted(first_opts)), exc)
        if result is None:
            result = await agent.run(prompt)

        text = result.text
        selection = validate_selection(text, catalog)
        reply = _extract_json(text)
        requested = reply.get("layers") or []
        requested = [requested] if isinstance(requested, str) else requested
        selection["confidence"] = _confidence(reply)
        selection["mode"] = ("refine" if context and reply.get("mode") == "refine"
                             else "new")
        selection["metrics"] = [m for m in METRICS if m in (reply.get("metrics") or [])]
        place = reply.get("place")
        selection["place"] = place.strip() if isinstance(place, str) and place.strip() else None
        selection["structured"] = used_structured
        selection["raw"] = text
        selection["candidates"] = len(shown)
        selection.setdefault("cql_rejected", None)
        if guard:
            guard_cql(selection, spec)
        telemetry.set_attributes(sp, **{
            "geo.structured.used": used_structured,
            "geo.layers.count": len(selection["layers"]),
            "geo.layers.dropped": max(0, len(requested) - len(selection["layers"])),
            "geo.cql.present": bool(reply.get("cql_filter")),
            "geo.cql.rejected": selection["cql_rejected"] is not None,
            "geo.confidence": selection["confidence"],
            "geo.retrieval.candidates": len(shown),
            "geo.retrieval.reason": retrieval_reason,
            "geo.prompt_cache": use_cache,
            "geo.mode": selection["mode"],
        })
        return selection


# ---- model routing ------------------------------------------------------------------
@dataclass
class Tier:
    """One model the resolver can use: an agent plus its provider/model label."""

    agent: Any
    provider: str
    model: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


DEFAULT_MIN_CONFIDENCE = 0.5


def escalation_reason(selection: dict, min_confidence: float) -> str | None:
    """Why a reply is not good enough to keep, or None.

    Only a LOW self-reported confidence escalates: an empty selection with high
    confidence is a legitimate "nothing in the catalog fits".
    """
    c = selection.get("confidence")
    if c is not None and c < min_confidence:
        return f"confidence {c:.2f} < {min_confidence:.2f}"
    return None


async def resolve_routed(tiers: list[Tier], query: str, catalog: list[LayerMeta],
                         spec: FilterSpec, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                         **kwargs) -> dict:
    """:func:`resolve` on the first tier; move to the next on failure or doubt.

    A tier is skipped when its call raises (unreachable model, unparseable
    reply) or :func:`escalation_reason` objects. The last tier's answer is
    kept whatever its confidence. Adds ``model`` (tier label), ``escalated``
    and ``escalations`` (list of ``{model, reason}``) to the selection.
    """
    if not tiers:
        raise ValueError("no resolver tiers configured")
    escalations: list[dict] = []
    for i, tier in enumerate(tiers):
        last = i == len(tiers) - 1
        try:
            sel = await resolve(tier.agent, query, catalog, spec,
                                provider=tier.provider, **kwargs)
        except Exception as exc:  # noqa: BLE001 — try the next model
            if last:
                raise
            escalations.append({"model": tier.label, "reason": f"{type(exc).__name__}: {exc}"})
            logger.warning("resolver tier %s failed (%s); escalating", tier.label, exc)
            continue
        reason = None if last else escalation_reason(sel, min_confidence)
        if reason is None:
            sel.update(model=tier.label, escalated=bool(escalations),
                       escalations=escalations)
            if escalations:
                telemetry.event("geo.resolve.escalated", **{
                    "geo.model": tier.label,
                    "geo.escalation.reasons": "; ".join(e["reason"] for e in escalations)})
            return sel
        escalations.append({"model": tier.label, "reason": reason})
        logger.info("resolver tier %s: %s; escalating", tier.label, reason)
    raise AssertionError("unreachable")


def tiers_from_settings(instructions: str = INSTRUCTIONS) -> list[Tier]:
    """Primary tier from the configured provider, plus an optional fallback.

    ``GEO_LLM_FALLBACK_PROVIDER`` (and optionally ``GEO_LLM_FALLBACK_MODEL``)
    add a second tier, e.g. a local Ollama model first and Claude when it is
    unsure.
    """
    import os

    from .agent import _build_chat_client, model_name, settings_for
    from .config import get_settings

    def tier(settings) -> Tier:
        client = _build_chat_client(settings)
        agent = client.as_agent(name="layer-resolver", instructions=instructions, tools=[])
        return Tier(agent, settings.llm_provider, model_name(settings))

    base = get_settings()
    tiers = [tier(base)]
    fb = (os.environ.get("GEO_LLM_FALLBACK_PROVIDER") or "").strip().lower()
    if fb:
        tiers.append(tier(settings_for(base, fb,
                                       (os.environ.get("GEO_LLM_FALLBACK_MODEL") or "").strip()
                                       or None)))
    return tiers


def min_confidence_from_env() -> float:
    import os
    raw = (os.environ.get("GEO_LLM_CONFIDENCE_MIN") or "").strip()
    return float(raw) if raw else DEFAULT_MIN_CONFIDENCE
