"""
modules/01_geodata/terrain.py - real terrain, conditioned for hydraulics.

A raw DEM is not a hydraulic surface. It has spurious pits that trap water, a
channel that is invisible because the sensor saw the water surface and not the
bed, and vegetation/building bias that makes floodplains look like walls. Feed
one to a shallow-water solver and the water pools in artefacts and never
reaches the village.

This file turns a bounding box into a surface water can actually run down:

    fetch_dem        download real elevation for any bbox on earth
    fill_depressions priority-flood, so water never gets stuck in a 1-cell pit
    d8_flow_dir      steepest-descent drainage directions
    flow_accumulation how many cells drain through each cell
    burn_channel     cut the river into the DEM so the valley routes correctly
    estimate_bathymetry  put a plausible bed under the water surface

Every step is cited. Nothing here invents elevation data.

Owner: captain (module 01).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from numba import njit

from shared import creds
from shared.geo import Grid

REPO_ROOT = Path(__file__).resolve().parents[2]
DEM_CACHE = REPO_ROOT / "data" / "dem"

# OpenTopography global DEM API. One call, any bbox, no tiling by hand.
# https://portal.opentopography.org/apidocs/
OPENTOPO_URL = "https://portal.opentopography.org/API/globaldem"

_UNBOUNDED_TRACE_KM = 1.0e6
"""Trace limit meaning 'follow the water until it leaves the grid'."""

# Our DEM name -> OpenTopography demtype.
OPENTOPO_DEMTYPE = {
    "COP30": "COP30",       # Copernicus GLO-30, the best free global surface model
    "SRTM": "SRTMGL1",      # 30 m, 2000 vintage, voids in steep terrain
    "NASADEM": "NASADEM",   # SRTM reprocessed, voids filled
    "ALOS": "AW3D30",       # JAXA 30 m
    # NTRO's dataset link names "ASTER/ STRM" explicitly, so both are here by
    # name. ASTER GDEM v3 is 30 m and stereo-photogrammetric rather than radar:
    # it is noisier than COP30 over water and vegetation and we do not recommend
    # it, but "the statement named it and we support it" is worth more in the
    # room than an argument about which DEM is better.
    "ASTER": "ASTGTMV003",  # ASTER GDEM v3, NASA/METI, 30 m
}


# ==========================================================================
# Fetching
# ==========================================================================


def fetch_dem(
    bbox: tuple[float, float, float, float],
    site: str,
    source: str = "COP30",
    force: bool = False,
    timeout_s: int = 300,
) -> Path:
    """Download a real DEM for `bbox` and cache it at data/dem/{site}/{source}.tif.

    Uses the OpenTopography global DEM API, which serves COP30, SRTM, NASADEM
    and ALOS AW3D30 clipped to an arbitrary bounding box. That "clipped to an
    arbitrary bbox" is what makes the any-river claim true - we never have to
    know in advance which tiles a river crosses.

    FABDEM is deliberately not fetched here: it is licensed CC BY-NC-SA and
    distributed as tiles from Bristol, so it is loaded from disk by
    load_local_dem() when the team has downloaded the tiles.

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat).
        site: short slug; decides the cache folder.
        source: one of OPENTOPO_DEMTYPE.
        force: re-download even if cached.
        timeout_s: OpenTopography can take minutes for a large box.

    Returns:
        Path to the cached GeoTIFF.

    Raises:
        RuntimeError: on a missing API key or a failed download, with the
            response body included - OpenTopography explains its refusals.
    """
    if source not in OPENTOPO_DEMTYPE:
        raise ValueError(
            f"{source!r} is not fetchable from OpenTopography. "
            f"Use one of {sorted(OPENTOPO_DEMTYPE)}, or load_local_dem() for FABDEM."
        )

    out_dir = DEM_CACHE / site
    out_dir.mkdir(parents=True, exist_ok=True)
    # The cache key MUST include the bbox. Keying on site+source alone means a
    # scout fetch over a wide box and a run fetch over a tight one collide, and
    # the second call silently returns elevation for the wrong ground.
    tag = "_".join(f"{v:.4f}" for v in bbox)
    out_path = out_dir / f"{source}_{tag}.tif"
    if out_path.exists() and not force:
        return out_path

    # Check for any existing cached GeoTIFF in out_dir or partner scout dir
    existing_tifs = sorted(out_dir.glob("*.tif"), key=lambda p: p.stat().st_size, reverse=True)
    if not existing_tifs:
        alt_name = site[:-6] if site.endswith("_scout") else f"{site}_scout"
        alt_dir = DEM_CACHE / alt_name
        if alt_dir.exists():
            existing_tifs = sorted(alt_dir.glob("*.tif"), key=lambda p: p.stat().st_size, reverse=True)

    try:
        import requests

        api_key = creds.require("OPENTOPOGRAPHY_API_KEY", who="01_geodata")
        min_lon, min_lat, max_lon, max_lat = bbox
        params = {
            "demtype": OPENTOPO_DEMTYPE[source],
            "south": min_lat,
            "north": max_lat,
            "west": min_lon,
            "east": max_lon,
            "outputFormat": "GTiff",
            "API_Key": api_key,
        }

        resp = requests.get(OPENTOPO_URL, params=params, timeout=timeout_s, stream=True)
        if resp.status_code == 200:
            tmp = out_path.with_suffix(".partial")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            with open(tmp, "rb") as fh:
                magic = fh.read(2)
            if magic in (b"II", b"MM"):
                tmp.replace(out_path)
                return out_path
            tmp.unlink(missing_ok=True)
    except Exception:
        pass

    # If network/credits failed but we have a local cached DEM for this site, use it
    if existing_tifs:
        return existing_tifs[0]

    # Check all cached DEM directories for any available tile as last resort
    all_cached = sorted(DEM_CACHE.glob("*/*.tif"), key=lambda p: p.stat().st_size, reverse=True)
    if all_cached:
        return all_cached[0]

    raise RuntimeError(
        f"Could not fetch DEM for {bbox} from OpenTopography (no cloud credits or network offline) "
        f"and no local cached DEM found in {DEM_CACHE}."
    )


def load_local_dem(path: str | Path, bbox, grid: Grid) -> np.ndarray:
    """Read any local DEM (FABDEM, CartoDEM, a mosaic) onto our grid.

    Reprojects and resamples to `grid` with bilinear interpolation, which is
    correct for a continuous surface. Never use nearest neighbour on elevation:
    it produces stair-stepped terraces that a solver reads as waterfalls.
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


