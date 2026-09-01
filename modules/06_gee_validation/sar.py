"""
modules/06_gee_validation/sar.py - observed flood extent from Sentinel-1, and
the one honest accuracy number we will be able to quote.

The problem statement asks for "near real-time flood analysis using Google
Earth Engine and open-source data". This is that deliverable, and it is also
what turns our simulation from a plausible picture into a measured claim.

How it works. Sentinel-1 is radar, so it sees through cloud - which matters,
because a Himalayan flood happens under monsoon cloud and every optical sensor
is blind. Smooth open water reflects radar away from the satellite, so water
appears DARK. We take one image before the event and one after, classify water
in both, and call "flood" the pixels that became water and were not water
before. Change detection, not absolute classification: it cancels out
permanent rivers, lakes, radar shadow and terrain effects that would otherwise
be misread as flooding.

Then we compare that observed extent with our simulated extent and report the
Critical Success Index. One number, honestly derived, stated with its limits.

IMPORTANT HONESTY NOTE, and say this out loud before a juror asks. The
4 October 2023 Teesta event was a GLOF from South Lhonak lake - roughly 50 MCM
arriving from upstream - which then destroyed Chungthang dam. It is NOT the
same event as "Chungthang's own 5 MCM reservoir fails". Comparing a 5 MCM
dam-break scenario against that observation would be dishonest and the CSI
would be meaningless. To validate against this observation, run the matching
scenario: a large upstream inflow volume. The metrics below are computed on
whatever run you hand them and they do not know what the run represents - that
judgement is yours, and it belongs in the report.

Owner: captain (module 06).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from shared import creds
from shared.geo import Grid

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVED_DIR = REPO_ROOT / "data" / "observed"

S1_COLLECTION = "COPERNICUS/S1_GRD"

VV_WATER_THRESHOLD_DB = -15.0
"""Backscatter below this counts as open water in VV polarisation.

-15 dB is the conventional operational threshold for VV open-water detection
and is the value used by the Copernicus Emergency Management Service rapid
mapping chain. It is a threshold, not a truth: wet soil, tarmac and radar
shadow also come back dark, which is exactly why we use change detection
rather than classifying a single image. Where a scene allows it, prefer
otsu_threshold() below, which derives the split from the image itself."""


# ==========================================================================
# Earth Engine
# ==========================================================================


def ee_init():
    """Authenticate Earth Engine from the service account in .env.

    Raises:
        RuntimeError: with the exact missing credential named, because "EE
            failed" with no detail costs an hour of somebody's evening.
    """
    import ee

    key_path = creds.ee_key_path()
    if not key_path.exists():
        raise RuntimeError(
            f"Earth Engine service-account key not found at {key_path}. "
            f"Set EE_SERVICE_ACCOUNT_KEY in .env - see .env.example."
        )
    email = creds.require("EE_SERVICE_ACCOUNT_EMAIL", who="06_gee_validation")
    project = creds.require("EE_PROJECT_ID", who="06_gee_validation")

    credentials = ee.ServiceAccountCredentials(email, str(key_path))
    ee.Initialize(credentials, project=project)
    return ee


def _s1_scene(ee, bbox, start: str, end: str, polarisation: str = "VV"):
    """Median Sentinel-1 GRD backscatter over a date window, in dB.

    Median over the window suppresses speckle, which is multiplicative noise
    that a single scene carries heavily. IW mode and a single orbit direction
    are enforced: mixing ascending and descending passes over mountains mixes
    two different geometries and the change detection becomes nonsense.
    """
    region = ee.Geometry.Rectangle(list(bbox))
    coll = (
        ee.ImageCollection(S1_COLLECTION)
        .filterBounds(region)
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarisation))
        .filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING"))
        .select(polarisation)
    )
    return coll.median().clip(region), coll.size()


def fetch_s1_pair(
    bbox: tuple[float, float, float, float],
    site: str,
    pre_start: str,
    pre_end: str,
    post_start: str,
    post_end: str,
    grid: Grid,
    polarisation: str = "VV",
    force: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Download pre- and post-event Sentinel-1 backscatter onto `grid`.

    Cached at data/observed/{site}/ so demo day needs no network.

    Returns:
        (pre_db, post_db, info) - both arrays on `grid`, values in dB, NaN
        where the sensor saw nothing. `info` records scene counts and dates,
        which go into the validation report: "median of 3 pre and 2 post
        scenes" is a materially different claim from "one scene each".
    """
    folder = OBSERVED_DIR / site
    folder.mkdir(parents=True, exist_ok=True)
    pre_path = folder / f"s1_pre_{pre_start}_{pre_end}.tif"
    post_path = folder / f"s1_post_{post_start}_{post_end}.tif"
    info_path = folder / "s1_info.json"

    if pre_path.exists() and post_path.exists() and not force:
        from .raster import read_to_grid

        info = json.loads(info_path.read_text()) if info_path.exists() else {}
        return read_to_grid(pre_path, grid), read_to_grid(post_path, grid), info

    ee = ee_init()
    pre_img, pre_n = _s1_scene(ee, bbox, pre_start, pre_end, polarisation)
    post_img, post_n = _s1_scene(ee, bbox, post_start, post_end, polarisation)

    n_pre = int(pre_n.getInfo())
    n_post = int(post_n.getInfo())
    if n_pre == 0 or n_post == 0:
        raise RuntimeError(
            f"Sentinel-1 returned {n_pre} pre-event and {n_post} post-event scenes "
            f"for {site}. Widen the date windows, or try the ASCENDING pass."
        )

    _download(ee, pre_img, bbox, grid, pre_path)
    _download(ee, post_img, bbox, grid, post_path)

    info = {
        "site": site,
        "collection": S1_COLLECTION,
        "polarisation": polarisation,
        "pre_window": [pre_start, pre_end],
        "post_window": [post_start, post_end],
        "n_scenes_pre": n_pre,
        "n_scenes_post": n_post,
        "orbit_pass": "DESCENDING",
        "reducer": "median",
    }
    info_path.write_text(json.dumps(info, indent=2))

    from .raster import read_to_grid

    return read_to_grid(pre_path, grid), read_to_grid(post_path, grid), info


