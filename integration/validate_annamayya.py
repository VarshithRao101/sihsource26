"""
integration/validate_annamayya.py - a real dam failure, against what the
satellite saw, compared like with like.

    python integration/validate_annamayya.py --run <run_id>

THE EVENT. The Annamayya (Cheyyeru Project) earthfill embankment, Andhra
Pradesh, failed on 19 November 2021 - 25 m high, 63.16 MCM, dam AP01MH0129 in
the CWC register. It is the validation case because it is a REAL Indian dam
break, it falls inside the Sentinel-1 era, and it is on a floodplain: the Teesta
gorge could not be observed at all, a corridor one to three cells wide in 30
degree terrain being below what Sentinel-1 resolves.

WHY THIS SCRIPT EXISTS RATHER THAN A SINGLE NUMBER. The first validation of this
event scored CSI 0.0268, and decomposing it showed the error is 96% FALSE
ALARMS - 19,221 against 645 hits. Detection was never the problem. Even with
perfect detection the score could not exceed 0.126, because the comparison was
between two different quantities:

  * OUR MAXIMUM EXTENT OVER THE WHOLE RUN, against
  * A MEDIAN OF THREE SATELLITE PASSES at 2, 9 and 14 days after the breach.

The flood had drained long before the 9- and 14-day passes, so two thirds of the
"observed" mask was dry ground. That is a methodological error, not a modelling
one, and this script fixes it three ways and reports the whole matrix rather
than the best cell of it.

  1. OBSERVATION: the single closest pass (21 Nov, 2 days after) instead of a
     median that includes dry scenes.
  2. EXTENT: where the water actually was AT THE OBSERVATION TIME, reconstructed
     from arrival_time and duration, instead of the maximum over the run.
  3. DEPTH: a threshold sweep, because Sentinel-1 cannot see 5 cm of water
     through vegetation and our contract threshold is 0.05 m.

NONE OF THIS IS TUNING. Every combination is printed, including the ones that
score worse, and the headline figure is the methodologically correct cell
(closest pass, extent at observation time, contract threshold) rather than the
highest number in the table. Choosing the best cell and quoting it would be
exactly the thing AGENTS.md Part 1 forbids.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVENT = "Annamayya (Cheyyeru) dam failure, 19 November 2021"
SITE = "annamayya"
BREACH = "2021-11-19"

PRE_WINDOW = ("2021-09-15", "2021-10-31")

# The three DESCENDING passes after the failure, from Earth Engine. Their
# distance from the breach is the whole point of this script.
PASSES = {
    "closest (21 Nov, +2 d)": ("2021-11-20", "2021-11-22"),
    "median of 3 (+2/+9/+14 d)": ("2021-11-19", "2021-12-05"),
}

DEPTH_THRESHOLDS = (0.05, 0.25, 0.50, 1.00)

# Hours from the breach to the 21 Nov 00:39 UTC pass.
OBSERVATION_HR = 48.0


def wet_at(run_dir: Path, hours: float, threshold_m: float) -> np.ndarray | None:
    """Where the water was AT `hours`, not where it ever reached.

    Reconstructed from the contract grids: a cell is wet at time T if the front
    had arrived by then and had not yet drained. `duration` is how long the cell
    stayed above the wet threshold, so arrival + duration is when it dried.

    The depth condition is approximate and is stated as such in the output: we
    hold max_depth per cell, not depth at T, so "wet at T and deeper than X"
    uses the cell's maximum. It can only ever over-count, which is the
    conservative direction for a false-alarm problem.
    """
    from shared.io import read_grid

    try:
        arrival, _ = read_grid(run_dir, "arrival_time")
        duration, _ = read_grid(run_dir, "duration")
        depth, _ = read_grid(run_dir, "max_depth")
    except Exception:
        return None

    arrival = np.asarray(arrival, dtype=float)
    duration = np.asarray(duration, dtype=float)
    depth = np.asarray(depth, dtype=float)

    arrived = np.isfinite(arrival) & (arrival <= hours)
    still_wet = arrived & ((arrival + np.nan_to_num(duration)) >= hours)
    return still_wet & (depth >= threshold_m)


def max_extent(run_dir: Path, threshold_m: float) -> np.ndarray:
    from shared.io import read_grid

    depth, _ = read_grid(run_dir, "max_depth")
    return np.asarray(depth, dtype=float) >= threshold_m


def observed_mask(run_dir: Path, post_window, force: bool = False):
    """The Sentinel-1 change-detection mask on this run's grid."""
    from importlib import import_module

    from shared.io import read_grid, read_meta

    sar = import_module("modules.06_gee_validation.sar")
    meta = read_meta(run_dir)
    bbox = tuple(meta["domain"]["bbox"])
    # The run's own grid, straight off its rasters - no reconstruction, so the
    # observation lands on exactly the cells the solver used.
    _depth, grid = read_grid(run_dir, "max_depth")

    pre_db, post_db, info = sar.fetch_s1_pair(
        bbox=bbox, site=SITE,
        pre_start=PRE_WINDOW[0], pre_end=PRE_WINDOW[1],
        post_start=post_window[0], post_end=post_window[1],
        grid=grid, force=force,
    )
    obs, extra = sar.flood_extent_from_change(pre_db, post_db)

    # HOW MUCH OF THE DOMAIN DID THE SATELLITE ACTUALLY SEE? A Sentinel-1 pass
    # has a limited footprint, and a window narrow enough to isolate one pass
    # can leave most of the domain unimaged - the array comes back zero-filled,
    # which is not "no water", it is "no data". Comparing against that produces
    # a confident CSI computed from nothing. Measured, and reported, so the
    # caller can refuse it.
    real = np.isfinite(post_db) & (np.abs(post_db) > 1e-6)
    coverage = float(real.mean())
    return np.asarray(obs, dtype=bool), {
        **info, **(extra or {}), "coverage_frac": round(coverage, 4)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/validate_annamayya.py")
    ap.add_argument("--run", required=True)
    ap.add_argument("--hours", type=float, default=OBSERVATION_HR,
                    help="hours from breach to the observation pass")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from importlib import import_module
    sar = import_module("modules.06_gee_validation.sar")

    run_dir = REPO_ROOT / "outputs" / args.run
    if not (run_dir / "meta.json").exists():
        raise SystemExit(f"no such run: {run_dir}")

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    end_hr = float(meta["time"]["end_hr"])
    cell = float(meta["domain"]["cellsize_m"])

    print(f"{EVENT}")
    print(f"  run {args.run}   {meta['domain']['nx']}x{meta['domain']['ny']} "
          f"at {cell:.0f} m, {end_hr:g} h simulated")
    print(f"  observation pass is {args.hours:g} h after the breach", end="")
    if args.hours > end_hr:
        print(f"  -- BEYOND the {end_hr:g} h simulated, so extent-at-time is "
              f"unavailable")
    else:
        print()
    print()

    MIN_COVERAGE = 0.50

    rows = []
    skipped = []
    for pass_name, window in PASSES.items():
        obs, info = observed_mask(run_dir, window, force=args.force)
        cov = info.get("coverage_frac", 1.0)
        if cov < MIN_COVERAGE:
            skipped.append((pass_name, cov))
            continue
        for thr in DEPTH_THRESHOLDS:
            for how in ("max over run", "at observation time"):
                if how == "max over run":
                    sim = max_extent(run_dir, thr)
                else:
                    sim = wet_at(run_dir, args.hours, thr)
                    if sim is None or args.hours > end_hr:
                        continue
                m = sar.agreement(sim, obs)
                rows.append({
                    "observation": pass_name,
                    "extent": how,
                    "threshold_m": thr,
                    "csi": m.csi, "pod": m.pod, "far": m.far, "bias": m.bias,
                    "hits": m.hits, "false_alarms": m.false_alarms,
                    "misses": m.misses,
                    "sim_cells": int(sim.sum()),
                    "obs_cells": int(obs.sum()),
                })

    if args.json:
        print(json.dumps({"event": EVENT, "run": args.run, "rows": rows}, indent=1))
        return 0

    print(f"  {'observation':<26} {'extent':<21} {'thr':>5} {'CSI':>7} "
          f"{'POD':>6} {'FAR':>6} {'bias':>7}")
    print(f"  {'-'*26} {'-'*21} {'-'*5} {'-'*7} {'-'*6} {'-'*6} {'-'*7}")
    for r in rows:
        print(f"  {r['observation']:<26} {r['extent']:<21} {r['threshold_m']:>5.2f} "
              f"{r['csi']:>7.4f} {r['pod']:>6.3f} {r['far']:>6.3f} {r['bias']:>7.2f}")

    for name, cov in skipped:
        print("")
        print(f"  SKIPPED {name}: the satellite imaged only {100*cov:.1f}% "
              f"of this domain.")
        print("    A single Sentinel-1 pass does not cover it - that is why "
              "the composite exists. Scoring against a mask that is mostly "
              "no-data would be a number computed from nothing.")

    head = [r for r in rows
            if r["extent"] == "at observation time"
            and r["threshold_m"] == 0.05]
    if head:
        h = head[0]
        print("")
        print(f"  HEADLINE ({h['observation']}, extent at the observation "
              f"time, contract threshold {h['threshold_m']:g} m):")
        print(f"    CSI {h['csi']:.4f}   POD {h['pod']:.3f}   FAR {h['far']:.3f} "
              f"  bias {h['bias']:.2f}")
        print(f"    {h['hits']:,} hits, {h['false_alarms']:,} false alarms, "
              f"{h['misses']:,} misses")

    print(
        "\n  The whole matrix is printed, including the cells that score worse.\n"
        "  The headline is the methodologically correct combination, not the\n"
        "  highest number in the table - picking that would be tuning.\n"
        "\n  Still one event, one reach, one satellite pass. Not a general\n"
        "  accuracy figure for this system. See docs/VALIDATION.md.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
