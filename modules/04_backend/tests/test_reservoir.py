"""
Physics tests for the reservoir level model.

Same standard as test_solver_physics.py: every test has an answer that does not
depend on us - a conservation law, an equilibrium, or an exact inverse. Each
one prints the number it measured.

    python -m pytest modules/04_backend/tests/test_reservoir.py -v
"""

from __future__ import annotations

import importlib
import math

res = importlib.import_module("modules.04_backend.reservoir")


def _cfg(**kw):
    return res.ReservoirConfig(**kw)


# ==========================================================================
# Geometry
# ==========================================================================


def test_storage_curve_round_trips():
    """level_m(volume_m3(y)) == y. If the inverse is wrong everything is."""
    cfg = _cfg()
    worst = 0.0
    for frac in (0.05, 0.25, 0.5, 0.75, 1.0):
        y = frac * cfg.dam_height_m
        err = abs(res.level_m(res.volume_m3(y, cfg), cfg) - y)
        worst = max(worst, err)
    print(f"\n  storage curve round trip, worst error: {worst:.3e} m")
    assert worst < 1e-9


def test_prismatic_tank_is_exactly_y_equals_v_over_a():
    """With k = 1 the model must collapse to the textbook y = V / A.

    This is the case the brief asks for by name. It is not approximated here,
    it falls out of the same power law with the exponent set to one.
    """
    cfg = _cfg(storage_exponent=1.0, dam_height_m=40.0, capacity_mcm=2.0)
    area = res.surface_area_m2(0.0, cfg)
    assert math.isclose(area, cfg.capacity_m3() / cfg.dam_height_m, rel_tol=1e-12)

    worst = 0.0
    for frac in (0.1, 0.5, 0.9, 1.0):
        v = frac * cfg.capacity_m3()
        worst = max(worst, abs(res.level_m(v, cfg) - v / area))
    print(f"\n  y = V/A at k = 1, A = {area:,.0f} m2, worst error: {worst:.3e} m")
    assert worst < 1e-9


def test_surface_area_is_the_derivative_of_storage():
    """A(y) must equal dV/dy, checked against a finite difference."""
    cfg = _cfg()
    y = 0.6 * cfg.dam_height_m
    dy = 1e-4
    numeric = (res.volume_m3(y + dy, cfg) - res.volume_m3(y - dy, cfg)) / (2 * dy)
    analytic = res.surface_area_m2(y, cfg)
    rel = abs(numeric - analytic) / analytic
    print(f"\n  dV/dy at y = {y:.1f} m: analytic {analytic:,.0f}, "
          f"numeric {numeric:,.0f}, rel error {rel:.3e}")
    assert rel < 1e-6


# ==========================================================================
# Conservation - the one that matters
# ==========================================================================


def test_mass_is_conserved():
    """Storage change must equal inflow minus outflow minus overflow, exactly.

    This is what makes the on-screen animation trustworthy: the water in the
    picture is the water in the ledger.
    """
    out = res.simulate(_cfg(flood_peak_cumecs=600.0), hours=24.0)
    mb = out["mass_balance"]
    print(f"\n  mass balance error: {mb['error_pct']:.8f}% of inflow")
    assert abs(mb["error_pct"]) < 1e-9


def test_mass_is_conserved_while_overflowing():
    """The clamp at V_max must account for the surplus, not discard it."""
    cfg = _cfg(base_inflow_cumecs=4000.0, initial_volume_frac=0.95,
               target_release_cumecs=10.0, spillway_length_m=5.0)
    out = res.simulate(cfg, hours=6.0)
    mb = out["mass_balance"]
    print(f"\n  overflow {mb['overflow_volume_mcm']:.4f} MCM, "
          f"mass error {mb['error_pct']:.8f}%")
    assert mb["overflow_volume_mcm"] > 0.0
    assert abs(mb["error_pct"]) < 1e-9


# ==========================================================================
# Clamps
# ==========================================================================


def test_volume_stays_inside_zero_and_capacity():
    """0 <= V <= V_max under both a flood and a hard drawdown."""
    for cfg in (
        _cfg(base_inflow_cumecs=5000.0, initial_volume_frac=0.9),
        _cfg(base_inflow_cumecs=0.0, target_release_cumecs=3000.0,
             initial_volume_frac=0.2),
    ):
        out = res.simulate(cfg, hours=12.0)
        vols = [s["volume_m3"] for s in out["samples"]]
        assert min(vols) >= -1e-9
        assert max(vols) <= cfg.capacity_m3() + 1e-6
        print(f"\n  V range: {min(vols)/res.MCM:.4f} - {max(vols)/res.MCM:.4f} MCM "
              f"(capacity {cfg.capacity_mcm} MCM)")