def _download(ee, image, bbox, grid: Grid, out_path: Path) -> None:
    """Pull an Earth Engine image to a local GeoTIFF at our grid size."""
    import requests

    region = ee.Geometry.Rectangle(list(bbox))
    url = image.getDownloadURL(
        {
            "region": region,
            "dimensions": f"{grid.nx}x{grid.ny}",
            "format": "GEO_TIFF",
        }
    )
    resp = requests.get(url, timeout=600)
    if resp.status_code != 200 or resp.content[:2] not in (b"II", b"MM"):
        raise RuntimeError(
            f"Earth Engine download failed ({resp.status_code}) for {out_path.name}: "
            f"{resp.content[:300]!r}"
        )
    out_path.write_bytes(resp.content)


# ==========================================================================
# Water classification
# ==========================================================================


def otsu_threshold(values: np.ndarray) -> float:
    """Otsu's method: the threshold that best separates a bimodal histogram.

    A flooded SAR scene is bimodal - dark water, brighter land - so the split
    can be derived from the image instead of hard-coded. Preferred over the
    fixed -15 dB whenever the scene actually is bimodal; when it is not, Otsu
    returns a meaningless split, so the caller checks separability.

    Source: Otsu, N. (1979), "A Threshold Selection Method from Gray-Level
    Histograms", IEEE Transactions on Systems, Man, and Cybernetics, 9(1), 62-66.
    """
    v = values[np.isfinite(values)]
    if v.size < 100:
        return VV_WATER_THRESHOLD_DB

    hist, edges = np.histogram(v, bins=256)
    centres = 0.5 * (edges[:-1] + edges[1:])
    total = hist.sum()
    if total == 0:
        return VV_WATER_THRESHOLD_DB

    w0 = np.cumsum(hist) / total
    w1 = 1.0 - w0
    mu0 = np.cumsum(hist * centres) / np.maximum(np.cumsum(hist), 1)
    mu_t = (hist * centres).sum() / total
    mu1 = (mu_t - w0 * mu0) / np.maximum(w1, 1e-12)

    between = w0 * w1 * (mu0 - mu1) ** 2
    return float(centres[int(np.nanargmax(between))])


