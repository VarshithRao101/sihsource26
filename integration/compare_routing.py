"""
integration/compare_routing.py - two independent 2D engines on one flood.

    python integration/compare_routing.py --run chungthangdam_overtop_fast_002

NTRO's statement asks us to model the flood "through 'Smooth Particle
Hydrodynamics' and 'Delf3D' model and compare the scenario". compare_engines.py
compares peak DISCHARGE across methods. This compares the routed INUNDATION,
which is the part a district officer actually acts on, by driving a second
independent solver with the identical forcing.

    our solver   2D shallow water, HLL Riemann, Audusse well-balanced
    SFINCS       Deltares' open-source reduced-physics flood model

Same conditioned DEM, same grid, same breach hydrograph, same Manning value,
same wet threshold. Anything the two disagree about is a difference between the
engines and not a difference in the problem.

    SFINCS IS NOT DELFT3D. It is a different Deltares model and it is
    reduced-physics. Delft3D remains reported as absent: the FM licence we
    requested was never answered, and Delft3D 4 - the structured model the
    statement actually names - is GPLv3 but ships as source, so its kernel is a
    compile we have not done. What this comparison demonstrates is that the
    framework drives an independent third-party solver at all.

Two engines will not agree exactly and should not. SFINCS trades momentum terms
for speed; the interesting output is WHERE they diverge, which this prints.

Owner: captain.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"


def _load_run(run_id: str) -> tuple[Path, dict]:
    run_dir = OUTPUTS / run_id
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise SystemExit(f"no such run: {run_dir}")
    return run_dir, json.loads(meta_path.read_text(encoding="utf-8"))


def _find_terrain(site: str, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray, dict]:
    """The conditioned DEM this run was solved on, from module 01's cache.

    Matched on grid shape, because a site can have several cached domains at
    different reach lengths and routing the wrong one would compare two
    different problems.
    """
    pattern = str(REPO_ROOT / "data" / "dem" / site / "cond_*.npz")
    for npz_path in sorted(glob.glob(pattern)):
        side = Path(npz_path).with_suffix(".json")
        if not side.exists():
            continue
        info = json.loads(side.read_text(encoding="utf-8"))
        if int(info.get("nx", -1)) == nx and int(info.get("ny", -1)) == ny:
            z = np.load(npz_path)
            return z["dem"].astype(np.float64), z["manning"].astype(np.float64), info
    raise SystemExit(
        f"no cached conditioned DEM for site {site!r} at {nx}x{ny}.\n"
        f"Looked in data/dem/{site}/. Re-run the scenario once to populate it."
    )


def _inflow_cell(meta: dict, nx: int, ny: int) -> tuple[int, int]:
    """Dam location as a grid cell, from the run's own bbox."""
    w, s, e, n = meta["domain"]["bbox"]
    lat, lon = meta["site"]["lat"], meta["site"]["lon"]
    col = int(round((lon - w) / (e - w) * (nx - 1)))
    row = int(round((n - lat) / (n - s) * (ny - 1)))
    return max(0, min(ny - 1, row)), max(0, min(nx - 1, col))


def agreement(a_wet: np.ndarray, b_wet: np.ndarray) -> dict:
    """Cell-by-cell agreement between two flood extents on the same grid."""
    hits = int(np.sum(a_wet & b_wet))
    only_a = int(np.sum(a_wet & ~b_wet))
    only_b = int(np.sum(~a_wet & b_wet))
    denom = hits + only_a + only_b
    return {
        "both_wet": hits,
        "ours_only": only_a,
        "sfincs_only": only_b,
        "csi": round(hits / denom, 4) if denom else 0.0,
        "agreement_pct": round(100.0 * hits / denom, 1) if denom else 0.0,
    }


