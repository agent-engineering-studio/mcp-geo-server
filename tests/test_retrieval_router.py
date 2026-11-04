"""Structural catalog retrieval and the resolver's model router."""

from __future__ import annotations

import json

import pytest

from mcp_geo_server import evals, retrieval
from mcp_geo_server.catalog import build_catalog
from mcp_geo_server.resolver import Tier, escalation_reason, filter_spec, resolve_routed
from mcp_geo_server.styling import load_config

CATALOG = evals.load_catalog(evals.EVAL_DIR / "catalog_ispra.json")
CASES = evals.load_cases(evals.EVAL_DIR / "cases.yaml")
SPEC = filter_spec(load_config())


# ---- families ------------------------------------------------------------------------
def test_families_of_the_ispra_catalog():
    fams = {f.pattern: f for f in retrieval.find_families(CATALOG)}
    assert set(fams) == {"aree_poly_*_opendata", "dgpv_poly_*_opendata", "frane_*_opendata"}
    assert len(fams["frane_*_opendata"].members) == 63          # 3 types x 21 regions
    i = fams["frane_*_opendata"].members.index("ispra:frane_line_emilia_romagna_opendata")
    assert fams["frane_*_opendata"].variants[i] == ("line", "emilia", "romagna")


def test_singletons_are_never_in_a_family():
    in_fam = {m for f in retrieval.find_families(CATALOG) for m in f.members}
    singles = {m.qualified for m in CATALOG} - in_fam
    assert {"ispra:dtm", "ispra:com01012023_g", "ispra:clc2018_it",
            "ispra:mosaicatura_ispra_2020_2021_aree_pericolosita_frana_pai"} <= singles
    assert len(singles) == 8


def test_no_family_below_min_size():
    cat = build_catalog([{"name": "a_x_b", "workspace": "w"}, {"name": "a_y_b", "workspace": "w"}])
    assert retrieval.find_families(cat) == []


def test_families_are_per_workspace():
    cat = build_catalog([{"name": f"roads_{r}", "workspace": ws}
                         for ws in ("a", "b") for r in ("x", "y")])
    assert retrieval.find_families(cat) == []


# ---- token matching -----------------------------------------------------------------------
@pytest.mark.parametrize("variant, query, hit", [
    ("toscana", "frane in Toscana", True),
    ("trento", "provincia autonoma di Trentino", True),     # shared stem, both >= 5
    ("romagna", "previsioni a Roma", False),                 # short word: exact only
    ("molise", "territorio molisano", True),
    ("line", "frane lineari", False),                        # 4 letters: exact only
])
def test_token_matches(variant, query, hit):
    assert retrieval.token_matches(variant, retrieval.query_tokens(query)) is hit


# ---- retrieval -------------------------------------------------------------------------------
def test_naming_a_region_narrows_to_its_members_plus_singletons():
    r = retrieval.Retriever().retrieve("frane in Toscana", CATALOG)
    names = {m.qualified for m in r.candidates}
    assert r.applied and len(names) == 13
    assert {"ispra:frane_poly_toscana_opendata", "ispra:aree_poly_toscana_opendata",
            "ispra:dtm"} <= names
    assert not any("liguria" in n for n in names)


def test_most_variant_hits_win_inside_a_family():
    cat = build_catalog([{"name": f"frane_{t}_{r}_opendata", "workspace": "w"}
                         for t in ("poly", "line", "piff") for r in ("molise", "lazio", "umbria")])
    r = retrieval.Retriever(min_catalog=0).retrieve("frane line molise", cat)
    assert [m.qualified for m in r.candidates] == ["w:frane_line_molise_opendata"]


@pytest.mark.parametrize("query", ["confini comunali", "show me the elevation model",
                                   "pericolosità da frana elevata"])
def test_no_variant_named_keeps_everything(query):
    r = retrieval.Retriever().retrieve(query, CATALOG)
    assert not r.applied and len(r.candidates) == len(CATALOG)


def test_small_catalog_is_not_narrowed():
    cat = CATALOG[:30]
    assert not retrieval.Retriever().retrieve("frane in Abruzzo", cat).applied


