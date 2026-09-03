"""
integration/grid_convergence.py - how much of the answer is the grid?

    python integration/grid_convergence.py --dam AP01MH0129 --reach 8

Every run this project has produced solves at 90 m on 30 m COP30 terrain. That
was the right trade while the solver swept the whole domain every step; it is
not obviously right now that it sweeps only the flood. This measures what the
choice actually costs.

WHAT A CONVERGENCE STUDY IS, and why it belongs in a project whose honesty
policy forbids unearned accuracy claims. Verification asks "did we solve the
equations correctly" and validation asks "does the answer match reality". There
is a third question sitting between them that we had not answered: HOW MUCH OF
THE ANSWER IS AN ARTEFACT OF THE MESH? Run the identical scenario at several
cell sizes. If the results converge as the cells shrink, the discretisation
error is bounded and you can say by how much. If they do not, the grid is
driving the answer and every number downstream of it inherits that.

This is standard practice in computational hydraulics and it is the honest
response to "what is the accuracy of your model?" - not a percentage, but a
measured statement about one specific error source we CAN quantify, next to the
ones (30 m terrain, no bathymetry) we cannot.

It drives the real API, so every run goes through the real terrain fetch,
conditioning, solver, contract writer and validator. Nothing is short-circuited.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"
DEFAULT_SIZES = (120.0, 90.0, 60.0, 45.0)


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def run_one(base: str, dam_id: str, reach_km: float, end_hr: float,
            cellsize_m: float, timeout_s: int = 3600) -> dict:
    """One scenario at one cell size, start to finish."""
    body = {
        "dam_id": dam_id,
        "failure_mode": "overtopping",
        "reservoir_level_frac": 1.0,
        "reach_length_km": reach_km,
        "end_hr": end_hr,
        "cellsize_m": cellsize_m,
        "real_terrain": True,
    }
    t0 = time.perf_counter()
    started = _post(base, "/api/runs", body)
    run_id = started["run_id"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = _get(base, f"/api/runs/{run_id}/status")
        if st.get("status") in ("done", "error", "failed"):
            break
        time.sleep(2.0)
    else:
        raise SystemExit(f"{run_id} did not finish inside {timeout_s}s")

    wall = time.perf_counter() - t0
    if st.get("status") != "done":
        return {"cellsize_m": cellsize_m, "run_id": run_id, "ok": False,
                "error": st.get("error")}

    meta = json.loads((OUTPUTS / run_id / "meta.json").read_text(encoding="utf-8"))
    dom = meta["domain"]
    res = meta.get("results", {})
    return {
        "cellsize_m": round(float(dom["cellsize_m"]), 2),
        "run_id": run_id,
        "ok": True,
        "grid": [int(dom["nx"]), int(dom["ny"])],
        "cells": int(dom["nx"]) * int(dom["ny"]),
        "wall_s": round(wall, 1),
        "steps": res.get("n_steps"),
        "max_depth_m": res.get("max_depth_m"),
        "flood_area_km2": res.get("flood_area_km2"),
        "peak_cumecs": res.get("peak_discharge_cumecs"),
        "mass_err_pct": res.get("mass_balance_err_pct"),
    }


def compare(rows: list[dict]) -> dict:
    """Change between successive refinements, finest last."""
    good = [r for r in rows if r.get("ok")]
    good.sort(key=lambda r: -r["cellsize_m"])
    deltas = []
    for coarse, fine in zip(good, good[1:]):
        def pct(key):
            a, b = coarse.get(key), fine.get(key)
            if not a or not b:
                return None
            return round(100.0 * (b - a) / a, 1)
        deltas.append({
            "from_m": coarse["cellsize_m"],
            "to_m": fine["cellsize_m"],
            "max_depth_pct": pct("max_depth_m"),
            "area_pct": pct("flood_area_km2"),
        })
    return {"runs": good, "deltas": deltas}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/grid_convergence.py")
    ap.add_argument("--dam", default="AP01MH0129", help="dam_id from the CWC register")
    ap.add_argument("--reach", type=float, default=8.0, help="reach length km")
    ap.add_argument("--hours", type=float, default=3.0, help="simulated duration")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sizes = [float(s) for s in args.sizes.split(",") if s.strip()]

    try:
        _get(args.base, "/health")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(
            f"no backend at {args.base} ({exc}). Start it with:\n"
            "  .venv\\Scripts\\python.exe -m uvicorn modules.04_backend.api:app --port 8000"
        )

    rows = []
    for s in sizes:
        print(f"  solving at {s:g} m ...", flush=True)
        rows.append(run_one(args.base, args.dam, args.reach, args.hours, s))

    out = compare(rows)
    if args.json:
        print(json.dumps(out, indent=1))
        return 0

    print(f"\nGrid convergence - dam {args.dam}, {args.reach:g} km reach, "
          f"{args.hours:g} h\n")
    print(f"  {'cell':>6}  {'grid':>11}  {'cells':>8}  {'wall':>7}  "
          f"{'max depth':>10}  {'area km2':>9}  {'mass %':>8}")
    print(f"  {'-'*6}  {'-'*11}  {'-'*8}  {'-'*7}  {'-'*10}  {'-'*9}  {'-'*8}")
    for r in out["runs"]:
        if not r.get("ok"):
            print(f"  {r['cellsize_m']:>5.0f}m  FAILED: {r.get('error')}")
            continue
        print(f"  {r['cellsize_m']:>5.0f}m  {r['grid'][0]:>4}x{r['grid'][1]:<5} "
              f"{r['cells']:>8,}  {r['wall_s']:>6.1f}s  "
              f"{r['max_depth_m']:>9.2f}m  {r['flood_area_km2']:>9.2f}  "
              f"{r['mass_err_pct']:>8.4f}")

    print("\n  change on refinement:")
    for d in out["deltas"]:
        print(f"    {d['from_m']:>3.0f}m -> {d['to_m']:>3.0f}m   "
              f"max depth {d['max_depth_pct']:+.1f}%   "
              f"area {d['area_pct']:+.1f}%")

    print(
        "\n  Read it as convergence, not as accuracy. Shrinking changes mean the\n"
        "  discretisation error is bounded and measured. It says nothing about\n"
        "  whether the answer matches a real flood - 30 m terrain and an\n"
        "  unsurveyed riverbed set a separate ceiling that no mesh crosses.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
