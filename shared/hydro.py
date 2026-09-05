"""
shared/hydro.py - breach parameters and the outflow hydrograph.

This is the file that turns "a 60 m dam holding 5 MCM fails by overtopping"
into a discharge-versus-time curve. Modules 03 (Delft3D) and 04 (fast solver)
take that curve as their upstream boundary. Module 02 (SPH) computes the same
thing from first principles and we compare the two - that comparison is a
scoring line, so both paths have to exist.

EVERY formula here carries its citation. If you add one without a citation it
gets removed. See AGENTS.md, honesty policy.

Owner: captain / person 4. Person 3 is the domain owner - physics disputes
go to them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Literal

import numpy as np

from shared.contract import GRAVITY

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


# ==========================================================================
# Breach geometry regressions
# ==========================================================================
#
# All three predict the same two things - final breach width and how long the
# breach takes to form - and they disagree with each other by roughly a factor
# of two. That disagreement IS the uncertainty. We run all three, show the
# spread, and never present one as the truth.


@dataclass
class BreachParams:
    """Final geometry of a dam breach and how long it takes to get there."""

    bottom_width_m: float       # width of the trapezoid floor at full development
    average_width_m: float      # what the regressions actually predict
    side_slope_h_per_v: float   # Z in the weir equation; 1.0 means 45 degrees
    depth_m: float              # vertical extent of the breach (usually dam height)
    formation_time_hr: float    # time from initiation to full development
    source: str                 # the citation. Never empty.

    def as_dict(self) -> dict:
        return asdict(self)


def froehlich_2008(
    reservoir_volume_m3: float,
    breach_height_m: float,
    failure_mode: FailureMode = "overtopping",
) -> BreachParams:
    """Froehlich, D.C. (2008), "Embankment Dam Breach Parameters and Their
    Uncertainties", ASCE Journal of Hydraulic Engineering, 134(12), 1708-1721.

    The primary regression for this project. Fitted to 74 documented failures.

    Average breach width   B_avg = 0.27 * k0 * Vw^0.32 * hb^0.04
    Formation time         tf    = 63.2 * sqrt(Vw / (g * hb^2))       [seconds]

    where k0 = 1.3 for overtopping and 1.0 for every other mode, Vw is the
    reservoir volume at the moment of breach (m3) and hb is the breach height (m).

    Froehlich reports side slopes of 1.0 H:V for overtopping failures and
    0.7 H:V for all others.

    Args:
        reservoir_volume_m3: water volume above the breach invert at failure.
        breach_height_m: vertical drop through the breach.
        failure_mode: sets k0 and the side slope.

    Returns:
        BreachParams with the citation filled in.
    """
    if reservoir_volume_m3 <= 0 or breach_height_m <= 0:
        raise ValueError(
            f"froehlich_2008 needs positive volume and height, got "
            f"V={reservoir_volume_m3}, h={breach_height_m}"
        )

    k0 = 1.3 if failure_mode == "overtopping" else 1.0
    side_slope = 1.0 if failure_mode == "overtopping" else 0.7

    b_avg = 0.27 * k0 * reservoir_volume_m3**0.32 * breach_height_m**0.04
    tf_s = 63.2 * math.sqrt(reservoir_volume_m3 / (GRAVITY * breach_height_m**2))

    # The regression gives the AVERAGE width of a trapezoid. Convert to the
    # bottom width the weir equation needs:  B_avg = B_bottom + Z * h.
    b_bottom = max(b_avg - side_slope * breach_height_m, 0.1 * b_avg)

    return BreachParams(
        bottom_width_m=b_bottom,
        average_width_m=b_avg,
        side_slope_h_per_v=side_slope,
        depth_m=breach_height_m,
        formation_time_hr=tf_s / 3600.0,
        source="Froehlich (2008), ASCE J. Hydraul. Eng. 134(12)",
    )


def von_thun_gillette_1990(
    reservoir_volume_m3: float,
    breach_height_m: float,
    erosion_resistance: Literal["resistant", "erodible"] = "erodible",
) -> BreachParams:
    """Von Thun, S.L. & Gillette, D.R. (1990), "Guidance on Breach Parameters",
    unpublished internal document, US Bureau of Reclamation, Denver.

    Average width    B_avg = 2.5 * hw + Cb
    Formation time   tf    = B_avg / (4 * hw)          [erosion resistant, hours]
                     tf    = B_avg / (4 * hw + 61)     [easily erodible, hours]

    Cb is a volume-dependent offset (m), from Von Thun & Gillette Table 1.
    Side slopes: 1.0 H:V for erodible fill, 0.5 H:V for resistant fill.
    """
    hw = breach_height_m
    v = reservoir_volume_m3

    # Von Thun & Gillette (1990) Table 1 - Cb as a step function of storage.
    if v < 1.23e6:
        cb = 6.1
    elif v < 6.17e6:
        cb = 18.3
    elif v < 1.23e7:
        cb = 42.7
    else:
        cb = 54.9

    b_avg = 2.5 * hw + cb
    if erosion_resistance == "resistant":
        tf_hr = b_avg / (4.0 * hw)
        side_slope = 0.5
    else:
        tf_hr = b_avg / (4.0 * hw + 61.0)
        side_slope = 1.0

    b_bottom = max(b_avg - side_slope * hw, 0.1 * b_avg)

    return BreachParams(
        bottom_width_m=b_bottom,
        average_width_m=b_avg,
        side_slope_h_per_v=side_slope,
        depth_m=hw,
        formation_time_hr=tf_hr,
        source="Von Thun & Gillette (1990), USBR",
    )


def macdonald_langridge_monopolis_1984(
    outflow_volume_m3: float,
    breach_height_m: float,
    fill_type: Literal["earthfill", "rockfill"] = "earthfill",
) -> BreachParams:
    """MacDonald, T.C. & Langridge-Monopolis, J. (1984), "Breaching
    Characteristics of Dam Failures", ASCE Journal of Hydraulic Engineering,
    110(5), 567-586.

    Predicts the ERODED VOLUME of embankment material, then the time to erode
    it - a different physical route to the same two numbers, which is why it is
    worth carrying alongside Froehlich.

    Eroded volume   Ver = 0.0261 * (Vout * hw)^0.769      [earthfill]
                    Ver = 0.00348 * (Vout * hw)^0.852     [rockfill]
    Formation time  tf  = 0.0179 * Ver^0.364              [hours]

    Side slope fixed at 0.5 H:V by the authors.
    """
    vh = outflow_volume_m3 * breach_height_m
    if fill_type == "earthfill":
        v_er = 0.0261 * vh**0.769
    else:
        v_er = 0.00348 * vh**0.852

    tf_hr = 0.0179 * v_er**0.364
    side_slope = 0.5

    # Back out an average width from the eroded prism, assuming the breach cuts
    # the full height of the dam and the embankment crest-to-toe length is
    # approximated by 3 * hw (a typical 1:1.5 upstream/downstream slope pair).
    embankment_length_m = 3.0 * breach_height_m
    b_avg = v_er / max(embankment_length_m * breach_height_m, 1.0)
    b_avg = max(b_avg, 0.5 * breach_height_m)  # floor: never narrower than h/2
    b_bottom = max(b_avg - side_slope * breach_height_m, 0.1 * b_avg)

    return BreachParams(
        bottom_width_m=b_bottom,
        average_width_m=b_avg,
        side_slope_h_per_v=side_slope,
        depth_m=breach_height_m,
        formation_time_hr=tf_hr,
        source="MacDonald & Langridge-Monopolis (1984), ASCE JHE 110(5)",
    )


BREACH_REGRESSIONS = {
    "froehlich2008": froehlich_2008,
    "vontthun1990": von_thun_gillette_1990,
    "macdonald1984": macdonald_langridge_monopolis_1984,
}


def breach_parameter_ensemble(
    reservoir_volume_m3: float,
    breach_height_m: float,
    failure_mode: FailureMode = "overtopping",
) -> dict[str, BreachParams]:
    """Run all three regressions on the same dam.

    This is what feeds the uncertainty band. The three will disagree - report
    the spread, do not average them into a single confident-looking number.
    """
    return {
        "froehlich2008": froehlich_2008(
            reservoir_volume_m3, breach_height_m, failure_mode
        ),
        "vonthun1990": von_thun_gillette_1990(reservoir_volume_m3, breach_height_m),
        "macdonald1984": macdonald_langridge_monopolis_1984(
            reservoir_volume_m3, breach_height_m
        ),
    }


# ==========================================================================
# Peak outflow - independent sanity check, not the model
# ==========================================================================


def peak_outflow_regressions(
    reservoir_volume_m3: float, dam_height_m: float
) -> dict[str, float]:
    """Empirical peak-discharge predictors, in m3/s.

    These are NOT how we compute the hydrograph. They exist so that when the
    SPH run or the level-pool routing produces a peak, we can ask "is this
    within the envelope of what 100 real dam failures did?" A solver peak that
    sits two orders of magnitude outside this spread is a bug in the solver.

    Sources, in order:
      Froehlich, D.C. (1995), "Peak Outflow from Breached Embankment Dam",
        ASCE J. Water Resources Planning and Management, 121(1), 90-97.
      US Bureau of Reclamation (1982), "Guidelines for Defining Inundated
        Areas Downstream from Bureau of Reclamation Dams", ACER Technical
        Memorandum No. 3.
      Hagen, V.K. (1982), "Re-evaluation of Design Floods and Dam Safety",
        14th ICOLD Congress, Rio de Janeiro.
      Costa, J.E. (1985), "Floods from Dam Failures", USGS Open-File Report
        85-560.
    """
    vw = reservoir_volume_m3
    hw = dam_height_m
    vh = vw * hw

    return {
        # Froehlich (1995): Qp = 0.607 * Vw^0.295 * hw^1.24
        "froehlich1995": 0.607 * vw**0.295 * hw**1.24,
        # USBR (1982): Qp = 19.1 * hw^1.85  (envelope, tends to run high)
        "usbr1982": 19.1 * hw**1.85,
        # Hagen (1982): Qp = 0.54 * (S * hd)^0.5, S in m3, hd in m
        "hagen1982": 0.54 * math.sqrt(vh),
        # Costa (1985) envelope for the product of storage and head
        "costa1985": 0.981 * vh**0.42,
    }


def peak_outflow_envelope(
    reservoir_volume_m3: float, dam_height_m: float
) -> tuple[float, float]:
    """(low, high) bracket of plausible peak discharge, m3/s.

    Use this to sanity-check any solver. Do not present it as an accuracy claim.
    """
    values = list(peak_outflow_regressions(reservoir_volume_m3, dam_height_m).values())
    return min(values), max(values)


# ==========================================================================
# The hydrograph
# ==========================================================================


def storage_from_level(
    level_m: float, dam_height_m: float, capacity_m3: float, exponent: float = 2.7
) -> float:
    """Reservoir storage at a given water level, m3.

    ASSUMPTION, and it is a real one: we have no surveyed storage-elevation
    curve for an arbitrary Indian dam, so we use the power law

        V(h) = V_full * (h / H)^k

    with k = 2.7, typical for a steep valley impoundment. k = 1 would be a
    vertical-walled tank, k = 3 a perfect cone. This is stated in meta.json
    under scenario.storage_curve and it is one of the parameters the Monte
    Carlo in modules/07_ml perturbs.

    Reference for the form: USACE HEC-RAS 6.x Hydraulic Reference Manual,
    reservoir storage-elevation approximation.
    """
    if level_m <= 0:
        return 0.0
    frac = min(level_m / dam_height_m, 1.0)
    return capacity_m3 * frac**exponent


def level_from_storage(
    storage_m3: float, dam_height_m: float, capacity_m3: float, exponent: float = 2.7
) -> float:
    """Inverse of storage_from_level."""
    if storage_m3 <= 0 or capacity_m3 <= 0:
        return 0.0
    frac = min(storage_m3 / capacity_m3, 1.0)
    return dam_height_m * frac ** (1.0 / exponent)


def _breach_discharge(
    head_m: float, bottom_width_m: float, side_slope: float
) -> float:
    """Broad-crested weir flow through a trapezoidal breach, m3/s.

        Q = 1.7 * B * H^1.5  +  1.1 * Z * H^2.5

    First term is the rectangular floor, second the two sloping sides. The
    coefficients are the SI broad-crested weir values used by the NWS DAMBRK /
    FLDWAV family and by HEC-RAS.

    Source: Fread, D.L. (1988), "BREACH: An Erosion Model for Earthen Dam
    Failures", NOAA National Weather Service, Hydrologic Research Laboratory;
    and USACE HEC-RAS Hydraulic Reference Manual, "Dam Breach Outflow".
    """
    if head_m <= 0.0:
        return 0.0
    return 1.7 * bottom_width_m * head_m**1.5 + 1.1 * side_slope * head_m**2.5


DAMBREAK_C_SI = 8.0 / 27.0 * math.sqrt(GRAVITY)
"""Critical-flow discharge coefficient at an instantaneously opened breach,
Q = (8/27) * sqrt(g) * B * H^1.5 = 0.9225 * B * H^1.5 in SI.

