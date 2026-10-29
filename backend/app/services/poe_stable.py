"""The 11 Formalities polygons whose source discovery is stable.

One canonical list — probe / isolated-run scripts re-export
`STABLE_REVIEW_MRGIDS` as `TARGET_MRGIDS`. Do not import `scripts/`.
"""
from __future__ import annotations

STABLE_REVIEW_MRGIDS: tuple[int, ...] = (
    5677, 8429, 8433, 8447, 8455, 8312, 21803, 48944, 5696, 8490, 5670,
)
STABLE_REVIEW_SET = frozenset(STABLE_REVIEW_MRGIDS)
STABLE_REVIEW_ORDER = {mid: i for i, mid in enumerate(STABLE_REVIEW_MRGIDS)}

# Fallback labels (Review queue) if the EEZ is in no run / v1.
STABLE_REVIEW_META: dict[int, dict[str, str]] = {
    5677: {"iso2": "FR", "name": "France", "sovereign": "France"},
    8429: {"iso2": "MX", "name": "Mexico", "sovereign": "Mexico"},
    8433: {"iso2": "VE", "name": "Venezuela", "sovereign": "Venezuela"},
    8447: {"iso2": "NU", "name": "Niue", "sovereign": "New Zealand"},
    8455: {"iso2": "NZ", "name": "New Zealand", "sovereign": "New Zealand"},
    8312: {"iso2": "NC", "name": "New Caledonia", "sovereign": "France"},
    21803: {"iso2": "SX", "name": "Sint-Maarten", "sovereign": "Netherlands"},
    48944: {"iso2": "YT", "name": "Mayotte", "sovereign": "France"},
    5696: {"iso2": "GB", "name": "United Kingdom", "sovereign": "United Kingdom"},
    8490: {"iso2": "EG", "name": "Egypt", "sovereign": "Egypt"},
    5670: {"iso2": "AL", "name": "Albania", "sovereign": "Albania"},
}


def is_stable_review_mrgid(mrgid) -> bool:
    try:
        return int(mrgid) in STABLE_REVIEW_SET
    except (TypeError, ValueError):
        return False


def stable_review_stub(mrgid: int) -> dict:
    meta = STABLE_REVIEW_META.get(int(mrgid), {})
    return {
        "mrgid": int(mrgid),
        "name": meta.get("name") or str(mrgid),
        "iso2": meta.get("iso2") or "",
        "sovereign": meta.get("sovereign") or "",
        "poe_count": 0,
    }
