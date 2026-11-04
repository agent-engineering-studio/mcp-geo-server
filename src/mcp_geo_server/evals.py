"""Evaluation of the natural-language -> layer resolver.

A golden set of requests (``tests/evals/resolver/cases.yaml``) is run through
the SAME :func:`mcp_geo_server.resolver.resolve` the chat uses, against a fixed
catalog snapshot, and scored:

* layer **precision / recall** and **exact** set match (``accept`` layers are
  tolerated extras: they don't count against precision);
* **forbidden** layers (regex) — e.g. "DTM della Valle d'Aosta" must not pull
  in the Valle d'Aosta landslide layers;
* **CQL** match, compared by meaning (attribute -> set of values), not text;
* **errors** (unparseable replies), structured-output **fallbacks**, latency.

Modes::

    python -m mcp_geo_server.evals                    # live, configured provider
    python -m mcp_geo_server.evals --record out.json  # live + save raw replies
    python -m mcp_geo_server.evals --replay out.json  # re-score saved replies, offline

Replay makes a run reproducible and free: CI scores recorded replies, and a
prompt/guard change can be re-scored against an old recording.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import cql as cqlmod
from .catalog import LayerMeta, build_catalog
from .resolver import FilterSpec, filter_spec, resolve

EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "evals" / "resolver"


# ---- data ---------------------------------------------------------------------
class _Unchecked:
    """Sentinel: the case does not score the CQL filter (vs None = must be null)."""

    def __repr__(self) -> str:
        return "UNCHECKED"


UNCHECKED = _Unchecked()


@dataclass
class Case:
    id: str
    query: str
    layers: list[str]
    accept: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    cql: Any = UNCHECKED          # absent = not scored; None = must be null
    tags: list[str] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases = []
    for c in raw:
        exp = c.get("expect", {}) or {}
        cases.append(Case(
            id=c["id"], query=c["query"], layers=list(exp.get("layers", [])),
            accept=list(exp.get("accept", [])), forbid=list(exp.get("forbid", [])),
            cql=exp["cql"] if "cql" in exp else UNCHECKED,
            tags=list(c.get("tags", []))))
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return cases


def load_catalog(path: Path) -> list[LayerMeta]:
    return build_catalog(json.loads(path.read_text(encoding="utf-8")))


# ---- scoring ----------------------------------------------------------------------
def cql_signature(text: str | None) -> dict[str, frozenset[str]] | None:
    """attribute -> values referenced, so `a IN ('x','y')` == `a='x' OR a='y'`."""
    if not text:
        return None
    try:
        node = cqlmod.parse(text)
    except cqlmod.CQLError:
        return {"__invalid__": frozenset([text])}
    sig: dict[str, set[str]] = {}
    for n, _ in cqlmod._walk(node):
        vals: tuple = ()
        if isinstance(n, cqlmod.Compare):
            vals = (n.value,)
        elif isinstance(n, cqlmod.In):
            vals = n.values
        elif isinstance(n, cqlmod.Between):
            vals = (n.low, n.high)
        elif isinstance(n, cqlmod.Like):
            vals = (n.pattern,)
        elif isinstance(n, cqlmod.IsNull):
            vals = ("NULL",)
        else:
            continue
        sig.setdefault(n.attr, set()).update(cqlmod._norm(v) for v in vals)
    return {k: frozenset(v) for k, v in sig.items()}


@dataclass
class Score:
    id: str
    precision: float
    recall: float
    exact: bool
    forbidden: list[str]
    cql_ok: bool | None          # None = not scored for this case
    error: str | None
    structured: bool | None
    latency_s: float | None
    got_layers: list[str]
    got_cql: str | None
    cql_rejected: str | None = None   # guard's reason, when it dropped the filter

    @property
    def passed(self) -> bool:
        return (self.error is None and self.exact and not self.forbidden
                and self.cql_ok is not False)


def score_case(case: Case, selection: dict | None, *, error: str | None = None,
               latency_s: float | None = None) -> Score:
    if selection is None:
        return Score(case.id, 0.0, 0.0, False, [], None if case.cql is UNCHECKED
                     else False, error or "no selection", None, latency_s, [], None)
    got = list(selection.get("layers") or [])
    got_set, want, ok_extra = set(got), set(case.layers), set(case.accept)
    counted = got_set - ok_extra
    tp = len(got_set & want)
    precision = (len(counted & want) / len(counted)) if counted else (1.0 if not want else 0.0)
    recall = (tp / len(want)) if want else (1.0 if not counted else 0.0)
    exact = want <= got_set and not (counted - want)
    forbidden = sorted(l for l in got for pat in case.forbid if re.search(pat, l))
    cql_ok: bool | None = None
    if case.cql is not UNCHECKED:
        cql_ok = cql_signature(selection.get("cql_filter")) == cql_signature(case.cql)
    rejected = selection.get("cql_rejected") or {}
    return Score(case.id, precision, recall, exact, forbidden, cql_ok, error,
                 selection.get("structured"), latency_s, got,
                 selection.get("cql_filter"),
                 f"{rejected.get('filter')!r}: {rejected.get('reason')}" if rejected else None)


def summarize(scores: list[Score]) -> dict:
    n = len(scores) or 1
    lat = sorted(s.latency_s for s in scores if s.latency_s is not None)
    cql_scored = [s for s in scores if s.cql_ok is not None]
    struct = [s for s in scores if s.structured is not None]
    return {
        "cases": len(scores),
        "passed": sum(s.passed for s in scores),
        "precision": round(sum(s.precision for s in scores) / n, 3),
        "recall": round(sum(s.recall for s in scores) / n, 3),
        "exact_rate": round(sum(s.exact for s in scores) / n, 3),
        "forbidden_hits": sum(bool(s.forbidden) for s in scores),
        "cql_accuracy": (round(sum(bool(s.cql_ok) for s in cql_scored) / len(cql_scored), 3)
                         if cql_scored else None),
        "error_rate": round(sum(s.error is not None for s in scores) / n, 3),
        "cql_rejected": sum(s.cql_rejected is not None for s in scores),
        "structured_fallbacks": sum(s.structured is False for s in struct),
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": (round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2)
                          if lat else None),
    }


# ---- running ------------------------------------------------------------------------
class _ReplayResult:
    def __init__(self, text: str):
        self.text = text


class ReplayAgent:
    """Agent stand-in that returns recorded replies by query."""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies

    async def run(self, prompt: str, options: dict | None = None) -> _ReplayResult:
        query = prompt.rsplit("Request: ", 1)[-1].rsplit("\n\nJSON:", 1)[0]
        if query not in self.replies:
            raise KeyError(f"no recorded reply for {query!r}")
        return _ReplayResult(self.replies[query])


async def run_cases(cases: list[Case], catalog: list[LayerMeta], spec: FilterSpec,
                    agent: Any, *, provider: str, structured: bool = True
                    ) -> tuple[list[Score], dict[str, str]]:
    scores, raw = [], {}
    for case in cases:
        t0 = time.perf_counter()
        try:
            sel = await resolve(agent, case.query, catalog, spec,
                                provider=provider, structured=structured)
            raw[case.query] = sel.get("raw", "")
            scores.append(score_case(case, sel, latency_s=time.perf_counter() - t0))
        except Exception as exc:  # noqa: BLE001 — a failure is a scored outcome
            scores.append(score_case(case, None, error=f"{type(exc).__name__}: {exc}",
                                     latency_s=time.perf_counter() - t0))
    return scores, raw


def _live_agent():
    from .agent import _build_chat_client
    from .config import get_settings
    from .resolver import INSTRUCTIONS

    settings = get_settings()
    client = _build_chat_client(settings)
    return (client.as_agent(name="layer-resolver", instructions=INSTRUCTIONS, tools=[]),
            settings.llm_provider, _model_name(settings))


def _model_name(settings) -> str:
    return {"anthropic": settings.anthropic_model, "openai": settings.openai_model
            }.get(settings.llm_provider, settings.ollama_model)


def _print_report(scores: list[Score], summary: dict, out=sys.stdout) -> None:
    for s in scores:
        mark = "PASS" if s.passed else "FAIL"
        extra = []
        if s.error:
            extra.append(f"error={s.error}")
        if not s.exact:
            extra.append(f"got={s.got_layers}")
        if s.forbidden:
            extra.append(f"forbidden={s.forbidden}")
        if s.cql_ok is False:
            extra.append(f"cql={s.got_cql!r}")
        if s.cql_rejected:
            extra.append(f"cql_rejected={s.cql_rejected}")
        print(f"{mark}  {s.id:<34} P={s.precision:.2f} R={s.recall:.2f} "
              + " ".join(extra), file=out)
    print("\n" + json.dumps(summary, indent=2), file=out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcp_geo_server.evals",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=Path, default=EVAL_DIR / "cases.yaml")
    ap.add_argument("--catalog", type=Path, default=EVAL_DIR / "catalog_ispra.json")
    ap.add_argument("--styles", type=Path, default=None,
                    help="styles.yml for the filterable attributes (default: packaged)")
    ap.add_argument("--replay", type=Path, help="score recorded replies, no LLM call")
    ap.add_argument("--record", type=Path, help="save raw live replies to this file")
    ap.add_argument("--no-structured", action="store_true",
                    help="disable the schema-constrained call")
    ap.add_argument("--only", help="regex on case id or tag")
    ap.add_argument("--json", type=Path, help="write per-case scores + summary as JSON")
    ap.add_argument("--min-exact", type=float, default=None,
                    help="exit 1 if the exact-match rate is below this")
    args = ap.parse_args(argv)

    from .styling import load_config
    cases = load_cases(args.cases)
    if args.only:
        rx = re.compile(args.only)
        cases = [c for c in cases if rx.search(c.id) or any(rx.search(t) for t in c.tags)]
    catalog = load_catalog(args.catalog)
    spec = filter_spec(load_config(str(args.styles)) if args.styles else load_config())

    meta: dict[str, Any] = {}
    if args.replay:
        rec = json.loads(args.replay.read_text(encoding="utf-8"))
        agent, provider = ReplayAgent(rec["replies"]), rec.get("provider", "replay")
        meta = {k: rec.get(k) for k in ("provider", "model", "recorded_at")}
    else:
        agent, provider, model = _live_agent()
        meta = {"provider": provider, "model": model}

    scores, raw = asyncio.run(run_cases(cases, catalog, spec, agent, provider=provider,
                                        structured=not args.no_structured))
    summary = {**meta, **summarize(scores)}
    _print_report(scores, summary)

    if args.record and not args.replay:
        args.record.write_text(json.dumps(
            {**meta, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "replies": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(
            {"summary": summary, "cases": [asdict(s) | {"passed": s.passed} for s in scores]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    if args.min_exact is not None and summary["exact_rate"] < args.min_exact:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
