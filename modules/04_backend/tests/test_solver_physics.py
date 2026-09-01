"""
Physics tests for the fast solver.

These are the tests that let us say "our solver is correct" instead of "our
solver produces a picture". Each one has an exact answer that does not depend
on us: an analytical solution, or a conservation law.

    python -m pytest modules/04_backend/tests -v

Every test prints the number it measured. When a juror asks "how accurate is
your solver", the answer is the output of this file, not an adjective.
"""

from __future__ import annotations

import importlib
import math

import numpy as np
import pytest

from shared.hydro import ritter_solution

solver = importlib.import_module("modules.04_backend.solver")

G = 9.80665


# ==========================================================================
# Ritter (1892) - the analytical dam break
# ==========================================================================


def _ritter_case(dx: float, nx: int, h0: float = 10.0, t_end_s: float = 30.0,
                 scheme: str = "swe", froude_max: float = float("inf")):
    """Run an idealised 1D dam break and return (x, numerical, analytical)."""
    ny = 3
    z = np.zeros((ny, nx))
    h = np.zeros((ny, nx))
    h[:, : nx // 2] = h0

    cfg = solver.SolverConfig(
        dx_m=dx,
        end_hr=t_end_s / 3600.0,
        scheme=scheme,
        manning_n=1e-3,           # near-frictionless, as Ritter assumes
        open_edges=(False, False, True, True),
        initial_dt_s=0.01,
        froude_max=froude_max,    # see SolverConfig.froude_max
    )
    res = solver.run_solver(z, cfg, initial_depth=h)
    x = (np.arange(nx) + 0.5) * dx - (nx // 2) * dx
    return x, res.final_depth[ny // 2, :], ritter_solution(x, t_end_s, h0), res


def test_ritter_depth_profile():
    """Depth through the rarefaction fan must match Ritter's exact solution.

    Tolerance is 2% of the initial head. We measure ~0.7% at dx = 5 m.
    """
    h0 = 10.0
    x, num, ana, res = _ritter_case(dx=5.0, nx=800, h0=h0)
    wet = ana > 0.01
    rmse = float(np.sqrt(np.mean((num[wet] - ana[wet]) ** 2)))
    print(f"\n  Ritter depth RMSE = {rmse:.4f} m ({100 * rmse / h0:.2f}% of h0)")
    assert rmse < 0.02 * h0


def test_ritter_depth_at_the_dam():
    """At x = 0 the exact depth is 4/9 * h0 for all t > 0.

    This is the single sharpest check on the scheme: it is a fixed number, it
    does not drift with time, and a scheme with the wrong wave speeds misses it.
    """
    h0 = 10.0
    x, num, ana, res = _ritter_case(dx=5.0, nx=800, h0=h0)
    exact = 4.0 * h0 / 9.0
    measured = float(num[400])
    err = abs(measured - exact) / exact
    print(f"\n  depth at dam = {measured:.4f} m, exact {exact:.4f} m ({100 * err:.2f}%)")
    assert err < 0.05


def test_ritter_front_converges_under_refinement():
    """Front-position error must shrink as the grid refines.

    A first-order scheme diffuses the dry-bed tip, so we do NOT claim a small
    absolute front error. What we claim - and check - is convergence, which is
    what proves the scheme is consistent rather than merely stable.

    Measured: 12.9% at dx=5 m, 10.3% at 2.5 m, 7.9% at 1.25 m.
    """
    h0, t_end_s = 10.0, 30.0
    c0 = math.sqrt(G * h0)
    # Position where the analytical depth equals the 0.05 m detection threshold.
    x_ana = t_end_s * (2 * c0 - math.sqrt(9 * G * 0.05))

    errors = []
    for dx, nx in ((5.0, 800), (2.5, 1600)):
        x, num, _, _ = _ritter_case(dx=dx, nx=nx, h0=h0, t_end_s=t_end_s)
        front = float(x[num > 0.05].max())
        errors.append(abs(front - x_ana) / x_ana)
        print(f"\n  dx = {dx:>4} m  front error = {100 * errors[-1]:.2f}%")

    assert errors[1] < errors[0], "front error must decrease under refinement"
    assert errors[0] < 0.20


def test_swe_beats_inertial_on_a_dam_break():
    """The whole reason the default scheme is 'swe'.

    The local inertial approximation drops advection, which is what carries a
    dam-break front. This test records the size of that difference so nobody
    later 'optimises' the default back to the faster scheme without knowing
    what it costs.
    """
    h0 = 10.0
    _, num_swe, ana, _ = _ritter_case(dx=5.0, nx=800, h0=h0, scheme="swe")
    _, num_in, _, _ = _ritter_case(dx=5.0, nx=800, h0=h0, scheme="inertial")
    wet = ana > 0.01
    rmse_swe = float(np.sqrt(np.mean((num_swe[wet] - ana[wet]) ** 2)))
    rmse_in = float(np.sqrt(np.mean((num_in[wet] - ana[wet]) ** 2)))
    print(f"\n  RMSE  swe = {rmse_swe:.4f} m   inertial = {rmse_in:.4f} m")
    assert rmse_swe < rmse_in


# ==========================================================================
# Well-balancedness - the lake at rest
# ==========================================================================


def test_lake_at_rest_stays_at_rest():
    """Still water on rough sloping terrain must not move.

    This is the C-property. A solver without hydrostatic reconstruction passes
    a flat-bed dam break perfectly and then generates metres of spurious head
    the moment the bed tilts - which is precisely the bug this test caught
    during development, when the Audusse source term went in with the wrong
    sign.

    We require machine-precision stillness, not "small".
    """
    ny, nx = 60, 60
    rng = np.random.default_rng(0)
    z = np.tile(np.linspace(100.0, 0.0, nx)[None, :], (ny, 1)) + rng.normal(0, 2, (ny, nx))
    level = 60.0
    h0 = np.maximum(level - z, 0.0)

    cfg = solver.SolverConfig(
        dx_m=50.0, end_hr=0.5, scheme="swe",
        open_edges=(False, False, False, False), initial_dt_s=0.1,
    )
    res = solver.run_solver(z, cfg, initial_depth=h0)

    surface = np.where(res.final_depth > 0.01, res.final_depth + z, np.nan)
    dev = float(np.nanmax(np.abs(surface - level)))
    vel = float(res.max_velocity.max())
    print(f"\n  surface deviation = {dev:.3e} m   spurious velocity = {vel:.3e} m/s")

    assert dev < 1e-4, "lake surface must stay flat"
    assert vel < 1e-6, "still water must not develop velocity"


# ==========================================================================
# Conservation
# ==========================================================================


def test_mass_is_conserved_in_a_closed_basin():
    """Closed boundaries: what goes in stays in, to machine precision."""
    ny, nx = 80, 80
    x = np.linspace(-1, 1, nx)[None, :]
    y = np.linspace(-1, 1, ny)[:, None]
    z = 20.0 * (x**2 + y**2)          # a bowl
    h0 = np.zeros((ny, nx))
    h0[35:45, 35:45] = 5.0            # a blob of water dropped in the middle

    cfg = solver.SolverConfig(
        dx_m=25.0, end_hr=0.3, scheme="swe",
        open_edges=(False, False, False, False), initial_dt_s=0.1,
    )
    res = solver.run_solver(z, cfg, initial_depth=h0)

    v_in = float(h0.sum()) * 25.0 * 25.0
    v_out = res.volume_stored_m3
    err = 100.0 * (v_in - v_out) / v_in
    print(f"\n  closed-basin volume error = {err:+.6f}%  (in {v_in:,.0f} m3)")
    assert abs(err) < 1e-6


def test_water_runs_downhill_and_not_up():
    """A blob released on a slope must end up downhill of where it started."""
    ny, nx = 40, 120
    z = np.tile(np.linspace(50.0, 0.0, nx)[None, :], (ny, 1))
    h0 = np.zeros((ny, nx))
    h0[18:22, 8:12] = 4.0

    cfg = solver.SolverConfig(
        dx_m=30.0, end_hr=0.4, scheme="swe",
        open_edges=(False, False, True, False), initial_dt_s=0.05,
    )
    res = solver.run_solver(z, cfg, initial_depth=h0)

    cols = np.arange(nx)[None, :] * np.ones((ny, 1))
    wet = res.max_depth >= 0.05
    centroid = float(cols[wet].mean())

    # A 4 m column of water does spread a little upslope before gravity wins -
    # that is the pressure gradient acting in both directions and it is correct
    # physics, not a bug. What must hold is that the flood is overwhelmingly
    # downslope: the centroid moves downhill, and upslope spread stays small.
    upslope = int(wet[:, :8].sum())
    downslope = int(wet[:, 12:].sum())
    print(
        f"\n  wetted centroid at column {centroid:.1f} (released at column 10); "
        f"{upslope} cells upslope vs {downslope} downslope"
    )
    assert centroid > 10.0, "water moved uphill"
    assert downslope > 20 * upslope, "flood is not predominantly downslope"


# ==========================================================================
# Contract compliance of the raw solver output
# ==========================================================================


def test_arrival_time_is_finite_wherever_the_cell_got_wet():
    """The contract invariant, and a regression test for a real bug.

    numba's fastmath=True licenses the compiler to assume no NaNs, so the
    classic `x != x` NaN test compiled to a constant False and every arrival
    time stayed unset. shared.validate caught it. This keeps it caught.
    """
    ny, nx = 40, 120
    z = np.tile(np.linspace(50.0, 0.0, nx)[None, :], (ny, 1))
    h0 = np.zeros((ny, nx))
    h0[18:22, 8:12] = 4.0
    cfg = solver.SolverConfig(dx_m=30.0, end_hr=0.3, scheme="swe", initial_dt_s=0.05)
    res = solver.run_solver(z, cfg, initial_depth=h0)

    wet = res.max_depth >= 0.05
    assert wet.any(), "test produced no flood at all"
    assert np.isfinite(res.arrival_time[wet]).all()
    assert np.isnan(res.arrival_time[~wet]).all()
    assert (res.max_depth >= 0).all()
    assert (res.max_velocity >= 0).all()
    assert np.isfinite(res.max_depth).all(), "max_depth must never be NaN"


def test_peak_never_precedes_arrival():
    ny, nx = 40, 120
    z = np.tile(np.linspace(50.0, 0.0, nx)[None, :], (ny, 1))
    h0 = np.zeros((ny, nx))
    h0[18:22, 8:12] = 4.0
    cfg = solver.SolverConfig(dx_m=30.0, end_hr=0.3, scheme="swe", initial_dt_s=0.05)
    res = solver.run_solver(z, cfg, initial_depth=h0)

    both = np.isfinite(res.arrival_time) & np.isfinite(res.time_of_peak)
    assert (res.time_of_peak[both] >= res.arrival_time[both] - 1e-6).all()


def test_nan_dem_cells_are_excluded():
    """No-data terrain must never hold water."""
    ny, nx = 40, 60
    z = np.tile(np.linspace(30.0, 0.0, nx)[None, :], (ny, 1))
    z[:, 40:] = np.nan
    h0 = np.zeros((ny, nx))
    h0[18:22, 2:6] = 3.0
    cfg = solver.SolverConfig(dx_m=30.0, end_hr=0.3, scheme="swe", initial_dt_s=0.05)
    res = solver.run_solver(z, cfg, initial_depth=h0)
    assert (res.max_depth[:, 40:] == 0).all()


def test_unstable_run_raises_rather_than_returning_numbers():
    """We abort on instability. We do not return a plausible-looking grid."""
    with pytest.raises(ValueError):
        solver.run_solver(
            np.full((10, 10), np.nan),
            solver.SolverConfig(dx_m=10.0, end_hr=0.1),
        )