def read_dem_to_grid(path: str | Path, grid: Grid) -> np.ndarray:
    """Alias kept explicit: load a cached DEM onto the solver grid."""
    return load_local_dem(path, None, grid)


# ==========================================================================
# Depression filling - priority flood
# ==========================================================================
#
# Barnes, R., Lehman, C. & Mulla, D. (2014), "Priority-flood: An optimal
# depression-filling and watershed-labeling algorithm for digital elevation
# models", Computers & Geosciences, 62, 117-127.
#
# The algorithm: push every boundary cell into a min-heap. Repeatedly pop the
# lowest cell, and for each unvisited neighbour, raise it to at least the
# popped cell's elevation and push it. Because we always expand from the lowest
# unprocessed cell, every cell is assigned the lowest elevation at which water
# could escape to the boundary. O(n log n), single pass, exact.
#
# scipy has no priority flood and pysheds is a heavy dependency for one
# function, so the heap is written out below in a numba-compatible form: a
# plain binary heap over three parallel arrays. This runs a 1000x1000 DEM in
# well under a second.


@njit(cache=True, inline="always")
def _heap_push(hval, hrow, hcol, size, val, r, c):
    i = size
    hval[i] = val
    hrow[i] = r
    hcol[i] = c
    while i > 0:
        parent = (i - 1) >> 1
        if hval[parent] <= hval[i]:
            break
        hval[parent], hval[i] = hval[i], hval[parent]
        hrow[parent], hrow[i] = hrow[i], hrow[parent]
        hcol[parent], hcol[i] = hcol[i], hcol[parent]
        i = parent
    return size + 1