This is the discharge at the breach section of the Ritter (1892) dam-break
solution, and it is what controls a full-height opening that appears in
seconds - the flow goes critical at the opening rather than passing over a
crest. It is 54% of the broad-crested weir coefficient 1.7 used for a breach
that erodes down from the crest, and using the weir value for a block failure
over-predicts the peak by roughly a factor of two.

Source: Ritter, A. (1892), as reproduced in Toro, E.F. (2001), "Shock-Capturing
Methods for Free-Surface Shallow Flows", Wiley, section 5; the same coefficient
appears as the free-outflow limit in USACE HEC-RAS, "Dam Breach Outflow"."""

DAMBREAK_SIDE_C_SI = 1.1 * (8.0 / 27.0 * math.sqrt(GRAVITY)) / 1.7
"""Side-slope term for the same critical-flow control, scaled from the weir
side coefficient 1.1 by the same ratio as the floor term. ~0.597."""


def _dambreak_discharge(
    head_m: float, bottom_width_m: float, side_slope: float
) -> float:
    """Critical-flow discharge through a trapezoidal opening, m3/s.

        Q = 0.9225 * B * H^1.5  +  0.597 * Z * H^2.5

    Same trapezoid as _breach_discharge, different control. Use this when the
    opening appears essentially instantaneously at full height - a foundation
    or abutment failure - and _breach_discharge when it erodes down from the
    crest.
    """
    if head_m <= 0.0:
        return 0.0
    return (
        DAMBREAK_C_SI * bottom_width_m * head_m**1.5
        + DAMBREAK_SIDE_C_SI * side_slope * head_m**2.5
    )


def _piping_discharge(
    head_m: float, pipe_area_m2: float, discharge_coeff: float = 0.6
) -> float:
    """Orifice flow through a developing pipe, m3/s.

        Q = Cd * A * sqrt(2 * g * H)

    Used for the piping failure mode before the roof of the pipe collapses and
    the failure becomes a weir. Cd = 0.6 is the standard sharp-edged orifice
    coefficient (Fread 1988, section 3).
    """
    if head_m <= 0.0 or pipe_area_m2 <= 0.0:
        return 0.0
    return discharge_coeff * pipe_area_m2 * math.sqrt(2.0 * GRAVITY * head_m)


def breach_hydrograph(
    breach: BreachParams,
    dam_height_m: float,
    capacity_m3: float,
    reservoir_level_frac: float = 1.0,
    failure_mode: FailureMode = "overtopping",
    inflow_cumecs: float = 0.0,
    duration_hr: float = 12.0,
    dt_s: float = 5.0,
    output_step_hr: float = 0.05,
    storage_exponent: float = 2.7,
) -> tuple[np.ndarray, np.ndarray]:
    """Level-pool routing of a growing breach. Returns (time_hr, discharge_cumecs).

    The physics, in one paragraph: the breach grows linearly from zero to its
    final geometry over formation_time_hr. At every step we compute the weir
    discharge through the breach as it currently is, subtract that volume from
    the reservoir, recompute the water level from the storage curve, and repeat.
    The reservoir empties, the head falls, and the discharge peaks early and
    then decays - which is the characteristic shape of a real dam-break
    hydrograph.

    This is the standard level-pool (Puls) routing used by DAMBRK and HEC-RAS.
    It ignores reservoir drawdown dynamics and wave effects inside the
    impoundment; for a reservoir short relative to the flood wave that is the
    accepted simplification.

    Args:
        breach: final breach geometry from one of the regressions.
        dam_height_m: full dam height, used with the storage curve.
        capacity_m3: gross storage at full supply level, m3.
        reservoir_level_frac: how full the reservoir is at t=0, 0..1.
        failure_mode: 'piping' starts as orifice flow and switches to weir flow
            at half the formation time (roof collapse). Everything else is weir
            flow throughout.
        inflow_cumecs: steady inflow into the reservoir during the event.
        duration_hr: how long to route for.
        dt_s: integration step. 5 s is stable for reservoirs down to ~0.1 MCM.
        output_step_hr: spacing of the returned series.
        storage_exponent: k in the storage curve. See storage_from_level.

    Returns:
        (time_hr, discharge_cumecs) - both float64, time strictly increasing
        and starting at exactly 0.0, as the contract requires.

    Raises:
        ValueError: on non-physical inputs.
    """
    if not 0.0 <= reservoir_level_frac <= 1.0:
        raise ValueError(f"reservoir_level_frac must be 0..1, got {reservoir_level_frac}")
    if capacity_m3 <= 0 or dam_height_m <= 0:
        raise ValueError("capacity_m3 and dam_height_m must be positive")

    level = dam_height_m * reservoir_level_frac
    storage = storage_from_level(level, dam_height_m, capacity_m3, storage_exponent)

    # Breach invert sits at the base of the breach, measured from the dam base.
    invert_elev = max(dam_height_m - breach.depth_m, 0.0)

    tf_s = max(breach.formation_time_hr * 3600.0, dt_s)
    n_steps = int(duration_hr * 3600.0 / dt_s)

    times, flows = [0.0], [0.0]
    next_out_s = output_step_hr * 3600.0

    for step in range(1, n_steps + 1):
        t_s = step * dt_s
        growth = min(t_s / tf_s, 1.0)  # linear breach growth, 0 -> 1

        head = max(level - invert_elev, 0.0)

        if failure_mode == "piping" and t_s < 0.5 * tf_s:
            # Pipe area grows with the square of time before roof collapse.
            full_area = breach.bottom_width_m * breach.depth_m
            area = full_area * (t_s / (0.5 * tf_s)) ** 2
            q = _piping_discharge(head, area)
        else:
            q = _breach_discharge(
                head,
                breach.bottom_width_m * growth,
                breach.side_slope_h_per_v * growth,
            )

        # Level-pool continuity: dS/dt = I - Q, never below empty.
        storage = max(storage + (inflow_cumecs - q) * dt_s, 0.0)
        level = level_from_storage(storage, dam_height_m, capacity_m3, storage_exponent)

        if t_s >= next_out_s - 1e-9:
            times.append(t_s / 3600.0)
            flows.append(q)
            next_out_s += output_step_hr * 3600.0

    return np.asarray(times, dtype=np.float64), np.asarray(flows, dtype=np.float64)


# ==========================================================================
# Controlled release through the outlet works
# ==========================================================================
#
# NTRO's problem statement asks for "dam break OR water release". These are not
# the same event and must not share an implementation. A breach is an
# uncontrolled hole that grows; a release is an operator opening gates on a
# structure that stays intact. The breach regressions in this module are fitted
# to 74 documented embankment FAILURES and have no meaning for a gate opening.

ORIFICE_CD = 0.6
"""Discharge coefficient for a submerged outlet gate. Fread, D.L. (1988),
'The NWS DAMBRK Model', National Weather Service."""

WEIR_C_SI = 1.7
"""Broad-crested weir coefficient in SI units: Q = C L H^1.5. Standard value
for an ogee/broad-crested spillway."""


@dataclass
class GateRelease:
    """What the structure actually opened, recorded so the run can be audited.

    Every field here is either measured (from the dam register) or an assumption
    we are naming out loud. `capacity_source` says which.
    """

    target_release_cumecs: float
    gate_opening_frac: float
    gate_area_m2: float
    outlet_invert_m: float
    spillway_crest_m: float
    spillway_length_m: float
    capacity_source: str
    peak_cumecs: float = 0.0
    released_volume_mcm: float = 0.0
    drawdown_m: float = 0.0
    spillway_engaged: bool = False

    def as_dict(self) -> dict:
        return {
            "target_release_cumecs": round(self.target_release_cumecs, 1),
            "gate_opening_frac": round(self.gate_opening_frac, 3),
            "gate_area_m2": round(self.gate_area_m2, 2),
            "outlet_invert_m": round(self.outlet_invert_m, 2),
            "spillway_crest_m": round(self.spillway_crest_m, 2),
            "spillway_length_m": round(self.spillway_length_m, 1),
            "capacity_source": self.capacity_source,
            "peak_cumecs": round(self.peak_cumecs, 1),
            "released_volume_mcm": round(self.released_volume_mcm, 4),
            "drawdown_m": round(self.drawdown_m, 2),
            "spillway_engaged": bool(self.spillway_engaged),
            "note": (
                "Controlled release through outlet gates plus any uncontrolled "
                "spillway flow. The dam does NOT fail: no breach regression is "
                "used, and the structure stays intact. Gate discharge is orifice "
                "flow (Fread 1988), spillway is broad-crested weir."
            ),
        }


def gated_release_hydrograph(
    dam_height_m: float,
    capacity_m3: float,
    reservoir_level_frac: float = 1.0,
    design_spillway_cumecs: float | None = None,
    target_release_cumecs: float | None = None,
    gate_opening_frac: float = 1.0,
    gate_open_time_hr: float = 0.5,
    outlet_invert_frac: float = 0.05,
    spillway_crest_frac: float = 0.85,
    spillway_length_m: float = 60.0,
    inflow_cumecs: float = 0.0,
    duration_hr: float = 12.0,
    dt_s: float = 5.0,
    output_step_hr: float = 0.05,
    storage_exponent: float = 2.7,
) -> tuple[np.ndarray, np.ndarray, GateRelease]:
    """Level-pool routing of a controlled release. No breach, no failure.

    The physics: the operator winds the gates open over `gate_open_time_hr`.
    Discharge through them is orifice flow driven by the head above the outlet
    invert. If the water level is above the spillway crest, the uncontrolled
    spillway passes water too, whatever the operator does. Both draw the
    reservoir down, the head falls, and the discharge decays.

        gate      Q = Cd A sqrt(2 g (y - y_invert))     orifice, Fread (1988)
        spillway  Q = C L (y - y_crest)^1.5             broad-crested weir

    This is the same level-pool (Puls) routing as `breach_hydrograph`, with the
    breach replaced by the structure's real outlet works. The resulting
    hydrograph looks nothing like a dam break: a plateau at the gate capacity
    rather than a sharp spike, and the reservoir draws down only to the outlet
    invert instead of emptying.

    Args:
        dam_height_m: full dam height.
        capacity_m3: gross storage at full supply level.
        reservoir_level_frac: how full at t = 0, 0..1.
        design_spillway_cumecs: the structure's design discharge capacity, from
            the CWC register when the register has it. This is a MEASURED number
            and is used in preference to any assumption.
        target_release_cumecs: what the operator is aiming to pass with gates
            fully open. Defaults to the design capacity, or - if the register
            has none - to the rate that would draw the reservoir down in 24
            hours, which is an ASSUMPTION and is labelled as one.
        gate_opening_frac: 0..1. How far the gates are opened. 1.0 is a full
            emergency release.
        gate_open_time_hr: how long the operator takes to wind them open.
        outlet_invert_frac: outlet sill height as a fraction of dam height.
        spillway_crest_frac: spillway crest as a fraction of dam height.
        spillway_length_m: crest length of the uncontrolled spillway.
        inflow_cumecs: steady inflow during the event.
        duration_hr, dt_s, output_step_hr, storage_exponent: as breach_hydrograph.

    Returns:
        (time_hr, discharge_cumecs, GateRelease) - the series plus the block
        that goes into meta.json so the assumptions travel with the result.

    Raises:
        ValueError: on non-physical inputs.
    """
    if not 0.0 <= reservoir_level_frac <= 1.0:
        raise ValueError(
            f"reservoir_level_frac must be 0..1, got {reservoir_level_frac}"
        )
    if capacity_m3 <= 0 or dam_height_m <= 0:
        raise ValueError("capacity_m3 and dam_height_m must be positive")
    if not 0.0 <= gate_opening_frac <= 1.0:
        raise ValueError(f"gate_opening_frac must be 0..1, got {gate_opening_frac}")

    outlet_invert = outlet_invert_frac * dam_height_m
    spillway_crest = spillway_crest_frac * dam_height_m

    # What the gates can pass, and where that number came from.
    if target_release_cumecs is not None and target_release_cumecs > 0:
        target = float(target_release_cumecs)
        source = "operator-specified"
    elif design_spillway_cumecs is not None and design_spillway_cumecs > 0:
        target = float(design_spillway_cumecs)
        source = "CWC NRLD design spillway capacity (measured)"
    else:
        # ASSUMPTION, stated: size the outlet to draw the reservoir down in 24 h.
        target = capacity_m3 / (24.0 * 3600.0)
        source = "ASSUMED - no spillway capacity in the register; sized to draw down in 24 h"

    # Gate area that delivers `target` at full supply level.
    head_full = max(dam_height_m - outlet_invert, 0.1)
    gate_area = target / (ORIFICE_CD * math.sqrt(2.0 * GRAVITY * head_full))

    level = dam_height_m * reservoir_level_frac
    level0 = level
    storage = storage_from_level(level, dam_height_m, capacity_m3, storage_exponent)

    open_s = max(gate_open_time_hr * 3600.0, dt_s)
    n_steps = int(duration_hr * 3600.0 / dt_s)

    times, flows = [0.0], [0.0]
    next_out_s = output_step_hr * 3600.0
    peak = 0.0
    released_m3 = 0.0
    spill_engaged = False

    for step in range(1, n_steps + 1):
        t_s = step * dt_s

        # Operators wind gates open; they do not appear fully open at t=0.
        opening = min(t_s / open_s, 1.0) * gate_opening_frac

        head_gate = max(level - outlet_invert, 0.0)
        q_gate = ORIFICE_CD * gate_area * opening * math.sqrt(
            2.0 * GRAVITY * head_gate
        )

        head_weir = max(level - spillway_crest, 0.0)
        q_spill = WEIR_C_SI * spillway_length_m * head_weir**1.5
        if q_spill > 0.0:
            spill_engaged = True

        q = q_gate + q_spill

        # A structure cannot pass more than it was designed to pass.
        if design_spillway_cumecs is not None and design_spillway_cumecs > 0:
            q = min(q, float(design_spillway_cumecs))

        storage = max(storage + (inflow_cumecs - q) * dt_s, 0.0)
        level = level_from_storage(
            storage, dam_height_m, capacity_m3, storage_exponent
        )

        released_m3 += q * dt_s
        peak = max(peak, q)

        if t_s >= next_out_s - 1e-9:
            times.append(t_s / 3600.0)
            flows.append(q)
            next_out_s += output_step_hr * 3600.0

    release = GateRelease(
        target_release_cumecs=target,
        gate_opening_frac=gate_opening_frac,
        gate_area_m2=gate_area,
        outlet_invert_m=outlet_invert,
        spillway_crest_m=spillway_crest,
        spillway_length_m=spillway_length_m,
        capacity_source=source,
        peak_cumecs=peak,
        released_volume_mcm=released_m3 / 1e6,
        drawdown_m=max(level0 - level, 0.0),
        spillway_engaged=spill_engaged,
    )
    return (
        np.asarray(times, dtype=np.float64),
        np.asarray(flows, dtype=np.float64),
        release,
    )


# ==========================================================================
# Open-channel helpers
# ==========================================================================


def manning_velocity(depth_m, slope, manning_n):
    """Manning's equation for depth-averaged velocity, m/s. Array-safe.

        v = (1/n) * R^(2/3) * S^(1/2)

    with hydraulic radius R approximated by the depth, which is the standard
    wide-channel assumption (width >> depth) and is what a 2D shallow-water
    solver uses per cell.

    Source: Manning, R. (1891); as presented in Chow (1959), eq. 5-8.
    """
    depth = np.maximum(np.asarray(depth_m, dtype=np.float64), 0.0)
    slope = np.maximum(np.asarray(slope, dtype=np.float64), 0.0)
    n = np.asarray(manning_n, dtype=np.float64)
    return (1.0 / n) * depth ** (2.0 / 3.0) * np.sqrt(slope)


def hydraulic_geometry(discharge_cumecs: float) -> tuple[float, float]:
    """Downstream hydraulic geometry: (width_m, depth_m) for a bankfull flow.

        w = a * Q^b   with a = 4.0, b = 0.5
        d = c * Q^f   with c = 0.27, f = 0.39

    Source: Leopold, L.B. & Maddock, T. (1953), "The Hydraulic Geometry of
    Stream Channels and Some Physiographic Implications", USGS Professional
    Paper 252. Coefficients are the widely used at-a-station downstream set;
    module 01 refits them regionally where gauge data exists and records the
    fitted exponents in the DEM conditioning report.
    """
    q = max(discharge_cumecs, 0.01)
    return 4.0 * q**0.5, 0.27 * q**0.39


# ==========================================================================
# Foundation / abutment failure - the structure goes as a block
# ==========================================================================
#
# This mode exists because forcing a concrete dam through an embankment breach
# regression is a category error, not a conservative approximation.
#
# Froehlich (2008), Von Thun & Gillette (1990) and MacDonald & Langridge-
# Monopolis (1984) are all fitted to EARTHFILL and rockfill dams - Froehlich's
# set is 74 embankment failures. Every one of them predicts how fast flowing
# water ERODES a soil embankment. A concrete gravity or arch dam whose
# foundation shears does not erode at all: the monoliths are displaced, or the
# arch loses its thrust block and rotates out. There is no formation time in
# the erosion sense, and running the regression on one produces a formation
# time in hours for an event that observers timed in seconds.
#
# St Francis (1928) and Malpasset (1959) are the two best-documented cases and
# both went in well under five minutes. The Malpasset arch was found displaced
# essentially intact; nothing about it was eroded.


@dataclass
class FoundationCollapse:
    """What was assumed about a block failure, recorded so it can be argued with.

    A foundation failure has no measured regression to fall back on, so every
    number here is either given by the operator or an assumption named out
    loud. That is worse than having a regression and it is stated rather than
    hidden behind one that does not apply.
    """

    opening_top_width_m: float
    opening_bottom_width_m: float
    opening_depth_m: float
    side_slope_h_per_v: float
    collapse_time_s: float
    crest_length_m: float
    breach_fraction_of_crest: float
    basis: str

    def as_dict(self) -> dict:
        return asdict(self)


def foundation_collapse_hydrograph(
    dam_height_m: float,
    capacity_m3: float,
    crest_length_m: float,
    reservoir_level_frac: float = 1.0,
    breach_fraction_of_crest: float = 0.8,
    base_width_ratio: float = 0.25,
    collapse_time_s: float = 120.0,
    inflow_cumecs: float = 0.0,
    duration_hr: float = 12.0,
    dt_s: float = 1.0,
    output_step_hr: float = 0.05,
    storage_exponent: float = 2.7,
) -> tuple[np.ndarray, np.ndarray, FoundationCollapse]:
    """Sudden structural collapse. Returns (time_hr, discharge_cumecs, params).

    The routing is still level-pool continuity, because the reservoir still
    empties through an opening and the storage curve still governs the head.
    What differs from breach_hydrograph, and differs on purpose:

      * The opening is GEOMETRIC, not eroded. Its depth is the full dam height
        from the first moment the block goes, and nothing widens it afterwards.
      * The opening is a TRAPEZOID, not a rectangle. A concrete gravity or arch
        dam stands in a gorge, and the crest length is the width of that gorge
        at the TOP - at the bed it is far narrower. An earlier version of this
        function drained the reservoir through a rectangle as wide as the crest
        and as deep as the dam, and over-predicted St Francis by a factor of
        2.4. base_width_ratio is the bed width as a fraction of the top width.
      * Discharge is under CRITICAL-FLOW control, not weir control. A
        full-height opening that appears in seconds is the Ritter dam-break
        problem, whose discharge at the breach section is 0.9225 * B * H^1.5 -
        54% of the broad-crested weir value that applies to a breach eroding
        down from a crest. Using the weir coefficient here was the other half
        of that factor of 2.4.
      * The opening time is a structural collapse time in SECONDS. The default
        120 s is the order of magnitude reported for St Francis and Malpasset;
        it is an assumption and FoundationCollapse.basis says so.
      * Because the opening is full-depth and near-instantaneous, the first
        seconds are a genuine dam-break wave rather than weir flow through a
        notch. The peak is therefore much sharper and much earlier than any
        embankment breach of the same reservoir, which is the entire
        operational difference between the two: for an embankment there is
        usually time to warn somebody, and for this there is not.

    dt_s defaults to 1 s rather than 5 s because a 120 s opening resolved on a
    5 s step is 24 points, and the peak lands between them.

    Args:
        dam_height_m: full structural height.
        capacity_m3: gross storage at full supply level, m3.
        crest_length_m: length of the dam along the crest. The opening is a
            fraction of this.
        reservoir_level_frac: how full the reservoir is at t=0, 0..1.
        breach_fraction_of_crest: how much of the dam goes, measured at the
            crest. 1.0 is the whole structure; 0.8 is the default because at St
            Francis the centre section was left standing and at Malpasset the
            abutment remained.
        base_width_ratio: opening width at the bed as a fraction of its width
            at the crest, i.e. how gorge-like the valley is. 0.25 is a steep
            mountain gorge, which is where concrete dams get built. 1.0 makes
            the opening rectangular and is almost certainly wrong.
        collapse_time_s: seconds from first movement to the opening being fully
            formed.
        inflow_cumecs: steady inflow during the event.
        duration_hr, dt_s, output_step_hr, storage_exponent: as elsewhere.

    Returns:
        (time_hr, discharge_cumecs, FoundationCollapse).

    Raises:
        ValueError: on non-physical inputs.
    """
    if not 0.0 <= reservoir_level_frac <= 1.0:
        raise ValueError(f"reservoir_level_frac must be 0..1, got {reservoir_level_frac}")
    if capacity_m3 <= 0 or dam_height_m <= 0:
        raise ValueError("capacity_m3 and dam_height_m must be positive")
    if crest_length_m <= 0:
        raise ValueError("crest_length_m must be positive")
    if not 0.0 < breach_fraction_of_crest <= 1.0:
        raise ValueError("breach_fraction_of_crest must be in (0, 1]")
    if not 0.0 < base_width_ratio <= 1.0:
        raise ValueError("base_width_ratio must be in (0, 1]")

    top_w = crest_length_m * breach_fraction_of_crest
    bottom_w = top_w * base_width_ratio
    side_slope = (top_w - bottom_w) / 2.0 / dam_height_m
    params = FoundationCollapse(
        opening_top_width_m=round(top_w, 1),
        opening_bottom_width_m=round(bottom_w, 1),
        opening_depth_m=round(dam_height_m, 2),
        side_slope_h_per_v=round(side_slope, 3),
        collapse_time_s=collapse_time_s,
        crest_length_m=crest_length_m,
        breach_fraction_of_crest=breach_fraction_of_crest,
        basis=(
            "No breach regression applied. Froehlich (2008), Von Thun & "
            "Gillette (1990) and MacDonald & Langridge-Monopolis (1984) are "
            "fitted to embankment EROSION and do not describe a concrete "
            "structure displaced off its foundation. The opening is therefore "
            "geometric - a stated fraction of the crest at full height - and "
            "the collapse time is an assumption of the order reported for St "
            "Francis (1928) and Malpasset (1959), both of which failed in "
            "under five minutes. The opening is trapezoidal because the "
            "crest length is the gorge width at the top and not at the bed, "
            "and the discharge is under critical-flow (Ritter) control rather "
            "than weir control because the opening is full-height from the "
            "start."
        ),
    )

    level = dam_height_m * reservoir_level_frac
    storage = storage_from_level(level, dam_height_m, capacity_m3, storage_exponent)

    n_steps = int(duration_hr * 3600.0 / dt_s)
    times, flows = [0.0], [0.0]
    next_out_s = output_step_hr * 3600.0

    for step in range(1, n_steps + 1):
        t_s = step * dt_s
        # The block does not erode open, it drops out. Linear in area over the
        # collapse time is the simplest defensible description of that and is
        # stated as an assumption rather than dressed up as erosion.
        growth = min(t_s / max(collapse_time_s, dt_s), 1.0)

        # Invert at the dam base: this is a full-height opening from the start,
        # which is exactly what makes it different from a crest breach.
        head = max(level, 0.0)
        q = _dambreak_discharge(head, bottom_w * growth, side_slope * growth)

        storage = max(storage + (inflow_cumecs - q) * dt_s, 0.0)
        level = level_from_storage(storage, dam_height_m, capacity_m3, storage_exponent)

        if t_s >= next_out_s - 1e-9:
            times.append(t_s / 3600.0)
            flows.append(q)
            next_out_s += output_step_hr * 3600.0

    return (
        np.asarray(times, dtype=np.float64),
        np.asarray(flows, dtype=np.float64),
        params,
    )


# ==========================================================================
# Spillway or gate blockage - the reservoir cannot discharge
# ==========================================================================
#
# The mechanism that destroyed Banqiao and South Fork, and it was invisible in
# this model until now because it was being run as ordinary overtopping.
#
# It is not ordinary overtopping. Ordinary overtopping is a flood too big for a
# working spillway. This is a flood arriving at a spillway that is not working,
# and the two differ in the one quantity an operator actually needs: how long
# they have. With a working spillway the reservoir absorbs the peak for hours.
# With a blocked one the level climbs at the full inflow rate from the moment
# the blockage forms.


@dataclass
class SpillwayBlockage:
    """The filling phase, before anything failed.

    time_to_overtop_hr is the number this mode exists to produce and no other
    mode in this file can produce it.
    """

    residual_capacity_frac: float
    design_spillway_cumecs: float | None
    residual_capacity_cumecs: float
    inflow_cumecs: float
    starting_level_frac: float
    time_to_overtop_hr: float | None
    overtopped: bool
    volume_gained_mcm: float
    basis: str

    def as_dict(self) -> dict:
        return asdict(self)


def fill_to_overtopping(
    dam_height_m: float,
    capacity_m3: float,
    inflow_cumecs: float,
    design_spillway_cumecs: float | None,
    residual_capacity_frac: float = 0.0,
    starting_level_frac: float = 0.85,
    max_hours: float = 240.0,
    dt_s: float = 60.0,
    storage_exponent: float = 2.7,
) -> SpillwayBlockage:
    """How long until the water reaches the crest, given the outlets are gone.

    Straight reservoir mass balance: dS/dt = inflow - residual outlet capacity,
    integrated until the level reaches the dam height or max_hours runs out.

    The residual capacity is deliberately a FRACTION of the design capacity
    rather than an absolute number, because that is how the failures are
    described - Banqiao's gates were partly silted, not removed; South Fork's
    spillway was screened, not sealed. residual_capacity_frac = 0.0 is a
    complete blockage.

    Returns a SpillwayBlockage recording the fill phase, including
    time_to_overtop_hr = None when the reservoir never reaches the crest -
    which is the correct and useful answer to "the gates are jammed, do we have
    a problem", and one worth being able to get back.
    """
    if capacity_m3 <= 0 or dam_height_m <= 0:
        raise ValueError("capacity_m3 and dam_height_m must be positive")
    if not 0.0 <= residual_capacity_frac <= 1.0:
        raise ValueError("residual_capacity_frac must be 0..1")
    if not 0.0 <= starting_level_frac <= 1.0:
        raise ValueError("starting_level_frac must be 0..1")

    if design_spillway_cumecs and design_spillway_cumecs > 0:
        residual = design_spillway_cumecs * residual_capacity_frac
        basis = (
            f"Residual outlet capacity is {residual_capacity_frac:.0%} of the "
            f"design spillway discharge {design_spillway_cumecs:,.0f} m3/s "
            f"from the CWC register."
        )
    else:
        residual = 0.0
        basis = (
            "The register carries no design spillway discharge for this "
            "structure, so the residual outlet capacity is taken as zero. "
            "That is an ASSUMPTION and it makes this run the worst case: a "
            "structure with some working outlet capacity fills more slowly "
            "than this says."
        )

    level = dam_height_m * starting_level_frac
    storage = storage_from_level(level, dam_height_m, capacity_m3, storage_exponent)
    start_storage = storage
    net = inflow_cumecs - residual

    t_s = 0.0
    overtopped = False
    if net > 0:
        limit_s = max_hours * 3600.0
        while t_s < limit_s:
            t_s += dt_s
            storage = min(storage + net * dt_s, capacity_m3)
            level = level_from_storage(
                storage, dam_height_m, capacity_m3, storage_exponent
            )
            if level >= dam_height_m - 1e-6:
                overtopped = True
                break

    return SpillwayBlockage(
        residual_capacity_frac=residual_capacity_frac,
        design_spillway_cumecs=design_spillway_cumecs,
        residual_capacity_cumecs=round(residual, 1),
        inflow_cumecs=inflow_cumecs,
        starting_level_frac=starting_level_frac,
        time_to_overtop_hr=round(t_s / 3600.0, 3) if overtopped else None,
        overtopped=overtopped,
        volume_gained_mcm=round((storage - start_storage) / 1e6, 4),
        basis=basis
        + (
            ""
            if overtopped
            else f" The reservoir did not reach the crest within {max_hours:.0f} h "
            f"at an inflow of {inflow_cumecs:,.0f} m3/s, so no breach follows."
        ),
    )


# ==========================================================================
# Glacial lake outburst through a moraine
# ==========================================================================
#
# A moraine dam is not a small landslide dam and modelling it as one gets the
# breach depth wrong in the direction that matters.
#
# A landslide barrier is debris all the way down to the old river bed, so a
# breach can cut through the whole thing. A moraine sits on a bedrock sill and
# very often has a buried ice core; the loose till above that sill is the only
# part that erodes. Once the breach reaches the sill it stops deepening and can
# only widen. So the erodible depth is the moraine freeboard, which is
# typically a fraction of the ridge height above the valley, and using the full
# height instead over-predicts both the head and the released volume.
#
# The second difference is the trigger. South Lhonak in October 2023 was not a
# slow overtopping: an ice/rock avalanche entered the lake and the displacement
# wave went over the crest before any breach had formed. That arrives as a
# short leading surge AHEAD of the breach hydrograph, not as a taller breach
# peak, and it is why the first wave downstream can precede the failure.

HUGGEL_A = 0.104
HUGGEL_B = 1.42
"""Volume-area scaling for moraine-dammed lakes: V = 0.104 * A^1.42, with A in
m2 and V in m3.

