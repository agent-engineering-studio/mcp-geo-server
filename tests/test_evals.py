"""Resolver evaluation: golden-set integrity, scoring, offline replay."""

from __future__ import annotations

import json
import re

import pytest

from mcp_geo_server import evals
from mcp_geo_server.resolver import filter_spec
from mcp_geo_server.styling import load_config

CASES = evals.load_cases(evals.EVAL_DIR / "cases.yaml")
CATALOG = evals.load_catalog(evals.EVAL_DIR / "catalog_ispra.json")
NAMES = {m.qualified for m in CATALOG}


# ---- golden-set integrity --------------------------------------------------------
def test_golden_set_size():
    assert len(CASES) >= 25


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case_references_real_layers(case):
    missing = (set(case.layers) | set(case.accept)) - NAMES
    assert not missing, f"not in catalog: {missing}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_forbid_does_not_hit_expected_layers(case):
    for pat in case.forbid:
        re.compile(pat)
        assert not [l for l in case.layers if re.search(pat, l)], pat


@pytest.mark.parametrize("case", [c for c in CASES if isinstance(c.cql, str)],
                         ids=lambda c: c.id)
def test_expected_cql_passes_the_guard(case):
    """An expected filter the guard would reject could never be scored correct."""
    from mcp_geo_server.resolver import guard_cql
    sel = guard_cql({"layers": list(case.layers), "cql_filter": case.cql},
                    filter_spec(load_config()))
    assert sel["cql_rejected"] is None, sel["cql_rejected"]


# ---- scoring ---------------------------------------------------------------------------
def _case(**kw):
    base = dict(id="t", query="q", layers=["a", "b"])
    return evals.Case(**{**base, **kw})


def test_score_exact():
    s = evals.score_case(_case(), {"layers": ["a", "b"]})
    assert (s.precision, s.recall, s.exact, s.passed) == (1.0, 1.0, True, True)


def test_score_partial_and_extra():
    s = evals.score_case(_case(), {"layers": ["a", "c"]})
    assert s.precision == 0.5 and s.recall == 0.5 and not s.exact


def test_accepted_extras_do_not_hurt():
    s = evals.score_case(_case(layers=["a"], accept=["x"]), {"layers": ["a", "x"]})
    assert s.precision == 1.0 and s.exact


def test_forbidden_fails():
    s = evals.score_case(_case(layers=["a"], forbid=["^z"]), {"layers": ["a", "zed"]})
    assert s.forbidden == ["zed"] and not s.passed


def test_empty_expectation():
    assert evals.score_case(_case(layers=[]), {"layers": []}).passed
    assert not evals.score_case(_case(layers=[]), {"layers": ["a"]}).passed


def test_cql_compared_by_meaning():
    case = _case(layers=["a"], cql="k IN ('x', 'y')")
    assert evals.score_case(case, {"layers": ["a"], "cql_filter": "k='y' OR k='x'"}).cql_ok
    assert not evals.score_case(case, {"layers": ["a"], "cql_filter": "k='x'"}).cql_ok
    assert not evals.score_case(case, {"layers": ["a"], "cql_filter": None}).cql_ok


def test_cql_null_expectation():
    case = _case(layers=["a"], cql=None)
    assert evals.score_case(case, {"layers": ["a"], "cql_filter": None}).cql_ok
    assert not evals.score_case(case, {"layers": ["a"], "cql_filter": "k=1"}).cql_ok


def test_error_case():
    s = evals.score_case(_case(), None, error="boom")
    assert s.error == "boom" and not s.passed


def test_summary():
    scores = [evals.score_case(_case(), {"layers": ["a", "b"], "structured": True},
                               latency_s=1.0),
              evals.score_case(_case(), {"layers": ["a"], "structured": False},
                               latency_s=3.0)]
    summary = evals.summarize(scores)
    assert summary["cases"] == 2 and summary["passed"] == 1
    assert summary["recall"] == 0.75 and summary["structured_fallbacks"] == 1
    assert summary["latency_p50_s"] == 2.0


# ---- offline replay, end to end -------------------------------------------------------
def _perfect_replies() -> dict:
    replies = {}
    for c in CASES:
        replies[c.query] = json.dumps({
            "layers": c.layers, "explanation": "ok", "confidence": 0.9,
            "mode": c.mode or "new", "metrics": [], "place": None,
            "cql_filter": c.cql if isinstance(c.cql, str) else None})
    return replies


def test_replay_of_perfect_replies_passes_every_case(tmp_path, capsys):
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"provider": "replay", "model": "oracle",
                               "replies": _perfect_replies()}))
    out = tmp_path / "scores.json"
    code = evals.main(["--replay", str(rec), "--json", str(out), "--min-exact", "1.0"])
    report = json.loads(out.read_text())
    failed = [c["id"] for c in report["cases"] if not c["passed"]]
    assert code == 0 and not failed, failed
    assert report["summary"]["passed"] == len(CASES)


def test_replay_detects_a_bad_model(tmp_path):
    replies = _perfect_replies()
    dtm = next(c for c in CASES if c.id == "dtm-valle-aosta")
    replies[dtm.query] = json.dumps({
        "layers": ["ispra:dtm", "ispra:frane_poly_valle_d_aosta_opendata"],
        "cql_filter": None, "explanation": "", "confidence": 0.4})
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"replies": replies}))
    out = tmp_path / "scores.json"
    assert evals.main(["--replay", str(rec), "--json", str(out), "--min-exact", "1.0"]) == 1
    case = next(c for c in json.loads(out.read_text())["cases"] if c["id"] == dtm.id)
    assert case["forbidden"] == ["ispra:frane_poly_valle_d_aosta_opendata"]