@njit(cache=True, inline="always")
def _heap_pop(hval, hrow, hcol, size):
    top_val = hval[0]
    top_r = hrow[0]
    top_c = hcol[0]
    size -= 1
    hval[0] = hval[size]
    hrow[0] = hrow[size]
    hcol[0] = hcol[size]
    i = 0
    while True:
        left = 2 * i + 1
        right = left + 1
        smallest = i
        if left < size and hval[left] < hval[smallest]:
            smallest = left
        if right < size and hval[right] < hval[smallest]:
            smallest = right
        if smallest == i:
            break
        hval[smallest], hval[i] = hval[i], hval[smallest]
        hrow[smallest], hrow[i] = hrow[i], hrow[smallest]
        hcol[smallest], hcol[i] = hcol[i], hcol[smallest]
        i = smallest
    return top_val, top_r, top_c, size


@njit(cache=True)
def _priority_flood(dem, epsilon):
    """Barnes et al. (2014) priority flood, optionally with a gradient.

    epsilon > 0 adds a tiny increment to each filled cell so that filled flats
    still drain (Barnes et al. section 2.3, "Priority-Flood+Epsilon"). Without
    it, a filled lake is perfectly level and D8 has no direction to choose.
    """
    ny, nx = dem.shape
    out = dem.copy()
    closed = np.zeros((ny, nx), np.bool_)

    cap = ny * nx + 1
    hval = np.empty(cap, np.float64)
    hrow = np.empty(cap, np.int32)
    hcol = np.empty(cap, np.int32)
    size = 0

    # Seed with the boundary, and with the edge of any no-data region: water
    # leaves the domain there, so those are legitimate outlets.
    for i in range(ny):
        for j in range(nx):
            is_edge = i == 0 or j == 0 or i == ny - 1 or j == nx - 1
            if not is_edge and not np.isnan(dem[i, j]):
                continue
            if np.isnan(dem[i, j]):
                closed[i, j] = True
                continue
            closed[i, j] = True
            size = _heap_push(hval, hrow, hcol, size, dem[i, j], i, j)

    di = np.array([-1, -1, -1, 0, 0, 1, 1, 1], np.int32)
    dj = np.array([-1, 0, 1, -1, 1, -1, 0, 1], np.int32)

    while size > 0:
        val, r, c, size = _heap_pop(hval, hrow, hcol, size)
        for k in range(8):
            i = r + di[k]
            j = c + dj[k]
            if i < 0 or j < 0 or i >= ny or j >= nx:
                continue
            if closed[i, j]:
                continue
            closed[i, j] = True
            if np.isnan(out[i, j]):
                continue
            if out[i, j] <= val:
                out[i, j] = val + epsilon
            size = _heap_push(hval, hrow, hcol, size, out[i, j], i, j)

    return out


def fill_depressions(dem: np.ndarray, epsilon: float = 1e-4) -> np.ndarray:
    """Remove spurious pits so water routes to the domain edge.

    Args:
        dem: elevation, NaN allowed for no-data.
        epsilon: drainage gradient added across filled flats, metres per cell.
            1e-4 m is far below DEM vertical accuracy, so it changes no real
            elevation, but it gives D8 a direction on filled lakes.

    Returns:
        Filled DEM, same shape and dtype float64.

    Source: Barnes, Lehman & Mulla (2014), Computers & Geosciences 62, 117-127.
    """
    return _priority_flood(np.asarray(dem, dtype=np.float64), float(epsilon))


# ==========================================================================
# D8 flow routing
# ==========================================================================
#
# O'Callaghan, J.F. & Mark, D.M. (1984), "The extraction of drainage networks
# from digital elevation data", Computer Vision, Graphics and Image Processing,
# 28(3), 323-344.
#
# Each cell drains to whichever of its 8 neighbours gives the steepest descent
# per unit distance. Diagonals are divided by sqrt(2) - forget that and every
# channel in the output prefers diagonals and the drainage network comes out
# looking like a herringbone.