def classify_water(
    backscatter_db: np.ndarray, threshold_db: float | None = None
) -> np.ndarray:
    """Boolean open-water mask from SAR backscatter."""
    thr = VV_WATER_THRESHOLD_DB if threshold_db is None else threshold_db
    return np.isfinite(backscatter_db) & (backscatter_db < thr)


def flood_extent_from_change(
    pre_db: np.ndarray,
    post_db: np.ndarray,
    threshold_db: float | None = None,
    use_otsu: bool = True,
) -> tuple[np.ndarray, dict]:
    """Observed flood = water after, and NOT water before.

    Returns (flood_mask, info). `info` carries the threshold actually used and
    how it was chosen, because the threshold is the single biggest lever on the
    resulting CSI and it must never be silently tuned to flatter the model.
    """
    thr = threshold_db
    method = "fixed"
    if thr is None and use_otsu:
        thr = otsu_threshold(post_db)
        method = "otsu"
        # Otsu on a scene with almost no water drifts to a nonsense split.
        if not (-25.0 < thr < -8.0):
            thr = VV_WATER_THRESHOLD_DB
            method = "otsu_rejected_fell_back_to_fixed"
    if thr is None:
        thr = VV_WATER_THRESHOLD_DB

    water_pre = classify_water(pre_db, thr)
    water_post = classify_water(post_db, thr)
    flood = water_post & ~water_pre

    return flood, {
        "threshold_db": round(float(thr), 3),
        "threshold_method": method,
        "water_pre_cells": int(water_pre.sum()),
        "water_post_cells": int(water_post.sum()),
        "new_water_cells": int(flood.sum()),
    }


# ==========================================================================
# The accuracy metrics
# ==========================================================================


@dataclass
class AgreementMetrics:
    """Contingency-table agreement between a simulated and an observed extent.

    Definitions, so nobody has to guess which convention we used:
        hits (TP)          wet in both
        false_alarms (FP)  wet in simulation only
        misses (FN)        wet in observation only
        csi   = TP / (TP + FP + FN)     Critical Success Index, 0..1
        pod   = TP / (TP + FN)          Probability of Detection
        far   = FP / (TP + FP)          False Alarm Ratio
        bias  = (TP + FP) / (TP + FN)   >1 means we over-predict extent

    CSI is the headline because it penalises both over- and under-prediction,
    which POD alone does not: a model that floods the entire domain scores
    POD = 1.0 and is useless.
    """

    hits: int
    false_alarms: int
    misses: int
    correct_negatives: int
    csi: float
    pod: float
    far: float
    bias: float
    n_cells: int

    def as_dict(self) -> dict:
        return asdict(self)


def agreement(simulated_wet: np.ndarray, observed_wet: np.ndarray) -> AgreementMetrics:
    """Compare two boolean extent masks of identical shape.

    Raises:
        ValueError: on a shape mismatch. Silently resampling one onto the other
            is how a meaningless accuracy figure gets published.
    """
    sim = np.asarray(simulated_wet, dtype=bool)
    obs = np.asarray(observed_wet, dtype=bool)
    if sim.shape != obs.shape:
        raise ValueError(
            f"simulated {sim.shape} and observed {obs.shape} grids differ. Both "
            f"must be on the run's grid - see shared.geo.Grid."
        )

    tp = int((sim & obs).sum())
    fp = int((sim & ~obs).sum())
    fn = int((~sim & obs).sum())
    tn = int((~sim & ~obs).sum())

    denom_csi = tp + fp + fn
    return AgreementMetrics(
        hits=tp,
        false_alarms=fp,
        misses=fn,
        correct_negatives=tn,
        csi=round(tp / denom_csi, 4) if denom_csi else 0.0,
        pod=round(tp / (tp + fn), 4) if (tp + fn) else 0.0,
        far=round(fp / (tp + fp), 4) if (tp + fp) else 0.0,
        bias=round((tp + fp) / (tp + fn), 4) if (tp + fn) else 0.0,
        n_cells=sim.size,
    )


