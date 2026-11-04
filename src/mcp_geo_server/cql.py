"""Guardrails for LLM-generated CQL filters.

The chat resolver asks a model for an optional ``cql_filter`` that ends up in
WMS requests. A regex over the text cannot tell a valid filter from a broken or
pathological one, so this module parses a deliberately SMALL, attribute-only
subset of ECQL into an AST, checks it against what the data really has, and
re-serializes it in canonical form. Anything outside the subset is rejected:

* comparisons ``= <> != < > <= >=`` against a string or number literal
* ``[NOT] LIKE`` / ``[NOT] ILIKE`` with a string pattern
* ``[NOT] IN (lit, ...)``, ``[NOT] BETWEEN lit AND lit``, ``IS [NOT] NULL``
* ``AND`` / ``OR`` / ``NOT`` and parentheses

No functions, no spatial predicates (the server adds ``INTERSECTS`` itself for
admin-area scoping), no attribute-to-attribute comparisons. Pure, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

MAX_PREDICATES = 16
MAX_DEPTH = 6
MAX_LENGTH = 2000


class CQLError(ValueError):
    """The filter is not valid, not allowed, or too complex."""


# ---- AST --------------------------------------------------------------------
Literal = Union[str, int, float]


@dataclass(frozen=True)
class Compare:
    attr: str
    op: str
    value: Literal


@dataclass(frozen=True)
class Like:
    attr: str
    pattern: str
    negated: bool = False
    insensitive: bool = False


@dataclass(frozen=True)
class In:
    attr: str
    values: tuple[Literal, ...]
    negated: bool = False


@dataclass(frozen=True)
class Between:
    attr: str
    low: Literal
    high: Literal
    negated: bool = False


@dataclass(frozen=True)
class IsNull:
    attr: str
    negated: bool = False


@dataclass(frozen=True)
class Not:
    item: "Node"


@dataclass(frozen=True)
class BoolOp:
    op: str                      # "AND" | "OR"
    items: tuple["Node", ...]


Node = Union[Compare, Like, In, Between, IsNull, Not, BoolOp]
_PREDICATES = (Compare, Like, In, Between, IsNull)

# ---- tokenizer ----------------------------------------------------------------
_KEYWORDS = {"AND", "OR", "NOT", "LIKE", "ILIKE", "IN", "BETWEEN", "IS", "NULL"}
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<op><>|!=|<=|>=|=|<|>)
  | (?P<punct>[(),])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


@dataclass(frozen=True)
class _Tok:
    kind: str     # str | num | op | punct | kw | ident | end
    value: str


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise CQLError(f"unexpected character {text[pos]!r} at position {pos}")
        pos = m.end()
        kind = m.lastgroup or ""
        val = m.group()
        if kind == "ws":
            continue
        if kind == "word":
            up = val.upper()
            kind, val = ("kw", up) if up in _KEYWORDS else ("ident", val)
        toks.append(_Tok(kind, val))
    toks.append(_Tok("end", ""))
    return toks


# ---- parser -------------------------------------------------------------------
class _Parser:
    def __init__(self, text: str):
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self) -> _Tok:
        return self.toks[self.i]

    def next(self) -> _Tok:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def accept_kw(self, kw: str) -> bool:
        if self.peek() == _Tok("kw", kw):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, value: str | None = None) -> _Tok:
        tok = self.next()
        if tok.kind != kind or (value is not None and tok.value != value):
            want = value or kind
            got = tok.value or "end of filter"
            raise CQLError(f"expected {want}, got {got!r}")
        return tok

    def parse(self) -> Node:
        node = self.or_expr()
        if self.peek().kind != "end":
            raise CQLError(f"unexpected {self.peek().value!r} after the filter")
        return node

    def or_expr(self) -> Node:
        items = [self.and_expr()]
        while self.accept_kw("OR"):
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else BoolOp("OR", tuple(items))

    def and_expr(self) -> Node:
        items = [self.not_expr()]
        while self.accept_kw("AND"):
            items.append(self.not_expr())
        return items[0] if len(items) == 1 else BoolOp("AND", tuple(items))

    def not_expr(self) -> Node:
        if self.accept_kw("NOT"):
            return Not(self.not_expr())
        return self.primary()

    def primary(self) -> Node:
        if self.peek() == _Tok("punct", "("):
            self.next()
            node = self.or_expr()
            self.expect("punct", ")")
            return node
        return self.predicate()

    def literal(self) -> Literal:
        tok = self.next()
        if tok.kind == "str":
            return tok.value[1:-1].replace("''", "'")
        if tok.kind == "num":
            return float(tok.value) if any(c in tok.value for c in ".eE") else int(tok.value)
        raise CQLError(f"expected a string or number, got {tok.value or 'end of filter'!r}")

    def predicate(self) -> Node:
        tok = self.next()
        if tok.kind != "ident":
            if tok.kind == "kw":
                raise CQLError(f"unexpected keyword {tok.value}")
            raise CQLError(f"expected an attribute name, got {tok.value or 'end of filter'!r}")
        attr = tok.value
        if self.peek() == _Tok("punct", "("):
            raise CQLError(f"functions are not allowed ({attr}(...))")

        nxt = self.peek()
        if nxt.kind == "op":
            self.next()
            op = "<>" if nxt.value == "!=" else nxt.value
            if self.peek().kind == "ident":
                raise CQLError("comparing two attributes is not allowed")
            return Compare(attr, op, self.literal())
        if self.accept_kw("IS"):
            negated = self.accept_kw("NOT")
            self.expect("kw", "NULL")
            return IsNull(attr, negated)

        negated = self.accept_kw("NOT")
        if self.peek() in (_Tok("kw", "LIKE"), _Tok("kw", "ILIKE")):
            insensitive = self.next().value == "ILIKE"
            pat = self.next()
            if pat.kind != "str":
                raise CQLError("LIKE needs a quoted string pattern")
            return Like(attr, pat.value[1:-1].replace("''", "'"), negated, insensitive)
        if self.accept_kw("IN"):
            self.expect("punct", "(")
            values = [self.literal()]
            while self.peek() == _Tok("punct", ","):
                self.next()
                values.append(self.literal())
            self.expect("punct", ")")
            return In(attr, tuple(values), negated)
        if self.accept_kw("BETWEEN"):
            low = self.literal()
            self.expect("kw", "AND")
            return Between(attr, low, self.literal(), negated)
        raise CQLError(f"expected an operator after {attr!r}")


def parse(text: str) -> Node:
    """Parse a filter in the supported subset, or raise :class:`CQLError`."""
    text = (text or "").strip()
    if not text:
        raise CQLError("empty filter")
    if len(text) > MAX_LENGTH:
        raise CQLError(f"filter longer than {MAX_LENGTH} characters")
    return _Parser(text).parse()


# ---- inspection / serialization -------------------------------------------------
def _walk(node: Node, depth: int = 1):
    yield node, depth
    if isinstance(node, Not):
        yield from _walk(node.item, depth + 1)
    elif isinstance(node, BoolOp):
        for item in node.items:
            yield from _walk(item, depth + 1)


def attributes(node: Node) -> set[str]:
    """Attribute names referenced by the filter."""
    return {n.attr for n, _ in _walk(node) if isinstance(n, _PREDICATES)}


def _lit(v: Literal) -> str:
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def to_cql(node: Node) -> str:
    """Serialize back to canonical CQL (sub-expressions parenthesized)."""
    def neg(flag: bool) -> str:
        return "NOT " if flag else ""

    if isinstance(node, Compare):
        return f"{node.attr} {node.op} {_lit(node.value)}"
    if isinstance(node, Like):
        kw = "ILIKE" if node.insensitive else "LIKE"
        return f"{node.attr} {neg(node.negated)}{kw} {_lit(node.pattern)}"
    if isinstance(node, In):
        vals = ", ".join(_lit(v) for v in node.values)
        return f"{node.attr} {neg(node.negated)}IN ({vals})"
    if isinstance(node, Between):
        return (f"{node.attr} {neg(node.negated)}BETWEEN "
                f"{_lit(node.low)} AND {_lit(node.high)}")
    if isinstance(node, IsNull):
        return f"{node.attr} IS {neg(node.negated)}NULL"
    if isinstance(node, Not):
        return f"NOT ({to_cql(node.item)})"
    if isinstance(node, BoolOp):
        return f" {node.op} ".join(
            f"({to_cql(i)})" if isinstance(i, (BoolOp, Not)) else to_cql(i)
            for i in node.items)
    raise TypeError(f"not a CQL node: {node!r}")


# ---- validation -------------------------------------------------------------------
def _norm(v: Literal) -> str:
    """Compare literals by text: '1', 1 and 1.0 all mean class "1"."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().casefold()