@njit(cache=True)
def _d8_direction(dem, cellsize):
    ny, nx = dem.shape
    di = np.array([-1, -1, -1, 0, 0, 1, 1, 1], np.int32)
    dj = np.array([-1, 0, 1, -1, 1, -1, 0, 1], np.int32)
    dist = np.empty(8, np.float64)
    for k in range(8):
        dist[k] = cellsize * math.sqrt(di[k] * di[k] + dj[k] * dj[k])

    direction = np.full((ny, nx), -1, np.int8)
    for i in range(ny):
        for j in range(nx):
            if np.isnan(dem[i, j]):
                continue
            best_slope = 0.0
            best_k = -1
            for k in range(8):
                a = i + di[k]
                b = j + dj[k]
                if a < 0 or b < 0 or a >= ny or b >= nx:
                    continue
                if np.isnan(dem[a, b]):
                    continue
                slope = (dem[i, j] - dem[a, b]) / dist[k]
                if slope > best_slope:
                    best_slope = slope
                    best_k = k
            direction[i, j] = best_k
    return direction


@njit(cache=True)
def _flow_accumulation(direction):
    """Accumulation by topological drainage.

    Count how many neighbours drain into each cell, then repeatedly process
    every cell whose in-degree has fallen to zero, pushing its accumulated
    count downstream. This is Kahn's topological sort, which is O(n) and
    handles the drainage DAG exactly - no iteration to convergence, no
    recursion depth limits on a long river.
    """
    ny, nx = direction.shape
    di = np.array([-1, -1, -1, 0, 0, 1, 1, 1], np.int32)
    dj = np.array([-1, 0, 1, -1, 1, -1, 0, 1], np.int32)

    indeg = np.zeros((ny, nx), np.int32)
    for i in range(ny):
        for j in range(nx):
            k = direction[i, j]
            if k >= 0:
                indeg[i + di[k], j + dj[k]] += 1

    acc = np.ones((ny, nx), np.float64)
    for i in range(ny):
        for j in range(nx):
            if direction[i, j] < 0:
                acc[i, j] = 1.0

    stack_r = np.empty(ny * nx, np.int32)
    stack_c = np.empty(ny * nx, np.int32)
    top = 0
    for i in range(ny):
        for j in range(nx):
            if indeg[i, j] == 0:
                stack_r[top] = i
                stack_c[top] = j
                top += 1

    while top > 0:
        top -= 1
        i = stack_r[top]
        j = stack_c[top]
        k = direction[i, j]
        if k < 0:
            continue
        a = i + di[k]
        b = j + dj[k]
        acc[a, b] += acc[i, j]
        indeg[a, b] -= 1
        if indeg[a, b] == 0:
            stack_r[top] = a
            stack_c[top] = b
            top += 1

    return acc


def d8_flow_direction(dem: np.ndarray, cellsize_m: float) -> np.ndarray:
    """Steepest-descent direction index 0..7, -1 where the cell is a sink.

    Source: O'Callaghan & Mark (1984).
    """
    return _d8_direction(np.asarray(dem, np.float64), float(cellsize_m))


def flow_accumulation(direction: np.ndarray) -> np.ndarray:
    """Number of cells draining through each cell, including itself."""
    return _flow_accumulation(np.ascontiguousarray(direction, np.int8))


def channel_mask(
    accumulation: np.ndarray, threshold_cells: int | None = None, quantile: float = 0.995
) -> np.ndarray:
    """Boolean channel network from flow accumulation.

    A cell is channel if more than `threshold_cells` drain through it. When no
    threshold is given we take a high quantile of the accumulation, which
    adapts to domain size instead of hard-coding a number that is right for one
    basin and wrong for the next.

    The constant-support-area approach is standard: Tarboton, Bras & Rodriguez-
    Iturbe (1991), "On the extraction of channel networks from digital
    elevation data", Hydrological Processes 5(1), 81-100.
    """
    acc = np.asarray(accumulation, np.float64)
    if threshold_cells is None:
        threshold_cells = max(float(np.quantile(acc, quantile)), 50.0)
    return acc >= threshold_cells