def validate_run(
    run_dir: str | Path,
    site: str,
    pre_window: tuple[str, str],
    post_window: tuple[str, str],
    event_name: str = "observed",
    force: bool = False,
    dem: np.ndarray | None = None,
) -> dict:
    """End to end: read a run, fetch the matching SAR pair, report agreement.

    Writes validation.json into the run folder and returns it.

    The returned dict always carries `caveats`. Read them out loud with the
    number - a CSI quoted without its caveats is a claim we cannot defend.
    """
    from shared.contract import WET_THRESHOLD_M
    from shared.io import read_grid, read_meta, write_json

    run_dir = Path(run_dir)
    meta = read_meta(run_dir)
    depth, grid = read_grid(run_dir, "max_depth")
    sim_wet = depth >= WET_THRESHOLD_M

    pre_db, post_db, scene_info = fetch_s1_pair(
        bbox=tuple(meta["domain"]["bbox"]),
        site=site,
        pre_start=pre_window[0],
        pre_end=pre_window[1],
        post_start=post_window[0],
        post_end=post_window[1],
        grid=grid,
        force=force,
    )

    # Terrain-masked change detection. Without the slope mask, radar shadow in
    # a gorge is classified as water and the metric is meaningless.
    # Pass `dem` in from the caller - it already has the conditioned terrain
    # from module 01 and re-deriving it here guesses at cache keys and fails
    # silently. Falling back to unmasked detection is allowed but it is
    # recorded in the output, because an unmasked CSI in mountains is not a
    # number anyone should quote.
    dem_for_mask = dem

    if dem_for_mask is not None and dem_for_mask.shape == depth.shape:
        observed_wet, thr_info = flood_extent_masked(
            pre_db, post_db, dem_for_mask, grid.cellsize_m()
        )
    else:
        observed_wet, thr_info = flood_extent_from_change(pre_db, post_db)
        thr_info["terrain_mask"] = "UNAVAILABLE - shadow false positives likely"

    metrics = agreement(sim_wet, observed_wet)

    # Sensitivity of the metric to the one tuned parameter. Published with the
    # result, always. A single CSI quoted without showing how it moves when the
    # slope mask changes is a number chosen rather than measured.
    sensitivity = []
    if dem_for_mask is not None and dem_for_mask.shape == depth.shape:
        for smax in (5, 10, 15, 20, 25, 30, 40, 90):
            obs_s, info_s = flood_extent_masked(
                pre_db, post_db, dem_for_mask, grid.cellsize_m(), max_slope_deg=smax
            )
            m_s = agreement(sim_wet, obs_s)
            sensitivity.append(
                {
                    "max_slope_deg": smax,
                    "floodable_cells": info_s["floodable_cells"],
                    "observed_flood_cells": info_s["new_water_cells"],
                    "csi": m_s.csi,
                    "pod": m_s.pod,
                    "far": m_s.far,
                    "bias": m_s.bias,
                }
            )

    terrain_note = None
    if dem_for_mask is not None and dem_for_mask.shape == depth.shape:
        slope = slope_degrees(dem_for_mask, grid.cellsize_m())
        median_slope = float(np.median(slope))
        if median_slope > 20.0:
            terrain_note = (
                f"Median terrain slope in this domain is {median_slope:.1f} degrees. "
                f"Sentinel-1 change detection is NOT reliable here: the flood "
                f"corridor is only one to three cells wide at this resolution and "
                f"radar shadow dominates the dark-pixel signal. Treat the CSI "
                f"below as inconclusive rather than as a measure of model skill, "
                f"and validate on a low-gradient reach instead."
            )

    payload = {
        "run_id": meta.get("run_id"),
        "event": event_name,
        "slope_mask_sensitivity": sensitivity,
        "terrain_warning": terrain_note,
        "observation": {**scene_info, **thr_info},
        "metrics": metrics.as_dict(),
        "simulated_wet_cells": int(sim_wet.sum()),
        "observed_wet_cells": int(observed_wet.sum()),
        "caveats": [
            "Sentinel-1 water detection is a backscatter threshold. Wet soil, "
            "smooth tarmac and radar shadow in steep terrain are also dark and "
            "can be misclassified as water.",
            "The satellite pass is at a fixed time and the flood is transient. "
            "A pass hours after the peak sees a receded extent, which appears "
            "as false alarms in our favour-free direction.",
            "Change detection removes permanent water, but it also removes any "
            "flooding already present in the pre-event window.",
            "This compares MAXIMUM simulated extent over the whole run against "
            "extent at one instant of observation. They are not the same "
            "quantity, and the bias figure should be read with that in mind.",
            "The scenario being validated must correspond to the event that was "
            "observed. Check meta.json scenario against the real event before "
            "quoting this number.",
        ],
    }
    write_json(run_dir, "validation.json", payload)
    return payload


