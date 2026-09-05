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

FAILURE_MODES = (
    "overtopping",
    "piping",
    "foundation_failure",
    "spillway_blockage",
    "gated_release",
    "blockage_breach",
    "glof_moraine",
    "river_flood",
)
"""Eight ways the water gets out, and each one is a DIFFERENT calculation -
not a label on the same hydrograph.

The first four were the whole list for most of this project, and that was a
real limitation rather than a simplification: it meant every concrete-dam
failure in the historical record had to be forced through an embankment
erosion regression that was never calibrated on concrete, and it meant a
blocked spillway - the mechanism that actually destroyed Banqiao and South
Fork - was indistinguishable from ordinary overtopping.

  overtopping         Water passes the crest and scours the embankment away.
                      Progressive erosion, Froehlich k0 = 1.3.
  piping              Internal erosion opens a conduit that enlarges and then
                      collapses. Orifice flow first, weir flow after.
  foundation_failure  The foundation or abutment shears and the structure goes
                      as a block. There is no erosion phase at all, so no
                      embankment regression applies - see
                      shared.hydro.foundation_collapse_hydrograph.
  spillway_blockage   The reservoir cannot discharge. Level rises on inflow
                      until it reaches the crest, and only then does an
                      overtopping breach begin. The extra quantity this mode
                      computes and no other does is the time the operator had.
  gated_release       Controlled release through the outlets. The dam does not
                      fail.
  blockage_breach     A landslide barrier across the river fails. No design
                      capacity exists, so the DEM supplies the storage.
  glof_moraine        A glacial lake bursts through its moraine. The erodible
                      depth is the moraine, not the whole barrier height, and
                      an avalanche wave can displace part of the lake before
                      the breach has opened at all.
  river_flood         No dam and no barrier: a flood wave routed down a river
                      from an injected hydrograph. This is the only mode that
                      does not begin with something failing.
"""

DAM_FAILURE_MODES = (
    "overtopping",
    "piping",
    "foundation_failure",
    "spillway_blockage",
    "gated_release",
    "blockage_breach",
    "glof_moraine",
)
"""Modes selectable when the source is a structure with a reservoir behind it."""

RIVER_FAILURE_MODES = ("river_flood", "blockage_breach", "glof_moraine")
"""Modes selectable when the source is a river reach.

A river has no crest to overtop, no embankment to pipe through, no foundation
and no gates, so four of the eight modes are not merely unlikely on a river -
they have no physical referent there and the validator rejects them.
"""