def compare(run_id: str, timeout_s: int = 1800) -> dict:
    from importlib import import_module

    from shared.contract import WET_THRESHOLD_M
    from shared.io import read_grid

    sf_engine = import_module("modules.09_sfincs.engine")
    sf_case = import_module("modules.09_sfincs.case")

    run_dir, meta = _load_run(run_id)
    dom = meta["domain"]
    nx, ny = int(dom["nx"]), int(dom["ny"])
    site = run_id.split("_")[0]
    end_hr = float(meta["time"]["end_hr"])
    manning = float(meta.get("scenario", {}).get("manning_n", 0.035) or 0.035)

    st = sf_engine.detect(probe=False)
    if not st.installed:
        return {
            "run_id": run_id,
            "sfincs": {"installed": False},
            "note": "SFINCS not installed - nothing to compare against.",
        }

    dem, _manning_grid, terr = _find_terrain(site, nx, ny)
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

    row, col = _inflow_cell(meta, nx, ny)
    case_dir = REPO_ROOT / "outputs" / run_id / "sfincs"

    cs = sf_case.write_case(
        case_dir, dem, dx_m=float(dom["cellsize_m"]),
        src_row=row, src_col=col, t_hr=t_hr, q_cumecs=q,
        end_hr=end_hr, manning=manning,
        dtmapout_s=max(end_hr * 3600.0 / 12.0, 300.0),
    )
    out = sf_case.run_case(case_dir, st.exe, timeout_s=timeout_s)
    if not out["ok"]:
        return {
            "run_id": run_id,
            "sfincs": {"installed": True, "ok": False, "tail": out["tail"]},
            "note": "SFINCS failed; see the log.",
        }

    res = sf_case.read_map(case_dir, dem=dem)
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
            "engine": "HLL shallow water (this project)",
            "wet_cells": int(a_wet.sum()),
            "flood_area_km2": round(float(a_wet.sum()) * cell_km2, 2),
            "max_depth_m": round(float(np.nanmax(ours)), 2),
        },
        "sfincs": {
            "engine": f"SFINCS {st.version or ''}".strip(),
            "installed": True,
            "ok": True,
            "wet_cells": int(b_wet.sum()),
            "flood_area_km2": round(float(b_wet.sum()) * cell_km2, 2),
            "max_depth_m": round(float(theirs.max()), 2),
            "is_delft3d": False,
        },
        "delft3d": {
            "installed": False,
            "note": (
                "Kernel not built. Delft3D 4 is GPLv3 and source-only; the FM "
                "licence we requested was never answered. Absent, never estimated."
            ),
        },
        "agreement": agreement(a_wet, b_wet),
        "reading": (
            "Two independent engines, identical forcing, terrain, grid and wet "
            "threshold. They are NOT expected to agree exactly: SFINCS trades "
            "momentum terms for speed, so it typically spreads water further and "
            "shallower. Where they disagree is the interesting part. SFINCS is "
            "not Delft3D and is not a substitute for it."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/compare_routing.py")
    ap.add_argument("--run", required=True, help="run id in outputs/")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args(argv)

    r = compare(args.run, timeout_s=args.timeout)

    if args.json:
        print(json.dumps(r, indent=1))
        return 0

    if not r.get("sfincs", {}).get("installed"):
        print(r["note"])
        return 1
    if not r["sfincs"].get("ok"):
        print(r["note"])
        print(r["sfincs"]["tail"])
        return 1

    f = r["forcing"]
    print(f"\nRouting comparison - {r['run_id']}")
    print(f"  grid {r['grid'][0]}x{r['grid'][1]} at {r['cellsize_m']} m, "
          f"{f['dem']} terrain, Manning {f['manning']}")
    print(f"  same forcing: peak {f['peak_cumecs']:,.0f} m3/s over {f['end_hr']} h, "
          f"wet >= {f['wet_threshold_m']} m\n")

    print(f"  {'engine':38s} {'wet km2':>9s} {'max depth':>10s}")
    print(f"  {'-'*38} {'-'*9} {'-'*10}")
    for key in ("ours", "sfincs"):
        e = r[key]
        print(f"  {e['engine'][:38]:38s} {e['flood_area_km2']:>9.2f} {e['max_depth_m']:>9.2f} m")
    print(f"  {'Delft3D FM':38s} {'n/a':>9s} {'n/a':>10s}")
    print(f"      {r['delft3d']['note']}\n")

    a = r["agreement"]
    print(f"  cells wet in both        {a['both_wet']:,}")
    print(f"  ours only                {a['ours_only']:,}")
    print(f"  SFINCS only              {a['sfincs_only']:,}")
    print(f"  extent agreement (CSI)   {a['csi']:.4f}  ({a['agreement_pct']}%)\n")
    print(f"  {r['reading']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
