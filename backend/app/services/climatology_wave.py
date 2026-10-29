"""Monthly swell P50 / P90 (WAVERYS). Do not label a mean as P90."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.services.climatology_common import (
    KIND,
    LICENSE_CMEMS,
    SOURCE_IDS,
    WAVE_PERIOD,
    climatology_dir,
    grid_step,
    is_land,
    iter_cells,
    loop_pauser,
    parse_month,
    product_meta,
    rule,
    tile_bbox,
    tile_step_deg,
    wrap_lon,
)

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


def wave_dir() -> Path:
    return climatology_dir() / "wave"


def wave_npz_path(month: int) -> Path:
    return wave_dir() / f"wave-{int(month):02d}.npz"


def has_snapshot(month: int | None = None) -> bool:
    if month is None:
        return wave_npz_path(1).is_file()
    return wave_npz_path(month).is_file()


@lru_cache(maxsize=12)
def _load_month(month: int, mtime: float):
    if np is None:
        return None
    path = wave_npz_path(month)
    if not path.is_file():
        return None
    with np.load(path, allow_pickle=False) as data:
        return {k: np.copy(data[k]) for k in data.files}


def _bundle(month: int):
    if np is None:
        return None
    path = wave_npz_path(month)
    if not path.is_file():
        return None
    return _load_month(month, path.stat().st_mtime)


def _nearest_index(arr, value) -> int:
    return int(abs(arr - value).argmin())


def wave_at(lat: float, lon: float, month: int) -> dict | None:
    month = parse_month(month)
    if is_land(lat, lon):
        return None
    bundle = _bundle(month)
    if bundle is None:
        return None
    lats, lons = bundle["lats"], bundle["lons"]
    lon = wrap_lon(lon)
    i, j = _nearest_index(lats, lat), _nearest_index(lons, lon)
    if abs(float(lats[i]) - lat) > 0.75 or abs(wrap_lon(float(lons[j]) - lon)) > 0.75:
        return None
    sea = bundle.get("sea_mask")
    if sea is not None and not bool(sea[i, j]):
        return None
    p50 = bundle.get("hs_p50")
    p90 = bundle.get("hs_p90")
    mean = bundle.get("hs_mean")
    if p50 is None and mean is None:
        return None
    def _val(arr):
        if arr is None:
            return None
        v = float(arr[i, j])
        if np is not None and np.isnan(v):
            return None
        return v

    hs_p50 = _val(p50)
    hs_p90 = _val(p90)
    hs_mean = _val(mean)
    # A V0 snapshot (P1M mean) must not call itself P90.
    stat = str(bundle["stat"][0]) if "stat" in bundle else (
        "p50_p90" if hs_p90 is not None else "mean"
    )
    if stat == "mean":
        if hs_mean is None:
            return None
        return {
            "hs_p50_m": None,
            "hs_p90_m": None,
            "hs_mean_m": round(hs_mean, 2),
            "period_s": _round(_val(bundle.get("period"))),
            "dir_deg": _round(_val(bundle.get("dir"))),
            "stat": "mean",
            "period": WAVE_PERIOD,
        }
    if hs_p50 is None:
        return None
    if hs_p90 is not None and hs_p90 < hs_p50:
        hs_p90 = hs_p50
    return {
        "hs_p50_m": round(hs_p50, 2),
        "hs_p90_m": None if hs_p90 is None else round(hs_p90, 2),
        "hs_mean_m": None,
        "period_s": _round(_val(bundle.get("period"))),
        "dir_deg": _round(_val(bundle.get("dir"))),
        "stat": "p50_p90",
        "period": WAVE_PERIOD,
    }


def _val_cell(arr, i: int, j: int):
    if arr is None:
        return None
    v = float(arr[i, j])
    if np is not None and np.isnan(v):
        return None
    return v


def _round(v):
    return None if v is None else round(float(v), 1)


def is_wave_hazard(lat: float, lon: float, month: int) -> bool | None:
    """True if Hs P90 > threshold. ``None`` if P90 is unavailable (not a disguised mean)."""
    w = wave_at(lat, lon, month)
    if not w or w.get("stat") != "p50_p90" or w.get("hs_p90_m") is None:
        return None
    nogo = float(rule("climatology.wave_nogo_m", 2.5))
    return w["hs_p90_m"] > nogo


def _wave_field(bundle: dict, want: str) -> tuple[str | None, object | None]:
    snapshot_stat = str(bundle["stat"][0]) if "stat" in bundle else None
    field_name = {"p50": "hs_p50", "p90": "hs_p90", "mean": "hs_mean"}[want]
    # Never serve a mean under the name P90.
    if want == "p90" and (field_name not in bundle or snapshot_stat == "mean"):
        field_name = None
    return snapshot_stat, (bundle.get(field_name) if field_name else None)


def _wave_features(month: int, bundle: dict, bbox, step: int, want: str, *, light: bool) -> tuple[list[dict], str | None]:
    snapshot_stat, arr = _wave_field(bundle, want)
    features: list[dict] = []
    if arr is None:
        return features, snapshot_stat
    shown_stat = want if want != "p90" or snapshot_stat != "mean" else "mean"
    pauser = loop_pauser()
    for i, j, lat, lon in iter_cells(bundle, bbox, step):
        pauser.tick()
        v = float(arr[i, j])
        if np.isnan(v) or v <= 0:
            continue
        dir_deg = _round(_val_cell(bundle.get("dir"), i, j))
        if light:
            props = {"hs_m": round(v, 2), "stat": shown_stat, "dir": dir_deg}
        else:
            props = {
                "kind": KIND,
                "month": month,
                "stat": shown_stat,
                "hs_m": round(v, 2),
                "period_s": _round(_val_cell(bundle.get("period"), i, j)),
                "dir_deg": dir_deg,
            }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
            "properties": props,
        })
    return features, snapshot_stat


def wave_geojson(month: int, stat: str = "p90", spacing_deg: float = 1.0) -> dict:
    month = parse_month(month)
    want = (stat or "p90").lower()
    if want not in ("p50", "p90", "mean"):
        want = "p90"
    spacing = max(0.5, min(4.0, float(spacing_deg)))
    features: list[dict] = []
    snapshot_stat = None
    if np is not None and has_snapshot(month):
        bundle = _bundle(month)
        if bundle is not None:
            features, snapshot_stat = _wave_features(
                month, bundle, None, grid_step(bundle, spacing), want, light=False,
            )
    return {
        "type": "FeatureCollection",
        "features": features,
        "attribution": f"{LICENSE_CMEMS} · {product_meta('wave')['provenance']}",
        "_climatology": {
            "kind": KIND,
            "month": month,
            "period": WAVE_PERIOD,
            "source_ids": [SOURCE_IDS["wave"]],
            "doi": product_meta("wave")["doi"],
            "grid_spacing_deg": spacing,
            "snapshot_present": has_snapshot(month),
            "stat": want,
            "snapshot_stat": snapshot_stat,
        },
    }


def wave_tile(month: int, z: int, x: int, y: int, stat: str = "p90") -> dict:
    """Light wave FeatureCollection for one XYZ tile. Empty if the snapshot is missing."""
    month = parse_month(month)
    want = (stat or "p90").lower()
    if want not in ("p50", "p90", "mean"):
        want = "p90"
    features: list[dict] = []
    if np is not None and has_snapshot(month):
        bundle = _bundle(month)
        if bundle is not None:
            features, _stat = _wave_features(
                month, bundle, tile_bbox(z, x, y), grid_step(bundle, tile_step_deg(z)),
                want, light=True,
            )
    return {"type": "FeatureCollection", "features": features}
