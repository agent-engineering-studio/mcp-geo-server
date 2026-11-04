"""Area reports: what data exists for a place, with exact counts.

``area_report("provincia di Bari", subject="frane")`` builds a fact sheet:

1. the area (admin scope: level, name, bbox, boundary);
2. the candidate layers — vector layers whose extent overlaps the area (from
   the capabilities bbox) and, if a ``subject`` is given, that match it;
   boundary layers are excluded;
3. per layer, the EXACT number of features inside the area (WFS hits with
   ``INTERSECTS``) and, when the domain config classifies it, the breakdown
   per class (:mod:`.analytics`); layers with nothing in the area are dropped;
4. a deterministic summary in Italian, plus Markdown and HTML renderings.

An optional **narrative** asks the LLM for a short readable synthesis of those
facts. It is fact-checked: every number in the narrative must appear in the
facts, or the narrative is discarded (the report says why). The deterministic
summary is always there, so a report never depends on the model being right.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import json
import logging
import re
import sys
from typing import Any

from . import analytics

logger = logging.getLogger("mcp_geo_server.report")

MAX_LAYERS = 25

NARRATIVE_INSTRUCTIONS = (
    "You write a short synthesis (3-6 sentences, in Italian) of a GIS fact sheet "
    "for a non-specialist reader. Use ONLY the facts given. Quote numbers exactly "
    "as written in the facts; do NOT compute sums, differences or percentages, "
    "and do not add numbers, dates or sources that are not in the facts. Say "
    "which data dominate and what is absent. Plain text, no headings."
)


def _overlaps(a: tuple | list | None, b: dict) -> bool:
    if not a:
        return True          # unknown extent: let the exact count decide
    minx, miny, maxx, maxy = a
    return not (maxx < b["minx"] or minx > b["maxx"] or maxy < b["miny"] or miny > b["maxy"])


def _numbers(text: str) -> set[str]:
    """Numbers in a text, normalized: '1.234' / '1,234' / '1234' -> '1234'."""
    out = set()
    for tok in re.findall(r"\d[\d.,]*\d|\d", text):
        digits = re.sub(r"[.,](?=\d{3}\b)", "", tok)        # thousands separators
        out.add(digits.replace(",", "."))
    return out


def fact_check(narrative: str, facts: dict) -> list[str]:
    """Numbers in ``narrative`` that do not appear anywhere in ``facts``."""
    allowed = _numbers(json.dumps(facts, ensure_ascii=False))
    for layer in facts.get("layers", []):                  # formatted forms too
        allowed |= _numbers(analytics.fmt(layer.get("total")))
    return sorted(_numbers(narrative) - allowed)


def summary_lines(facts: dict) -> list[str]:
    area = facts["area"]
    lines = []
    if not facts["layers"]:
        lines.append(f"Nessun dato vettoriale trovato per {area['name']}"
                     + (f" sul tema «{facts['subject']}»." if facts.get("subject") else "."))
    for lay in facts["layers"]:
        lines.append(analytics.describe(lay["layer"], lay["total"], lay.get("groups"),
                                        lay.get("attribute"), lay.get("title")))
    if facts.get("rasters"):
        lines.append("Raster che coprono l'area: " + ", ".join(facts["rasters"]) + ".")
    return lines


def to_markdown(rep: dict) -> str:
    f = rep["facts"]
    a = f["area"]
    out = [f"# {a['level'].capitalize()} di {a['name']}"
           + (f" — {f['subject']}" if f.get("subject") else ""), "",
           f"_Generato il {f['generated_at']} da mcp-geo-server; conteggi esatti via WFS._", ""]
    if rep.get("narrative"):
        out += ["## Sintesi", "", rep["narrative"], ""]
    out += ["## Dati nell'area", ""]
    if f["layers"]:
        out += ["| Layer | Elementi nell'area |", "|---|---|"]
        out += [f"| {lay.get('title') or lay['layer']} | {analytics.fmt(lay['total'])} |"
                for lay in f["layers"]]
        for lay in f["layers"]:
            if lay.get("groups"):
                out += ["", f"### {lay.get('title') or lay['layer']} per {lay['attribute']}", "",
                        "| Classe | Elementi |", "|---|---|"]
                out += [f"| {g['label']} | {analytics.fmt(g['count'])} |" for g in lay["groups"]]
    else:
        out.append(summary_lines(f)[0])
    if f.get("rasters"):
        out += ["", "Raster che coprono l'area: " + ", ".join(f["rasters"]) + "."]
    if rep.get("narrative_rejected"):
        out += ["", f"> Sintesi automatica scartata: {rep['narrative_rejected']}"]
    return "\n".join(out) + "\n"


def to_html(rep: dict) -> str:
    f = rep["facts"]
    a = f["area"]
    e = html.escape
    title = f"{a['level'].capitalize()} di {a['name']}" + (f" — {f['subject']}" if f.get("subject") else "")
    rows = "".join(f"<tr><td>{e(lay.get('title') or lay['layer'])}</td>"
                   f"<td class=n>{analytics.fmt(lay['total'])}</td></tr>" for lay in f["layers"])
    groups = ""
    for lay in f["layers"]:
        if not lay.get("groups"):
            continue
        top = max(g["count"] for g in lay["groups"]) or 1
        bars = "".join(
            f"<tr><td>{e(g['label'])}</td><td class=n>{analytics.fmt(g['count'])}</td>"
            f"<td><span class=bar style='width:{round(100 * g['count'] / top)}%'></span></td></tr>"
            for g in lay["groups"])
        groups += (f"<h3>{e(lay.get('title') or lay['layer'])} per {e(lay['attribute'])}</h3>"
                   f"<table>{bars}</table>")
    narrative = f"<h2>Sintesi</h2><p>{e(rep['narrative'])}</p>" if rep.get("narrative") else ""
    rejected = (f"<p class=note>Sintesi automatica scartata: {e(rep['narrative_rejected'])}</p>"
                if rep.get("narrative_rejected") else "")
    rasters = (f"<p>Raster che coprono l'area: {e(', '.join(f['rasters']))}.</p>"
               if f.get("rasters") else "")
    body = (f"<table><tr><th>Layer</th><th>Elementi nell'area</th></tr>{rows}</table>"
            if f["layers"] else f"<p>{e(summary_lines(f)[0])}</p>")
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#1c2420}}
table{{border-collapse:collapse;width:100%;margin:8px 0 16px}}td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}.bar{{display:inline-block;height:10px;background:#2f7d5b;border-radius:2px}}
.meta,.note{{color:#5b675f;font-size:13px}}</style></head><body>
<h1>{e(title)}</h1><p class=meta>Generato il {e(f['generated_at'])} da mcp-geo-server; conteggi esatti via WFS.</p>
{narrative}<h2>Dati nell'area</h2>{body}{groups}{rasters}{rejected}</body></html>"""


