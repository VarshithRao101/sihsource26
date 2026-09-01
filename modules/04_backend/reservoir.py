"""
modules/04_backend/reservoir.py - reservoir level routing, y = f(t, V).

The lumped counterpart to solver.py. solver.py routes water *downstream* over a
2D grid; this file routes water *inside* the impoundment, which is the thing an
operator actually watches on a gauge:

    dV/dt = Q_in(t) - Q_out(y)                   mass balance
    y     = level_from_storage(V)                geometry

It exists for three reasons. It is the live web simulation served at
/reservoir; it is the reference implementation the browser mirrors; and it is
the same fill-time integral blockage.time_to_overtop uses when a landslide dam
fills before it overtops (that case is this model with Q_out = 0).

Nothing here is new physics. The storage curve belongs to shared.hydro, and the
weir and orifice coefficients are the ones already cited there.

    .venv\\Scripts\\python.exe -m modules.04_backend.reservoir --hours 24

Owner: captain / person 4.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass

from shared.contract import GRAVITY
from shared.hydro import (
    ORIFICE_CD,
    WEIR_C_SI,
    level_from_storage,
    storage_from_level,
)

MCM = 1.0e6
"""m3 per million cubic metres. The contract reports volume in MCM."""

# Outlet-works coefficients now live in shared.hydro, which is also what
# gated_release_hydrograph uses. They were duplicated here with a comment
# saying "do not fork it to a different number" - importing them is how you
# actually guarantee that.
#   ORIFICE_CD  Fread, D.L. (1988), "BREACH", NOAA NWS, section 3
#   WEIR_C_SI   USACE HEC-RAS Hydraulic Reference Manual, weir flow


# ==========================================================================
# Configuration
# ==========================================================================


@dataclass
class ReservoirConfig:
    """Everything the model needs. One object, no globals.

    Defaults are Chungthang-sized (60 m, 5 MCM) so the numbers on screen are
    the same order as the rest of the project, but every field is a control in
    the web UI and nothing downstream depends on the defaults.
    """

    # --- geometry -------------------------------------------------------
    dam_height_m: float = 60.0
    capacity_mcm: float = 5.0
    storage_exponent: float = 2.7
    """k in V = V_max (y/H)^k. k = 1 is a vertical-walled tank, which is the
    y = V/A case; 2.7 is the steep-valley default from shared.hydro."""

    # --- inflow, Q_in(t) ------------------------------------------------
    base_inflow_cumecs: float = 60.0
    flood_peak_cumecs: float = 0.0
    flood_peak_time_hr: float = 6.0
    flood_duration_hr: float = 3.0

    # --- outflow, Q_out(y) ----------------------------------------------
    target_release_cumecs: float = 40.0
    """Controlled release at full supply level. This sizes the outlet; the
    actual release falls off as sqrt(head) once the reservoir draws down."""
    outlet_invert_frac: float = 0.05
    spillway_crest_frac: float = 0.85
    spillway_length_m: float = 60.0

    # --- state ----------------------------------------------------------
    initial_volume_frac: float = 0.50

    # --- numerics -------------------------------------------------------
    dt_s: float = 10.0
    max_volume_frac_per_step: float = 0.01
    """Euler is first order, so a step that would move more than this fraction
    of capacity gets subdivided. Keeps the integration honest when a user drags
    inflow to the top of its range."""

    # --- warning thresholds, fraction of dam height ---------------------
    low_frac: float = 0.25

    def capacity_m3(self) -> float:
        return self.capacity_mcm * MCM

    def spillway_crest_m(self) -> float:
        return self.spillway_crest_frac * self.dam_height_m

    def outlet_invert_m(self) -> float:
        return self.outlet_invert_frac * self.dam_height_m

    def outlet_area_m2(self) -> float:
        """Gate area that delivers target_release_cumecs at full supply level.

        Q = Cd A sqrt(2 g H)  ->  A = Q / (Cd sqrt(2 g H)).
        """
        head = max(self.dam_height_m - self.outlet_invert_m(), 1e-6)
        return self.target_release_cumecs / (
            ORIFICE_CD * math.sqrt(2.0 * GRAVITY * head)
        )

    def as_dict(self) -> dict:
        return asdict(self)


# ==========================================================================
# Geometry:  V <-> y
# ==========================================================================


def level_m(volume: float, cfg: ReservoirConfig) -> float:
    """y from V. The inverse storage curve, straight out of shared.hydro."""
    return level_from_storage(
        volume, cfg.dam_height_m, cfg.capacity_m3(), cfg.storage_exponent
    )


def volume_m3(level: float, cfg: ReservoirConfig) -> float:
    """V from y."""
    return storage_from_level(
        level, cfg.dam_height_m, cfg.capacity_m3(), cfg.storage_exponent
    )


def surface_area_m2(level: float, cfg: ReservoirConfig) -> float:
    """dV/dy at this level, m2 - the water surface area.

    For k = 1 this is the constant A of the y = V/A tank model, which is why
    the UI can show that identity holding exactly when the exponent is 1.
    """
    h = cfg.dam_height_m
    k = cfg.storage_exponent
    if h <= 0.0:
        return 0.0
    if level <= 0.0:
        return cfg.capacity_m3() * k / h if k == 1.0 else 0.0
    return cfg.capacity_m3() * k * (min(level, h) ** (k - 1.0)) / (h**k)


# ==========================================================================
# Q_in(t) and Q_out(y)
# ==========================================================================


def inflow_cumecs(t_s: float, cfg: ReservoirConfig) -> float:
    """Q_in(t): a steady base flow plus an optional Gaussian flood wave.

    The Gaussian is a shape, not a claim about any real catchment - the UI
    labels it a synthetic hydrograph. A measured one can be fed in instead;
    modules/07_ml/inflow.py produces one.
    """
    q = cfg.base_inflow_cumecs
    if cfg.flood_peak_cumecs > 0.0 and cfg.flood_duration_hr > 0.0:
        t_hr = t_s / 3600.0
        sigma = cfg.flood_duration_hr / 2.0
        z = (t_hr - cfg.flood_peak_time_hr) / sigma
        q += cfg.flood_peak_cumecs * math.exp(-0.5 * z * z)
    return q


def outflow_cumecs(level: float, cfg: ReservoirConfig) -> dict:
    """Q_out(y): controlled outlet + uncontrolled spillway. Both head-driven.

    outlet    Q = Cd A sqrt(2 g (y - y_invert))      orifice, Fread (1988)
    spillway  Q = C L (y - y_crest)^1.5              broad-crested weir

    Returns the split, not just the total, because the operator needs to see
    which one is carrying the water: a reservoir passing 800 m3/s over the
    spillway is a different situation from one releasing 800 through the gates.
    """
    outlet_head = max(level - cfg.outlet_invert_m(), 0.0)
    gate = ORIFICE_CD * cfg.outlet_area_m2() * math.sqrt(2.0 * GRAVITY * outlet_head)

    weir_head = max(level - cfg.spillway_crest_m(), 0.0)
    spill = WEIR_C_SI * cfg.spillway_length_m * weir_head**1.5

    return {"gate": gate, "spillway": spill, "total": gate + spill}


# ==========================================================================
# Warning state
# ==========================================================================


def status(level: float, cfg: ReservoirConfig, overflowing: bool) -> str:
    """LOW / NORMAL / HIGH / OVERFLOW, from the level alone.

    HIGH is not a percentage picked because it looks good: it is the spillway
    crest, the level at which the reservoir starts passing water it can no
    longer hold back.
    """
    if overflowing or level >= cfg.dam_height_m:
        return "overflow"
    if level >= cfg.spillway_crest_m():
        return "high"
    if level <= cfg.low_frac * cfg.dam_height_m:
        return "low"
    return "normal"


# ==========================================================================
# The step - Euler, clamped, substepped
# ==========================================================================


@dataclass
class State:
    """Everything that survives from one step to the next."""

    t_s: float = 0.0
    volume_m3: float = 0.0
    inflow_volume_m3: float = 0.0
    outflow_volume_m3: float = 0.0
    overflow_volume_m3: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def initial_state(cfg: ReservoirConfig) -> State:
    return State(t_s=0.0, volume_m3=cfg.initial_volume_frac * cfg.capacity_m3())


def sample(state: State, cfg: ReservoirConfig, q_over: float = 0.0,
           substeps: int = 0) -> dict:
    """The reading an instrument would take right now. No integration here."""
    y = level_m(state.volume_m3, cfg)
    q_in = inflow_cumecs(state.t_s, cfg)
    out = outflow_cumecs(y, cfg)
    overflowing = q_over > 0.0 or state.volume_m3 >= cfg.capacity_m3()
    return {
        "t_s": state.t_s,
        "t_hr": state.t_s / 3600.0,
        "volume_m3": state.volume_m3,
        "volume_mcm": state.volume_m3 / MCM,
        "level_m": y,
        "level_frac": y / cfg.dam_height_m if cfg.dam_height_m else 0.0,
        "area_m2": surface_area_m2(y, cfg),
        "inflow_cumecs": q_in,
        "outflow_cumecs": out["total"],
        "gate_cumecs": out["gate"],
        "spillway_cumecs": out["spillway"],
        "overflow_cumecs": q_over,
        "status": status(y, cfg, overflowing),
        "substeps": substeps,
    }


def step(state: State, cfg: ReservoirConfig, dt_s: float | None = None) -> dict:
    """Advance one timestep. Mutates `state`, returns the sample it produced.

        V_next = V + (Q_in - Q_out) * dt,      0 <= V <= V_max

    Two clamps, and both are reported rather than hidden:
      * V_next > V_max - the surplus leaves as overflow, V_max is held.
      * V_next < 0     - you cannot release water that is not there, so the
                         outflow is cut to what the reservoir plus the inflow
                         can actually supply.

    The mass ledger lives in the state so the caller can close the balance and
    show that nothing was invented or lost.
    """
    dt = cfg.dt_s if dt_s is None else dt_s

    y = level_m(state.volume_m3, cfg)
    net = inflow_cumecs(state.t_s, cfg) - outflow_cumecs(y, cfg)["total"]
    budget = cfg.max_volume_frac_per_step * cfg.capacity_m3()
    n_sub = 1
    if budget > 0.0 and abs(net) * dt > budget:
        n_sub = min(int(math.ceil(abs(net) * dt / budget)), 1000)
    h = dt / n_sub

    v = state.volume_m3
    v_max = cfg.capacity_m3()
    over_volume = 0.0

    for _ in range(n_sub):
        y = level_m(v, cfg)
        q_in = inflow_cumecs(state.t_s, cfg)
        q_out = outflow_cumecs(y, cfg)["total"]

        v_next = v + (q_in - q_out) * h
        if v_next > v_max:
            over_volume += v_next - v_max
            state.overflow_volume_m3 += v_next - v_max
            v_next = v_max
        elif v_next < 0.0:
            q_out = v / h + q_in           # drain what exists, no more
            v_next = 0.0

        state.inflow_volume_m3 += q_in * h
        state.outflow_volume_m3 += q_out * h
        state.t_s += h
        v = v_next

    state.volume_m3 = v
    return sample(state, cfg, q_over=over_volume / dt if dt > 0 else 0.0,
                  substeps=n_sub)


def simulate(cfg: ReservoirConfig, hours: float, sample_every_s: float = 60.0) -> dict:
    """Run the model start to finish. Used by the API and by the tests.

    Returns the sampled trajectory plus the closed mass balance. The mass error
    is reported the way the solver reports its own: as a percentage of the
    water that entered, computed rather than asserted.
    """
    state = initial_state(cfg)
    v_start = state.volume_m3
    samples = [sample(state, cfg)]

    n_steps = int(round(hours * 3600.0 / cfg.dt_s))
    every = max(1, int(round(sample_every_s / cfg.dt_s)))
    for i in range(1, n_steps + 1):
        s = step(state, cfg)
        if i % every == 0 or i == n_steps:
            samples.append(s)

    stored = state.volume_m3 - v_start
    accounted = (
        state.inflow_volume_m3 - state.outflow_volume_m3 - state.overflow_volume_m3
    )
    denom = max(state.inflow_volume_m3, 1e-9)

    return {
        "config": cfg.as_dict(),
        "samples": samples,
        "mass_balance": {
            "initial_volume_mcm": v_start / MCM,
            "final_volume_mcm": state.volume_m3 / MCM,
            "inflow_volume_mcm": state.inflow_volume_m3 / MCM,
            "outflow_volume_mcm": state.outflow_volume_m3 / MCM,
            "overflow_volume_mcm": state.overflow_volume_m3 / MCM,
            "error_pct": 100.0 * (stored - accounted) / denom,
        },
        "assumptions": ASSUMPTIONS,
        "is_fake": False,
    }


ASSUMPTIONS = [
    "Storage curve V = V_max (y/H)^k - no surveyed elevation-capacity table "
    "exists for an arbitrary dam. Source: USACE HEC-RAS reservoir storage "
    "approximation, via shared.hydro.storage_from_level.",
    "Outlet is a single orifice, Cd = 0.6 (Fread 1988, BREACH, section 3).",
    "Spillway is an uncontrolled broad-crested weir, C = 1.7 SI (USACE HEC-RAS).",
    "Inflow is a steady base flow plus an optional synthetic Gaussian flood "
    "wave. It is not a measured hydrograph.",
    "Euler integration, first order, substepped so no step moves more than a "
    "set fraction of capacity.",
]


# ==========================================================================
# CLI
# ==========================================================================


def _main() -> None:
    p = argparse.ArgumentParser(description="Reservoir level routing, y = f(t, V).")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--height", type=float, default=60.0)
    p.add_argument("--capacity", type=float, default=5.0, help="MCM")
    p.add_argument("--inflow", type=float, default=60.0, help="m3/s")
    p.add_argument("--release", type=float, default=40.0, help="m3/s at full head")
    p.add_argument("--initial", type=float, default=0.5, help="fraction of capacity")
    p.add_argument("--flood-peak", type=float, default=0.0, help="m3/s")
    p.add_argument("--exponent", type=float, default=2.7)
    a = p.parse_args()

    cfg = ReservoirConfig(
        dam_height_m=a.height,
        capacity_mcm=a.capacity,
        storage_exponent=a.exponent,
        base_inflow_cumecs=a.inflow,
        target_release_cumecs=a.release,
        initial_volume_frac=a.initial,
        flood_peak_cumecs=a.flood_peak,
    )
    res = simulate(cfg, a.hours, sample_every_s=a.hours * 3600.0 / 12.0)

    print(f"  {'t (hr)':>8} {'V (MCM)':>10} {'y (m)':>9} {'Qin':>9} {'Qout':>9} status")
    for s in res["samples"]:
        print(
            f"  {s['t_hr']:8.2f} {s['volume_mcm']:10.3f} {s['level_m']:9.2f} "
            f"{s['inflow_cumecs']:9.1f} {s['outflow_cumecs']:9.1f} {s['status']}"
        )
    mb = res["mass_balance"]
    print(
        f"\n  mass balance: in {mb['inflow_volume_mcm']:.4f} MCM, "
        f"out {mb['outflow_volume_mcm']:.4f}, "
        f"overflow {mb['overflow_volume_mcm']:.4f}, "
        f"error {mb['error_pct']:.6f}%"
    )


if __name__ == "__main__":
    _main()