def test_empty_reservoir_cannot_release_water():
    """When V hits zero the outflow is cut to what is actually available."""
    cfg = _cfg(base_inflow_cumecs=0.0, target_release_cumecs=5000.0,
               initial_volume_frac=0.05)
    out = res.simulate(cfg, hours=6.0)
    last = out["samples"][-1]
    mb = out["mass_balance"]
    print(f"\n  drained to V = {last['volume_mcm']:.6f} MCM, "
          f"released {mb['outflow_volume_mcm']:.4f} MCM of "
          f"{cfg.initial_volume_frac * cfg.capacity_mcm:.4f} available")
    assert last["volume_m3"] == 0.0
    assert mb["outflow_volume_mcm"] <= cfg.initial_volume_frac * cfg.capacity_mcm + 1e-9


# ==========================================================================
# Equilibrium and monotonicity - "does changing inflow really change y"
# ==========================================================================


def test_steady_state_balances_inflow_and_outflow():
    """Left alone, the level settles where Q_out(y) = Q_in. Nothing else."""
    cfg = _cfg(base_inflow_cumecs=120.0, target_release_cumecs=40.0)
    out = res.simulate(cfg, hours=200.0)
    last = out["samples"][-1]
    gap = abs(last["outflow_cumecs"] - last["inflow_cumecs"])
    print(f"\n  settled at y = {last['level_m']:.3f} m with "
          f"Qin {last['inflow_cumecs']:.2f} vs Qout {last['outflow_cumecs']:.2f} m3/s")
    assert gap < 0.05


def test_more_inflow_gives_a_higher_level():
    """The headline claim of the dashboard, tested rather than asserted."""
    levels = []
    for q in (30.0, 60.0, 120.0, 240.0):
        out = res.simulate(_cfg(base_inflow_cumecs=q), hours=48.0)
        levels.append(out["samples"][-1]["level_m"])
    print("\n  Qin -> y at 48 hr: " +
          ", ".join(f"{q:.0f} m3/s -> {y:.2f} m"
                    for q, y in zip((30, 60, 120, 240), levels)))
    assert all(b > a for a, b in zip(levels, levels[1:]))


def test_more_release_gives_a_lower_level():
    levels = []
    for q in (10.0, 40.0, 80.0):
        out = res.simulate(_cfg(target_release_cumecs=q), hours=48.0)
        levels.append(out["samples"][-1]["level_m"])
    print("\n  release -> y at 48 hr: " +
          ", ".join(f"{q:.0f} m3/s -> {y:.2f} m"
                    for q, y in zip((10, 40, 80), levels)))
    assert all(b < a for a, b in zip(levels, levels[1:]))


# ==========================================================================
# Numerics - the answer must not depend on the timestep
# ==========================================================================


def test_result_is_insensitive_to_timestep():
    """Halving dt must not move the answer. If it does, the integration is
    not converged and the animation speed would change the physics."""
    ys = []
    for dt in (30.0, 10.0, 2.0):
        out = res.simulate(_cfg(dt_s=dt, flood_peak_cumecs=600.0), hours=12.0)
        ys.append(out["samples"][-1]["level_m"])
    spread = max(ys) - min(ys)
    print(f"\n  y at 12 hr for dt = 30/10/2 s: "
          + ", ".join(f"{y:.4f}" for y in ys)
          + f"  spread {spread * 100:.3f} cm")
    assert spread < 0.01


# ==========================================================================
# Warning states
# ==========================================================================


def test_warning_states_follow_the_level():
    cfg = _cfg()
    h = cfg.dam_height_m
    cases = [
        (0.10 * h, "low"),
        (0.50 * h, "normal"),
        (0.90 * h, "high"),
        (1.00 * h, "overflow"),
    ]
    for y, expected in cases:
        got = res.status(y, cfg, overflowing=False)
        print(f"\n  y = {y:5.1f} m ({y / h:.0%} of height) -> {got}")
        assert got == expected
