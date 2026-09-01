"""
shared/contract.py - the data contract, as constants.

Every module imports from here. Nobody reimplements these numbers.
If a value in this file is wrong, six people are wrong in the same way,
which is the point: we find it once instead of five times.

Owner: captain / person 4. Nobody else edits this file.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA_VERSION = "2.0"
"""2.0 adds the ML layer (modules/07_ml) artefacts and the uncertainty block.
1.0 run folders still validate; the extra keys are optional."""


# --------------------------------------------------------------------------
# Physics constants
# --------------------------------------------------------------------------

GRAVITY = 9.80665
"""m/s^2. Standard gravity, ISO 80000-3."""

WET_THRESHOLD_M = 0.05
"""m. Below this depth a cell counts as dry, everywhere, in every module.
Chosen to sit above DEM vertical noise on 30 m SRTM/COP30 while still
catching sheet flow. Used for arrival time, extent polygons, wet-cell counts."""

DRY_VELOCITY_EPS = 1e-6
"""m/s. Velocity below this is treated as zero on a dry cell, to stop the
h -> 0 division in the momentum update producing garbage."""

MIN_DEPTH_FOR_VELOCITY_M = 0.01
"""m. Depth floor used as the denominator when converting discharge per unit
width (q) back to velocity. Prevents v = q/h exploding on a drying front."""

DEFAULT_MANNING_N = 0.035
"""s/m^(1/3). Natural stream, clean, winding, some pools and shoals.
Source: Chow, V.T. (1959) "Open-Channel Hydraulics", Table 5-6, item D-1-b.
Only a fallback - module 01 writes a per-cell Manning raster from land cover."""

MANNING_N_BY_LANDCOVER = {
    # ESA WorldCover 2021 v200 class code -> Manning's n
    # Source: Chow (1959) Table 5-6; Arcement & Schneider (1989), USGS WSP 2339.
    10: 0.100,   # Tree cover        - heavy timber, few down trees
    20: 0.050,   # Shrubland         - medium to dense brush
    30: 0.035,   # Grassland         - high grass
    40: 0.040,   # Cropland          - mature field crops
    50: 0.150,   # Built-up          - urban, obstructed (Kalyanapu et al. 2009)
    60: 0.025,   # Bare / sparse     - clean earth
    70: 0.025,   # Snow and ice
    80: 0.030,   # Permanent water   - channel, clean and straight
    90: 0.045,   # Herbaceous wetland
    95: 0.070,   # Mangroves
    100: 0.035,  # Moss and lichen
}

MANNING_N_BOUNDS = (0.010, 0.200)
"""Hard clamp. Anything outside this is a bug, not a landscape."""


# --------------------------------------------------------------------------
# Enumerations - the validator rejects anything not in these tuples
# --------------------------------------------------------------------------

ENGINES = ("fast", "delft3d", "sph", "sphcoupled", "surrogate")
"""'surrogate' is new in schema 2.0: a run produced by the ML emulator
(modules/07_ml). It is a prediction, not a simulation, and the frontend
labels it as such."""

FAILURE_MODES = ("overtopping", "piping", "gated_release", "blockage_breach")

DEM_SOURCES = ("FABDEM", "COP30", "SRTM", "NASADEM", "CartoDEM", "ALOS", "SYNTHETIC")

BATHYMETRY = ("none", "estimated", "surveyed")

DELIVERY_CRS = "EPSG:4326"
"""Everything leaves a module in WGS84 lat/lon. Model in UTM if you want;
reproject on write, never before."""


# --------------------------------------------------------------------------
# Run folder layout
# --------------------------------------------------------------------------

REQUIRED_GRIDS = ("max_depth", "arrival_time", "time_of_peak", "max_velocity")
OPTIONAL_GRIDS = ("duration", "max_dv", "hazard_class")

GRID_DRY_VALUE = {
    # what a cell that never got wet contains
    "max_depth": 0.0,
    "arrival_time": float("nan"),
    "time_of_peak": float("nan"),
    "max_velocity": 0.0,
    "duration": 0.0,
    "max_dv": 0.0,
    "hazard_class": 0.0,
}

GRID_UNITS = {
    "max_depth": "m",
    "arrival_time": "hr",
    "time_of_peak": "hr",
    "max_velocity": "m/s",
    "duration": "hr",
    "max_dv": "m2/s",
    "hazard_class": "class_index",
}

REQUIRED_FILES = (
    "meta.json",
    "max_depth.tif",
    "arrival_time.tif",
    "time_of_peak.tif",
    "max_velocity.tif",
    "hydrograph.csv",
    "extent.geojson",
)

OPTIONAL_FILES = (
    "duration.tif",
    "max_dv.tif",
    "impact.json",
    "packed.png",
    "uncertainty.json",
    "frames/",
)

HYDROGRAPH_COLUMNS = ("time_hr", "discharge_cumecs")

RASTER_DTYPE = "float32"
RASTER_COMPRESS = "LZW"


# --------------------------------------------------------------------------
# meta.json required keys - dotted paths, checked by shared.validate
# --------------------------------------------------------------------------

REQUIRED_META_KEYS = (
    "schema_version",
    "run_id",
    "created_utc",
    "engine",
    "is_fake",
    "site.name",
    "site.lat",
    "site.lon",
    "scenario.failure_mode",
    "domain.bbox",
    "domain.crs",
    "domain.cellsize_m",
    "time.start_hr",
    "time.end_hr",
    "results.runtime_s",
    "provenance.module",
)


# --------------------------------------------------------------------------
# Hazard classification
# --------------------------------------------------------------------------
#
# Source: Australian Disaster Resilience Guideline 7-3, "Flood Hazard" (2017),
# Table 1, derived from Smith, G.P., Davey, E.K. & Cox, R. (2014) "Flood
# Hazard", WRL Technical Report 2014/07, UNSW Water Research Laboratory.
#
# The Australian H1-H6 scale is collapsed to the four classes the contract
# carries. A cell takes the first class whose three limits it satisfies.
#
#   H1  DV <= 0.3, D <= 0.3, V <= 2.0   generally safe                 -> low
#   H2  DV <= 0.6, D <= 0.5, V <= 2.0   unsafe for small vehicles      -> moderate
#   H3  DV <= 0.6, D <= 1.2, V <= 2.0   unsafe for vehicles/children   -> moderate
#   H4  DV <= 1.0, D <= 2.0, V <= 2.0   unsafe for people and vehicles -> significant
#   H5  DV <= 4.0, D <= 4.0, V <= 4.0   unsafe for all, buildings hit  -> extreme
#   H6  DV  > 4.0                       all building types may fail    -> extreme

HAZARD_CLASSES = ("none", "low", "moderate", "significant", "extreme")
"""Index into this tuple is what hazard_class.tif stores. 0 = none (dry)."""

HAZARD_THRESHOLDS = (
    # (name, max_dv_m2s, max_depth_m, max_velocity_ms)
    ("low", 0.3, 0.3, 2.0),
    ("moderate", 0.6, 1.2, 2.0),
    ("significant", 1.0, 2.0, 2.0),
    ("extreme", float("inf"), float("inf"), float("inf")),
)


def hazard_class(depth_m: float, velocity_ms: float) -> str:
    """Classify a single cell. See HAZARD_THRESHOLDS for the source.

    >>> hazard_class(0.0, 0.0)
    'none'
    >>> hazard_class(0.2, 0.5)
    'low'
    >>> hazard_class(8.0, 3.0)
    'extreme'
    """
    if depth_m < WET_THRESHOLD_M:
        return "none"
    dv = depth_m * velocity_ms
    for name, dv_max, d_max, v_max in HAZARD_THRESHOLDS:
        if dv <= dv_max and depth_m <= d_max and velocity_ms <= v_max:
            return name
    return "extreme"


# --------------------------------------------------------------------------
# Numerics - the fast solver's operating envelope
# --------------------------------------------------------------------------

CFL_DEFAULT = 0.45
"""Courant number for the explicit shallow-water update. The stability limit
for the first-order upwind scheme in modules/04_backend is 0.5; 0.45 leaves
headroom for the wet/dry front."""

MAX_TIMESTEP_S = 30.0
MIN_TIMESTEP_S = 1e-4
"""If dt falls below this the solver has gone unstable. Abort - do not limp on
producing numbers nobody should trust."""

MASS_BALANCE_TOLERANCE_PCT = 1.0
"""A run whose volume error exceeds this fails the validator. We report the
actual figure in meta.json results.mass_balance_err_pct - never round it to
zero, never omit it."""


# --------------------------------------------------------------------------
# run_id
# --------------------------------------------------------------------------

RUN_ID_PATTERN = r"^[a-z0-9]+_[a-z0-9]+_[a-z0-9]+_\d{3}$"
"""{site}_{scenario}_{engine}_{nnn}, e.g. teesta_overtop_fast_001."""

SCENARIO_SLUGS = ("overtop", "piping", "gated", "blockage")

FAILURE_MODE_TO_SLUG = {
    "overtopping": "overtop",
    "piping": "piping",
    "gated_release": "gated",
    "blockage_breach": "blockage",
}
