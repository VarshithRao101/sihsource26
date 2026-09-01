"""
shared/io.py - reading and writing a run folder.

Nobody writes their own GeoTIFF writer. A duplicated writer is a bug that only
surfaces on integration day, when one module's rasters are float64 and
north-down and everything downstream silently misreads them.

Owner: captain / person 4. Everyone imports. Nobody else edits.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from shared.contract import (
    DELIVERY_CRS,
    GRID_DRY_VALUE,
    HYDROGRAPH_COLUMNS,
    RASTER_COMPRESS,
    RASTER_DTYPE,
    RUN_ID_PATTERN,
    SCHEMA_VERSION,
    WET_THRESHOLD_M,
)
from shared.geo import Grid


# --------------------------------------------------------------------------
# run_id
# --------------------------------------------------------------------------


def make_run_id(site: str, scenario: str, engine: str, seq: int = 1) -> str:
    """Build a contract-valid run_id: {site}_{scenario}_{engine}_{nnn}."""
    slug = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    run_id = f"{slug(site)}_{slug(scenario)}_{slug(engine)}_{seq:03d}"
    if not re.match(RUN_ID_PATTERN, run_id):
        raise ValueError(f"generated run_id {run_id!r} does not match the contract")
    return run_id


def parse_run_id(run_id: str) -> dict[str, str]:
    if not re.match(RUN_ID_PATTERN, run_id):
        raise ValueError(f"{run_id!r} is not a valid run_id")
    site, scenario, engine, seq = run_id.split("_")
    return {"site": site, "scenario": scenario, "engine": engine, "seq": seq}


def next_sequence(outputs_dir: Path, site: str, scenario: str, engine: str) -> int:
    """Lowest unused sequence number for this site/scenario/engine triple."""
    prefix = f"{site}_{scenario}_{engine}_"
    used = {
        int(p.name[len(prefix):])
        for p in Path(outputs_dir).glob(prefix + "[0-9][0-9][0-9]")
        if p.is_dir()
    }
    seq = 1
    while seq in used:
        seq += 1
    return seq


# --------------------------------------------------------------------------
# meta.json
# --------------------------------------------------------------------------


def utc_now() -> str:
    """Timestamp in the exact form meta.json wants."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_dotted(obj: dict, path: str, default: Any = None) -> Any:
    """Fetch meta['a']['b'] as get_dotted(meta, 'a.b'). Returns default if absent."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def read_meta(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "meta.json"
    if not path.exists():
        raise FileNotFoundError(f"no meta.json in {run_dir}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_meta(run_dir: str | Path, meta: dict) -> Path:
    """Write meta.json, filling in schema_version and created_utc if missing."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta.setdefault("schema_version", SCHEMA_VERSION)
    meta.setdefault("created_utc", utc_now())
    path = run_dir / "meta.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, allow_nan=False)
        fh.write("\n")
    return path


def build_meta(
    run_id: str,
    engine: str,
    grid: Grid,
    site: dict,
    scenario: dict,
    time_block: dict,
    results: dict,
    module: str,
    dem: dict | None = None,
    is_fake: bool = False,
    notes: str = "",
    code_version: str = "",
    **extra: Any,
) -> dict:
    """Assemble a contract-shaped meta.json dict. Use this, do not hand-roll it."""
    meta = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_utc": utc_now(),
        "engine": engine,
        "is_fake": bool(is_fake),
        "site": site,
        "scenario": scenario,
        "domain": grid.to_meta_domain(),
        "dem": dem
        or {
            "source": "SYNTHETIC",
            "native_resolution_m": grid.cellsize_m(),
            "bathymetry": "none",
            "conditioning": "none",
        },
        "time": time_block,
        "results": results,
        "provenance": {
            "module": module,
            "code_version": code_version,
            "notes": notes,
        },
    }
    meta.update(extra)
    return meta


# --------------------------------------------------------------------------
# GeoTIFF grids
# --------------------------------------------------------------------------


def write_grid(
    run_dir: str | Path,
    name: str,
    array: np.ndarray,
    grid: Grid,
    description: str = "",
) -> Path:
    """Write one contract-compliant GeoTIFF into a run folder.

    float32, single band, LZW, EPSG:4326, NaN nodata, north-up. Every one of
    those is checked by shared.validate, so do not pass this a float64 array
    and hope.

    Args:
        run_dir: the run folder.
        name: grid name without extension, e.g. 'max_depth'.
        array: 2-D, shape must equal grid.shape.
        grid: the Grid every raster in this run shares.
        description: written into the band description for QGIS.
    """
    import rasterio

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(array, dtype=np.float32)
    if arr.shape != grid.shape:
        raise ValueError(
            f"{name}: array shape {arr.shape} does not match grid {grid.shape}"
        )

    path = run_dir / f"{name}.tif"
    profile = {
        "driver": "GTiff",
        "height": grid.ny,
        "width": grid.nx,
        "count": 1,
        "dtype": RASTER_DTYPE,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": np.nan,
        "compress": RASTER_COMPRESS,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)
        dst.set_band_description(1, description or name)
        dst.update_tags(1, unit=_unit_for(name))
    return path


