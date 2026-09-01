"""
modules/01_geodata/provider.py - the real TerrainProvider.

This is the one object that flips the whole system from synthetic to real.
runner.py takes any object with get_terrain() and .source; SyntheticTerrain
gives it a generated valley, RealTerrain gives it a downloaded, conditioned,
hydraulically correct DEM with a Manning raster derived from land cover.

    from modules... import RealTerrain      # see load() below, digit-prefixed
    run_scenario(spec, terrain=RealTerrain(site="teesta"))

Once that is passed, meta.json carries dem.source = COP30 (not SYNTHETIC) and
is_fake goes false, which is what lets a run appear in the live demo.

Owner: captain (module 01).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from shared.contract import DEFAULT_MANNING_N, MANNING_N_BOUNDS
from shared.geo import Grid

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "dem"


class RealTerrain:
    """Downloaded DEM + conditioning + Manning raster, cached per site.

    The cache is keyed on (site, source, bbox, cellsize). Conditioning a
    600 x 1200 grid takes a few seconds; caching it means the second scenario
    at the same site starts solving immediately, which matters when a juror
    asks "now try piping instead".
    """

    def __init__(
        self,
        site: str,
        source: str = "COP30",
        local_dem: str | Path | None = None,
        bathymetry: bool = True,
        manning_source: str = "auto",
        cache: bool = True,
        dam_lonlat: tuple[float, float] | None = None,
        reach_length_km: float = 60.0,
    ):
        """
        Args:
            site: short slug, e.g. 'teesta'. Decides the cache folder.
            source: COP30 / SRTM / NASADEM / ALOS, or FABDEM with local_dem set.
            local_dem: path to a DEM already on disk (FABDEM tiles, CartoDEM).
                Takes precedence over downloading.
            bathymetry: estimate a channel bed rather than a flat burn.
            manning_source: 'auto' tries Earth Engine land cover and falls back
                to a channel/floodplain split; 'constant' uses one value.
            cache: write conditioned arrays to data/dem/{site}/ for reuse.
        """
        self.site = site
        self._source = source
        self.local_dem = Path(local_dem) if local_dem else None
        self.bathymetry = bathymetry
        self.manning_source = manning_source
        self.cache = cache
        self.dam_lonlat = dam_lonlat
        self.reach_length_km = reach_length_km
        self.conditioning = ""
        self.last_products: dict | None = None

    @property
    def source(self) -> str:
        """One of shared.contract.DEM_SOURCES - goes straight into meta.json."""
        return "FABDEM" if self.local_dem and self._source == "FABDEM" else self._source

    # ------------------------------------------------------------------

    def _cache_paths(self, grid: Grid) -> tuple[Path, Path]:
        dam = f"{self.dam_lonlat[0]:.4f}_{self.dam_lonlat[1]:.4f}" if self.dam_lonlat else "nodam"
        key = (
            f"{self.source}_{grid.nx}x{grid.ny}_{grid.bbox[0]:.4f}_{grid.bbox[1]:.4f}"
            f"_{dam}_r{self.reach_length_km:.0f}"
        )
        folder = CACHE_DIR / self.site
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"cond_{key}.npz", folder / f"cond_{key}.json"

    def get_terrain(self, bbox, cellsize_m):
        """Returns (dem_m, manning_n, grid) - the TerrainProvider contract.

        Raises:
            RuntimeError: if the DEM cannot be fetched. We do NOT silently fall
                back to synthetic terrain: a run that claims dem.source = COP30
                must actually be COP30, and a quiet fallback is exactly how a
                fabricated result reaches a juror.
        """
        from . import terrain as tr

        grid = Grid.from_bbox_cellsize(tuple(bbox), float(cellsize_m))
        npz_path, meta_path = self._cache_paths(grid)

        if self.cache and npz_path.exists():
            blob = np.load(npz_path)
            self.conditioning = json.loads(meta_path.read_text())["conditioning"]
            self.last_products = {
                "channel": blob["channel"],
                "accumulation": blob["accumulation"],
            }
            return blob["dem"].astype(np.float64), blob["manning"].astype(np.float64), grid

        # --- elevation -------------------------------------------------
        if self.local_dem is not None:
            if not self.local_dem.exists():
                raise RuntimeError(f"local DEM not found: {self.local_dem}")
            raw = tr.load_local_dem(self.local_dem, bbox, grid)
        else:
            path = tr.fetch_dem(tuple(bbox), site=self.site, source=self._source)
            raw = tr.load_local_dem(path, bbox, grid)

        if not np.isfinite(raw).any():
            raise RuntimeError(
                f"DEM for {self.site} over {bbox} is entirely no-data. "
                f"Check the bbox is on land and in (min_lon, min_lat, max_lon, max_lat) order."
            )

        nan_frac = float(np.isnan(raw).mean())
        if nan_frac > 0.30:
            raise RuntimeError(
                f"DEM for {self.site} is {nan_frac:.0%} no-data - too many voids to "
                f"model on. Try source='NASADEM' (voids filled) instead of {self._source}."
            )
        if nan_frac > 0:
            raw = _fill_voids(raw)

        # --- conditioning ----------------------------------------------
        products = tr.condition_dem(
            raw,
            grid.cellsize_m(),
            grid=grid,
            dam_lonlat=self.dam_lonlat,
            reach_length_km=self.reach_length_km,
            bathymetry=self.bathymetry,
        )
        dem = products["dem_conditioned"]
        self.conditioning = products["conditioning"]
        self.last_products = products

        # --- roughness -------------------------------------------------
        manning = self._manning(grid, products)

        if self.cache:
            np.savez_compressed(
                npz_path,
                dem=dem.astype(np.float32),
                manning=manning.astype(np.float32),
                channel=products["channel"],
                accumulation=products["accumulation"].astype(np.float32),
            )
            meta_path.write_text(
                json.dumps(
                    {
                        "site": self.site,
                        "source": self.source,
                        "bbox": list(grid.bbox),
                        "nx": grid.nx,
                        "ny": grid.ny,
                        "cellsize_m": grid.cellsize_m(),
                        "conditioning": self.conditioning,
                        "nodata_frac": round(nan_frac, 5),
                    },
                    indent=2,
                )
            )

        return dem, manning, grid

    # ------------------------------------------------------------------

    def _manning(self, grid: Grid, products: dict) -> np.ndarray:
        """Per-cell Manning's n.

        'auto' asks module 06's Earth Engine land cover if it is available and
        falls back to a channel/floodplain split, which is crude but honest:
        the channel is smoother than the valley walls, and that is the first-
        order effect on travel time.
        """
        if self.manning_source == "constant":
            return np.full(grid.shape, DEFAULT_MANNING_N, dtype=np.float64)

        manning = None
        if self.manning_source == "auto":
            try:
                from .roughness import manning_from_landcover

                manning = manning_from_landcover(grid, site=self.site)
            except Exception:
                manning = None  # land cover is a nice-to-have, never a blocker

        if manning is None:
            manning = np.full(grid.shape, DEFAULT_MANNING_N, dtype=np.float64)
            manning[products["channel"]] = 0.030

        # Land cover NEVER covers the whole grid: the WorldCover pull is
        # reprojected onto our bbox and comes back with no-data around the
        # edges - 63% of cells on the first Teesta domain. A NaN here is not a
        # cosmetic problem. It propagates n -> friction -> velocity -> depth,
        # and the solver kernels run under fastmath=True, which licenses the
        # compiler to assume NaNs do not exist; the depth test then takes the
        # wrong branch and the cell is silently zeroed. That destroyed 60% of
        # the water in the run and reported it as a mass-balance error with no
        # indication of where it went.
        #
        # Fill from the same fallback the no-landcover path uses, and count it
        # so the substitution is visible rather than assumed.
        bad = ~np.isfinite(manning)
        if bad.any():
            fallback = np.full(grid.shape, DEFAULT_MANNING_N, dtype=np.float64)
            fallback[products["channel"]] = 0.030
            manning = np.where(bad, fallback, manning)
            self.manning_nodata_frac = float(bad.mean())
        else:
            self.manning_nodata_frac = 0.0

        manning = np.clip(manning, *MANNING_N_BOUNDS).astype(np.float64)
        if not np.isfinite(manning).all():
            raise RuntimeError("manning raster still non-finite after fill")
        return manning

    def dem_meta(self, grid: Grid | None = None) -> dict:
        """The dem block for meta.json, with the conditioning declared."""
        return {
            "source": self.source,
            "native_resolution_m": 30.0,
            "bathymetry": "estimated" if self.bathymetry else "none",
            "conditioning": self.conditioning or "none",
        }


def _fill_voids(dem: np.ndarray) -> np.ndarray:
    """Fill small no-data holes by nearest-neighbour, which is the right choice
    here: a void in a 30 m DEM is almost always radar shadow in steep terrain,
    and the nearest valid elevation is a better guess than an interpolated
    plane across a gorge."""
    from scipy import ndimage

    arr = np.asarray(dem, np.float64)
    mask = np.isnan(arr)
    if not mask.any():
        return arr
    idx = ndimage.distance_transform_edt(
        mask, return_distances=False, return_indices=True
    )
    return arr[tuple(idx)]
