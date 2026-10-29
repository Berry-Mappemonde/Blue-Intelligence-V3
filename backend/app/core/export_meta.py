"""
app.core.export_meta — Versioned GeoJSON exports (inspired by Open Waters: Seamap).

Each exported FeatureCollection carries a self-describing ``metadata`` block:
dataset name, UTC timestamp, count, content fingerprint
(sha256 truncated to 12 hex) and legal warning. The version
``YYYY-MM-DD.<hash12>`` identifies content stably: two exports with
identical content share the same fingerprint, two different contents
cannot share it.

The warning discipline ("not for navigation") follows the seamap
README: data are participatory / automatically extracted; no
hydrographic or customs authority verifies them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

GENERATOR = "Blue Intelligence — blueintelligence.online"

DISCLAIMER_EN = (
    "Not for navigation. Crowd-sourced / automatically extracted data, provided "
    "as-is: always verify against official sources (nautical charts, government "
    "publications) before any use at sea."
)
DISCLAIMER_FR = (
    "Ne convient pas à la navigation. Données participatives / extraites "
    "automatiquement, fournies telles quelles : vérifiez toujours les sources "
    "officielles (cartes marines, publications gouvernementales) avant toute "
    "utilisation en mer."
)


def content_fingerprint(features: list) -> str:
    """Stable content fingerprint: sha256 of canonicalized features, 12 hex."""
    canon = json.dumps(features, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def versioned_fc(fc: dict, dataset: str, *, license_note: str | None = None,
                 now: datetime | None = None,
                 period: str | None = None,
                 month: int | None = None,
                 source_ids: list | None = None,
                 doi: str | None = None,
                 extra_metadata: dict | None = None) -> dict:
    """Return a shallow copy of ``fc`` with the ``metadata`` block.

    Features are never modified; existing FeatureCollection keys
    (``attribution``…) are preserved.
    """
    now = now or datetime.now(timezone.utc)
    features = fc.get("features") or []
    fingerprint = content_fingerprint(features)
    out = dict(fc)
    out["metadata"] = {
        "dataset": dataset,
        "version": f"{now.strftime('%Y-%m-%d')}.{fingerprint}",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(features),
        "content_sha256": fingerprint,
        "generator": GENERATOR,
        "disclaimer": DISCLAIMER_EN,
        "disclaimer_fr": DISCLAIMER_FR,
    }
    if license_note:
        out["metadata"]["license"] = license_note
    if period:
        out["metadata"]["period"] = period
    if month is not None:
        out["metadata"]["month"] = month
    if source_ids:
        out["metadata"]["source_ids"] = list(source_ids)
    if doi:
        out["metadata"]["doi"] = doi
    if extra_metadata:
        for key, value in extra_metadata.items():
            out["metadata"].setdefault(key, value)
    return out


def export_response(fc: dict, dataset: str, filename: str, *,
                    license_note: str | None = None) -> JSONResponse:
    """Uniform export response: metadata + Content-Disposition + HTTP version."""
    out = versioned_fc(fc, dataset, license_note=license_note)
    return JSONResponse(out, headers={
        "Content-Disposition": f"attachment; filename={filename}",
        "X-Dataset-Version": out["metadata"]["version"],
    })
