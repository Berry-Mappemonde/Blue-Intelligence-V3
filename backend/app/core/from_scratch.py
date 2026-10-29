"""Complete vs incremental — a Full run rebuilds the map, it does not skip what is already placed.

``from_scratch=None`` + scope/mode ``full`` → True.
A Test stays incremental. An explicit boolean always wins.
"""
from __future__ import annotations


def resolve_from_scratch(
    explicit: bool | None,
    *,
    scope: str | None = None,
    mode: str | None = None,
) -> bool:
    if explicit is not None:
        return bool(explicit)
    kind = (scope or mode or "full").strip().lower()
    return kind == "full"