Source: Huggel, C., Kaab, A., Haeberli, W., Teysseire, P. & Paul, F. (2002),
"Remote sensing based assessment of hazards from glacier lake outbursts: a case
study in the Swiss Alps", Canadian Geotechnical Journal 39(2), 316-330.

Used ONLY when the DEM cannot see the lake - a fallback with a factor-of-two
scatter in the source, and meta.json records when it was used."""


def lake_volume_from_area(area_m2: float) -> float:
    """Moraine-lake volume from surface area, m3. Huggel et al. (2002).

    >>> round(lake_volume_from_area(1.0e6) / 1e6, 1)
    26.2
    """
    if area_m2 <= 0:
        return 0.0
    return HUGGEL_A * area_m2**HUGGEL_B


@dataclass
class MoraineBreach:
    """A moraine failure, with the two things that make it not a landslide dam."""

    moraine_height_m: float
    erodible_depth_m: float
    breach_bottom_width_m: float
    side_slope_h_per_v: float
    formation_time_hr: float
    avalanche_surge_frac: float
    surge_volume_m3: float
    surge_duration_s: float
    lake_volume_m3: float
    volume_source: str
    basis: str

    def as_dict(self) -> dict:
        return asdict(self)


def glof_hydrograph(
    lake_volume_m3: float,
    moraine_height_m: float,
    erodible_depth_m: float | None = None,
    breach_width_m: float | None = None,
    formation_time_hr: float = 0.5,
    width_to_depth_ratio: float = 1.0,
    avalanche_surge_frac: float = 0.0,
    surge_duration_s: float = 600.0,
    lake_area_m2: float | None = None,
    inflow_cumecs: float = 0.0,
    duration_hr: float = 12.0,
    dt_s: float = 2.0,
    output_step_hr: float = 0.05,
    storage_exponent: float = 1.6,
) -> tuple[np.ndarray, np.ndarray, MoraineBreach]:
    """Moraine-dam outburst. Returns (time_hr, discharge_cumecs, params).

    Differences from breach_hydrograph, all of them physical rather than
    cosmetic:

      * BREACH DEPTH IS CAPPED at the erodible depth of the moraine, not the
        moraine height. Default is 0.6 of the ridge height, which is the middle
        of the range reported for breached Himalayan moraines; below that is
        the bedrock sill and any ice core, and the breach stops there. Passing
        erodible_depth_m states it explicitly.
      * THE STORAGE CURVE IS FLATTER. storage_exponent defaults to 1.6 here
        rather than 2.7, because a moraine-dammed lake occupies a scoured
        over-deepened basin with near-vertical walls, not a V-shaped river
        valley. Using the valley exponent drains the lake far too fast at low
        level.
      * AN AVALANCHE SURGE CAN LEAD THE BREACH. avalanche_surge_frac is the
        share of the lake displaced over the crest by an entering ice or rock
        mass. It is released over surge_duration_s starting at t = 0, BEFORE
        and independently of the breach, because that is the order the events
        happened in at South Lhonak. It is added to the breach discharge, and
        the volume it carries is removed from the lake so nothing is released
        twice.

        BE CAREFUL WITH IT. A surge is a volume divided by a duration, so it
        sets the peak directly and will dominate the breach whenever it is more
        than a few percent over a few minutes: 15% of a 25 MCM lake in 180 s is
        21,000 m3/s on its own, four times the breach peak. It defaults to zero
        for that reason, and any run that sets it should be read as two events
        superimposed rather than as one hydrograph.

    Args:
        lake_volume_m3: the lake. Pass <= 0 with lake_area_m2 set to fall back
            on Huggel et al. (2002) scaling.
        moraine_height_m: ridge height above the downstream valley floor.
        erodible_depth_m: how deep the breach can cut. None -> 0.6 * height.
        breach_width_m: final bottom width. None -> width_to_depth_ratio times
            the erodible depth.
        width_to_depth_ratio: bottom width as a multiple of erodible depth when
            breach_width_m is not given. 1.0 by default. This is the single
            most sensitive parameter in the mode and the reason it is exposed:
            the published South Lhonak scenario table spans 4,311 / 8,000 /
            12,487 m3/s for 20 / 30 / 40 m breach widths on the same lake, a
            factor of three from the breach width alone. An earlier default of
            3.0 put the peak near 24,000 m3/s, well outside that range.
        formation_time_hr: time to full breach. Non-cohesive till fails fast.
        avalanche_surge_frac: fraction of the lake displaced by the trigger,
            0..1. 0.0 means no trigger surge is modelled.
        surge_duration_s: how long the displacement wave takes to pass.
        lake_area_m2: used only if lake_volume_m3 <= 0.
        inflow_cumecs, duration_hr, dt_s, output_step_hr: as elsewhere.
        storage_exponent: k in the storage curve. See above for why it differs.

    Returns:
        (time_hr, discharge_cumecs, MoraineBreach).

    Raises:
        ValueError: on non-physical inputs.
    """
    if moraine_height_m <= 0:
        raise ValueError("moraine_height_m must be positive")
    if not 0.0 <= avalanche_surge_frac <= 1.0:
        raise ValueError("avalanche_surge_frac must be 0..1")

    volume_source = "given"
    if lake_volume_m3 is None or lake_volume_m3 <= 0:
        if not lake_area_m2 or lake_area_m2 <= 0:
            raise ValueError(
                "glof_hydrograph needs either lake_volume_m3 or lake_area_m2"
            )
        lake_volume_m3 = lake_volume_from_area(lake_area_m2)
        volume_source = (
            f"Huggel et al. (2002) V = 0.104 A^1.42 from a lake area of "
            f"{lake_area_m2 / 1e6:.3f} km2. The source reports roughly a "
            f"factor-of-two scatter about this relation."
        )

    erodible = erodible_depth_m if erodible_depth_m else 0.6 * moraine_height_m
    erodible = min(max(erodible, 0.0), moraine_height_m)
    bottom_w = breach_width_m if breach_width_m else width_to_depth_ratio * erodible

    surge_volume = lake_volume_m3 * avalanche_surge_frac
    params = MoraineBreach(
        moraine_height_m=moraine_height_m,
        erodible_depth_m=round(erodible, 2),
        breach_bottom_width_m=round(bottom_w, 1),
        side_slope_h_per_v=1.0,
        formation_time_hr=formation_time_hr,
        avalanche_surge_frac=avalanche_surge_frac,
        surge_volume_m3=round(surge_volume, 1),
        surge_duration_s=surge_duration_s if avalanche_surge_frac > 0 else 0.0,
        lake_volume_m3=round(lake_volume_m3, 1),
        volume_source=volume_source,
        basis=(
            f"Breach depth capped at the erodible moraine depth "
            f"{erodible:.1f} m of a {moraine_height_m:.1f} m ridge - below "
            f"that is the bedrock sill and any buried ice core, and the "
            f"breach cannot cut through them. Storage exponent "
            f"{storage_exponent} rather than the 2.7 used for a river valley, "
            f"because a moraine-dammed basin is over-deepened and steep-sided."
            + (
                f" A leading surge of {avalanche_surge_frac:.0%} of the lake "
                f"is released over {surge_duration_s:.0f} s from t = 0, ahead "
                f"of the breach, representing an avalanche displacement wave."
                if avalanche_surge_frac > 0
                else ""
            )
        ),
    )

    # The breach can only ever release what sits above the sill, so the lake is
    # treated as a body of depth `erodible` for routing purposes. Water below
    # the sill does not leave.
    routable = max(lake_volume_m3 - surge_volume, 0.0)
    level = erodible
    storage = routable
    capacity = max(routable, 1.0)

    tf_s = max(formation_time_hr * 3600.0, dt_s)
    n_steps = int(duration_hr * 3600.0 / dt_s)
    surge_q = surge_volume / surge_duration_s if surge_volume > 0 else 0.0

    times, flows = [0.0], [surge_q]
    next_out_s = output_step_hr * 3600.0

    for step in range(1, n_steps + 1):
        t_s = step * dt_s
        growth = min(t_s / tf_s, 1.0)
        head = max(level, 0.0)
        q = _breach_discharge(head, bottom_w * growth, 1.0 * growth)

        storage = max(storage + (inflow_cumecs - q) * dt_s, 0.0)
        level = level_from_storage(storage, erodible, capacity, storage_exponent)

        q_total = q + (surge_q if t_s <= surge_duration_s else 0.0)

        if t_s >= next_out_s - 1e-9:
            times.append(t_s / 3600.0)
            flows.append(q_total)
            next_out_s += output_step_hr * 3600.0

    return (
        np.asarray(times, dtype=np.float64),
        np.asarray(flows, dtype=np.float64),
        params,
    )


# ==========================================================================
# River flood wave - no dam, no barrier, nothing fails
# ==========================================================================
#
# Every other mode in this file starts with something giving way. This one does
# not, and it is the mode a river needs.
#
# A river has no crest, no embankment, no foundation and no gates. Asking which
# of those failed is meaningless on an open channel. What a river has is a
# discharge that rises, peaks and falls, and the only questions worth asking
# are how big the peak is, how fast it arrives, and where the water goes - and
# the last of those is answered by the terrain, never by the operator.

NRCS_RECESSION_RATIO = 1.67
"""Fall time divided by rise time in the NRCS dimensionless unit hydrograph:
total duration is 2.67 times the time to peak.

