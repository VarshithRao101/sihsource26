"""
integration/run_all.py - the pipeline smoke test.

    python integration/run_all.py

Run this before every push. It exercises one path through every module that
exists and fails loudly on the first thing that is broken. It is deliberately
fast - well under a minute - because a gate nobody runs is not a gate.

What it does NOT do: hit the network. Every check here works with the wifi
unplugged, so it can be run on the morning of the pitch. Checks that need
Earth Engine or OpenTopography are listed as skipped, with the reason, rather
than silently passing.

Exit code 0 means the pipeline holds together. Anything else means stop and fix
it before pushing.

Owner: captain.
"""

from __future__ import annotations

import shutil
import sys
import time
import traceback
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SCRATCH = REPO_ROOT / "outputs" / "_integration"


class Check:
    """One named step, with its own timing and its own failure message."""

    def __init__(self, results: list):
        self.results = results

    def __call__(self, name: str, fn, required: bool = True):
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            self.results.append(("pass", name, detail, time.perf_counter() - t0))
        except _Skip as exc:
            self.results.append(("skip", name, str(exc), time.perf_counter() - t0))
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if not required:
                self.results.append(("warn", name, detail, time.perf_counter() - t0))
            else:
                self.results.append(("FAIL", name, detail, time.perf_counter() - t0))
                if "-v" in sys.argv:
                    traceback.print_exc()


class _Skip(Exception):
    """Raised by a check that cannot run here, with the reason."""


# ==========================================================================
# The checks
# ==========================================================================


def check_contract():
    from shared.contract import SCHEMA_VERSION, WET_THRESHOLD_M, hazard_class

    assert hazard_class(0.0, 0.0) == "none"
    assert hazard_class(8.0, 3.0) == "extreme"
    assert 0.0 < WET_THRESHOLD_M < 1.0
    return f"schema {SCHEMA_VERSION}, wet threshold {WET_THRESHOLD_M} m"


def check_geo():
    from shared.geo import Grid, bbox_around, haversine_km, utm_epsg

    g = Grid.from_bbox_cellsize((88.3, 27.2, 88.6, 27.7), 90.0)
    r, c = g.rowcol(88.45, 27.45)
    lon, lat = g.lonlat(r, c)
    assert abs(lon - 88.45) < 0.01 and abs(lat - 27.45) < 0.01, "rowcol/lonlat disagree"
    assert utm_epsg(88.4, 27.5) == "EPSG:32645"
    d = haversine_km(88.0, 27.0, 88.0, 28.0)
    assert 110 < d < 112, f"a degree of latitude should be ~111 km, got {d}"
    return f"{g.nx}x{g.ny} grid, cell {g.cellsize_m():.1f} m"


def check_hydro():
    from shared.hydro import breach_hydrograph, froehlich_2008, peak_outflow_regressions

    b = froehlich_2008(5e6, 60.0, "overtopping")
    assert b.formation_time_hr > 0 and b.average_width_m > 0
    t, q = breach_hydrograph(b, dam_height_m=60.0, capacity_m3=5e6, duration_hr=6.0)
    assert t[0] == 0.0 and (q >= 0).all()

    # The routed peak must sit inside the empirical envelope, or one of the two
    # is wrong and we need to know which before a juror asks.
    regs = peak_outflow_regressions(5e6, 60.0)
    lo, hi = min(regs.values()), max(regs.values())
    peak = float(q.max())
    assert lo * 0.5 <= peak <= hi * 2.0, (
        f"routed peak {peak:,.0f} is far outside the regression envelope "
        f"[{lo:,.0f}, {hi:,.0f}]"
    )
    return f"peak {peak:,.0f} m3/s inside envelope [{lo:,.0f}, {hi:,.0f}]"


def check_fake_run():
    from shared.fake import generate_fake_run
    from shared.validate import validate_run

    run_dir = generate_fake_run(
        run_id="integration_overtop_fast_001", outputs_dir=SCRATCH, nx=80, ny=120, end_hr=6.0
    )
    rep = validate_run(run_dir)
    assert rep.ok, "synthetic run failed the validator:\n  " + "\n  ".join(rep.errors)
    assert rep.warnings, "a fake run must warn that it is fake"
    return "contract-valid, flagged is_fake"


