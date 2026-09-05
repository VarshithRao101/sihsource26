"""
modules/04_backend/scenario.py - what the user asks for.

One dataclass that describes a dam-break or river-blockage scenario completely
enough to run it, validate it, hash it, and put it in meta.json. The API takes
this as JSON, the ML surrogate takes it as a feature vector, and the Monte
Carlo perturbs it.

Owner: person 4 / captain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Literal

from shared.contract import (
    DAM_FAILURE_MODES,
    DEM_SOURCES,
    ENGINES,
    FAILURE_MODES,
    RIVER_FAILURE_MODES,
)

FailureMode = Literal[
    "overtopping",
    "piping",
    "foundation_failure",
    "spillway_blockage",
    "gated_release",
    "blockage_breach",
    "glof_moraine",
    "river_flood",
]


@dataclass
class SiteSpec:
    """A dam, or a landslide blockage point, and its reservoir."""

    name: str
    lat: float
    lon: float
    river: str = ""
    state: str = ""
    dam_height_m: float = 60.0
    reservoir_capacity_mcm: float = 5.0
    source: str = "user"
    """Where the site record came from: 'GRanD v1.3', 'CWC NRLD', 'user'."""

    kind: str = "engineered"
    """'engineered' or 'natural'. Decides what may be assumed about the site.

    An engineered dam has a published height, a gross storage and often a
    design spillway discharge. A natural one - a moraine, a landslide barrier -
    has a height somebody estimated off a satellite image and nothing else, so
    its impounded volume is read off the terrain instead. Treating the two
    alike is how an invented capacity ends up driving a breach regression."""

    crest_length_m: float | None = None
    """Length of the dam along its crest, m. From the CWC register's length
    column where it has one.

    Only failure_mode='foundation_failure' reads it, and that mode needs it:
    the opening left by a displaced block is a fraction of the crest, and there
    is no regression to fall back on if the crest length is unknown."""

    height_source: str = ""
    """How the barrier height was obtained - 'CWC NRLD 2019', 'surveyed',
    'estimated', 'reported'. Travels into meta.json so a result computed from
    a satellite-estimated height is never mistaken for one computed from a
    surveyed one."""

    def validate(self) -> list[str]:
        errs = []
        if not self.name.strip():
            errs.append("site.name is empty")
        if not -90 <= self.lat <= 90:
            errs.append(f"site.lat {self.lat} is not a latitude")
        if not -180 <= self.lon <= 180:
            errs.append(f"site.lon {self.lon} is not a longitude")
        if self.dam_height_m <= 0:
            errs.append("dam_height_m must be positive")
        if self.reservoir_capacity_mcm <= 0:
            errs.append("reservoir_capacity_mcm must be positive")
        return errs


@dataclass
class ScenarioSpec:
    """A complete, runnable failure scenario."""

    site: SiteSpec
    failure_mode: FailureMode = "overtopping"
    reservoir_level_frac: float = 1.0
    """How full the reservoir is at t = 0, as a fraction of dam height."""

    breach_regression: str = "froehlich2008"
    """Which breach-parameter regression to use: froehlich2008, vonthun1990,
    macdonald1984. All three are computed for the uncertainty band regardless;
    this picks the one that drives the deterministic run."""

    breach_width_m: float | None = None
    """Override the regression. Leave None to let the regression decide -
    and if you set it, meta.json records that a human overrode the physics."""
    formation_time_hr: float | None = None

    reach_length_km: float = 60.0
    corridor_width_km: float = 12.0
    cellsize_m: float = 60.0
    """Solver cell size, in metres.

    60 m because it is the coarsest grid that is CONVERGED, measured rather than
    chosen. docs/CONVERGENCE.md runs the same scenario at 120 / 90 / 60 / 45 m
    on two independent dams: refining 90 -> 60 m still moves max depth by 5.9%
    and 3.2%, while 60 -> 45 m moves it 1.1% and 0.6%. So 90 m - the old default
    - sits outside the converged range and carried several percent of pure
    discretisation error in every depth it produced.

    It was 90 m because the solver used to sweep the whole domain every step and
    finer grids cost what the domain cost. The windowed sweep made cost scale
    with the flood instead, which is what made this affordable.

    Flood EXTENT converges more slowly than depth and on one of the two sites had
    not converged even at 45 m, so area figures still carry a several-percent
    grid dependence. Depth figures no longer do."""

    end_hr: float = 12.0
    output_step_hr: float = 0.25
    engine: str = "fast"
    scheme: str = "swe"
    manning_n: float = 0.035
    inflow_cumecs: float = 0.0
    storage_exponent: float = 2.7

    dem_source: str = "SYNTHETIC"
    flow_bearing_deg: float = 180.0
    """Compass bearing the valley runs away from the dam. Module 01 replaces
    this with the real traced channel direction; it is only the first guess."""

    # --- controlled release, failure_mode = 'gated_release' -------------
    gate_opening_frac: float = 1.0
    """How far the outlet gates are opened, 0..1. 1.0 is a full emergency
    release. The dam does not fail in this mode - see
    shared.hydro.gated_release_hydrograph."""

    gate_open_time_hr: float = 0.5
    """How long the operator takes to wind the gates open."""

    design_spillway_cumecs: float | None = None
    """The structure's design discharge capacity, m3/s. Filled in from the CWC
    register when it has one; None means we fall back to a stated assumption."""

    target_release_cumecs: float | None = None
    """Override what the operator is aiming to pass. None uses the design
    capacity."""

    spillway_length_m: float = 60.0
    """Crest length of the uncontrolled spillway."""

    # --- foundation / abutment failure, 'foundation_failure' ------------
    foundation_breach_frac: float = 0.8
    """How much of the crest goes when the foundation shears, 0..1.

    0.8 rather than 1.0 because at St Francis the centre section was left
    standing and at Malpasset one abutment remained."""

    foundation_base_width_ratio: float = 0.25
    """Opening width at the bed as a fraction of its width at the crest.

    A concrete dam stands in a gorge, so the crest length is the valley width
    at the TOP. Treating the opening as a rectangle of crest width and dam
    height over-predicted the St Francis peak by a factor of 2.4."""

    collapse_time_min: float = 2.0
    """Minutes from first movement to the opening fully formed. Minutes, not
    hours: this is a structural collapse, not an erosion process."""

    # --- spillway or gate blockage, 'spillway_blockage' -----------------
    residual_spillway_frac: float = 0.0
    """What fraction of the design outlet capacity still works, 0..1.

    0.0 is a complete blockage. Banqiao's gates were silted, not removed, and
    South Fork's spillway was screened, not sealed, so intermediate values are
    the realistic ones."""

    blockage_start_level_frac: float = 0.85
    """How full the reservoir is when the outlets are lost. The fill phase
    starts here, which is why this is separate from reservoir_level_frac -
    that one describes a reservoir at the moment of failure, and in this mode
    the level at failure is whatever the filling reached."""

    blockage_height_m: float = 40.0
    """Height of the landslide debris above the river bed, for
    failure_mode = 'blockage_breach'.

    A natural dam has no published capacity, so this height plus the DEM is
    what determines the impounded volume - see modules/04_backend/blockage.py.
    Ignored for engineered-dam failure modes."""

    # --- glacial lake outburst, 'glof_moraine' --------------------------
    moraine_height_m: float = 30.0
    """Height of the moraine ridge above the downstream valley floor."""

    moraine_erodible_depth_m: float | None = None
    """How deep the breach can cut into the moraine. None -> 0.6 of the ridge
    height.

    Below this is the bedrock sill and any buried ice core, and the breach
    stops there. Using the full ridge height instead over-predicts both the
    head and the released volume."""

    glof_breach_width_m: float | None = None
    """Final breach bottom width, m. None -> one times the erodible depth.

    The most sensitive number in the mode: the published South Lhonak scenario
    table spans 4,311 / 8,000 / 12,487 m3/s for 20 / 30 / 40 m widths on the
    same lake."""

    avalanche_surge_frac: float = 0.0
    """Fraction of the lake displaced over the crest by an entering ice or rock
    mass, ahead of any breach.

    Zero by default. It is a volume over a duration, so it sets the peak
    directly and will dominate the breach whenever it is more than a few
    percent - which is a real effect and not a reason to leave it on."""

    avalanche_surge_duration_s: float = 600.0
    """How long the displacement wave takes to pass the crest."""

    lake_area_km2: float | None = None
    """Lake surface area. Used ONLY when the DEM cannot see the lake, in which
    case the volume falls back on Huggel et al. (2002) V = 0.104 A^1.42 - a
    relation with roughly a factor-of-two scatter, recorded as such."""

    # --- river flood wave, 'river_flood' --------------------------------
    peak_discharge_cumecs: float = 2000.0
    """Peak of the flood wave entering the reach. The one number a river run
    cannot be built without."""

    time_to_peak_hr: float = 3.0
    """Hours from the start of the rise to the peak."""

    flood_duration_hr: float | None = None
    """Total flood duration. None -> 2.67 times the time to peak, the NRCS
    dimensionless unit hydrograph ratio."""

    base_flow_cumecs: float = 0.0
    """Discharge in the channel before the flood arrives and after it passes."""

    source_kind: str = "dam"
    """'dam' or 'river'. What the operator selected, which decides which
    failure modes are legal.

    A river has no crest, no embankment, no foundation and no gates, so four of
    the eight modes have no physical referent on one. This is validated rather
    than left to the UI, because the API is reachable without the UI."""

    domain_bbox: tuple[float, float, float, float] | None = None
    """Explicit model domain, normally from module 01's plan_domain(), which
    traces the real channel with D8 instead of guessing a compass bearing.
    Leave None to fall back to the straight-corridor estimate - which is fine
    for a synthetic valley and wrong for any river that bends."""

    # --- SPH coupling, engine = 'sphcoupled' ---------------------------
    sph_run: str | None = None
    """Path to a finished module 02 SPH run folder whose hydrograph.csv becomes
    the near-field boundary of this simulation.

    The problem statement asks for the flood to be modelled THROUGH Smoothed
    Particle Hydrodynamics, and this is the join. SPH resolves the first minute
    or so of water tearing through the opening, which is exactly the part a
    level-pool reservoir model cannot see; level-pool resolves the hours of
    drawdown, which is exactly the part SPH cannot afford. Neither covers the
    other, so the coupled hydrograph uses each where it is valid.

    The two are spliced, never blended: SPH discharge up to the last second it
    simulated, the level-pool curve after that, and the step between them
    published in meta.json as a measured disagreement. Averaging them would
    produce a smooth curve that neither engine computed."""

    notes: str = ""
    tags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Every reason this scenario cannot be run. Empty list means good."""
        errs = list(self.site.validate())
        if self.failure_mode not in FAILURE_MODES:
            errs.append(f"failure_mode {self.failure_mode!r} not in {FAILURE_MODES}")
        elif self.source_kind == "river" and self.failure_mode not in RIVER_FAILURE_MODES:
            errs.append(
                f"failure_mode {self.failure_mode!r} needs a dam. A river reach "
                f"has no crest to overtop, no embankment to pipe through, no "
                f"foundation and no gates. Legal on a river: "
                f"{', '.join(RIVER_FAILURE_MODES)}."
            )
        elif self.source_kind == "dam" and self.failure_mode not in DAM_FAILURE_MODES:
            errs.append(
                f"failure_mode {self.failure_mode!r} is a river mode; it models "
                f"a flood arriving in a channel, not a structure failing. "
                f"Legal on a dam: {', '.join(DAM_FAILURE_MODES)}."
            )
        if self.source_kind not in ("dam", "river"):
            errs.append(f"source_kind {self.source_kind!r} must be 'dam' or 'river'")
        if not 0.0 <= self.reservoir_level_frac <= 1.0:
            errs.append("reservoir_level_frac must be between 0 and 1")
        if self.failure_mode == "foundation_failure":
            if not self.site.crest_length_m or self.site.crest_length_m <= 0:
                errs.append(
                    "foundation_failure needs site.crest_length_m - the opening "
                    "is a fraction of the crest and no regression substitutes "
                    "for it. The CWC register carries a length for most dams; "
                    "enter one by hand for a structure it does not list."
                )
            if not 0.0 < self.foundation_breach_frac <= 1.0:
                errs.append("foundation_breach_frac must be in (0, 1]")
            if not 0.0 < self.foundation_base_width_ratio <= 1.0:
                errs.append("foundation_base_width_ratio must be in (0, 1]")
            if self.collapse_time_min <= 0:
                errs.append("collapse_time_min must be positive")
        if self.failure_mode == "spillway_blockage":
            if not 0.0 <= self.residual_spillway_frac <= 1.0:
                errs.append("residual_spillway_frac must be between 0 and 1")
            if not 0.0 <= self.blockage_start_level_frac <= 1.0:
                errs.append("blockage_start_level_frac must be between 0 and 1")
            if self.inflow_cumecs <= 0:
                errs.append(
                    "spillway_blockage needs inflow_cumecs above zero - with no "
                    "inflow the reservoir never rises and nothing overtops. "
                    "That is the point of the mode."
                )
        if self.failure_mode == "glof_moraine":
            if self.moraine_height_m <= 0:
                errs.append("moraine_height_m must be positive")
            if not 0.0 <= self.avalanche_surge_frac <= 1.0:
                errs.append("avalanche_surge_frac must be between 0 and 1")
            if (
                self.moraine_erodible_depth_m is not None
                and self.moraine_erodible_depth_m > self.moraine_height_m
            ):
                errs.append(
                    "moraine_erodible_depth_m cannot exceed moraine_height_m - "
                    "the breach cannot cut below the bedrock sill"
                )
        if self.failure_mode == "river_flood":
            if self.peak_discharge_cumecs <= 0:
                errs.append("river_flood needs peak_discharge_cumecs above zero")
            if self.time_to_peak_hr <= 0:
                errs.append("time_to_peak_hr must be positive")
            if self.base_flow_cumecs < 0:
                errs.append("base_flow_cumecs cannot be negative")
            if self.base_flow_cumecs >= self.peak_discharge_cumecs:
                errs.append("base_flow_cumecs must be below peak_discharge_cumecs")
            if (
                self.flood_duration_hr is not None
                and self.flood_duration_hr <= self.time_to_peak_hr
            ):
                errs.append("flood_duration_hr must exceed time_to_peak_hr")
        if self.engine not in ENGINES:
            errs.append(f"engine {self.engine!r} not in {ENGINES}")
        if self.scheme not in ("swe", "inertial"):
            errs.append(f"scheme {self.scheme!r} must be 'swe' or 'inertial'")
        if self.dem_source not in DEM_SOURCES:
            errs.append(f"dem_source {self.dem_source!r} not in {DEM_SOURCES}")
        if self.cellsize_m <= 0:
            errs.append("cellsize_m must be positive")
        if self.end_hr <= 0:
            errs.append("end_hr must be positive")
        if self.reach_length_km <= 0:
            errs.append("reach_length_km must be positive")
        if self.breach_width_m is not None and self.breach_width_m <= 0:
            errs.append("breach_width_m override must be positive")
        if self.engine == "sphcoupled":
            # Fail here rather than forty seconds into a solve that was never
            # going to be an SPH-coupled run.
            if not self.sph_run:
                errs.append(
                    "engine 'sphcoupled' needs sph_run pointing at a finished "
                    "module 02 SPH run folder"
                )
            else:
                from pathlib import Path as _P

                if not (_P(self.sph_run) / "hydrograph.csv").is_file():
                    errs.append(f"no hydrograph.csv in sph_run {self.sph_run!r}")
        if self.sph_run and self.engine != "sphcoupled":
            errs.append("sph_run is set but engine is not 'sphcoupled'")
        return errs

    def require_valid(self) -> None:
        errs = self.validate()
        if errs:
            raise ValueError("invalid scenario:\n  - " + "\n  - ".join(errs))

    @property
    def capacity_m3(self) -> float:
        return self.site.reservoir_capacity_mcm * 1e6

    @property
    def water_volume_m3(self) -> float:
        """Reservoir volume actually available to escape, m3.

        Uses the same power-law storage curve as shared.hydro, so the number
        that feeds the breach regression is the same number the routing drains.
        """
        return self.capacity_m3 * self.reservoir_level_frac**self.storage_exponent

    @property
    def site_slug(self) -> str:
        import re

        slug = re.sub(r"[^a-z0-9]", "", self.site.name.lower())
        return slug or "site"

    @property
    def scenario_slug(self) -> str:
        from shared.contract import FAILURE_MODE_TO_SLUG

        return FAILURE_MODE_TO_SLUG[self.failure_mode]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """The model domain, derived from the dam and the reach.

        One definition, used by the solver, by module 01's terrain fetch and by
        the exposure download. If these ever disagree, the flood grids and the
        village list are describing different pieces of ground.

        A domain_bbox set by plan_domain() wins, because it was derived from the
        river the water will actually follow.
        """
        if self.domain_bbox is not None:
            return tuple(self.domain_bbox)

        from shared.geo import bbox_downstream

        return bbox_downstream(
            self.site.lon,
            self.site.lat,
            self.reach_length_km,
            self.corridor_width_km,
            self.flow_bearing_deg,
        )

    def fingerprint(self) -> str:
        """Stable 12-character hash of everything that affects the result.

        Two scenarios with the same fingerprint must produce the same run, so
        the API can cache on it and the ML training set can deduplicate on it.
        Deliberately excludes `notes` and `tags`.
        """
        payload = asdict(self)
        payload.pop("notes", None)
        payload.pop("tags", None)
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def to_meta_scenario(self, breach) -> dict:
        """The scenario block of meta.json.

        The breach fields are written only for a mode in which something
        actually breached. A controlled release opens no breach and a river
        flood has no barrier to breach, so publishing a width for either would
        be a number no part of the run computed - and the rule in this
        repository is that blank beats invented.
        """
        from shared.contract import BREACHING_MODES

        block = {
            "failure_mode": self.failure_mode,
            "source_kind": self.source_kind,
            "reservoir_level_frac": self.reservoir_level_frac,
            "storage_curve": f"power law, k = {self.storage_exponent}",
            "inflow_cumecs": self.inflow_cumecs,
            "fingerprint": self.fingerprint(),
        }
        if self.failure_mode in BREACHING_MODES:
            block.update(
                {
                    "breach_width_m": round(breach.average_width_m, 1),
                    "breach_bottom_width_m": round(breach.bottom_width_m, 1),
                    "breach_side_slope": breach.side_slope_h_per_v,
                    "formation_time_hr": round(breach.formation_time_hr, 4),
                    "breach_param_source": breach.source,
                }
            )
        else:
            block["breach"] = (
                "none - nothing breached in this mode, so no breach geometry "
                "and no breach regression exist for this run"
            )
        if self.breach_width_m is not None:
            block["breach_width_overridden_by_user"] = True
        if self.formation_time_hr is not None:
            block["formation_time_overridden_by_user"] = True
        return block

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "ScenarioSpec":
        data = dict(payload)
        site = data.pop("site")
        return cls(site=SiteSpec(**site) if isinstance(site, dict) else site, **data)


# --------------------------------------------------------------------------
# A couple of real Indian sites, for the demo and the tests
# --------------------------------------------------------------------------
#
# Coordinates and dam figures below are from public records - GRanD v1.3, the
# CWC National Register of Large Dams, and the post-event reports on the
# October 2023 Teesta GLOF. Anything we are not sure of is left at the default
# and flagged in `source`. Never add a site here with an invented number.

DEMO_SITES = {
    "chungthang": SiteSpec(
        name="Chungthang",
        lat=27.6003,
        lon=88.6428,
        river="Teesta",
        state="Sikkim",
        dam_height_m=60.0,
        reservoir_capacity_mcm=5.0,
        source="Teesta-III post-event reports, Oct 2023 GLOF; verify before citing",
    ),
    "rishiganga": SiteSpec(
        name="Rishiganga",
        lat=30.5000,
        lon=79.7000,
        river="Rishiganga",
        state="Uttarakhand",
        dam_height_m=32.0,
        reservoir_capacity_mcm=1.0,
        source="Feb 2021 Chamoli event; run-of-river, capacity approximate",
    ),
}