def _unit_for(name: str) -> str:
    from shared.contract import GRID_UNITS

    return GRID_UNITS.get(name, "")


def read_grid(run_dir: str | Path, name: str) -> tuple[np.ndarray, Grid]:
    """Read a grid back as (array, Grid). Array is float32 with NaN nodata."""
    import rasterio

    path = Path(run_dir) / f"{name}.tif"
    if not path.exists():
        raise FileNotFoundError(path)
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        b = src.bounds
        grid = Grid(
            bbox=(b.left, b.bottom, b.right, b.top),
            nx=src.width,
            ny=src.height,
            crs=str(src.crs),
        )
    return arr, grid


def raster_profile(path: str | Path) -> dict:
    """Enough of a GeoTIFF's header to check the contract without reading pixels."""
    import rasterio

    with rasterio.open(path) as src:
        b = src.bounds
        return {
            "dtype": src.dtypes[0],
            "count": src.count,
            "crs": str(src.crs),
            "width": src.width,
            "height": src.height,
            "bounds": (b.left, b.bottom, b.right, b.top),
            "nodata": src.nodata,
            "transform": tuple(src.transform)[:6],
        }


def dry_fill(name: str, shape: tuple[int, int]) -> np.ndarray:
    """An all-dry grid of the right dtype and fill value for `name`."""
    return np.full(shape, GRID_DRY_VALUE.get(name, 0.0), dtype=np.float32)


# --------------------------------------------------------------------------
# hydrograph.csv
# --------------------------------------------------------------------------


def write_hydrograph(
    run_dir: str | Path, time_hr, discharge_cumecs
) -> Path:
    """Two columns, the contract header, no index. Validated on the way out."""
    t = np.asarray(time_hr, dtype=np.float64)
    q = np.asarray(discharge_cumecs, dtype=np.float64)

    if t.shape != q.shape:
        raise ValueError(f"hydrograph length mismatch: {t.shape} vs {q.shape}")
    if t.size < 2:
        raise ValueError("hydrograph needs at least two samples")
    if abs(t[0]) > 1e-9:
        raise ValueError(f"hydrograph must start at time_hr = 0.0, got {t[0]}")
    if np.any(np.diff(t) <= 0):
        raise ValueError("hydrograph time_hr must be strictly increasing")
    if np.any(q < 0):
        raise ValueError("hydrograph discharge_cumecs must be >= 0")

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "hydrograph.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HYDROGRAPH_COLUMNS)
        for ti, qi in zip(t, q):
            writer.writerow([f"{ti:.4f}", f"{qi:.2f}"])
    return path


