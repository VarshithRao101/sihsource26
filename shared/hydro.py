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

FailureMode = Literal["overtopping", "piping", "gated_release", "blockage_breach"]


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
