"""
modules/01_geodata/roughness.py - Manning's n from land cover.

Roughness is the second most important input after terrain. It sets how fast
the flood travels, which sets warning time, which is the number the whole
product exists to produce. A single n = 0.035 everywhere means a forest slows
water down exactly as much as a paddy field, which is wrong by a factor of
three at the extremes.

We take ESA WorldCover 2021 (10 m, global, free) and map each class to a
Manning's n from Chow (1959) and Arcement & Schneider (1989). The lookup lives
in shared/contract.py so that every module sees the same table.

Owner: captain (module 01).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from shared.contract import (
    DEFAULT_MANNING_N,
    MANNING_N_BOUNDS,
    MANNING_N_BY_LANDCOVER,
)
from shared.geo import Grid

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUGHNESS_DIR = REPO_ROOT / "data" / "roughness"

WORLDCOVER_EE_ASSET = "ESA/WorldCover/v200"


def manning_from_landcover(
    grid: Grid, site: str, force: bool = False
) -> np.ndarray | None:
    """Manning's n raster on `grid`, from ESA WorldCover.

    Tries, in order:
      1. a cached GeoTIFF at data/roughness/{site}/manning.tif
      2. a cached land-cover GeoTIFF at data/roughness/{site}/worldcover.tif
      3. Earth Engine, exporting WorldCover for the bbox

    Returns None if none of those work - the caller falls back to a
    channel/floodplain split rather than failing the run. Roughness is
    important but it is never worth blocking a demo over.
    """
    folder = ROUGHNESS_DIR / site
    manning_path = folder / "manning.tif"
    cover_path = folder / "worldcover.tif"

    if manning_path.exists() and not force:
        return _read_to_grid(manning_path, grid, categorical=False)

    if not cover_path.exists() or force:
        if not _fetch_worldcover_ee(grid, cover_path):
            return None

    classes = _read_to_grid(cover_path, grid, categorical=True)
    if classes is None:
        return None

    manning = classes_to_manning(classes)
    _write_manning(manning_path, manning, grid)
    return manning


def classes_to_manning(classes: np.ndarray) -> np.ndarray:
    """Map WorldCover class codes to Manning's n via the contract table."""
    out = np.full(classes.shape, DEFAULT_MANNING_N, dtype=np.float64)
    for code, n in MANNING_N_BY_LANDCOVER.items():
        out[classes == code] = n
    return np.clip(out, *MANNING_N_BOUNDS)


def _fetch_worldcover_ee(grid: Grid, out_path: Path) -> bool:
    """Export ESA WorldCover for the grid bbox via Earth Engine.

    Returns False rather than raising: this is an optional enhancement, and
    module 06 owns the serious Earth Engine work.
    """
    try:
        import ee
        import requests

        from shared import creds

        _ee_init(ee, creds)

        region = ee.Geometry.Rectangle(list(grid.bbox))
        image = ee.ImageCollection(WORLDCOVER_EE_ASSET).first().clip(region)
        url = image.getDownloadURL(
            {
                "region": region,
                "dimensions": f"{grid.nx}x{grid.ny}",
                "format": "GEO_TIFF",
                "bands": ["Map"],
            }
        )
        resp = requests.get(url, timeout=300)
        if resp.status_code != 200 or resp.content[:2] not in (b"II", b"MM"):
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return True
    except Exception:
        return False


def _ee_init(ee, creds) -> None:
    """Authenticate Earth Engine from the service account in .env."""
    key_path = creds.ee_key_path()
    email = creds.require("EE_SERVICE_ACCOUNT_EMAIL", who="01_geodata")
    project = creds.require("EE_PROJECT_ID", who="01_geodata")
    credentials = ee.ServiceAccountCredentials(email, str(key_path))
    ee.Initialize(credentials, project=project)


def _read_to_grid(path: Path, grid: Grid, categorical: bool) -> np.ndarray | None:
    """Resample a raster onto our grid.

    Categorical data uses nearest neighbour - averaging land-cover class codes
    would produce class 45, which is not a class. Continuous data uses bilinear.
    """
    try:
        import rasterio
        from rasterio.warp import Resampling, reproject

        dest = np.zeros(grid.shape, dtype=np.float64)
        with rasterio.open(path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=dest,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                resampling=Resampling.nearest if categorical else Resampling.bilinear,
            )
        return dest
    except Exception:
        return None


def _write_manning(path: Path, manning: np.ndarray, grid: Grid) -> None:
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=grid.ny,
        width=grid.nx,
        count=1,
        dtype="float32",
        crs=grid.crs,
        transform=grid.transform,
        nodata=np.nan,
        compress="LZW",
    ) as dst:
        dst.write(manning.astype(np.float32), 1)
        dst.set_band_description(1, "Manning n")