def test_candidates_keep_catalog_order():
    r = retrieval.Retriever().retrieve("frane in Puglia", CATALOG)
    order = [m.qualified for m in CATALOG]
    idx = [order.index(m.qualified) for m in r.candidates]
    assert idx == sorted(idx)


def test_golden_set_never_loses_an_expected_layer():
    """The guarantee the design is built on, measured on the real catalog."""
    r = retrieval.Retriever()
    lost = {c.id: sorted(set(c.layers) - {m.qualified for m in r.retrieve(c.query, CATALOG).candidates})
            for c in CASES}
    assert not {k: v for k, v in lost.items() if v}


def test_env(monkeypatch):
    monkeypatch.setenv("GEO_RETRIEVAL", "false")
    assert retrieval.retriever_from_env() is None
    monkeypatch.setenv("GEO_RETRIEVAL", "true")
    monkeypatch.setenv("GEO_RETRIEVAL_MIN_CATALOG", "5")
    assert retrieval.retriever_from_env().min_catalog == 5


# ---- router ------------------------------------------------------------------------------------
class _R:
    def __init__(self, t):
        self.text = t


class Agent:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    async def run(self, prompt, options=None):
        self.calls += 1
        if self.error:
            raise self.error
        return _R(json.dumps(self.reply) if isinstance(self.reply, dict) else self.reply)


def _reply(conf, layers=("ispra:dtm",)):
    return {"layers": list(layers), "cql_filter": None, "explanation": "", "confidence": conf}


def test_escalation_reason():
    assert escalation_reason({"confidence": 0.2}, 0.5).startswith("confidence 0.20")
    assert escalation_reason({"confidence": 0.9}, 0.5) is None
    assert escalation_reason({"confidence": None}, 0.5) is None
    assert escalation_reason({"confidence": 0.9, "layers": []}, 0.5) is None


async def test_confident_primary_is_kept():
    small, big = Agent(_reply(0.9)), Agent(_reply(0.9))
    sel = await resolve_routed([Tier(small, "ollama", "qwen"), Tier(big, "anthropic", "c")],
                               "dtm", CATALOG, SPEC)
    assert sel["model"] == "ollama:qwen" and not sel["escalated"] and big.calls == 0


async def test_low_confidence_escalates():
    small, big = Agent(_reply(0.2)), Agent(_reply(0.8))
    sel = await resolve_routed([Tier(small, "ollama", "qwen"), Tier(big, "anthropic", "c")],
                               "dtm", CATALOG, SPEC, min_confidence=0.5)
    assert sel["model"] == "anthropic:c" and sel["escalated"]
    assert sel["escalations"][0]["reason"].startswith("confidence 0.20")


async def test_failure_escalates_and_last_tier_errors_propagate():
    down = Agent(error=ConnectionError("refused"))
    sel = await resolve_routed([Tier(down, "ollama"), Tier(Agent(_reply(0.1)), "anthropic")],
                               "dtm", CATALOG, SPEC)
    assert sel["model"] == "anthropic" and "ConnectionError" in sel["escalations"][0]["reason"]
    with pytest.raises(ValueError):
        await resolve_routed([Tier(Agent("not json"), "ollama")], "dtm", CATALOG, SPEC)


async def test_single_tier_keeps_low_confidence():
    sel = await resolve_routed([Tier(Agent(_reply(0.1)), "ollama")], "dtm", CATALOG, SPEC)
    assert sel["confidence"] == 0.1 and not sel["escalated"]


def test_settings_for_fallback(monkeypatch):
    from mcp_geo_server import config
    from mcp_geo_server.agent import model_name, settings_for

    s = settings_for(config.get_settings(), "anthropic", "claude-opus-5-5")
    assert s.llm_provider == "anthropic" and model_name(s) == "claude-opus-5-5"
    with pytest.raises(RuntimeError):
        settings_for(config.get_settings(), "nope")


def test_tiers_from_settings(monkeypatch):
    from mcp_geo_server.resolver import tiers_from_settings

    monkeypatch.setenv("GEO_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("GEO_LLM_FALLBACK_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEO_LLM_FALLBACK_MODEL", "claude-opus-5-5")
    from mcp_geo_server import config
    config.reset_settings()
    tiers = tiers_from_settings()
    assert [t.label for t in tiers] == ["ollama:qwen2.5", "anthropic:claude-opus-5-5"]
