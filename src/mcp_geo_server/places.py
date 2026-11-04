"""Administrative-area scoping: find the place named in a request, get its shape.

When a chat request names an administrative unit ("frane in provincia di
Bari"), the map zooms to it and clips the layers to its boundary. WHICH layers
hold the boundaries is domain config, not code: the ``admin`` section of the
domain YAML (the same file as the styles, see ``styles_default.yml``)::

    admin:
      workspace: ""            # optional; default = GEOSERVER_DEFAULT_WORKSPACE
      levels:                  # most specific first
        - {level: comune,    layer: com01012023_g,    attr: comune}
        - {level: provincia, layer: provcm01012023_g, attr: den_uts}
      level_hints:             # words in the request that force a level
        provincia: provincia
        comune: comune

No ``admin`` section = no area scoping, which is the right default for a
GeoServer without boundary layers. ``GEO_ADMIN_WORKSPACE`` overrides the
workspace. The matching helpers are pure; :class:`AdminAreas` adds the
GeoServer reads and the caches.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mcp_geo_server.places")

# Below this many characters a name matches too much ("Ne", "Re").
MIN_NAME_LEN = 3
# The clip WKT goes into WMS tile GET URLs (Tomcat caps the request line at
# ~8 KB together with the other params), so it is simplified to fit this.
MAX_CLIP_WKT = 3500


@dataclass(frozen=True)
class AdminLevel:
    level: str      # e.g. "comune"
    layer: str      # boundary layer (bare name)
    attr: str       # attribute holding the unit's name


@dataclass(frozen=True)
class AdminConfig:
    levels: tuple[AdminLevel, ...] = ()
    hints: dict[str, str] = field(default_factory=dict)   # word -> level
    workspace: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.levels)


def admin_config(domain_config: dict) -> AdminConfig:
    """Read the ``admin`` section of a domain config dict (styles.yml shape).

    A config with NO ``admin`` key falls back to the packaged default's section
    (with a warning): before this section existed the ISTAT levels were built
    in, and a custom styles.yml must not silently lose area scoping. An explicit
    ``admin: {}`` turns scoping off.
    """
    if "admin" not in domain_config:
        from .styling import default_config_path
        import yaml
        with open(default_config_path(), encoding="utf-8") as fh:
            packaged = yaml.safe_load(fh) or {}
        logger.warning("Domain config has no 'admin' section — using the packaged "
                       "default (ISTAT boundaries). Add 'admin:' to your styles.yml "
                       "(or 'admin: {}' to disable area scoping).")
        domain_config = {"admin": packaged.get("admin") or {}}
    sec = domain_config.get("admin") or {}
    levels = []
    for i, lv in enumerate(sec.get("levels") or []):
        try:
            levels.append(AdminLevel(str(lv["level"]), str(lv["layer"]), str(lv["attr"])))
        except KeyError as exc:
            raise ValueError(f"admin.levels[{i}] is missing {exc}") from exc
    known = {lv.level for lv in levels}
    hints = {}
    for word, level in (sec.get("level_hints") or {}).items():
        if str(level) not in known:
            raise ValueError(f"admin.level_hints: '{word}' points to unknown "
                             f"level '{level}' (known: {sorted(known)})")
        hints[norm(str(word))] = str(level)
    ws = (os.environ.get("GEO_ADMIN_WORKSPACE") or sec.get("workspace") or "").strip()
    return AdminConfig(tuple(levels), hints, ws)


# ---- text normalization ---------------------------------------------------------
def demojibake(s: str) -> str:
    """Repair classic double-encoded latin-1/UTF-8 text ('AgliÃ¨' -> 'Agliè').

    Some boundary layers were loaded with the wrong encoding, so the DB holds
    the mojibake. Used only for MATCHING against user text and for display — the
    raw stored value is still used in the CQL filter that fetches the geometry.
    """
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def norm(s: str) -> str:
    """Accent/case/punctuation-insensitive key for matching place names."""
    s = unicodedata.normalize("NFKD", demojibake(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ---- matching (pure) ----------------------------------------------------------------
IndexEntry = tuple[str, AdminLevel, str]     # (normalized name, level, raw name)


def build_index(names_by_level: dict[AdminLevel, list[str]]) -> list[IndexEntry]:
    idx = []
    for level, names in names_by_level.items():
        for raw in names:
            n = norm(raw)
            if len(n) >= MIN_NAME_LEN:
                idx.append((n, level, raw))
    return idx


def level_hint(q_norm: str, hints: dict[str, str]) -> str | None:
    """The admin level explicitly named in the (normalized) query, if any."""
    tokens = set(q_norm.split())
    for word, level in hints.items():
        if word in tokens:
            return level
    return None


def match(query: str, index: list[IndexEntry], cfg: AdminConfig
          ) -> tuple[AdminLevel, str] | None:
    """The admin unit named in ``query``, if any.

    Matches whole normalized phrases (space-padded) so 'regione' won't spuriously
    match a short comune name. If the query names a level ('provincia di Bari'),
    that level wins; otherwise the longest phrase, then the most specific level.
    """
    q_norm = norm(query)
    q = f" {q_norm} "
    hint = level_hint(q_norm, cfg.hints)
    order = {lv.level: i for i, lv in enumerate(cfg.levels)}
    best, best_key = None, (99, 0, 99)
    for n, level, raw in index:
        if f" {n} " not in q:
            continue
        key = (0 if hint and level.level == hint else 1, -len(n), order.get(level.level, 98))
        if key < best_key:
            best_key, best = key, (level, raw)
    return best


def clip_wkt(geom: Any, max_len: int = MAX_CLIP_WKT) -> str:
    """Simplify a shapely geometry until its WKT fits ``max_len`` characters.

    Coordinates are then snapped to a 1e-6° grid: raw simplified vertices carry
    floating-point noise that makes JTS throw "non-noded intersection" when
    GeoServer clips a vector layer. Snapping + make-valid removes those slivers.
    """
    import shapely

    def _wkt(g):
        g = shapely.set_precision(g, 1e-6)
        if not g.is_valid:
            g = g.buffer(0)
        return g.wkt

    tol = 0.001
    wkt = _wkt(geom.simplify(tol, preserve_topology=True))
    while len(wkt) > max_len and tol < 1:
        tol *= 2
        wkt = _wkt(geom.simplify(tol, preserve_topology=True))
    return wkt


# ---- GeoServer-backed resolver ---------------------------------------------------------
class AdminAreas:
    """Detect admin units in requests and fetch their scope, with caches."""

    def __init__(self, cfg: AdminConfig, client_getter=None):
        self.cfg = cfg
        if client_getter is None:
            from .client import get_client as client_getter
        self._client = client_getter
        self._index: list[IndexEntry] | None = None
        self._scopes: dict[tuple[str, str], dict | None] = {}

    def _workspace(self) -> str:
        return self.cfg.workspace or (self._client().settings.default_workspace or "")

    async def _wfs_json(self, params: dict) -> dict:
        client = self._client()
        resp = await client.ows({"service": "WFS", "version": "2.0.0",
                                 "request": "GetFeature",
                                 "outputFormat": "application/json", **params},
                                base=client.settings.wfs_base)
        return json.loads(resp.text)

    async def _names(self, level: AdminLevel) -> list[str]:
        from .client import GeoServerError
        try:
            data = await self._wfs_json({
                "typeNames": f"{self._workspace()}:{level.layer}",
                "propertyName": level.attr, "count": "20000"})
        except (GeoServerError, ValueError) as exc:
            logger.warning("admin names for %s unavailable: %s", level.layer, exc)
            return []
        return [f.get("properties", {}).get(level.attr)
                for f in data.get("features", [])
                if f.get("properties", {}).get(level.attr)]

    async def index(self) -> list[IndexEntry]:
        """All unit names, read once per process."""
        if self._index is None:
            self._index = build_index({lv: await self._names(lv) for lv in self.cfg.levels})
        return self._index

    async def detect(self, query: str) -> tuple[AdminLevel, str] | None:
        if not self.cfg.enabled:
            return None
        return match(query, await self.index(), self.cfg)

    async def scope(self, level: AdminLevel, raw_name: str) -> dict | None:
        """Zoom bbox + clip (EPSG:4326 EWKT) for one unit, or None. Cached."""
        key = (level.layer, raw_name)
        if key in self._scopes:
            return self._scopes[key]
        scope = None
        try:
            cql = f"{level.attr}='{raw_name.replace(chr(39), chr(39) * 2)}'"
            data = await self._wfs_json({
                "typeNames": f"{self._workspace()}:{level.layer}",
                "cql_filter": cql, "count": "1", "srsName": "EPSG:4326"})
            feats = data.get("features", [])
            geom_json = feats[0].get("geometry") if feats else None
            if geom_json:
                from shapely.geometry import shape
                geom = shape(geom_json)
                minx, miny, maxx, maxy = geom.bounds
                scope = {
                    "level": level.level,
                    "name": demojibake(raw_name),
                    "bbox": {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
                    "clip": f"SRID=4326;{clip_wkt(geom)}",
                }
        except Exception as exc:  # noqa: BLE001 — scoping is best-effort
            logger.warning("admin scope for %s '%s' failed: %s", level.layer, raw_name, exc)
            scope = None
        self._scopes[key] = scope
        return scope