FAILURE_MODE_INFO = {
    # label      : what the operator picks it by
    # summary    : one line under the picker
    # physics    : the calculation this mode runs that no other mode runs
    # controls   : which basic inputs are meaningful, so the UI shows those and
    #              hides the rest instead of greying out nine boxes
    # reference  : a real failure this mode was written against, so the choice
    #              can be checked rather than believed
    "overtopping": {
        "label": "Overtopping - water passes the crest",
        "summary": (
            "Inflow exceeds what the spillway can pass and the water cuts the "
            "embankment away from the top down."
        ),
        "physics": (
            "Progressive erosion. Froehlich (2008) with k0 = 1.3 and a 1:1 side "
            "slope, weir flow through a trapezoid that widens and deepens over "
            "the formation time."
        ),
        "controls": ["reservoir_level_frac"],
        "reference": (
            "Machchhu II, Morbi, 1979 - 600 mm in 24 h, inflow near three times "
            "spillway capacity, 0.6 m over the flanks."
        ),
        "structure_types": ["embankment", "earthfill", "any"],
    },
    "piping": {
        "label": "Piping - internal erosion through the body",
        "summary": (
            "Seepage opens a conduit inside the dam that enlarges until the "
            "roof above it collapses."
        ),
        "physics": (
            "Orifice flow while the pipe is submerged, switching to weir flow "
            "at roof collapse (half the formation time). Froehlich k0 = 1.0, "
            "0.7 side slope - a piping breach is narrower and steeper than an "
            "overtopping one."
        ),
        "controls": ["reservoir_level_frac"],
        "reference": (
            "Teton, Idaho, 1976 - reservoir water bypassed the grout curtain "
            "through fractured rhyolite on first filling."
        ),
        "structure_types": ["embankment", "earthfill", "any"],
    },
    "foundation_failure": {
        "label": "Foundation or abutment failure - the structure goes as a block",
        "summary": (
            "The rock the dam stands on shears or dissolves and the structure "
            "is displaced whole. Nothing erodes."
        ),
        "physics": (
            "No erosion phase and NO embankment regression - Froehlich, Von "
            "Thun and MacDonald are all fitted to earthfill dams and none of "
            "them describes a concrete monolith being pushed off its "
            "foundation. The opening is a stated fraction of the crest length "
            "over the full dam height, formed in minutes, and the release is a "
            "near-instantaneous dam-break wave."
        ),
        "controls": [
            "reservoir_level_frac", "foundation_breach_frac", "collapse_time_min",
        ],
        "reference": (
            "St Francis, California, 1928 (gypsum veins dissolved in the "
            "western abutment) and Malpasset, France, 1959 (foundation shear "
            "under hydrostatic uplift)."
        ),
        "structure_types": ["concrete gravity", "arch", "masonry"],
    },
    "spillway_blockage": {
        "label": "Spillway or gates blocked - the reservoir cannot discharge",
        "summary": (
            "Debris, silt or a jammed gate takes the outlet capacity away. The "
            "level climbs on inflow until it reaches the crest."
        ),
        "physics": (
            "Two phases. First a reservoir mass balance at the residual outlet "
            "capacity, which yields the one number no other mode produces - the "
            "hours between the blockage and the first water over the crest, "
            "which is the warning time the operator actually had. Then an "
            "overtopping breach from a full reservoir with the inflow still "
            "arriving, which is why peaks in this mode exceed what the stored "
            "volume alone would give."
        ),
        "controls": [
            "residual_spillway_frac", "inflow_cumecs", "reservoir_level_frac",
        ],
        "reference": (
            "Banqiao, Henan, 1975 - sluice gates silted shut before 1,060 mm of "
            "Typhoon Nina arrived. South Fork, Pennsylvania, 1889 - fish "
            "screens across the spillway and a crest lowered for a carriage "
            "road."
        ),
        "structure_types": ["any"],
    },
    "gated_release": {
        "label": "Controlled release - gates opened, dam intact",
        "summary": "The operator passes water on purpose. Nothing fails.",
        "physics": (
            "Gate-opening curve into orifice discharge, bounded by the design "
            "spillway capacity from the CWC register where the structure has "
            "one. No breach exists and meta.json says so."
        ),
        "controls": ["reservoir_level_frac", "gate_opening_frac"],
        "reference": (
            "Routine pre-monsoon drawdown; also the counterfactual against "
            "which a failure run is read."
        ),
        "structure_types": ["any"],
    },
    "blockage_breach": {
        "label": "Landslide dam across the river",
        "summary": (
            "A debris barrier blocks the channel, impounds a lake, and then "
            "fails."
        ),
        "physics": (
            "The storage is READ OFF THE TERRAIN by filling the valley behind "
            "the barrier to the debris height, because no natural dam has a "
            "published capacity. Breach through non-cohesive debris."
        ),
        "controls": ["blockage_height_m"],
        "reference": (
            "Phuktal / Tsarap Chu, Ladakh, 2015 - overtopped after 110 days "
            "behind a reported 15 km lake."
        ),
        "structure_types": ["natural"],
    },
    "glof_moraine": {
        "label": "Glacial lake outburst through a moraine",
        "summary": (
            "A moraine-dammed lake bursts, often after an avalanche or icefall "
            "drops into it."
        ),
        "physics": (
            "Two things separate this from a landslide dam. The erodible depth "
            "is the MORAINE freeboard, not the whole barrier height - the ice "
            "core and bedrock sill below it do not go - so the breach bottoms "
            "out early. And an avalanche wave can displace part of the lake "
            "over the crest before the breach has opened at all, which arrives "
            "as a separate leading surge rather than as a taller breach peak. "
            "Lake volume falls back on Huggel et al. (2002) V = 0.104 A^1.42 "
            "when the DEM cannot see the lake."
        ),
        "controls": [
            "moraine_height_m", "moraine_erodible_depth_m", "avalanche_surge_frac",
        ],
        "reference": (
            "South Lhonak, Sikkim, October 2023 - lake grew 1.12 to 1.63 km2 "
            "between 2016 and 2023, then drained into the Teesta and took "
            "Teesta III with it."
        ),
        "structure_types": ["natural", "moraine"],
    },
    "river_flood": {
        "label": "River flood wave - no dam involved",
        "summary": (
            "A flood hydrograph enters the reach and is routed downstream. "
            "Nothing fails, because there is nothing there to fail."
        ),
        "physics": (
            "The only mode with no barrier and no breach. The inflow is a "
            "dimensionless hydrograph scaled to a peak discharge and a time to "
            "peak (NRCS NEH-4 shape, recession 1.67x the rise), injected at the "
            "chosen point. Where the water goes is decided entirely by the DEM "
            "- the operator does not and cannot set a direction, because a "
            "river's course is a property of the ground and not of the run."
        ),
        "controls": [
            "peak_discharge_cumecs", "time_to_peak_hr", "flood_duration_hr",
        ],
        "reference": (
            "The routing half of every event in the record; the mode to use "
            "when the question is 'this much water arrives here, who is in the "
            "way'."
        ),
        "structure_types": ["none"],
    },
}
"""Per-mode metadata, in the contract so the API, the UI and the validator all
read the same description. `controls` is what makes the input panel honest: a
mode shows the inputs that change its answer and hides the ones that do
nothing, rather than presenting nine boxes of which two matter."""


BREACHING_MODES = (
    "overtopping",
    "piping",
    "foundation_failure",
    "spillway_blockage",
    "blockage_breach",
    "glof_moraine",
)
"""Modes in which a barrier actually fails, so a breach geometry exists.
'gated_release' and 'river_flood' have no breach and meta.json says so."""

# ASTER is here because NTRO's dataset link names it. Adding a source is
# additive - an old run's meta.json still validates, because nothing that was
# legal before became illegal.
DEM_SOURCES = (
    "FABDEM", "COP30", "SRTM", "NASADEM", "CartoDEM", "ALOS", "ASTER", "SYNTHETIC",
)

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

SCENARIO_SLUGS = (
    "overtop", "piping", "foundation", "spillblock", "gated", "blockage",
    "glof", "riverflood",
)

FAILURE_MODE_TO_SLUG = {
    "overtopping": "overtop",
    "piping": "piping",
    "foundation_failure": "foundation",
    "spillway_blockage": "spillblock",
    "gated_release": "gated",
    "blockage_breach": "blockage",
    "glof_moraine": "glof",
    "river_flood": "riverflood",
}