async def area_report(place: str, subject: str | None = None, *, assistant=None,
                      narrative: bool = False, agent: Any = None) -> dict:
    """Build the fact sheet for ``place`` (and optional ``subject``).

    Returns ``{facts, summary, narrative, narrative_rejected, markdown, html}``.
    Raises ``ValueError`` if the place is not an administrative unit.
    """
    from .answer import get_assistant
    from .retrieval import layer_search_text, match_score

    a = assistant or get_assistant()
    hit = await a.detect_admin(place)
    if not hit:
        raise ValueError(f"no administrative unit found in {place!r}")
    scope = await a.admin_scope(*hit)
    if not scope:
        raise ValueError(f"no boundary available for {hit[0].level} {hit[1]}")
    wkt = scope["clip"].split(";", 1)[-1]
    boundary = {lv.layer for lv in a.admin_areas().cfg.levels}
    spec = a.filter_spec()

    candidates = [m for m in await a.load_catalog()
                  if m.name not in boundary and _overlaps(m.bbox, scope["bbox"])
                  and (not subject or match_score(subject, layer_search_text(m)) > 0)]
    if len(candidates) > MAX_LAYERS:
        logger.warning("report for %s: %d candidate layers, keeping %d", place,
                       len(candidates), MAX_LAYERS)
        candidates = candidates[:MAX_LAYERS]

    layers, rasters = [], []
    for m in candidates:
        info = await a.layer_info(m.workspace, m.name)
        if not info:
            continue
        if info.get("geometry") == "raster":
            rasters.append(m.title or m.name)
            continue
        area_filter = f"INTERSECTS({info.get('geometry_column') or 'geom'}, {wkt})"
        total = await analytics.count(m.qualified, area_filter)
        if not total:
            continue
        attrs = [x for x in spec.attributes_for_layer(m.name) if x in (info.get("fields") or [])]
        groups, attribute = None, None
        if attrs:
            attribute = attrs[0]
            groups = await analytics.count_by(m.qualified, attribute,
                                              list(spec.values.get(attribute, [])), area_filter)
        layers.append({"layer": m.qualified, "title": m.title if m.title != m.name else None,
                       "total": total, "attribute": attribute, "groups": groups})
    layers.sort(key=lambda r: -(r["total"] or 0))

    facts = {"area": {"level": scope["level"], "name": scope["name"], "bbox": scope["bbox"]},
             "subject": subject, "layers": layers, "rasters": rasters,
             "generated_at": dt.date.today().isoformat()}
    rep: dict = {"facts": facts, "summary": summary_lines(facts),
                 "narrative": None, "narrative_rejected": None}

    if narrative and layers:
        try:
            if agent is None:
                agent = _narrative_agent()
            view = {k: facts[k] for k in ("area", "subject", "layers", "rasters")}
            result = await agent.run("Facts:\n" + json.dumps(view, ensure_ascii=False, indent=1))
            text = " ".join(result.text.split())
            bad = fact_check(text, facts)
            if bad:
                rep["narrative_rejected"] = ("contiene numeri non presenti nei dati: "
                                             + ", ".join(bad))
            else:
                rep["narrative"] = text
        except Exception as exc:  # noqa: BLE001 — narrative is optional
            rep["narrative_rejected"] = f"modello non disponibile ({exc})"
    rep["markdown"] = to_markdown(rep)
    rep["html"] = to_html(rep)
    return rep


def _narrative_agent():
    from .agent import _build_chat_client
    from .config import get_settings
    client = _build_chat_client(get_settings())
    return client.as_agent(name="report-writer", instructions=NARRATIVE_INSTRUCTIONS, tools=[])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcp_geo_server.report",
                                 description="Fact sheet of the data for an administrative area.")
    ap.add_argument("place", help='e.g. "provincia di Bari"')
    ap.add_argument("--subject", help='e.g. "frane"')
    ap.add_argument("--narrative", action="store_true", help="add a fact-checked LLM synthesis")
    ap.add_argument("--format", choices=("md", "html", "json"), default="md")
    args = ap.parse_args(argv)
    rep = asyncio.run(area_report(args.place, args.subject, narrative=args.narrative))
    if args.format == "json":
        print(json.dumps({k: rep[k] for k in ("facts", "summary", "narrative",
                                              "narrative_rejected")},
                         ensure_ascii=False, indent=1))
    else:
        print(rep["markdown"] if args.format == "md" else rep["html"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