def check_solver_physics():
    """Ritter's analytical dam break. The only exact answer we can check against."""
    import numpy as np

    from shared.hydro import ritter_solution

    solver = import_module("modules.04_backend.solver")

    nx, ny, dx = 400, 3, 5.0
    z = np.zeros((ny, nx))
    h0 = 10.0
    h = np.zeros((ny, nx))
    h[:, : nx // 2] = h0

    cfg = solver.SolverConfig(
        dx_m=dx, end_hr=8.0 / 3600.0, open_edges=(False, False, True, True),
        manning_n=0.0, scheme="swe",
    )
    res = solver.run_solver(z, cfg, initial_depth=h)

    x = (np.arange(nx) - nx // 2 + 0.5) * dx
    exact = ritter_solution(x, 8.0, h0)
    got = res.final_depth[ny // 2]
    band = (x > -60) & (x < 60)
    rmse = float(np.sqrt(np.mean((got[band] - exact[band]) ** 2)))
    assert rmse < 1.0, f"Ritter RMSE {rmse:.3f} m is too large"
    return f"Ritter RMSE {rmse:.3f} m over the rarefaction"


def check_solver_mass():
    """A closed basin must conserve mass exactly."""
    import numpy as np

    solver = import_module("modules.04_backend.solver")

    ny, nx, dx = 60, 60, 50.0
    z = np.zeros((ny, nx))
    t = np.array([0.0, 0.1, 0.4, 0.6])
    q = np.array([0.0, 400.0, 400.0, 0.0])
    cfg = solver.SolverConfig(
        dx_m=dx, end_hr=1.5, open_edges=(False, False, False, False)
    )
    res = solver.run_solver(
        z, cfg, inflow_hydrograph=(t, q), inflow_cells=[(30, 30)]
    )
    # Judge against the contract's tolerance, not an ad-hoc number invented
    # here. One definition of "acceptable mass error" for the whole project,
    # and it lives in shared/contract.py where the validator reads it too.
    #
    # The residual on a flat bed sits in the wet/dry front: a first-order
    # scheme clamps thin films at the advancing edge, and a flat basin has a
    # very long perimeter of them. Curved terrain, where the front is confined
    # to a channel, comes out an order of magnitude cleaner.
    from shared.contract import MASS_BALANCE_TOLERANCE_PCT

    err = abs(res.mass_balance_err_pct)
    assert err < MASS_BALANCE_TOLERANCE_PCT, (
        f"closed basin mass error {res.mass_balance_err_pct:+.3f}% exceeds the "
        f"contract tolerance of {MASS_BALANCE_TOLERANCE_PCT}%"
    )
    return f"closed-basin mass error {res.mass_balance_err_pct:+.4f}%"


def check_solver_no_boundary_inflow():
    """Open boundaries must never ADD water.

    Regression test for a real bug: the zero-gradient ghost cell is symmetric,
    so where flow near an edge pointed inward the domain manufactured volume.
    On flat terrain the Hirakud run imported 48,332 MCM through its own edges.
    """
    import numpy as np

    solver = import_module("modules.04_backend.solver")

    ny, nx, dx = 50, 50, 100.0
    z = np.full((ny, nx), 100.0)  # perfectly flat: worst case for this bug
    t = np.array([0.0, 0.2, 0.5])
    q = np.array([0.0, 800.0, 0.0])
    cfg = solver.SolverConfig(dx_m=dx, end_hr=2.0, open_edges=(True, True, True, True))
    res = solver.run_solver(z, cfg, inflow_hydrograph=(t, q), inflow_cells=[(25, 25)])

    assert res.volume_out_m3 >= -1.0, (
        f"boundary let {-res.volume_out_m3 / 1e6:.2f} MCM INTO the domain - "
        f"open edges must be outflow-only"
    )
    from shared.contract import MASS_BALANCE_TOLERANCE_PCT

    assert abs(res.mass_balance_err_pct) < MASS_BALANCE_TOLERANCE_PCT, (
        f"mass error {res.mass_balance_err_pct:+.3f}% on a flat open domain"
    )
    return f"no inflow; mass error {res.mass_balance_err_pct:+.4f}%"


def check_scenario_to_run():
    """A whole scenario, on synthetic terrain, through the validator."""
    from shared.validate import validate_run

    sc = import_module("modules.04_backend.scenario")
    rn = import_module("modules.04_backend.runner")

    site = sc.SiteSpec(
        name="Integration Dam", river="Test", state="Test",
        lat=27.6, lon=88.6, dam_height_m=50.0, reservoir_capacity_mcm=4.0,
    )
    spec = sc.ScenarioSpec(
        site=site, reach_length_km=15.0, cellsize_m=150.0, end_hr=4.0
    )
    run_dir = rn.run_scenario(
        spec, outputs_dir=SCRATCH, run_id="integration_overtop_fast_002"
    )
    rep = validate_run(run_dir)
    assert rep.ok, "scenario run failed the validator:\n  " + "\n  ".join(rep.errors)
    return "scenario -> run folder -> validator"


def check_river_blockage():
    """River blockage is a real scenario, not an enum value.

    NTRO asks for dam break AND river blockage. This checks the three things
    that make a blockage different: storage read off the DEM, a fill time, and
    natural-dam breach regressions that give a wider, faster breach than an
    engineered embankment of the same height.
    """
    import numpy as np

    from shared.geo import Grid

    bl = import_module("modules.04_backend.blockage")

    # A synthetic V-valley draining south, blocked partway down.
    ny, nx = 60, 40
    rows = np.arange(ny)[:, None]
    cols = np.arange(nx)[None, :]
    dem = 1000.0 - 2.0 * rows + 0.8 * np.abs(cols - nx // 2)
    grid = Grid(bbox=(88.0, 27.0, 88.2, 27.3), nx=nx, ny=ny)

    tr = import_module("modules.01_geodata.terrain")
    filled = tr.fill_depressions(dem)
    direction = tr.d8_flow_direction(filled, grid.cellsize_m())

    lake = bl.impounded_volume(filled, grid, (30, nx // 2), 40.0, direction)
    assert lake["volume_m3"] > 0, "blockage impounds nothing"
    assert lake["cells"] < ny * nx * 0.5, (
        f"lake covers {lake['cells']} of {ny * nx} cells - it is flooding "
        f"downstream, which means the upstream traversal is broken"
    )
    # The deepest lake cell can never be deeper than the blockage is tall, and
    # sits within one cell's bed drop of it - the debris cell itself is not
    # lake, so the deepest water is the cell immediately upstream. On this
    # synthetic 2 m/row valley that is 38 m behind a 40 m blockage, and on the
    # real Teesta channel it comes out at the full height.
    assert 0.9 * 40.0 <= lake["max_depth_m"] <= 40.0, (
        f"deepest lake cell should be just under the {40.0} m blockage height, "
        f"got {lake['max_depth_m']} m"
    )

    # Natural dams breach wider than engineered ones of the same height.
    from shared.hydro import froehlich_2008

    nat = bl.blockage_breach(lake["volume_m3"], 40.0)
    eng = froehlich_2008(lake["volume_m3"], 40.0, "overtopping")
    assert nat.average_width_m > eng.average_width_m, (
        "a landslide dam must breach wider than a compacted embankment"
    )

    fill = bl.time_to_overtop(lake["volume_m3"], 50.0)
    assert fill["hours"] and fill["hours"] > 0

    return (
        f"lake {lake['volume_mcm']} MCM, breach {nat.average_width_m:.0f} m "
        f"vs {eng.average_width_m:.0f} m engineered, fills in {fill['hours']:.1f} hr"
    )


def check_inflow_timestep_limit():
    """A violent breach must not manufacture water.

    Regression test: 30,000 m3/s injected into a handful of cells used to
    outrun the wave-speed CFL, drive neighbouring cells negative, and gain
    2.5% mass through the positivity clamp. The timestep is now limited by the
    inflow as well.
    """
    import numpy as np

    solver = import_module("modules.04_backend.solver")

    ny, nx, dx = 60, 60, 90.0
    z = 500.0 - 1.5 * np.arange(ny)[:, None] * np.ones((1, nx))
    t = np.array([0.0, 0.05, 0.2, 0.5])
    q = np.array([0.0, 30000.0, 30000.0, 0.0])
    cells = [(2, nx // 2 + k) for k in (-2, -1, 0, 1, 2)]

    cfg = solver.SolverConfig(dx_m=dx, end_hr=1.5, cfl=0.45)
    res = solver.run_solver(z, cfg, inflow_hydrograph=(t, q), inflow_cells=cells)

    from shared.contract import MASS_BALANCE_TOLERANCE_PCT

    err = abs(res.mass_balance_err_pct)
    assert err < MASS_BALANCE_TOLERANCE_PCT, (
        f"violent inflow gained {res.mass_balance_err_pct:+.3f}% mass at the "
        f"default CFL - the inflow timestep limit has regressed"
    )
    return f"30,000 m3/s into 5 cells: mass error {res.mass_balance_err_pct:+.4f}%"


def check_damage_model():
    dm = import_module("modules.07_ml.damage")

    assert abs(float(dm.damage_factor(1.0)) - 0.55) < 1e-9, "JRC curve changed"
    assert float(dm.damage_factor(100.0)) == 1.0

    out = dm.total_damage(
        [{"name": "X", "population": 1000, "max_depth_m": 2.0, "max_velocity_ms": 1.0}]
    )
    assert out["damage_inr_crore"] > 0
    assert out["damage_curve_source"], "money figure must carry its citation"
    return f"{out['damage_inr_crore']} crore for a 1,000-person test settlement"


def check_dam_catalogue():
    dams = import_module("modules.01_geodata.dams")
    try:
        rows = dams.search(q="hirakud")
    except FileNotFoundError as exc:
        raise _Skip("catalogue not built: python -m modules.01_geodata.dams build") from exc

    assert rows, "Hirakud should be in the National Register"
    h = rows[0]
    assert h["state"] == "Odisha" and 60 < h["height_m"] < 62
    return f"{len(dams.load_catalogue()):,} dams, {len(dams.states())} states"


def check_sar_metrics():
    import numpy as np

    sar = import_module("modules.06_gee_validation.sar")

    a = np.zeros((10, 10), bool); a[2:6, 2:6] = True
    assert sar.agreement(a, a).csi == 1.0
    b = np.zeros((10, 10), bool); b[8:10, 8:10] = True
    assert sar.agreement(a, b).csi == 0.0
    return "CSI/POD/FAR agree on identical and disjoint masks"


def check_api_imports():
    api = import_module("modules.04_backend.api")
    paths = {r.path for r in api.app.routes}
    for needed in ("/api/runs", "/api/dams", "/api/dams/states", "/ws/runs/{run_id}"):
        assert needed in paths, f"missing route {needed}"
    ui = REPO_ROOT / "modules" / "05_frontend" / "index.html"
    assert ui.exists(), "the console is missing"
    return f"{len(paths)} routes, console present"


def check_gated_release():
    """A controlled release is not a relabelled dam break.

    NTRO asks for "dam break OR water release". For a long time gated_release
    was an enum value that returned byte-identical breach parameters to piping
    and had no branch anywhere in the pipeline - a label, not a scenario. This
    asserts the two are actually different physics, so it cannot silently
    become a label again.
    """
    import numpy as np

    from shared.hydro import (
        breach_hydrograph,
        froehlich_2008,
        gated_release_hydrograph,
    )

    V, H, DESIGN = 63.16e6, 25.0, 8069.0  # Annamayya, from the CWC register

    breach = froehlich_2008(V, H, "overtopping")
    _, q_breach = breach_hydrograph(breach, dam_height_m=H, capacity_m3=V, duration_hr=24.0)

    t, q, rel = gated_release_hydrograph(
        dam_height_m=H, capacity_m3=V, design_spillway_cumecs=DESIGN, duration_hr=24.0
    )

    assert q.max() < q_breach.max(), (
        f"a controlled release ({q.max():,.0f}) cannot peak above a dam break "
        f"({q_breach.max():,.0f}) on the same reservoir"
    )
    assert q.max() <= DESIGN * 1.001, (
        f"release {q.max():,.0f} exceeds the structure's design capacity {DESIGN:,.0f}"
    )
    assert rel.capacity_source.startswith("CWC"), (
        f"design capacity should come from the register, got {rel.capacity_source!r}"
    )

    # Closing the gates must reduce the peak, or the opening does nothing.
    _, q_half, _ = gated_release_hydrograph(
        dam_height_m=H, capacity_m3=V, design_spillway_cumecs=DESIGN,
        gate_opening_frac=0.25, duration_hr=24.0,
    )
    assert q_half.max() < q.max(), "gate_opening_frac has no effect on the release"

    assert t[0] == 0.0 and np.all(np.diff(t) > 0), "time series must start at 0 and increase"

    ratio = q_breach.max() / q.max()
    return (
        f"release {q.max():,.0f} vs breach {q_breach.max():,.0f} m3/s "
        f"({ratio:.1f}x), capped at the register's design capacity"
    )


def check_delft3d_absence():
    """Delft3D absence is measured, not assumed.

    NTRO asked for Delft3D. We do not have it, and the one thing worse than
    not having it is claiming we do. This asserts the detector actually looks
    and reports honestly - and that it never mistakes the Deltares licence
    manager, which is a separate download, for a solver.
    """
    d3d = import_module("modules.03_delft3d.engine")
    st = d3d.status()

    assert isinstance(st["installed"], bool), "installed must be a real boolean"
    assert st["searched"], "detector reported nothing searched - it did not look"

    if st["installed"]:
        from pathlib import Path

        assert Path(st["kernel"]).exists(), f"kernel reported but missing: {st['kernel']}"
        return f"D-Flow FM kernel present at {st['kernel']}"

    # Absent is the expected answer today. It must say so in words.
    assert "NOT INSTALLED" in st["summary"], f"unclear absence: {st['summary']}"
    assert st["kernel"] is None, "kernel path set while reporting not installed"
    if st["licence_tooling_only"]:
        return "absent and said so - licence manager present, kernel is not"
    return "absent and said so - no kernel found"


def check_sph_case():
    """Module 02 can write a valid DualSPHysics case. Does NOT solve one.

    Generating the XML is the part that can silently rot when the geometry code
    changes; running the solver takes minutes and belongs in a manual step, not
    a gate that has to stay under a minute.
    """
    sph = import_module("modules.02_sph.breach")

    case = sph.BreachCase(dam_height_m=40.0, water_depth_m=35.0, dp=3.0)
    n = case.estimate_particles()
    assert 1_000 < n < 4_000_000, f"implausible particle estimate {n}"

    xml = sph.write_case_xml(case, SCRATCH / "sph", "gate")
    boxes = sph.write_flowtool_boxes(case, SCRATCH / "sph", "gate")
    text = xml.read_text(encoding="utf-8")

    import xml.etree.ElementTree as ET

    ET.fromstring(text)  # must be well-formed XML
    assert "<setmkvoid />" in text, "breach void carving missing from the case"
    assert text.count("<drawbox>") >= 5, "case geometry looks incomplete"
    assert boxes.read_text().startswith("BOX @Reservoir")

    binaries = "present" if sph.GENCASE.exists() else "BINARIES NOT INSTALLED"
    return f"case XML valid, ~{n:,} particles, DualSPHysics {binaries}"


def check_ml_layer():
    """The ML modules import and their pure-maths parts are right."""
    import numpy as np

    inflow = import_module("modules.07_ml.inflow")

    # SCS curve number: below the initial abstraction, runoff is exactly zero.
    s = 25400.0 / 75.0 - 254.0
    assert float(inflow.scs_runoff_mm(np.array([0.2 * s * 0.99]), 75.0)[0]) == 0.0
    big = float(inflow.scs_runoff_mm(np.array([200.0]), 75.0)[0])
    assert 0 < big < 200.0, "runoff cannot exceed rainfall"

    # Linear reservoir conserves volume over a long enough tail.
    q = inflow.route_linear_reservoir(np.array([100.0] + [0.0] * 200), 100.0, 1.5)
    routed_mcm = float(q.sum() * 86400.0 / 1e6)
    expected_mcm = 100.0 / 1000.0 * 100.0 * 1e6 / 1e6
    assert abs(routed_mcm - expected_mcm) / expected_mcm < 0.02, (
        f"routing lost volume: {routed_mcm:.2f} vs {expected_mcm:.2f} MCM"
    )

    ev = import_module("modules.07_ml.evacuation")
    assert ev.DANGEROUS_DEPTH_M == 0.30

    mc = import_module("modules.07_ml.montecarlo")
    band = mc.summarise(5.0, 60.0, n=200)
    p = band["peak_discharge_cumecs"]
    assert p["p5"] < p["p50"] < p["p95"], "uncertainty percentiles out of order"

    return f"SCS+routing exact, MC band {p['p5']:,.0f}-{p['p95']:,.0f} m3/s"


def check_surrogate():
    """The trained emulator loads and answers, if it has been trained."""
    sg = import_module("modules.07_ml.surrogate")
    if not sg.MODEL_PATH.exists():
        raise _Skip("surrogate not trained: python -m modules.07_ml.surrogate train")

    import json as _json

    metrics = _json.loads((sg.MODEL_DIR / "surrogate_metrics.json").read_text())
    csi = metrics.get("extent_csi", 0.0)
    assert csi > 0.5, (
        f"surrogate extent CSI {csi} is too low to be useful - retrain "
        f"(the Dice loss matters: plain MSE gives ~0.01)"
    )

    out = sg.predict(
        {"reservoir_level_frac": 1.0, "capacity_mcm": 5.0,
         "dam_height_m": 60.0, "formation_time_hr": 0.5}
    )
    assert out["max_depth"].max() > 0.0, "emulator predicts no water at all"
    return f"CSI {csi} vs solver, {out['inference_ms']:.0f} ms"


def check_credentials():
    from shared import creds

    missing = [n for n, (tier, _) in creds.REGISTRY.items() if tier == 1 and not creds.get(n)]
    if missing:
        raise _Skip(f"{len(missing)} tier-1 credential(s) unset: {', '.join(missing[:3])}…")
    return "all tier-1 credentials present"


# ==========================================================================


def main() -> int:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)

    results: list = []
    check = Check(results)

    print("SIH26161 integration check\n")

    check("shared.contract", check_contract)
    check("shared.geo", check_geo)
    check("shared.hydro", check_hydro)
    check("shared.fake -> validator", check_fake_run)
    check("solver: Ritter analytical", check_solver_physics)
    check("solver: closed-basin mass", check_solver_mass)
    check("solver: no boundary inflow", check_solver_no_boundary_inflow)
    check("scenario -> run folder", check_scenario_to_run)
    check("river blockage physics", check_river_blockage)
    check("solver: violent inflow", check_inflow_timestep_limit)
    check("07_ml damage model", check_damage_model)
    check("01_geodata dam catalogue", check_dam_catalogue)
    check("06_gee validation metrics", check_sar_metrics)
    check("04_backend API + console", check_api_imports)
    check("02_sph case generation", check_sph_case)
    check("04_backend gated release physics", check_gated_release)
    check("03_delft3d absence is measured", check_delft3d_absence)
    check("07_ml maths (SCS, routing, MC)", check_ml_layer)
    check("07_ml surrogate", check_surrogate)
    check("credentials", check_credentials)

    width = max(len(n) for _, n, _, _ in results)
    for status, name, detail, secs in results:
        mark = {"pass": "  ok  ", "FAIL": " FAIL ", "skip": " skip ", "warn": " warn "}[status]
        print(f"[{mark}] {name:<{width}}  {secs:5.2f}s  {detail}")

    failed = [r for r in results if r[0] == "FAIL"]
    skipped = [r for r in results if r[0] == "skip"]
    print(
        f"\n{len(results) - len(failed) - len(skipped)}/{len(results)} passed"
        + (f", {len(skipped)} skipped" if skipped else "")
        + (f", {len(failed)} FAILED" if failed else "")
    )

    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
