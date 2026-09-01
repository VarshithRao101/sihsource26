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

from shared.contract import DEM_SOURCES, ENGINES, FAILURE_MODES

FailureMode = Literal["overtopping", "piping", "gated_release", "blockage_breach"]


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
    cellsize_m: float = 90.0
    """Solver cell size. 90 m runs a 60 km reach in tens of seconds; 30 m is
    four times as many cells and roughly eight times the runtime because the
    timestep shrinks too."""

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

    blockage_height_m: float = 40.0
    """Height of the landslide debris above the river bed, for
    failure_mode = 'blockage_breach'.

    A natural dam has no published capacity, so this height plus the DEM is
    what determines the impounded volume - see modules/04_backend/blockage.py.
    Ignored for engineered-dam failure modes."""

    domain_bbox: tuple[float, float, float, float] | None = None
    """Explicit model domain, normally from module 01's plan_domain(), which
    traces the real channel with D8 instead of guessing a compass bearing.
    Leave None to fall back to the straight-corridor estimate - which is fine
    for a synthetic valley and wrong for any river that bends."""

    notes: str = ""
    tags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Every reason this scenario cannot be run. Empty list means good."""
        errs = list(self.site.validate())
        if self.failure_mode not in FAILURE_MODES:
            errs.append(f"failure_mode {self.failure_mode!r} not in {FAILURE_MODES}")
        if not 0.0 <= self.reservoir_level_frac <= 1.0:
            errs.append("reservoir_level_frac must be between 0 and 1")
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
        """The scenario block of meta.json."""
        block = {
            "failure_mode": self.failure_mode,
            "reservoir_level_frac": self.reservoir_level_frac,
            "breach_width_m": round(breach.average_width_m, 1),
            "breach_bottom_width_m": round(breach.bottom_width_m, 1),
            "breach_side_slope": breach.side_slope_h_per_v,
            "formation_time_hr": round(breach.formation_time_hr, 4),
            "breach_param_source": breach.source,
            "storage_curve": f"power law, k = {self.storage_exponent}",
            "inflow_cumecs": self.inflow_cumecs,
            "fingerprint": self.fingerprint(),
        }
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
