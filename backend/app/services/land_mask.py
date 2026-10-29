"""Land / ocean mask without the 933 MB resident grid.

``global_land_mask`` 1.0 stores a boolean array of shape (21600, 43200)
(~1 km). Importing ``globe`` decompresses the whole array into RAM
(~933 MB) and every ``is_land`` call builds temporary NumPy arrays.
Here the same file is inflated row by row into a run-length encoding
(~2 MB). Point answers match ``globe.is_land`` / ``globe.is_ocean``
for coordinates the package accepts (latitude clamped to the grid,
truncation toward zero — the same rule as ``astype(int)``).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
from numpy.lib import format as _npformat

_NPZ_NAME = "globe_combined_mask_compressed.npz"
_LOCK = threading.Lock()
_STATE: "_Mask | None" = None
_BUILDS = 0


class _Mask:
    __slots__ = (
        "lengths", "row_off", "start_ocean",
        "lat0", "dlat", "lat_min", "lat_max", "nlat",
        "lon0", "dlon", "lon_min", "lon_max", "nlon",
    )

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def mask_path() -> Path | None:
    """Locate the packaged npz without importing ``global_land_mask``.

    The package ``__init__`` imports ``globe``, which is the allocation
    this module exists to avoid.
    """
    for entry in sys.path:
        if not entry:
            continue
        cand = Path(entry) / "global_land_mask" / _NPZ_NAME
        if cand.is_file():
            return cand
    return None


def reset() -> None:
    """Drop the in-process encoding (tests)."""
    global _STATE
    with _LOCK:
        _STATE = None


def builds() -> int:
    return _BUILDS


def resident_bytes() -> int:
    state = _STATE
    if state is None:
        return 0
    return (
        int(state.lengths.nbytes)
        + int(state.row_off.nbytes)
        + int(state.start_ocean.nbytes)
    )


def _axis_params(arr) -> tuple[float, float, float, float, int]:
    origin = float(arr[0])
    step = float(arr[1] - arr[0])
    return origin, step, float(arr.min()), float(arr.max()), int(arr.shape[0])


def _read_axes(path: Path) -> dict:
    # Touch only the small lat/lon members. ``mask`` stays compressed.
    with np.load(path, allow_pickle=False) as data:
        lat0, dlat, lat_min, lat_max, nlat = _axis_params(data["lat"])
        lon0, dlon, lon_min, lon_max, nlon = _axis_params(data["lon"])
    if nlat < 2 or nlon < 2 or dlat == 0.0 or dlon == 0.0:
        raise RuntimeError(f"grille de masque illisible : {path}")
    return {
        "lat0": lat0, "dlat": dlat, "lat_min": lat_min, "lat_max": lat_max, "nlat": nlat,
        "lon0": lon0, "dlon": dlon, "lon_min": lon_min, "lon_max": lon_max, "nlon": nlon,
    }


def _runs_of(row: np.ndarray) -> tuple[bool, list[int]]:
    change = np.flatnonzero(row[1:] != row[:-1]) + 1
    bounds = np.empty(change.size + 2, dtype=np.int32)
    bounds[0] = 0
    if change.size:
        bounds[1:-1] = change
    bounds[-1] = row.size
    lengths = (bounds[1:] - bounds[:-1]).tolist()
    return bool(row[0]), lengths


def _encode_mask(path: Path, axes: dict) -> _Mask:
    nlat = axes["nlat"]
    nlon = axes["nlon"]
    lengths: list[int] = []
    row_off = [0]
    start_ocean: list[bool] = []
    rows_done = 0
    chunk_rows = 64
    pauser_at = time.perf_counter()

    import zipfile
    with zipfile.ZipFile(path) as archive:
        with archive.open("mask.npy") as raw:
            version = _npformat.read_magic(raw)
            if version != (1, 0):
                raise RuntimeError(f"npy masque inattendu : {version}")
            shape, fortran, dtype = _npformat.read_array_header_1_0(raw)
            if fortran or shape != (nlat, nlon) or dtype != np.dtype(bool):
                raise RuntimeError(
                    f"masque inattendu shape={shape} fortran={fortran} dtype={dtype}"
                )
            while rows_done < nlat:
                blob = raw.read(chunk_rows * nlon)
                if not blob:
                    break
                got = len(blob) // nlon
                if got == 0:
                    break
                grid = np.frombuffer(blob, dtype=np.uint8, count=got * nlon).reshape(got, nlon)
                for row in grid:
                    ocean0, runs = _runs_of(row)
                    if sum(runs) != nlon:
                        raise RuntimeError("encodage du masque incohérent")
                    start_ocean.append(ocean0)
                    lengths.extend(runs)
                    row_off.append(len(lengths))
                rows_done += got
                now = time.perf_counter()
                if now - pauser_at >= 0.2:
                    time.sleep(0)
                    pauser_at = time.perf_counter()
    if rows_done != nlat or len(row_off) != nlat + 1:
        raise RuntimeError(f"masque incomplet : {rows_done} lignes sur {nlat}")
    return _Mask(
        lengths=np.asarray(lengths, dtype=np.uint16),
        row_off=np.asarray(row_off, dtype=np.int32),
        start_ocean=np.asarray(start_ocean, dtype=bool),
        **axes,
    )


def _ensure() -> _Mask:
    global _STATE, _BUILDS
    state = _STATE
    if state is not None:
        return state
    with _LOCK:
        if _STATE is not None:
            return _STATE
        path = mask_path()
        if path is None:
            raise FileNotFoundError(_NPZ_NAME)
        axes = _read_axes(path)
        _STATE = _encode_mask(path, axes)
        _BUILDS += 1
        return _STATE


def _index(value: float, origin: float, step: float, vmin: float, vmax: float, n: int) -> int:
    if value > vmax:
        value = vmax
    elif value < vmin:
        value = vmin
    idx = int((value - origin) / step)
    if idx < 0:
        return 0
    if idx >= n:
        return n - 1
    return idx


def _lat_index(state: _Mask, lat: float) -> int:
    return _index(lat, state.lat0, state.dlat, state.lat_min, state.lat_max, state.nlat)


def _lon_index(state: _Mask, lon: float) -> int:
    return _index(lon, state.lon0, state.dlon, state.lon_min, state.lon_max, state.nlon)


def _ocean_scalar(state: _Mask, lat_i: int, lon_i: int) -> bool:
    a = int(state.row_off[lat_i])
    b = int(state.row_off[lat_i + 1])
    pos = 0
    ocean = bool(state.start_ocean[lat_i])
    for length in state.lengths[a:b]:
        pos += int(length)
        if lon_i < pos:
            return ocean
        ocean = not ocean
    return ocean


def _ocean_many(state: _Mask, lat_i: int, lon_idx: np.ndarray) -> np.ndarray:
    a = int(state.row_off[lat_i])
    b = int(state.row_off[lat_i + 1])
    prefix = np.cumsum(state.lengths[a:b], dtype=np.int32)
    pos = np.searchsorted(prefix, lon_idx, side="right")
    if state.start_ocean[lat_i]:
        return (pos & 1) == 0
    return (pos & 1) == 1


def is_ocean(lat: float, lon: float) -> bool:
    state = _ensure()
    return _ocean_scalar(state, _lat_index(state, float(lat)), _lon_index(state, float(lon)))


def is_land(lat: float, lon: float) -> bool:
    return not is_ocean(lat, lon)


def land_flags(lats, lons) -> np.ndarray:
    """Boolean grid, True on land, same index rule as :func:`is_land`."""
    state = _ensure()
    lat_i = [_lat_index(state, float(v)) for v in lats]
    lon_i = np.fromiter(
        (_lon_index(state, float(v)) for v in lons),
        dtype=np.int32,
        count=len(lons),
    )
    out = np.empty((len(lat_i), lon_i.size), dtype=bool)
    # Identical latitudes share one run walk.
    cache: dict[int, np.ndarray] = {}
    paused = time.perf_counter()
    for row, li in enumerate(lat_i):
        ocean = cache.get(li)
        if ocean is None:
            ocean = _ocean_many(state, li, lon_i)
            cache[li] = ocean
        out[row] = ~ocean
        now = time.perf_counter()
        if now - paused >= 0.2:
            time.sleep(0)
            paused = time.perf_counter()
    return out