def read_hydrograph(run_dir: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Read hydrograph.csv as (time_hr, discharge_cumecs)."""
    path = Path(run_dir) / "hydrograph.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    t, q = [], []
    with open(path, "r", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if tuple(h.strip() for h in header) != HYDROGRAPH_COLUMNS:
            raise ValueError(
                f"{path}: header is {header}, contract requires {list(HYDROGRAPH_COLUMNS)}"
            )
        for row in reader:
            if not row:
                continue
            t.append(float(row[0]))
            q.append(float(row[1]))
    return np.asarray(t), np.asarray(q)


def hydrograph_volume_m3(time_hr, discharge_cumecs) -> float:
    """Total released volume by trapezoidal integration, m3.

    This is the number the mass balance check compares the flooded volume
    against. If they disagree by more than a percent the solver is losing water.
    """
    t = np.asarray(time_hr, dtype=np.float64) * 3600.0
    q = np.asarray(discharge_cumecs, dtype=np.float64)
    return float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t))


# --------------------------------------------------------------------------
# extent.geojson
# --------------------------------------------------------------------------


def write_extent(
    run_dir: str | Path,
    max_depth: np.ndarray,
    grid: Grid,
    threshold_m: float = WET_THRESHOLD_M,
    simplify_deg: float = 0.0,
) -> Path:
    """Polygonise the wet mask into extent.geojson, EPSG:4326.

    Uses rasterio.features.shapes on the boolean wet mask, which walks the cell
    boundaries exactly - no smoothing, no marching squares artefacts. Each
    polygon carries area_km2 and min_depth_m as the contract requires.
    """
    from rasterio import features
    from shapely.geometry import mapping, shape

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    depth = np.asarray(max_depth, dtype=np.float32)
    wet = np.where(np.isfinite(depth) & (depth >= threshold_m), 1, 0).astype(np.uint8)

    polys = []
    for geom, value in features.shapes(wet, mask=wet.astype(bool), transform=grid.transform):
        if value != 1:
            continue
        poly = shape(geom)
        if simplify_deg > 0:
            poly = poly.simplify(simplify_deg, preserve_topology=True)
        if poly.is_empty:
            continue
        polys.append(poly)

    cell_km2 = grid.cell_area_m2() / 1e6
    features_out = []
    for poly in sorted(polys, key=lambda p: p.area, reverse=True):
        # Area from cell count is more honest than the degree-space polygon area.
        n_cells = poly.area / (grid.dx_deg * grid.dy_deg)
        features_out.append(
            {
                "type": "Feature",
                "geometry": mapping(poly),
                "properties": {
                    "area_km2": round(n_cells * cell_km2, 4),
                    "min_depth_m": round(float(threshold_m), 3),
                },
            }
        )

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features_out,
    }
    path = run_dir / "extent.geojson"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh)
    return path


def read_json(run_dir: str | Path, name: str) -> dict:
    path = Path(run_dir) / name
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(run_dir: str | Path, name: str, payload: dict) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / name
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, allow_nan=False)
        fh.write("\n")
    return path


# --------------------------------------------------------------------------
# packed.png - the browser texture
# --------------------------------------------------------------------------


def write_packed_png(
    run_dir: str | Path,
    arrival_time: np.ndarray,
    time_of_peak: np.ndarray,
    max_depth: np.ndarray,
    duration: np.ndarray | None,
    end_hr: float,
) -> tuple[Path, float]:
    """Pack four grids into one RGBA PNG for the GPU shader in module 05.

    R = arrival_time / end_hr        G = time_of_peak / end_hr
    B = max_depth / packed_depth_max A = duration / end_hr

    Returns (path, packed_depth_max_m). Put that second value into
    meta.json results.packed_depth_max_m - the shader cannot decode B without it.

    This is a RENDERING of the arrival-time and peak-depth products, not a
    frame-by-frame solver output. Say so, in the UI and out loud. frames/ is
    where true solver output lives.
    """
    from PIL import Image

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    depth = np.nan_to_num(np.asarray(max_depth, dtype=np.float32), nan=0.0)
    packed_depth_max = float(max(depth.max(), 1e-3))

    def norm(a: np.ndarray, scale: float) -> np.ndarray:
        v = np.nan_to_num(np.asarray(a, dtype=np.float32), nan=0.0)
        return np.clip(v / scale, 0.0, 1.0)

    end = max(float(end_hr), 1e-6)
    r = norm(arrival_time, end)
    g = norm(time_of_peak, end)
    b = norm(depth, packed_depth_max)
    a = norm(duration, end) if duration is not None else (depth >= WET_THRESHOLD_M).astype(np.float32)

    # Never-wet cells get A = 0 so the shader can skip them entirely.
    never_wet = ~np.isfinite(np.asarray(arrival_time, dtype=np.float32))
    a = np.where(never_wet, 0.0, a)

    rgba = np.stack([r, g, b, a], axis=-1)
    img = Image.fromarray((rgba * 255.0).round().astype(np.uint8), mode="RGBA")
    path = run_dir / "packed.png"
    img.save(path, optimize=True)
    return path, packed_depth_max


# --------------------------------------------------------------------------
# The run folder as an object
# --------------------------------------------------------------------------


class RunFolder:
    """Thin convenience wrapper. Everything it does is available as a function
    above; this just saves passing run_dir around."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.run_id = self.path.name

    def __repr__(self) -> str:
        return f"RunFolder({self.path})"

    @property
    def meta(self) -> dict:
        return read_meta(self.path)

    @property
    def grid(self) -> Grid:
        return Grid.from_meta(self.meta)

    def exists(self) -> bool:
        return (self.path / "meta.json").exists()

    def grid_array(self, name: str) -> np.ndarray:
        return read_grid(self.path, name)[0]

    def hydrograph(self):
        return read_hydrograph(self.path)

    def impact(self) -> dict | None:
        try:
            return read_json(self.path, "impact.json")
        except FileNotFoundError:
            return None

    def is_fake(self) -> bool:
        return bool(self.meta.get("is_fake", False))

    def frames(self) -> list[Path]:
        frames_dir = self.path / "frames"
        return sorted(frames_dir.glob("depth_*.tif")) if frames_dir.exists() else []


def list_runs(outputs_dir: str | Path = "outputs") -> list[RunFolder]:
    """Every valid-looking run folder under outputs/, newest first."""
    out = Path(outputs_dir)
    if not out.exists():
        return []
    runs = [RunFolder(p) for p in out.iterdir() if p.is_dir() and (p / "meta.json").exists()]
    runs.sort(key=lambda r: r.meta.get("created_utc", ""), reverse=True)
    return runs