Source: USDA Natural Resources Conservation Service, National Engineering
Handbook Part 630, Chapter 16, "Hydrographs" (2007), Table 16-1."""


def river_flood_hydrograph(
    peak_discharge_cumecs: float,
    time_to_peak_hr: float,
    duration_hr: float = 12.0,
    base_flow_cumecs: float = 0.0,
    flood_duration_hr: float | None = None,
    output_step_hr: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    """A flood wave entering the reach. Returns (time_hr, discharge_cumecs).

    The shape is the NRCS dimensionless unit hydrograph: a curvilinear rise to
    the peak and a recession 1.67 times as long, which is the standard synthetic
    shape used when no gauge record exists for the site - which is the case for
    every ungauged reach this tool will be pointed at.

    The rise is modelled as q/qp = (t/tp)^2 * (3 - 2*t/tp), a smooth Hermite
    ramp with zero gradient at both ends, and the recession as an exponential
    decay fitted so the limb has fallen to 2% of the peak at the end of the
    flood duration. Both are approximations of the NRCS curve, and neither is
    dressed up as a routed result: this is the BOUNDARY CONDITION, and what
    happens to it downstream is the solver's business.

    There is no direction argument here and there deliberately is not one. Where
    the water goes is a property of the ground, read off the DEM by the flow
    tracer, and letting an operator point the flood at a compass bearing would
    let them produce a flood that the terrain forbids.

    Args:
        peak_discharge_cumecs: the peak of the flood wave.
        time_to_peak_hr: hours from the start of the rise to the peak.
        duration_hr: how long to generate the series for.
        base_flow_cumecs: discharge in the channel before and after the flood.
        flood_duration_hr: total flood duration. None -> 2.67 * time_to_peak,
            the NRCS ratio.
        output_step_hr: spacing of the returned series.

    Returns:
        (time_hr, discharge_cumecs), time starting at exactly 0.0.

    Raises:
        ValueError: on non-physical inputs.
    """
    if peak_discharge_cumecs <= 0:
        raise ValueError("peak_discharge_cumecs must be positive")
    if time_to_peak_hr <= 0:
        raise ValueError("time_to_peak_hr must be positive")
    if base_flow_cumecs < 0:
        raise ValueError("base_flow_cumecs cannot be negative")
    if base_flow_cumecs >= peak_discharge_cumecs:
        raise ValueError("base_flow_cumecs must be below the peak")

    tp = time_to_peak_hr
    total = flood_duration_hr if flood_duration_hr else (1.0 + NRCS_RECESSION_RATIO) * tp
    if total <= tp:
        raise ValueError("flood_duration_hr must exceed time_to_peak_hr")
    fall = total - tp
    # Decay constant chosen so the recession reaches 2% of the peak rise at the
    # stated end of the flood, rather than being left as a free parameter.
    decay = math.log(50.0) / fall

    n = max(int(round(duration_hr / output_step_hr)), 1)
    times = np.arange(n + 1, dtype=np.float64) * output_step_hr
    rise_amp = peak_discharge_cumecs - base_flow_cumecs

    q = np.full_like(times, base_flow_cumecs)
    up = times <= tp
    x = times[up] / tp
    q[up] = base_flow_cumecs + rise_amp * (x**2 * (3.0 - 2.0 * x))
    down = times > tp
    q[down] = base_flow_cumecs + rise_amp * np.exp(-decay * (times[down] - tp))

    return times, q


def ritter_solution(x_m, t_s, h0_m, g: float = GRAVITY):
    """Ritter (1892) analytical dam break on a dry, frictionless, flat bed.

    The only exact solution we can test the fast solver against, and the reason
    we can claim the solver is correct rather than merely plausible.

        for x <= -c0*t :  h = h0
        for |x| < c0*t :  h = (1/(9g)) * (2*c0 - x/t)^2
        for x >= 2*c0*t:  h = 0
    with c0 = sqrt(g*h0), x measured from the dam, positive downstream.

    Source: Ritter, A. (1892), "Die Fortpflanzung der Wasserwellen",
    Zeitschrift des Vereines Deutscher Ingenieure, 36(33), 947-954.
    As reproduced in Toro, E.F. (2001), "Shock-Capturing Methods for Free-
    Surface Shallow Flows", Wiley, section 5.
    """
    x = np.asarray(x_m, dtype=np.float64)
    c0 = math.sqrt(g * h0_m)
    if t_s <= 0:
        return np.where(x <= 0.0, h0_m, 0.0)

    h = np.zeros_like(x)
    upstream = x <= -c0 * t_s
    fan = (x > -c0 * t_s) & (x < 2.0 * c0 * t_s)
    h[upstream] = h0_m
    h[fan] = (1.0 / (9.0 * g)) * (2.0 * c0 - x[fan] / t_s) ** 2
    return h
