"""
modules/06_gee_validation/raster.py - read a downloaded raster onto our grid.

Kept separate and tiny so sar.py stays about the science.

Owner: captain (module 06).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from shared.geo import Grid


def read_to_grid(path: str | Path, grid: Grid) -> np.ndarray:
    """Reproject/resample any raster onto the run's grid, float64, NaN nodata.

    Bilinear, because backscatter in dB is continuous. The contract invariant
    is that every grid in a run shares shape and transform, so an observed
    layer that does not land on `grid` cannot be compared cell by cell.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject

    dest = np.full(grid.shape, np.nan, dtype=np.float64)
    with rasterio.open(path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dest,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            resampling=Resampling.bilinear,
            src_nodata=src.nodata,
            dst_nodata=np.nan,
        )
    return dest
