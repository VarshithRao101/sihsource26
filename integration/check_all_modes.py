"""
integration/check_all_modes.py - every failure mode, end to end, in one command.

    python -m integration.check_all_modes

Runs all eight failure modes through runner.run_scenario on synthetic terrain
and checks that each one produces a contract-valid run folder. Synthetic on
purpose: this asks whether the DISPATCH is right - does each mode reach its own
physics, write its own meta block, and conserve mass - and that question does
not need a DEM download to answer. Whether the flood lands in the right valley
is a different question, answered by integration/build_demo_runs.py on real
terrain.

It also asserts the thing that is easiest to get wrong and hardest to notice:
a mode in which nothing breaches must NOT publish a breach width. Before the
check existed, `gated_release` and `river_flood` both wrote a breach geometry
into meta.json that no part of the run had computed - a number that came out of
a regression called speculatively at the top of run_scenario and then never
used. Blank beats invented, and this is where that rule is enforced.

Exits non-zero if any mode fails, so it can gate a commit.

Owner: captain.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from importlib import import_module

runner = import_module("modules.04_backend.runner")
sc = import_module("modules.04_backend.scenario")
ScenarioSpec, SiteSpec = sc.ScenarioSpec, sc.SiteSpec

out = Path(tempfile.mkdtemp(prefix="modecheck_"))
print(f"scratch run folders: {out}")

BASE = dict(
    reach_length_km=20.0,
    corridor_width_km=6.0,
    cellsize_m=90.0,
    end_hr=4.0,
    output_step_hr=0.25,
    dem_source="SYNTHETIC",
)

site_eng = SiteSpec(
    name="Testdam", lat=27.90, lon=88.20, river="Test", state="Sikkim",
    dam_height_m=60.0, reservoir_capacity_mcm=50.0, crest_length_m=300.0,
    kind="engineered", height_source="CWC NRLD 2019",
)
site_nat = SiteSpec(
    name="Testmoraine", lat=27.90, lon=88.20, river="", state="Sikkim",
    dam_height_m=35.0, reservoir_capacity_mcm=1.0,
    kind="natural", height_source="estimated",
)

CASES = [
    ("overtopping",        site_eng, {}),
    ("piping",             site_eng, {}),
    ("foundation_failure", site_eng, {"collapse_time_min": 2.0}),
    ("spillway_blockage",  site_eng, {"inflow_cumecs": 900.0,
                                      "residual_spillway_frac": 0.1,
                                      "design_spillway_cumecs": 1500.0}),
    ("gated_release",      site_eng, {"gate_opening_frac": 1.0,
                                      "design_spillway_cumecs": 1500.0}),
    ("blockage_breach",    site_nat, {"blockage_height_m": 35.0}),
    ("glof_moraine",       site_nat, {"moraine_height_m": 35.0,
                                      "avalanche_surge_frac": 0.05}),
    ("river_flood",        site_nat, {"source_kind": "river",
                                      "peak_discharge_cumecs": 3000.0,
                                      "time_to_peak_hr": 1.5}),
]

print(f"{'mode':<20} {'peak m3/s':>11} {'wet km2':>9} {'maxdep m':>9} "
      f"{'mass err %':>10}  meta block")
print("-" * 88)

failures = []
for mode, site, extra in CASES:
    spec = ScenarioSpec(site=site, failure_mode=mode, **BASE, **extra)
    errs = spec.validate()
    if errs:
        failures.append((mode, "invalid: " + "; ".join(errs)))
        print(f"{mode:<20} INVALID: {errs}")
        continue
    try:
        run_dir = runner.run_scenario(spec, outputs_dir=out)
    except Exception as exc:  # noqa: BLE001
        failures.append((mode, f"{type(exc).__name__}: {exc}"))
        print(f"{mode:<20} CRASH: {type(exc).__name__}: {exc}")
        continue

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    res = meta["results"]
    import csv
    with (run_dir / "hydrograph.csv").open(encoding="utf-8") as fh:
        peak = max(float(r["discharge_cumecs"]) for r in csv.DictReader(fh))

    extra_blocks = [
        k for k in ("blockage", "gated_release", "foundation_failure",
                    "spillway_blockage", "glof_moraine", "river_flood")
        if k in meta
    ]
    print(f"{mode:<20} {peak:>11,.0f} {res.get('flood_area_km2', 0):>9.2f} "
          f"{res.get('max_depth_m', 0):>9.2f} "
          f"{res.get('mass_balance_err_pct', 0):>10.3f}  {', '.join(extra_blocks)}")

    # The scenario block must not claim a breach where none happened.
    scen = meta["scenario"]
    if mode in ("gated_release", "river_flood"):
        assert "breach_width_m" not in scen, f"{mode} published a breach width"
        assert "breach" in scen, f"{mode} missing the no-breach note"
    else:
        assert "breach_width_m" in scen, f"{mode} lost its breach geometry"

print()
if failures:
    print("FAILURES:")
    for m, why in failures:
        print(f"  {m}: {why}")
else:
    print("all 8 modes produced a run folder")
shutil.rmtree(out, ignore_errors=True)
sys.exit(1 if failures else 0)
