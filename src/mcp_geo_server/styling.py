"""Config-driven thematic SLD styles + idempotent (re)assignment.

The styles and the layer->style assignment rules live in a YAML file, NOT in
code, so the same stack serves any domain: edit the config (or point
``GEO_STYLES_CONFIG`` at your own file) and re-run. The shipped default
(``styles_default.yml``) reproduces the ISPRA landslide/hazard domain.

SLDs are built programmatically from the config's classification tables so the
polygon / line / point variants of a categorical style stay in sync.

Run standalone to (re)apply everything to the published layers::

    python -m mcp_geo_server.styling
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from importlib.resources import files
from pathlib import Path

import yaml

from .client import GeoServerError, get_client
from .config import get_settings
from .formatting import extract
from .tools.styles import (
    geo_assign_style_to_layer,
    geo_create_style,
    geo_update_style,
)

logger = logging.getLogger("mcp_geo_server.styling")

_DEFAULT_ELSE = "#cccccc"


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
def default_config_path() -> Path:
    """Path to the packaged default style config."""
    return Path(str(files("mcp_geo_server").joinpath("styles_default.yml")))


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Load the style config (env ``GEO_STYLES_CONFIG`` > arg > packaged default).

    If the chosen path does not exist, fall back to the packaged ISPRA default so
    a fresh checkout (where ``data/styles.yml`` may be absent) still works.
    """
    chosen = path or os.environ.get("GEO_STYLES_CONFIG") or default_config_path()
    if not Path(chosen).is_file():
        logger.warning("Style config '%s' not found — using packaged default.",
                       chosen)
        chosen = default_config_path()
    with open(chosen, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("styles", {})
    cfg.setdefault("assign", [])
    logger.info("Loaded style config from %s.", chosen)
    return cfg


# --------------------------------------------------------------------------
# SLD builders (SLD 1.0)
# --------------------------------------------------------------------------
def _xml(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _symbolizer(kind: str, color: str, stroke: bool = True) -> str:
    if kind == "line":
        return (f'<LineSymbolizer><Stroke>'
                f'<CssParameter name="stroke">{color}</CssParameter>'
                f'<CssParameter name="stroke-width">2</CssParameter>'
                f'</Stroke></LineSymbolizer>')
    if kind == "point":
        return (f'<PointSymbolizer><Graphic><Mark>'
                f'<WellKnownName>circle</WellKnownName>'
                f'<Fill><CssParameter name="fill">{color}</CssParameter></Fill>'
                f'<Stroke><CssParameter name="stroke">#333333</CssParameter>'
                f'<CssParameter name="stroke-width">0.5</CssParameter></Stroke>'
                f'</Mark><Size>7</Size></Graphic></PointSymbolizer>')
    # polygon — stroke optional (dense mosaics read better without it).
    stroke_xml = (
        '<Stroke><CssParameter name="stroke">#333333</CssParameter>'
        '<CssParameter name="stroke-width">0.3</CssParameter></Stroke>'
    ) if stroke else ""
    return (f'<PolygonSymbolizer>'
            f'<Fill><CssParameter name="fill">{color}</CssParameter>'
            f'<CssParameter name="fill-opacity">0.7</CssParameter></Fill>'
            f'{stroke_xml}</PolygonSymbolizer>')


def _rule(title: str, attribute: str, value: str, kind: str, color: str,
          stroke: bool) -> str:
    return (
        f"<Rule><Title>{_xml(title)}</Title>"
        f"<ogc:Filter><ogc:PropertyIsEqualTo>"
        f"<ogc:PropertyName>{_xml(attribute)}</ogc:PropertyName>"
        f"<ogc:Literal>{_xml(value)}</ogc:Literal>"
        f"</ogc:PropertyIsEqualTo></ogc:Filter>"
        f"{_symbolizer(kind, color, stroke)}</Rule>"
    )


def _wrap(name: str, rules: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<StyledLayerDescriptor version="1.0.0" '
        'xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<NamedLayer><Name>{_xml(name)}</Name><UserStyle><Name>{_xml(name)}</Name>"
        f"<FeatureTypeStyle>{rules}</FeatureTypeStyle>"
        "</UserStyle></NamedLayer></StyledLayerDescriptor>"
    )


def build_style(name: str, spec: dict) -> str:
    """Render one style definition into an SLD 1.0 document."""
    kind = (spec.get("kind") or "polygon").lower()

    if kind == "flat":
        color = spec.get("color", "#3182bd")
        opacity = spec.get("opacity", 0.6)
        sym = (f'<PolygonSymbolizer><Fill>'
               f'<CssParameter name="fill">{color}</CssParameter>'
               f'<CssParameter name="fill-opacity">{opacity}</CssParameter></Fill>'
               f'<Stroke><CssParameter name="stroke">{color}</CssParameter>'
               f'<CssParameter name="stroke-width">0.5</CssParameter></Stroke>'
               f'</PolygonSymbolizer>')
        return _wrap(name, f"<Rule><Title>{_xml(spec.get('title', name))}</Title>"
                           f"{sym}</Rule>")

    if kind == "outline":
        color = spec.get("color", "#444444")
        width = spec.get("width", 1)
        sym = (f'<PolygonSymbolizer><Stroke>'
               f'<CssParameter name="stroke">{color}</CssParameter>'
               f'<CssParameter name="stroke-width">{width}</CssParameter></Stroke>'
               f'</PolygonSymbolizer>')
        return _wrap(name, f"<Rule><Title>{_xml(spec.get('title', name))}</Title>"
                           f"{sym}</Rule>")

    # categorical polygon / line / point
    if kind not in ("polygon", "line", "point"):
        raise ValueError(f"style '{name}': unknown kind '{kind}'")
    attribute = spec.get("attribute")
    classes = spec.get("classes") or []
    if not attribute or not classes:
        raise ValueError(f"style '{name}': categorical kind needs 'attribute' + "
                         "'classes'.")
    stroke = bool(spec.get("stroke", True))
    else_color = spec.get("else_color", _DEFAULT_ELSE)
    rules = "".join(
        _rule(c["label"], attribute, c["value"], kind, c["color"], stroke)
        for c in classes
    )
    rules += (f"<Rule><Title>Altro</Title><ElseFilter/>"
              f"{_symbolizer(kind, else_color, stroke)}</Rule>")
    return _wrap(name, rules)


def build_styles(cfg: dict) -> dict[str, str]:
    """Build every ``name -> SLD`` from the config's ``styles`` section."""
    return {name: build_style(name, spec) for name, spec in cfg["styles"].items()}


# --------------------------------------------------------------------------
# Layer -> style mapping (structured fields + name escape hatch)
# --------------------------------------------------------------------------
def _rule_matches(rule: dict, name: str) -> bool:
    """A rule matches when ALL of its name conditions hold (domain-agnostic)."""
    matched_any = False
    if "name_contains" in rule:
        matched_any = True
        if str(rule["name_contains"]).lower() not in name.lower():
            return False
    if "name_matches" in rule:
        matched_any = True
        if not re.search(rule["name_matches"], name):
            return False
    # A rule with no condition never matches (avoid styling everything).
    return matched_any


def style_for_layer(name: str, assign: list[dict]) -> str | None:
    """First assignment rule whose name conditions match, else None.

    Matching is purely name-based (``name_contains`` / ``name_matches``) so the
    engine stays independent of any naming convention — the rules live in the
    config.
    """
    for rule in assign:
        if _rule_matches(rule, name):
            return rule.get("style")
    return None


# --------------------------------------------------------------------------
# Apply (idempotent)
# --------------------------------------------------------------------------
async def ensure_styles(styles: dict[str, str]) -> None:
    """Create (or update) every style as a global GeoServer style."""
    client = get_client()
    data = await client.get_json("styles.json")
    existing = {s.get("name") for s in extract(data, "styles", "style")}
    for name, sld in styles.items():
        if name in existing:
            await geo_update_style(name, sld=sld)
            logger.info("Updated style '%s'.", name)
        else:
            await geo_create_style(name, sld=sld)
            logger.info("Created style '%s'.", name)


async def _published_layers(workspace: str | None) -> list[tuple[str, str]]:
    client = get_client()
    data = await client.get_json("layers.json")
    pairs = []
    for entry in extract(data, "layers", "layer"):
        full = entry.get("name", "") if isinstance(entry, dict) else str(entry)
        ws, _, bare = full.partition(":") if ":" in full else ("", "", full)
        if workspace and ws != workspace:
            continue
        pairs.append((bare, ws))
    return pairs


async def assign_styles(layers: list[tuple[str, str]], assign: list[dict]) -> int:
    """Assign the matching style as default to each ``(name, ws)`` layer."""
    count = 0
    for name, ws in layers:
        style = style_for_layer(name, assign)
        if not style:
            continue
        try:
            await geo_assign_style_to_layer(name, style, workspace=ws, default=True)
            count += 1
        except GeoServerError as exc:
            logger.error("Failed to assign '%s' to %s:%s — %s", style, ws, name, exc)
    return count


async def apply(workspace: str | None = None, config_path: str | None = None) -> int:
    """Load config, ensure styles exist, assign them to matching layers."""
    cfg = load_config(config_path)
    styles = build_styles(cfg)
    await ensure_styles(styles)
    layers = await _published_layers(workspace)
    styled = await assign_styles(layers, cfg["assign"])
    logger.info("Thematic styles applied to %d layer(s)%s.", styled,
                f" in workspace '{workspace}'" if workspace else "")
    return styled


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GEO_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    get_settings()
    workspace = (os.environ.get("GEO_STYLE_WORKSPACE") or "").strip() or None
    asyncio.run(apply(workspace))


if __name__ == "__main__":
    main()