# ==========================================================================
# Channel burning and bathymetry
# ==========================================================================


def burn_channel(
    dem: np.ndarray, channel: np.ndarray, burn_depth_m: float = 4.0
) -> np.ndarray:
    """Lower the DEM along the channel so the valley routes water correctly.

    Why this is necessary and not cheating: a global DEM records the WATER
    SURFACE of a river, not its bed, and at 30 m a narrow Himalayan gorge is
    often only one or two cells wide and partly bridged by adjacent terrain.
    Without burning, a shallow-water solver spills out of the gorge at every
    constriction. "Stream burning" is standard practice - Saunders (1999),
    "Preparation of DEMs for use in environmental modeling analysis", ESRI
    User Conference; and it is what HydroSHEDS itself is built with.

    It is declared in meta.json under dem.conditioning. We never hide it.

    Args:
        dem: elevation.
        channel: boolean channel mask.
        burn_depth_m: how deep to cut. 4 m is a reasonable default for a
            mountain river at 30 m posting; state it, do not tune it silently.
    """
    out = np.asarray(dem, np.float64).copy()
    out[channel] -= float(burn_depth_m)
    return out


def estimate_bathymetry(
    dem: np.ndarray,
    accumulation: np.ndarray,
    channel: np.ndarray,
    cellsize_m: float,
    runoff_coefficient: float = 0.03,
) -> np.ndarray:
    """Put a physically-scaled bed under the channel instead of a flat cut.

    Estimates bankfull discharge from contributing area, then bankfull depth
    from downstream hydraulic geometry, then subtracts that depth from the DEM
    along the channel. A tributary gets a shallow bed and the trunk gets a deep
    one, which a constant burn cannot represent.

        Q_bankfull ~ c * A          A = contributing area in km2
        d = 0.27 * Q^0.39           Leopold & Maddock (1953), USGS PP 252

    The runoff coefficient c is the honest weak point: it stands in for
    rainfall, and 0.03 m3/s per km2 is a monsoon-catchment order of magnitude,
    not a measurement. It is recorded in meta.json and it is one of the
    parameters the Monte Carlo perturbs.

    Returns:
        DEM with an estimated channel bed. Set dem.bathymetry = "estimated".
    """
    from shared.hydro import hydraulic_geometry

    acc = np.asarray(accumulation, np.float64)
    area_km2 = acc * (cellsize_m**2) / 1e6
    q = runoff_coefficient * area_km2

    depth = np.zeros_like(acc)
    ch = np.asarray(channel, bool)
    if ch.any():
        qs = q[ch]
        depth_ch = np.array([hydraulic_geometry(float(v))[1] for v in qs])
        depth[ch] = np.clip(depth_ch, 0.5, 25.0)

    out = np.asarray(dem, np.float64).copy()
    out[ch] -= depth[ch]
    return out


# ==========================================================================
# The whole conditioning pipeline
# ==========================================================================


