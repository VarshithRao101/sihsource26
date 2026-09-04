"""
integration/reference_quality.py - how good can the Sentinel-1 reference get,
and would a better one raise the score?

    python integration/reference_quality.py

THE QUESTION. Every Annamayya CSI in this repository is around 0.03, and
docs/SEVERITY_INVERSION.md showed why the observation is suspect: 732
disconnected components with a median size of one cell, where a flood is one
corridor. The obvious next move is to stop blaming the model and improve the
OBSERVATION. This measures whether that works.

THE RULE THAT MAKES THIS LEGITIMATE. Every filter below is fixed A PRIORI from
radar physics or operational flood-mapping practice, with its citation, and none
is swept for best CSI. Choosing a filter because it raises the score would be
tuning the ruler to fit the object - the exact thing AGENTS.md Part 1 forbids -
and it is an easy mistake to make here, because the filters really do make the
mask look better.

WHAT IT FOUND, so you do not have to run it to know. Cleaning the reference
works: the mask goes from 732 fragments to 15, and the largest component from
6.5% of the wet area to 41%. It becomes flood-shaped, which confirms the speckle
diagnosis. But the CSI goes DOWN, not up, and the reason is the useful part.

The binding constraint was never noise. It is AREA. Our simulation wets 19,866
cells; the raw observation has 2,752 and the cleaned one 444. With the simulated
extent fixed, the best CSI reachable even with PERFECT detection is |obs|/|sim| -
0.139 against the raw mask, 0.022 against the cleaned one. Cleaning the reference
removes noise cells, which shrinks |obs|, which LOWERS the ceiling.

So no amount of work on the satellite gets this event to CSI 0.6. That needs the
simulated and observed areas to be comparable, which needs the event's forcing
(unpublished) and an observation at the flood peak (the passes are +2, +9 and
+14 days, by which time it had drained). Both are outside the project.

A SEPARATE DEFECT THIS TURNED UP. sar.validate_run() takes the DEM from its
caller and falls back to unmasked change detection when it does not get one.
For Annamayya it never got one - validation.json records
`terrain_mask: "UNAVAILABLE - shadow false positives likely"` - so every figure
for this site was scored against the unmasked reference. V1 below is what the
masked detector actually gives.

Owner: captain.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RUN = "cheyyeruprojectannamayya_overtop_fast_002"
DEM_NPZ = ("data/dem/cheyyeruprojectannamayya/"
           "cond_COP30_653x380_78.9384_14.1653_79.0158_14.2193_r40.npz")
SITE = "annamayya"
PRE_WINDOW = ("2021-09-15", "2021-10-31")
POST_WINDOW = ("2021-11-19", "2021-12-05")
WET_THRESHOLD_M = 0.05

# Fixed a priori. Sources in the docstrings below.
MAX_SLOPE_DEG = 10.0
MIN_MAPPING_UNIT_CELLS = 5
HAND_THRESHOLDS_M = (10.0, 20.0)
CHANNEL_PERCENTILE = 2.0


def components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """8-connected components. A flood is one; speckle is hundreds."""
    mask = np.asarray(mask, bool)
    H, W = mask.shape
    seen = np.zeros_like(mask)
    nbr = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
           (0, 1), (1, -1), (1, 0), (1, 1))
    out = []
    for y in range(H):
        for x in range(W):
            if mask[y, x] and not seen[y, x]:
                q = deque([(y, x)])
                seen[y, x] = True
                cells = [(y, x)]
                while q:
                    cy, cx = q.popleft()
                    for dy, dx in nbr:
                        ny, nx = cy + dy, cx + dx
                        if (0 <= ny < H and 0 <= nx < W
                                and mask[ny, nx] and not seen[ny, nx]):
                            seen[ny, nx] = True
                            q.append((ny, nx))
                            cells.append((ny, nx))
                out.append(cells)
    return out


def drop_small(mask: np.ndarray, min_cells: int) -> np.ndarray:
    """Minimum mapping unit.

    Operational flood mapping discards isolated water objects below a minimum
    size: a one-pixel "flood" is speckle, not inundation. 5 cells at 60 m is
    1.8 ha. Copernicus EMS rapid mapping applies an MMU of this order.
    """
    out = np.zeros_like(mask, bool)
    for c in components(mask):
        if len(c) >= min_cells:
            for y, x in c:
                out[y, x] = True
    return out


def hand(dem: np.ndarray, channel: np.ndarray) -> np.ndarray:
    """Height Above Nearest Drainage, by breadth-first search from the channel.

    Nothing floods 20 m above the river it came from. HAND is the standard
    terrain descriptor for excluding such cells (Nobre et al. 2011, J. Hydrol.
    404, 13-29; Twele et al. 2016 use it beside slope in an automated
    Sentinel-1 chain).

    The BFS propagates each cell's nearest channel elevation outward, so this
    is nearest-in-flow-path only approximately - adequate for a mask, and its
    approximation is stated rather than hidden.
    """
    H, W = dem.shape
    ref = np.full((H, W), np.nan)
    q = deque()
    for y, x in zip(*np.where(channel)):
        ref[y, x] = dem[y, x]
        q.append((y, x))
    for cy, cx in iter(lambda: q.popleft() if q else None, None):
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < H and 0 <= nx < W and np.isnan(ref[ny, nx]):
                ref[ny, nx] = ref[cy, cx]
                q.append((ny, nx))
    return dem - ref


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python integration/reference_quality.py").parse_args(argv)

    from importlib import import_module
    from shared.io import read_grid, read_meta

    sar = import_module("modules.06_gee_validation.sar")

    run_dir = REPO_ROOT / "outputs" / RUN
    if not (run_dir / "meta.json").is_file():
        raise SystemExit(f"no such run: {run_dir}")

    depth, grid = read_grid(run_dir, "max_depth")
    depth = np.asarray(depth, float)
    sim = depth >= WET_THRESHOLD_M
    meta = read_meta(run_dir)
    cell = float(meta["domain"]["cellsize_m"])

    pre_db, post_db, _info = sar.fetch_s1_pair(
        bbox=tuple(meta["domain"]["bbox"]), site=SITE,
        pre_start=PRE_WINDOW[0], pre_end=PRE_WINDOW[1],
        post_start=POST_WINDOW[0], post_end=POST_WINDOW[1],
        grid=grid,
    )

    npz = np.load(REPO_ROOT / DEM_NPZ)
    dem = next((np.asarray(npz[k], float) for k in npz.files
                if getattr(npz[k], "shape", None) == depth.shape), None)
    if dem is None:
        raise SystemExit(f"{DEM_NPZ} holds no array shaped {depth.shape}")

    # No-data is not dry - see docs/SEVERITY_INVERSION.md.
    valid = (np.isfinite(pre_db) & np.isfinite(post_db)
             & (np.abs(post_db) > 1e-6) & (np.abs(pre_db) > 1e-6))
    sim_v = sim & valid
    n_sim = int(sim_v.sum())

    rows = []

    def score(obs, label, note=""):
        o = np.asarray(obs, bool) & valid
        tp = int((sim_v & o).sum())
        fp = int((sim_v & ~o).sum())
        fn = int((~sim_v & o).sum())
        comps = components(o)
        sizes = sorted((len(c) for c in comps), reverse=True)
        n_obs = int(o.sum())
        rows.append({
            "label": label, "note": note, "cells": n_obs,
            "components": len(comps),
            "largest_frac": (sizes[0] / max(n_obs, 1)) if sizes else 0.0,
            "csi": tp / (tp + fp + fn) if (tp + fp + fn) else 0.0,
            "pod": tp / (tp + fn) if (tp + fn) else 0.0,
            "far": fp / (tp + fp) if (tp + fp) else 0.0,
            "bias": (tp + fp) / (tp + fn) if (tp + fn) else 0.0,
            "ceiling": n_obs / max(n_sim, 1),
        })

    v0, _ = sar.flood_extent_from_change(pre_db, post_db)
    score(v0, "V0 raw change detection", "every figure to date")

    v1, i1 = sar.flood_extent_masked(pre_db, post_db, dem, cell,
                                     max_slope_deg=MAX_SLOPE_DEG)
    score(v1, f"V1 + slope mask <={MAX_SLOPE_DEG:.0f} deg",
          f"thr {i1['threshold_db']} dB")

    v2 = drop_small(v1, MIN_MAPPING_UNIT_CELLS)
    score(v2, f"V2 + min mapping unit {MIN_MAPPING_UNIT_CELLS} cells")

    h = hand(dem, dem <= np.nanpercentile(dem, CHANNEL_PERCENTILE))
    for hmax in HAND_THRESHOLDS_M:
        score(drop_small(v1 & (h <= hmax), MIN_MAPPING_UNIT_CELLS),
              f"V3 + HAND <= {hmax:.0f} m")

    print(f"\nCan a better Sentinel-1 reference raise the score?   run {RUN}")
    print(f"  simulated: {n_sim:,} wet cells in "
          f"{len(components(sim_v))} components, median terrain slope "
          f"{np.median(sar.slope_degrees(dem, cell)):.1f} deg")
    print(f"  every filter fixed a priori from the literature, none swept "
          f"for best CSI\n")

    hdr = (f"  {'observation variant':<32} {'cells':>7} {'comps':>6} "
           f"{'largest':>8} {'CSI':>8} {'POD':>6} {'FAR':>6} {'bias':>7} "
           f"{'ceiling':>8}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['label']:<32} {r['cells']:>7,} {r['components']:>6,} "
              f"{100*r['largest_frac']:>7.1f}% {r['csi']:>8.4f} "
              f"{r['pod']:>6.3f} {r['far']:>6.3f} {r['bias']:>7.2f} "
              f"{r['ceiling']:>8.4f}"
              + (f"   <- {r['note']}" if r["note"] else ""))

    best_shape = max(rows, key=lambda r: r["largest_frac"])
    print("")
    print("  THE REFERENCE GETS BETTER AND THE SCORE GETS WORSE.")
    print(f"  Cleaning takes the mask from {rows[0]['components']:,} components "
          f"to {best_shape['components']:,}, and the largest from "
          f"{100*rows[0]['largest_frac']:.1f}% of the wet area to "
          f"{100*best_shape['largest_frac']:.1f}%.")
    print("  It becomes flood-shaped, which confirms the speckle diagnosis.")
    print("")
    print("  But CEILING is |obs|/|sim|: the best CSI reachable if detection")
    print("  were PERFECT and the simulated extent stayed as it is. Removing")
    print("  noise cells shrinks |obs|, so it LOWERS the ceiling - from "
          f"{rows[0]['ceiling']:.4f} to {best_shape['ceiling']:.4f}.")
    print("")
    print("  The binding constraint is AREA AGREEMENT, not detection skill,")
    print("  and no work on the satellite touches it. Closing it needs the")
    print("  event's forcing (never published) and an observation at the flood")
    print("  peak (the passes are +2, +9 and +14 days). Both are outside this")
    print("  project. See docs/VALIDATION.md section 6.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