def check(node: Node, *,
          allowed_attributes: set[str] | None = None,
          allowed_values: dict[str, set[str]] | None = None,
          max_predicates: int = MAX_PREDICATES,
          max_depth: int = MAX_DEPTH) -> None:
    """Raise :class:`CQLError` unless the filter is within limits and whitelists.

    ``allowed_attributes`` (if given) is the full set of usable columns.
    ``allowed_values`` maps a categorical attribute to its EXACT legal values:
    equality / IN on such an attribute must use one of them, because a value the
    model made up silently matches nothing.
    """
    nodes = list(_walk(node))
    preds = [n for n, _ in nodes if isinstance(n, _PREDICATES)]
    if len(preds) > max_predicates:
        raise CQLError(f"too many conditions ({len(preds)} > {max_predicates})")
    depth = max(d for _, d in nodes)
    if depth > max_depth:
        raise CQLError(f"filter nested too deeply ({depth} > {max_depth})")

    if allowed_attributes is not None:
        unknown = attributes(node) - set(allowed_attributes)
        if unknown:
            raise CQLError("unknown attribute(s): " + ", ".join(sorted(unknown)))

    values = {k: {_norm(v) for v in vs} for k, vs in (allowed_values or {}).items()}
    for p in preds:
        legal = values.get(p.attr)
        if not legal:
            continue
        given: tuple[Literal, ...] = ()
        if isinstance(p, Compare) and p.op in ("=", "<>"):
            given = (p.value,)
        elif isinstance(p, In):
            given = p.values
        bad = [v for v in given if _norm(v) not in legal]
        if bad:
            raise CQLError(f"value(s) {', '.join(_lit(v) for v in bad)} not valid "
                           f"for {p.attr}")


def sanitize(text: str, **limits) -> str:
    """Parse + :func:`check` + canonical re-serialization, in one call."""
    node = parse(text)
    check(node, **limits)
    return to_cql(node)