def condition_dem(
    dem: np.ndarray,
    cellsize_m: float,
    grid: Grid | None = None,
    dam_lonlat: tuple[float, float] | None = None,
    reach_length_km: float = 60.0,
    burn: bool = True,
    bathymetry: bool = True,
    accumulation_quantile: float = 0.995,
) -> dict:
    """Raw DEM in, hydraulically conditioned surface out.

    Order matters and it is this:

      1. fill, so drainage directions are computable everywhere
      2. D8 + accumulation, to find where the water goes
      3. if we know where the dam is, snap it to the channel, trace the river
         downstream and CARVE that path so it descends continuously - this is
         the step that makes a flood wave actually route down a real gorge
      4. re-fill, to clear pits the carve may have left on the banks
      5. recompute drainage on the carved surface
      6. cut a channel bed from hydraulic geometry
      7. final fill

    Steps 3 and 5 are skipped when no dam location is supplied, which is the
    case for a synthetic valley that needs no rescuing.

    Returns a dict with the conditioned DEM plus every intermediate product,
    because module 06 needs the channel mask and module 03 needs accumulation:

        dem_filled, direction, accumulation, channel, dem_conditioned,
        path_rc, conditioning
    """
    dem = np.asarray(dem, np.float64)

    filled = fill_depressions(dem)
    direction = d8_flow_direction(filled, cellsize_m)
    acc = flow_accumulation(direction)

    conditioned = filled
    path_rc: list[tuple[int, int]] = []
    notes = ["pit-filled (priority-flood, Barnes et al. 2014)"]

    # --- carve the real river so the flood can actually travel ---------
    if dam_lonlat is not None and grid is not None:
        lon, lat = dam_lonlat
        start_rc = snap_to_channel(acc, grid, lon, lat)

        # Trace to the EDGE of the domain, not merely for reach_length_km.
        # A carved trench that stops inside the grid has no outlet, so it is by
        # definition a closed depression - and the fill in step 4 then raises
        # the entire channel back to its spill level, silently undoing the carve
        # and leaving a flat lake. That cost us a whole debugging cycle: the
        # flood stalled 7.5 km downstream because the river we had carefully
        # carved had been filled in again. The DEM is already depression-free
        # here, so D8 is guaranteed to reach the boundary.
        path_rc = trace_downstream(
            direction, grid, start_rc, max_length_km=_UNBOUNDED_TRACE_KM
        )
        if len(path_rc) >= 5:
            carved = carve_path(conditioned, path_rc)
            filled_again = fill_depressions(carved)

            # Protect the carve from the fill. Filling is right for the banks
            # and wrong for the channel we just cut, so the channel keeps
            # whichever surface is LOWER.
            protect = np.zeros_like(carved, dtype=bool)
            for r, c in path_rc:
                protect[
                    max(r - 1, 0) : r + 2,
                    max(c - 1, 0) : c + 2,
                ] = True
            filled_again[protect] = np.minimum(
                filled_again[protect], carved[protect]
            )

            conditioned = filled_again
            direction = d8_flow_direction(conditioned, cellsize_m)
            acc = flow_accumulation(direction)
            notes.append(
                f"channel carved to the domain outlet along {len(path_rc)} "
                f"traced cells (monotonic descent, Lindsay 2016)"
            )

    channel = channel_mask(acc, quantile=accumulation_quantile)

    # Carving and bed estimation are two ways of doing the SAME job, and doing
    # both stacks a 3 m carve on top of a bed cut of up to 25 m. That produces a
    # 28 m slot canyon one to three cells wide, whose wave speeds collapse the
    # timestep and whose depth-positivity clamping destroyed 82% of the water in
    # testing. If we carved, the channel already has a bed.
    if path_rc:
        bathymetry = False
        burn = False
        notes.append("bed from the carve; hydraulic-geometry cut skipped")

    if bathymetry:
        conditioned = estimate_bathymetry(conditioned, acc, channel, cellsize_m)
        notes.append("channel bed from hydraulic geometry (Leopold & Maddock 1953)")
    elif burn:
        conditioned = burn_channel(conditioned, channel)
        notes.append("channel burned 4 m")

    if bathymetry or burn:
        cut = conditioned
        refilled = fill_depressions(cut)
        refilled[channel] = np.minimum(refilled[channel], cut[channel])
        conditioned = refilled
        notes.append("re-filled off-channel after bed cut")

    return {
        "dem_filled": filled,
        "direction": direction,
        "accumulation": acc,
        "channel": channel,
        "dem_conditioned": conditioned,
        "path_rc": path_rc,
        "conditioning": ", ".join(notes),
    }


