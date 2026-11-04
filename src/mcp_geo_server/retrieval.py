"""Catalog retrieval: show the resolver fewer layers without losing the right one.

Sending the whole catalog on every request makes the prompt and the JSON-Schema
``enum`` grow with the catalog, and long lists of near-identical names are where
models pick the wrong sibling. But ranking layers by text similarity and keeping
the top-k is the WRONG cut for GeoServer catalogs: many layers have opaque names
(``com01012023_g`` is the ISTAT municipalities) that share no word with the
request, so a lexical top-k drops them (measured on the golden set: 8/29
requests lost an expected layer at k=20).

The redundancy is structural instead: catalogs are mostly **families** of layers
that differ by one variable part — the same dataset per region, per year, per
tile (``frane_poly_<regione>_opendata``: 5 families x 21 regions = 105 of the
113 layers in the ISPRA catalog). So:

* layers that belong to no family are **always** kept (they are few and often
  have opaque names the model still recognizes);
* a family is cut down to the members whose **variant** part the request names
  ("frane in Toscana" -> the ``toscana`` member of each family);
* if the request names no variant, nothing is cut — the full catalog goes in.

Retrieval therefore only narrows when it is sure, and never removes a layer
the request could not have pointed at by name. Families are detected from the
names alone (common prefix/suffix tokens), with no domain vocabulary.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass

from .catalog import LayerMeta
from .places import norm

logger = logging.getLogger("mcp_geo_server.retrieval")

# A name pattern shared by at least this many layers is a family.
MIN_FAMILY = 3
# Below this many layers retrieval is skipped: the whole catalog is cheap.
DEFAULT_MIN_CATALOG = 40
# Variant tokens shorter than this are ignored when matching ('d' in valle_d_aosta).
MIN_VARIANT_TOKEN = 3


def name_tokens(name: str) -> list[str]:
    return [t for t in name.lower().replace("-", "_").split("_") if t]


def query_tokens(query: str) -> set[str]:
    return set(norm(query.replace("_", " ")).split())


def token_matches(variant_token: str, query: set[str]) -> bool:
    """Equal, or same 4-letter stem when BOTH words have 5+ letters.

    The length guard keeps inflections together (trento/trentino,
    puglia/pugliese) while stopping short words from hitting longer names
    ("roma" must not select "romagna").
    """
    if variant_token in query:
        return True
    if len(variant_token) < 5:
        return False
    st = variant_token[:4]
    return any(len(t) >= 5 and t[:4] == st for t in query)


# ---- families ---------------------------------------------------------------------
@dataclass(frozen=True)
class Family:
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    members: tuple[str, ...]                      # qualified names
    variants: tuple[tuple[str, ...], ...]         # variant tokens per member

    @property
    def pattern(self) -> str:
        return "_".join([*self.prefix, "*", *self.suffix])


def find_families(catalog: list[LayerMeta], min_size: int = MIN_FAMILY) -> list[Family]:
    """Group layers whose names share a prefix + suffix and differ in the middle.

    Names often vary along several axes (``frane_<type>_<region>_opendata``).
    Each name goes to the affix pair with the best ``size x specificity``
    (members x affix tokens) among those shared by at least ``min_size`` names
    in its workspace: the broad pattern (``frane_*_opendata``, 63 x 2) wins over
    a narrow one (``frane_*_abruzzo_opendata``, 3 x 3), and the variant part
    then carries every varying token (type AND region).
    """
    toks = {m.qualified: (m.workspace, name_tokens(m.name)) for m in catalog}
    groups: dict[tuple, set[str]] = defaultdict(set)
    for q, (ws, t) in toks.items():
        for p in range(1, len(t)):
            for s in range(0, len(t) - p):
                groups[(ws, tuple(t[:p]), tuple(t[len(t) - s:]) if s else ())].add(q)

    def quality(key: tuple) -> tuple:
        # size x specificity, then the more specific, then a stable order
        spec = len(key[1]) + len(key[2])
        return (len(groups[key]) * spec, spec, key)

    best: dict[str, tuple] = {}
    for key, members in groups.items():
        if len(members) < min_size:
            continue
        for q in members:
            cur = best.get(q)
            if cur is None or quality(key) > quality(cur):
                best[q] = key

    by_key: dict[tuple, list[str]] = defaultdict(list)
    for q, key in best.items():
        by_key[key].append(q)
    families = []
    for (ws, prefix, suffix), members in by_key.items():
        if len(members) < min_size:
            continue
        members.sort()
        variants = tuple(tuple(toks[q][1][len(prefix):len(toks[q][1]) - len(suffix)])
                         for q in members)
        families.append(Family(prefix, suffix, tuple(members), variants))
    return sorted(families, key=lambda f: f.members[0])


# ---- retrieval -----------------------------------------------------------------------------
@dataclass
class Retrieval:
    candidates: list[LayerMeta]
    applied: bool                  # True = the catalog was narrowed
    reason: str                    # why (for logs / spans / evals)


class Retriever:
    def __init__(self, *, min_catalog: int = DEFAULT_MIN_CATALOG):
        self.min_catalog = min_catalog
        self._key: tuple | None = None
        self._families: list[Family] = []

    def families(self, catalog: list[LayerMeta]) -> list[Family]:
        key = tuple(m.qualified for m in catalog)
        if key != self._key:
            self._families, self._key = find_families(catalog), key
        return self._families

    def retrieve(self, query: str, catalog: list[LayerMeta]) -> Retrieval:
        if self.min_catalog < 0 or len(catalog) <= self.min_catalog:
            return Retrieval(list(catalog), False, "small catalog")
        families = self.families(catalog)
        if not families:
            return Retrieval(list(catalog), False, "no layer families")
        q = query_tokens(query)
        in_family = {m for f in families for m in f.members}
        keep: set[str] = set()
        for f in families:
            # Keep the members naming the MOST variant tokens of the request:
            # "frane lineari del Molise" -> frane_line_molise (2 hits) beats
            # frane_poly_molise (1 hit) in the frane_*_opendata family.
            hits = [sum(token_matches(t, q) for t in v if len(t) >= MIN_VARIANT_TOKEN)
                    for v in f.variants]
            top = max(hits, default=0)
            if top > 0:
                keep.update(m for m, h in zip(f.members, hits) if h == top)
        if not keep:
            return Retrieval(list(catalog), False, "no variant named")
        cands = [m for m in catalog if m.qualified not in in_family or m.qualified in keep]
        return Retrieval(cands, True, f"{len(keep)} family member(s) by variant")


def retriever_from_env() -> Retriever | None:
    """``GEO_RETRIEVAL=false`` disables it; ``GEO_RETRIEVAL_MIN_CATALOG`` tunes it."""
    if (os.environ.get("GEO_RETRIEVAL") or "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return Retriever(min_catalog=int(os.environ.get("GEO_RETRIEVAL_MIN_CATALOG")
                                     or DEFAULT_MIN_CATALOG))