# ==========================================================================
# Terrain masking - the step that makes SAR usable in a gorge
# ==========================================================================


def slope_degrees(dem: np.ndarray, cellsize_m: float) -> np.ndarray:
    """Surface slope in degrees, from the DEM gradient."""
    dz_dy, dz_dx = np.gradient(np.asarray(dem, dtype=np.float64), cellsize_m)
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))


def floodable_mask(
    dem: np.ndarray, cellsize_m: float, max_slope_deg: float = 10.0
) -> np.ndarray:
    """Cells where standing water is physically possible.

    This is not a convenience filter, it is a correctness fix. Sentinel-1
    measures backscatter, and a radar-shadowed slope returns almost nothing -
    so it is dark, and a dark-pixel water test classifies it as water. In flat
    country that barely matters. In the Teesta gorge it dominates: the raw
    threshold called 5,677 cells water BEFORE the flood, in a domain where the
    river occupies a few hundred. Those false positives then flow straight
    through the change detection and destroy the CSI.

    Water does not stand on a 40-degree valley wall. Excluding slopes above
    max_slope_deg removes shadow and layover artefacts while keeping every cell
    a flood could actually occupy.

    Standard practice; see for example Twele, A., Cao, W., Plank, S. &
    Martinis, S. (2016), "Sentinel-1-based flood mapping: a fully automated
    processing chain", International Journal of Remote Sensing, 37(13),
    2990-3004, which applies exactly this terrain masking (they use HAND and
    slope) ahead of thresholding.

    Args:
        dem: elevation on the run grid.
        cellsize_m: cell size.
        max_slope_deg: cells steeper than this cannot hold water. 10 degrees is
            already generous for standing floodwater.

    Returns:
        Boolean mask, True where flooding is possible.
    """
    return slope_degrees(dem, cellsize_m) <= max_slope_deg


def flood_extent_masked(
    pre_db: np.ndarray,
    post_db: np.ndarray,
    dem: np.ndarray,
    cellsize_m: float,
    threshold_db: float | None = None,
    max_slope_deg: float = 10.0,
) -> tuple[np.ndarray, dict]:
    """Change-detected flood extent, restricted to terrain that can flood.

    Same as flood_extent_from_change() with the terrain mask applied to both
    epochs, and the Otsu threshold derived only from floodable cells - which
    matters, because a histogram dominated by shadowed mountainside is not
    bimodal in the way the method assumes.
    """
    floodable = floodable_mask(dem, cellsize_m, max_slope_deg)

    thr = threshold_db
    method = "fixed"
    if thr is None:
        thr = otsu_threshold(post_db[floodable])
        method = "otsu_on_floodable"
        if not (-25.0 < thr < -8.0):
            thr = VV_WATER_THRESHOLD_DB
            method = "otsu_rejected_fell_back_to_fixed"

    water_pre = classify_water(pre_db, thr) & floodable
    water_post = classify_water(post_db, thr) & floodable
    flood = water_post & ~water_pre

    return flood, {
        "threshold_db": round(float(thr), 3),
        "threshold_method": method,
        "max_slope_deg": max_slope_deg,
        "floodable_cells": int(floodable.sum()),
        "water_pre_cells": int(water_pre.sum()),
        "water_post_cells": int(water_post.sum()),
        "new_water_cells": int(flood.sum()),
    }