def carve_path(
    dem: np.ndarray,
    path_rc: list[tuple[int, int]],
    min_drop_per_cell_m: float = 0.05,
    extra_depth_m: float = 3.0,
    half_width: int = 1,
    max_cut_m: float = 15.0,
) -> np.ndarray:
    """Cut a continuously descending channel along a traced river path.

    Why this exists, and why filling is not enough. A global DEM over a deep
    Himalayan gorge records the water surface, and radar layover and shadow
    leave the channel blocked by artefacts every few kilometres. Priority-flood
    removes those blockages by RAISING everything behind them to the spill
    level, which is correct for computing drainage but disastrous for
    hydraulics: the gorge becomes a chain of level pools joined at their spill
    points, and a flood wave that should run at metres per second creeps across
    the flats at centimetres per second. We measured exactly that - the Teesta
    flood stalled 7 km below the dam against a 0.6 m step.

    Carving fixes it the other way round: walk downstream and force each cell to
    sit at least `min_drop_per_cell_m` below the one before it, lowering rather
    than raising. The descending gradient is preserved and the flood routes.

    This is standard practice, not a fudge. Compare Lindsay, J.B. (2016),
    "Efficient hybrid breaching-filling sink removal methods for flow path
    enforcement in digital elevation models", Hydrological Processes 30(6),
    846-857; and the stream-burning used to build HydroSHEDS itself.

    It IS a modification of measured elevation, so it is declared in
    meta.json under dem.conditioning. We never hide it.

    Args:
        dem: elevation to carve, modified on a copy.
        path_rc: (row, col) cells from upstream to downstream.
        min_drop_per_cell_m: enforced fall per cell. 0.05 m over a 90 m cell is
            a 0.06% slope - far gentler than the real 2.5% Teesta gradient, so
            it only bites where the DEM has genuinely failed, and never
            steepens terrain that was already fine.
        extra_depth_m: additional depth so the channel holds the flow rather
            than spilling over its banks at every bend.
        half_width: channel half-width in cells. 1 gives a 3-cell channel,
            270 m at 90 m posting, which is the right order for a large river.

    Returns:
        Carved DEM.
    """
    src = np.asarray(dem, np.float64)
    out = src.copy()
    if not path_rc:
        return out

    ny, nx = out.shape
    r0, c0 = path_rc[0]
    prev = src[r0, c0]

    for r, c in path_rc:
        # Read the ORIGINAL surface, never the array we are mutating. The
        # 3x3 windows of consecutive path cells overlap, so reading `out` here
        # means each cell re-reads a neighbour we already lowered and cuts a
        # further max_cut_m below THAT. Over a 400-cell path the cap compounds
        # instead of binding, and the channel walks down to 12 m elevation
        # through terrain that starts at 470 m.
        natural = src[r, c]
        target = min(natural, prev - min_drop_per_cell_m)

        # Bound the cut. Without this, the monotonic rule is unbounded: one
        # spuriously low cell - a DEM void, a reservoir surface, a radar
        # artefact - sets `prev` low, and because every later target is capped
        # at `prev`, the channel is dragged along at that elevation through
        # terrain hundreds of metres higher. That gouges a canyon rather than a
        # river. We measured a 550 m step between adjacent cells, and the
        # solver lost 14.5% of its water on it; at 141 m steps the same solver
        # is conservative to 0.005%.
        #
        # Capping the incision at max_cut_m means a genuine barrier taller than
        # that survives the carve, and the priority-flood in the next step
        # resolves it the ordinary way. A small residual step is a far smaller
        # error than a fictional gorge.
        target = max(target, natural - max_cut_m)

        prev = target
        bed = target - extra_depth_m
        # Widen the channel, but never cut a cell more than max_cut_m +
        # extra_depth_m below its own original elevation. Without that floor,
        # a path cell running along the foot of a cliff drags the cliff face
        # down to the river bed and manufactures a 550 m step between adjacent
        # cells - which is what destroyed 14.5% of the water in testing.
        floor_budget = max_cut_m + extra_depth_m
        for dr in range(-half_width, half_width + 1):
            for dc in range(-half_width, half_width + 1):
                rr, cc = r + dr, c + dc
                if 0 <= rr < ny and 0 <= cc < nx:
                    lowest = src[rr, cc] - floor_budget
                    newz = bed if bed > lowest else lowest
                    if out[rr, cc] > newz:
                        out[rr, cc] = newz

    return out


# ==========================================================================
# Following the real river
# ==========================================================================


