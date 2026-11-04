"""CQL guardrails: parsing, canonical form, whitelists, limits."""

from __future__ import annotations

import pytest

from mcp_geo_server import cql
from mcp_geo_server.cql import CQLError


@pytest.mark.parametrize("text, canonical", [
    ("per_fr_ita = 'Elevata P3'", "per_fr_ita = 'Elevata P3'"),
    ("tipo_movim=1", "tipo_movim = 1"),
    ("tipo_movim != '1'", "tipo_movim <> '1'"),
    ("a >= 2.5", "a >= 2.5"),
    ("a in ('x','y')", "a IN ('x', 'y')"),
    ("a NOT IN (1, 2)", "a NOT IN (1, 2)"),
    ("a BETWEEN 1 AND 5", "a BETWEEN 1 AND 5"),
    ("a not between 1 and 5", "a NOT BETWEEN 1 AND 5"),
    ("name LIKE 'Bar%'", "name LIKE 'Bar%'"),
    ("name ilike '%bari%'", "name ILIKE '%bari%'"),
    ("name NOT LIKE 'x%'", "name NOT LIKE 'x%'"),
    ("a IS NULL", "a IS NULL"),
    ("a is not null", "a IS NOT NULL"),
    ("a = 'it''s'", "a = 'it''s'"),
    ("a = 1 AND b = 2 OR c = 3", "(a = 1 AND b = 2) OR c = 3"),
    ("a = 1 AND (b = 2 OR c = 3)", "a = 1 AND (b = 2 OR c = 3)"),
    ("NOT a = 1", "NOT (a = 1)"),
])
def test_parse_and_canonical_form(text, canonical):
    assert cql.to_cql(cql.parse(text)) == canonical


def test_canonical_form_round_trips():
    text = "(a = 1 AND b IN ('x', 'y')) OR NOT (c LIKE 'z%')"
    once = cql.to_cql(cql.parse(text))
    assert cql.to_cql(cql.parse(once)) == once


@pytest.mark.parametrize("text", [
    "",
    "a",
    "a =",
    "a = b",                                   # attribute-to-attribute
    "INTERSECTS(geom, POINT(1 2))",            # spatial / function
    "strToLowerCase(a) = 'x'",                 # function
    "a = 1; DROP TABLE x",
    "a = 'unterminated",
    "a = 1 AND",
    "a IN ()",
    "(a = 1",
    "a = 1)",
    "AND = 1",
    "a LIKE 5",
    "a = \"double quoted\"",
])
def test_rejects_outside_the_subset(text):
    with pytest.raises(CQLError):
        cql.parse(text)


def test_attributes():
    node = cql.parse("a = 1 AND (b IN (1,2) OR NOT c IS NULL)")
    assert cql.attributes(node) == {"a", "b", "c"}


def test_unknown_attribute_rejected():
    with pytest.raises(CQLError, match="unknown attribute"):
        cql.sanitize("bogus = 1", allowed_attributes={"tipo_movim"})


def test_categorical_values_must_be_exact():
    values = {"per_fr_ita": {"Elevata P3", "Molto elevata P4"}}
    assert cql.sanitize("per_fr_ita = 'Elevata P3'", allowed_values=values)
    # case-insensitive, but a made-up class is rejected
    assert cql.sanitize("per_fr_ita = 'elevata p3'", allowed_values=values)
    with pytest.raises(CQLError, match="not valid"):
        cql.sanitize("per_fr_ita = 'Alta'", allowed_values=values)
    with pytest.raises(CQLError, match="not valid"):
        cql.sanitize("per_fr_ita IN ('Elevata P3', 'P5')", allowed_values=values)


def test_numeric_and_string_class_values_are_equivalent():
    values = {"tipo_movim": {"1", "5"}}
    for text in ("tipo_movim = 1", "tipo_movim = '1'", "tipo_movim = 1.0"):
        cql.sanitize(text, allowed_values=values)


def test_range_on_categorical_is_not_value_checked():
    # Only equality / IN are value-checked; a range can legitimately use bounds.
    cql.sanitize("tipo_movim > 3", allowed_values={"tipo_movim": {"1"}})


def test_complexity_limits():
    many = " OR ".join(f"a = {i}" for i in range(cql.MAX_PREDICATES + 1))
    with pytest.raises(CQLError, match="too many"):
        cql.sanitize(many)
    deep = "NOT " * (cql.MAX_DEPTH + 1) + "a = 1"
    with pytest.raises(CQLError, match="nested"):
        cql.sanitize(deep)
    with pytest.raises(CQLError, match="longer"):
        cql.parse("a = '" + "x" * cql.MAX_LENGTH + "'")
