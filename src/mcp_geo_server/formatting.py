"""Normalize the quirky shapes that GeoServer REST returns.

GeoServer wraps collections twice, e.g.::

    {"workspaces": {"workspace": [ {...}, {...} ]}}

and, when empty, returns the literal empty string instead of an empty list::

    {"workspaces": ""}

Singletons are returned un-wrapped (a dict instead of a one-element list).
These helpers flatten all of that into plain Python lists/values.
"""

from __future__ import annotations

from typing import Any


def as_list(value: Any) -> list[Any]:
    """Coerce a GeoServer value into a list.

    ``None`` and ``""`` (GeoServer's "empty") become ``[]``; a single dict
    becomes a one-element list; an existing list is returned unchanged.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract(data: Any, outer: str, inner: str) -> list[Any]:
    """Pull ``data[outer][inner]`` out as a flat list.

    Handles the ``{outer: {inner: [...]}}`` wrapping, the ``{outer: ""}``
    empty case, and the singleton (dict instead of list) case.
    """
    if not isinstance(data, dict):
        return []
    container = data.get(outer)
    if container is None or container == "":
        return []
    if isinstance(container, dict):
        return as_list(container.get(inner))
    return as_list(container)


def unwrap(data: Any, key: str) -> Any:
    """Return ``data[key]`` for single-object responses (``{"workspace": {...}}``).

    Returns the value as-is, or ``data`` itself if the key is absent.
    """
    if isinstance(data, dict) and key in data:
        return data[key]
    return data