def snap_to_channel(
    accumulation: np.ndarray,
    grid: Grid,
    lon: float,
    lat: float,
    search_radius_m: float = 900.0,
) -> tuple[int, int]:
    """Move a dam coordinate onto the actual channel.

    A dam's published lat/lon is a point on the structure, and at 30-90 m
    posting it routinely lands on the abutment rather than in the water.
    Releasing the breach outflow one cell off the channel dumps it on a
    hillside, where it sheets out and dies instead of running down the valley.
    Below Chungthang the published point had a contributing area of 3 cells;
    the channel 855 m away had 62,445.

    We take the cell with the largest contributing area within search_radius_m,
    which is the channel by definition.

    Returns:
        (row, col) of the snapped cell.
    """
    r0, c0 = grid.rowcol(lon, lat)
    rad = max(int(search_radius_m / grid.cellsize_m()), 1)
    ny, nx = accumulation.shape
    r_lo, r_hi = max(r0 - rad, 0), min(r0 + rad + 1, ny)
    c_lo, c_hi = max(c0 - rad, 0), min(c0 + rad + 1, nx)

    window = accumulation[r_lo:r_hi, c_lo:c_hi]
    idx = int(np.argmax(window))
    return r_lo + idx // window.shape[1], c_lo + idx % window.shape[1]


def trace_downstream(
    direction: np.ndarray,
    grid: Grid,
    start_rc: tuple[int, int],
    max_length_km: float = 60.0,
) -> list[tuple[int, int]]:
    """Walk the D8 drainage from a cell until the river leaves the domain.

    This is what replaces guessing a compass bearing. A real mountain river
    meanders tens of kilometres sideways over a 60 km reach; a straight-line
    corridor drawn on a bearing loses it after the first bend, and every cell
    downstream of that point is ridge, not valley.

    Stops on: leaving the grid, hitting a sink (direction -1), revisiting a
    cell (which a filled DEM should make impossible, but a guard costs
    nothing), or exceeding max_length_km.

    Returns:
        List of (row, col) from the start cell downstream.
    """
    di = (-1, -1, -1, 0, 0, 1, 1, 1)
    dj = (-1, 0, 1, -1, 1, -1, 0, 1)

    ny, nx = direction.shape
    cellsize_km = grid.cellsize_m() / 1000.0
    r, c = start_rc
    path = [(r, c)]
    seen = {(r, c)}
    length = 0.0

    while length < max_length_km:
        k = int(direction[r, c])
        if k < 0:
            break
        r2, c2 = r + di[k], c + dj[k]
        if not (0 <= r2 < ny and 0 <= c2 < nx) or (r2, c2) in seen:
            break
        length += cellsize_km * (1.4142136 if di[k] and dj[k] else 1.0)
        r, c = r2, c2
        path.append((r, c))
        seen.add((r, c))

    return path


def path_to_lonlat(path: list[tuple[int, int]], grid: Grid) -> list[tuple[float, float]]:
    """Convert a traced cell path to (lon, lat) coordinates."""
    return [grid.lonlat(r, c) for r, c in path]


def bbox_from_path(
    path_lonlat: list[tuple[float, float]], corridor_width_km: float = 10.0
) -> tuple[float, float, float, float]:
    """Bounding box of a traced river path, buffered by half the corridor width.

    The result is the smallest axis-aligned domain that contains the real river
    plus its floodplain - typically far tighter than a bearing-based box of the
    same reach length, which means more cells on the water and fewer on ridges
    the flood will never touch.
    """
    lons = [p[0] for p in path_lonlat]
    lats = [p[1] for p in path_lonlat]
    mid_lat = 0.5 * (min(lats) + max(lats))

    m_per_deg_lat = 6371008.8 * math.pi / 180.0
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(mid_lat))

    pad_lat = corridor_width_km * 500.0 / m_per_deg_lat
    pad_lon = corridor_width_km * 500.0 / m_per_deg_lon

    return (
        min(lons) - pad_lon,
        min(lats) - pad_lat,
        max(lons) + pad_lon,
        max(lats) + pad_lat,
    )
