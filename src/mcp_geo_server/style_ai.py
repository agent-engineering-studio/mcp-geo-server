"""Thematic styles from a natural-language instruction.

"Colora le frane per tipo di movimento, toni caldi" -> a categorical style for
that layer. The model never writes SLD: it fills an entry of the styles.yml
schema that :func:`mcp_geo_server.styling.build_style` already compiles, so the
SLD is always well-formed and the result can be kept as config.

Two constrained steps, so nothing is invented:

1. **choose the attribute** — the model sees the layer's columns with sample
   values and picks one (JSON-Schema ``enum`` of the real column names);
2. **label and colour the classes** — the server reads the attribute's REAL
   distinct values (WFS), and the model assigns a label and a colour to those
   exact values (``enum`` again). Values it leaves out fall to ``else_color``.

The symbolizer kind (polygon / line / point) comes from the geometry type, not
from the model. :func:`propose` returns the spec, its YAML snippet for
``data/styles.yml`` and the SLD; :func:`apply` publishes it and makes it the
layer's default style.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from typing import Any

import yaml

logger = logging.getLogger("mcp_geo_server.style_ai")

MAX_CLASSES = 30
DISTINCT_SAMPLE = 5000
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

ATTRIBUTE_INSTRUCTIONS = (
    "You help style a GIS layer. Given the layer profile (columns with sample "
    "values) and the user's styling request, pick the ONE column the map should "
    "be coloured by: a categorical column with a handful of distinct values "
    "that matches what the user asked for. Reply with the JSON object only."
)
CLASSES_INSTRUCTIONS = (
    "You design a categorical map legend. For the given column you receive its "
    "EXACT distinct values (with counts). Give each value a short human label in "
    "{language} (decode codes when the column name or the request makes their "
    "meaning clear, otherwise keep the value as label) and a colour as #RRGGBB "
    "that follows the user's request (palette, emphasis). Use clearly "
    "distinguishable colours, and a neutral grey for 'unknown/undetermined' "
    "classes. Reply with the JSON object only."
)


def kind_for_geometry(geometry: str | None) -> str:
    g = (geometry or "").lower()
    if "line" in g:
        return "line"
    if "point" in g:
        return "point"
    return "polygon"


def style_name(layer: str) -> str:
    return "ai_" + re.sub(r"[^a-z0-9_]+", "_", layer.split(":")[-1].lower())


async def distinct_values(workspace: str, name: str, attribute: str, client=None
                          ) -> list[tuple[Any, int]]:
    """Distinct values of one attribute with counts (most frequent first).

    Read from up to ``DISTINCT_SAMPLE`` features — WFS has no DISTINCT — which
    is enough for the categorical columns worth styling by.
    """
    from .client import get_client

    client = client or get_client()
    resp = await client.ows(
        {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
         "typeNames": f"{workspace}:{name}", "propertyName": attribute,
         "count": str(DISTINCT_SAMPLE), "outputFormat": "application/json"},
        base=client.settings.wfs_base)
    counts: Counter = Counter()
    for f in json.loads(resp.text).get("features", []):
        v = f.get("properties", {}).get(attribute)
        if v not in (None, ""):
            counts[json.dumps(v, ensure_ascii=False)] += 1
    return [(json.loads(k), n) for k, n in counts.most_common()]


def attribute_schema(columns: list[str]) -> dict:
    return {"type": "object",
            "properties": {"attribute": {"type": "string", "enum": columns},
                           "reason": {"type": "string"}},
            "required": ["attribute", "reason"], "additionalProperties": False}


def classes_schema(values: list[str]) -> dict:
    return {"type": "object", "properties": {
        "title": {"type": "string"},
        "classes": {"type": "array", "items": {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": values},
                           "label": {"type": "string"},
                           "color": {"type": "string"}},
            "required": ["value", "label", "color"], "additionalProperties": False}},
        "else_color": {"type": "string"},
    }, "required": ["title", "classes", "else_color"], "additionalProperties": False}


def clean_classes(reply: dict, values: list[str]) -> dict:
    """Keep only real values with valid colours; one class per value."""
    legal = set(values)
    seen, classes = set(), []
    for c in reply.get("classes") or []:
        v, color = str(c.get("value", "")), str(c.get("color", "")).strip()
        if v in legal and v not in seen and _HEX.match(color):
            seen.add(v)
            label = " ".join(str(c.get("label") or v).split())[:60] or v
            classes.append({"value": v, "label": label, "color": color.lower()})
    if not classes:
        raise ValueError("the model returned no valid class (value + #RRGGBB colour)")
    else_color = str(reply.get("else_color") or "").strip()
    return {"title": " ".join(str(reply.get("title") or "").split())[:80],
            "classes": classes[:MAX_CLASSES],
            "else_color": else_color.lower() if _HEX.match(else_color) else "#cccccc"}


async def _ask(agent, prompt: str, schema: dict, provider: str) -> dict:
    from .catalog import _extract_json
    from .resolver import response_format_for

    try:
        result = await agent.run(prompt, options={
            "response_format": response_format_for(provider, schema)})
    except Exception as exc:  # noqa: BLE001 — backend may not support schemas
        logger.warning("structured style call failed (%s); retrying without", exc)
        result = await agent.run(prompt)
    return _extract_json(result.text)


def build_agents(language: str = "Italian"):
    from .agent import _build_chat_client
    from .config import get_settings

    settings = get_settings()
    client = _build_chat_client(settings)
    pick = client.as_agent(name="style-attribute", instructions=ATTRIBUTE_INSTRUCTIONS, tools=[])
    legend = client.as_agent(name="style-legend",
                             instructions=CLASSES_INSTRUCTIONS.format(language=language),
                             tools=[])
    return pick, legend, settings.llm_provider


async def propose(layer: str, instruction: str, *, attribute: str | None = None,
                  agents=None, provider: str | None = None, client=None) -> dict:
    """Design a style for ``layer`` (``ws:name``) from ``instruction``.

    Returns ``{name, layer, attribute, spec, yaml, sld, reason}``. ``attribute``
    skips step 1 when the caller already knows the column.
    """
    from .metadata import layer_profile
    from .styling import build_style

    ws, _, name = layer.partition(":")
    if not name:
        raise ValueError("layer must be qualified: workspace:name")
    profile = await layer_profile(ws, name, client=client)
    columns = [c["name"] for c in profile["columns"]]
    if not columns:
        raise ValueError(f"{layer} has no attributes to style by")
    if agents is None:
        pick, legend, provider = build_agents()
    else:
        pick, legend = agents
    provider = provider or "ollama"

    reason = "given by the caller"
    if attribute is None:
        view = [{k: c.get(k) for k in ("name", "type", "samples", "distinct_in_sample")}
                for c in profile["columns"]]
        reply = await _ask(pick, f"Styling request: {instruction}\n\nLayer: {layer} "
                                 f"({profile['geometry']})\nColumns:\n"
                                 + json.dumps(view, ensure_ascii=False, indent=1)
                                 + "\n\nJSON:", attribute_schema(columns), provider)
        attribute, reason = reply.get("attribute"), str(reply.get("reason") or "")
    if attribute not in columns:
        raise ValueError(f"attribute {attribute!r} is not a column of {layer}")

    counted = await distinct_values(ws, name, attribute, client=client)
    if not counted:
        raise ValueError(f"{layer}.{attribute} has no values")
    if len(counted) > MAX_CLASSES:
        raise ValueError(f"{layer}.{attribute} has {len(counted)}+ distinct values: "
                         f"too many for a categorical style (max {MAX_CLASSES})")
    values = [str(v) for v, _ in counted]
    legend_reply = await _ask(
        legend, f"Styling request: {instruction}\n\nLayer: {layer}\nColumn: {attribute}\n"
                "Distinct values (value: count):\n"
                + "\n".join(f"- {v}: {n}" for v, n in counted) + "\n\nJSON:",
        classes_schema(values), provider)
    legend_spec = clean_classes(legend_reply, values)

    sname = style_name(layer)
    spec = {"kind": kind_for_geometry(profile["geometry"]), "attribute": attribute,
            **({"title": legend_spec["title"]} if legend_spec["title"] else {}),
            "stroke": True, "else_color": legend_spec["else_color"],
            "classes": legend_spec["classes"]}
    sld = build_style(sname, spec)           # raises on an invalid spec
    snippet = yaml.safe_dump(
        {"styles": {sname: spec},
         "assign": [{"name_matches": f"^{re.escape(name)}$", "style": sname}]},
        allow_unicode=True, sort_keys=False)
    return {"name": sname, "layer": layer, "attribute": attribute, "reason": reason,
            "spec": spec, "yaml": snippet, "sld": sld}


async def apply(proposal: dict) -> None:
    """Publish the style and make it the layer's default."""
    from .styling import ensure_styles
    from .tools.styles import geo_assign_style_to_layer

    ws, _, name = proposal["layer"].partition(":")
    await ensure_styles({proposal["name"]: proposal["sld"]})
    await geo_assign_style_to_layer(name, proposal["name"], workspace=ws, default=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcp_geo_server.style_ai",
                                 description="Design a thematic style from a sentence.")
    ap.add_argument("layer", help="qualified layer, e.g. ispra:frane_poly_molise_opendata")
    ap.add_argument("instruction", help='e.g. "colora per tipo di movimento, toni caldi"')
    ap.add_argument("--attribute", help="skip the attribute choice")
    ap.add_argument("--apply", action="store_true",
                    help="publish it and set it as the layer's default style")
    args = ap.parse_args(argv)

    async def run() -> int:
        p = await propose(args.layer, args.instruction, attribute=args.attribute)
        print(f"# attribute: {p['attribute']} — {p['reason']}\n")
        print(p["yaml"])
        if args.apply:
            await apply(p)
            print(f"applied: {p['name']} is now the default style of {p['layer']}.\n"
                  "Add the YAML above to data/styles.yml to keep it across `make styles`.")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
