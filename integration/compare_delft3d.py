"""
integration/compare_delft3d.py - our solver against Delft3D, on one flood.

    python integration/compare_delft3d.py --run godavariatgangapur_blockage_fast_001

NTRO's statement asks for the flood to be modelled "through 'Smooth Particle
Hydrodynamics' and 'Delf3D' model and compare the scenario". This is that
comparison, and it is the only one in this repository that uses the engine the
statement actually names.

    our solver   2D shallow water, HLL Riemann, Audusse well-balanced, explicit
    Delft3D 4    Delft3D-FLOW, curvilinear finite difference, implicit ADI

Same conditioned DEM, same grid, same breach hydrograph, same Manning value,
same wet threshold. Anything the two disagree about is a difference between the
engines and not a difference in the problem.

WHAT THIS IS NOT. It is not validation. Two independent engines agreeing tells
you the numerics are not obviously wrong; both could be wrong the same way, and
neither has been checked against a measured flood on this reach. See
docs/VALIDATION.md, which says the same thing about SFINCS.

The companion is integration/compare_routing.py, which does this against SFINCS.
SFINCS is a different Deltares model and reduced-physics; this one is Delft3D.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"


def agreement(ours_wet: np.ndarray, theirs_wet: np.ndarray) -> dict:
    """Cell-by-cell extent agreement between two engines on the same grid."""
    hits = int(np.sum(ours_wet & theirs_wet))
    only_a = int(np.sum(ours_wet & ~theirs_wet))
    only_b = int(np.sum(~ours_wet & theirs_wet))
    denom = hits + only_a + only_b
    return {
        "both_wet": hits,
        "ours_only": only_a,
        "delft3d_only": only_b,
        "csi": round(hits / denom, 4) if denom else 0.0,
        "agreement_pct": round(100.0 * hits / denom, 1) if denom else 0.0,
    }


def compare(run_id: str, dt_minutes: float = 0.1, timeout_s: int = 7200) -> dict:
    from importlib import import_module

    from shared.contract import WET_THRESHOLD_M
    from shared.io import read_grid

    routing = import_module("integration.compare_routing")
    d3d_engine = import_module("modules.03_delft3d.engine")
    d3d_case = import_module("modules.03_delft3d.case")

    st = d3d_engine.detect()
    if not st.can_solve:
        return {"run_id": run_id, "delft3d": {"installed": False,
                "summary": st.summary}, "note": "No usable Delft3D kernel."}

    run_dir, meta = routing._load_run(run_id)
    dom = meta["domain"]
    nx, ny = int(dom["nx"]), int(dom["ny"])
    site = run_id.split("_")[0]
    end_hr = float(meta["time"]["end_hr"])
    manning = float(meta.get("scenario", {}).get("manning_n", 0.035) or 0.035)

    dem, _mann_grid, terr = routing._find_terrain(site, nx, ny)
    ours, _grid = read_grid(run_dir, "max_depth")

    t_hr, q = [], []
    with open(run_dir / "hydrograph.csv", encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            if not line.strip():
                continue
            a, b = line.split(",")[:2]
            t_hr.append(float(a))
            q.append(float(b))
    t_hr, q = np.asarray(t_hr), np.asarray(q)

    row, col = routing._inflow_cell(meta, nx, ny)
    case_dir = OUTPUTS / run_id / "delft3d"

    c = d3d_case.write_case(
        case_dir, dem, dx_m=float(dom["cellsize_m"]),
        src_row=row, src_col=col, t_hr=t_hr, q_cumecs=q,
        end_hr=end_hr, manning=manning, dt_minutes=dt_minutes,
        map_interval_min=max(end_hr * 60.0 / 12.0, 5.0),
    )

    t0 = time.perf_counter()
    out = d3d_case.run_case(case_dir, st.kernel, timeout_s=timeout_s)
    wall = time.perf_counter() - t0
    if not out["ok"]:
        return {"run_id": run_id, "delft3d": {"installed": True, "ok": False,
                "tail": out["tail"]}, "note": "Delft3D failed; see the log."}

    res = d3d_case.read_map(case_dir, dem=dem)
    theirs = res["max_depth_m"]

    cell_km2 = (float(dom["cellsize_m"]) ** 2) / 1e6
    a_wet = np.asarray(ours) >= WET_THRESHOLD_M
    b_wet = theirs >= WET_THRESHOLD_M

    return {
        "run_id": run_id,
        "site": site,
        "grid": [nx, ny],
        "cellsize_m": round(float(dom["cellsize_m"]), 2),
        "forcing": {
            "peak_cumecs": round(float(q.max()), 1),
            "end_hr": end_hr,
            "manning": manning,
            "wet_threshold_m": WET_THRESHOLD_M,
            "dem": terr.get("source"),
        },
        "ours": {
            "engine": "HLL shallow water, explicit (this project)",
            "wet_cells": int(a_wet.sum()),
            "flood_area_km2": round(float(a_wet.sum()) * cell_km2, 2),
            "max_depth_m": round(float(np.nanmax(ours)), 2),
        },
        "delft3d": {
            "engine": "Delft3D-FLOW (Delft3D 4, structured, implicit ADI)",
            "installed": True,
            "ok": True,
            "kernel": str(st.kernel),
            "wet_cells": int(b_wet.sum()),
            "flood_area_km2": round(float(b_wet.sum()) * cell_km2, 2),
            "max_depth_m": round(float(theirs.max()), 2),
            "dt_minutes": dt_minutes,
            "runtime_s": round(wall, 1),
        },
        "case": c.as_dict(),
        "agreement": agreement(a_wet, b_wet),
        "reading": (
            "Two independent engines, identical terrain, grid, forcing and wet "
            "threshold. They are NOT expected to agree exactly: ours is an "
            "explicit finite-volume shallow-water solver, Delft3D is an "
            "implicit ADI finite-difference one, and they treat the wet/dry "
            "front differently. Agreement bounds the numerics; it is not "
            "validation against a measured flood, and neither engine has been "
            "checked against one on this reach."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/compare_delft3d.py")
    ap.add_argument("--run", required=True, help="run id in outputs/")
    ap.add_argument("--dt", type=float, default=0.1, help="Delft3D timestep, minutes")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    r = compare(args.run, dt_minutes=args.dt, timeout_s=args.timeout)

    if args.json:
        print(json.dumps(r, indent=1))
        return 0

    if not r.get("delft3d", {}).get("installed"):
        print(r["note"], "\n ", r["delft3d"].get("summary", ""))
        return 1
    if not r["delft3d"].get("ok"):
        print(r["note"])
        print(r["delft3d"]["tail"])
        return 1

    f = r["forcing"]
    print(f"\nDelft3D comparison - {r['run_id']}")
    print(f"  grid {r['grid'][0]}x{r['grid'][1]} at {r['cellsize_m']} m, "
          f"{f['dem']} terrain, Manning {f['manning']}")
    print(f"  same forcing: peak {f['peak_cumecs']:,.0f} m3/s over {f['end_hr']} h, "
          f"wet >= {f['wet_threshold_m']} m\n")

    print(f"  {'engine':46s} {'wet km2':>9s} {'max depth':>10s}")
    print(f"  {'-'*46} {'-'*9} {'-'*10}")
    for key in ("ours", "delft3d"):
        e = r[key]
        print(f"  {e['engine'][:46]:46s} {e['flood_area_km2']:>9.2f} "
              f"{e['max_depth_m']:>9.2f} m")
    print(f"\n  Delft3D ran in {r['delft3d']['runtime_s']} s "
          f"at dt = {r['delft3d']['dt_minutes']} min\n")

    a = r["agreement"]
    print(f"  cells wet in both        {a['both_wet']:,}")
    print(f"  ours only                {a['ours_only']:,}")
    print(f"  Delft3D only             {a['delft3d_only']:,}")
    print(f"  extent agreement (CSI)   {a['csi']:.4f}  ({a['agreement_pct']}%)\n")
    print(f"  {r['reading']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
