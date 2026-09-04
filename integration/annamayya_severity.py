"""
integration/annamayya_severity.py - what breach severity is the observation
consistent with?

    python integration/annamayya_severity.py

THIS IS INVERSE MODELLING, NOT VALIDATION, AND NOT ACCURACY. Read that again
before quoting anything from the output.

The Annamayya (Cheyyeru) embankment failed on 19 November 2021. Nobody published
its breach parameters, so every simulation of it in this repository assumes a
FULL reservoir and a COMPLETE breach - the worst case, chosen because it is
defensible when the truth is unknown, not because it is what happened. That
assumption is the leading candidate for the large over-prediction the validation
reports: bias 7.2 against Sentinel-1.

So this sweeps the one unknown - how much of the reservoir was released - and
asks which value the observation is CONSISTENT WITH. That is a statement about
the EVENT, not about the model.

    "The available observation is most consistent with approximately X%
     reservoir release under the tested scenarios."

is a legitimate conclusion.

    "Our model accuracy is X%"

is not, and no cell of this table may ever be quoted that way. The highest CSI
here is the severity that best matches a noisy satellite composite; it is not a
measure of how well the hydraulics work. Those are different claims and the
report must keep them apart.

FOUR TERMS THIS FILE KEEPS DISTINCT
  model validation      does the model reproduce a known event? Needs the event's
                        forcing to be known. It is not here, so we cannot do it.
  inverse estimation    given the observation, what forcing is consistent? THIS.
  sensitivity analysis  how much does the answer move when an input moves? Also
                        this, as a by-product.
  system accuracy       how well does the system predict, in general? Not
                        measurable from one event, and nothing here reports it.

NO-DATA IS NOT DRY. modules/06's agreement() compares two boolean masks, so a
cell the satellite never imaged counts as "observed dry" and lands in correct
negatives or false alarms. That flatters or penalises depending on where the
gaps fall. Here every metric is computed ONLY over cells with valid backscatter,
and the number of excluded cells is reported.

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

SITE = "annamayya"
EVENT = "Annamayya (Cheyyeru) dam failure, 19 November 2021"
PRE_WINDOW = ("2021-09-15", "2021-10-31")
POST_WINDOW = ("2021-11-19", "2021-12-05")   # the 3-scene composite; the only
                                             # window that covers this domain
WET_THRESHOLD_M = 0.05                        # the contract threshold, unchanged
MIN_COVERAGE = 0.50


def masked_metrics(sim: np.ndarray, obs: np.ndarray, valid: np.ndarray) -> dict:
    """CSI/POD/FAR/bias over the cells the satellite actually imaged.

    `valid` excludes no-data. Counting a cell the sensor never saw as "dry"
    would be inventing an observation.
    """
    sim = np.asarray(sim, bool) & valid
    obs = np.asarray(obs, bool) & valid

    tp = int((sim & obs & valid).sum())
    fp = int((sim & ~obs & valid).sum())
    fn = int((~sim & obs & valid).sum())
    tn = int((~sim & ~obs & valid).sum())
    return {
        "hits": tp, "false_alarms": fp, "misses": fn, "correct_negatives": tn,
        "csi": round(tp / (tp + fp + fn), 4) if (tp + fp + fn) else 0.0,
        "pod": round(tp / (tp + fn), 4) if (tp + fn) else 0.0,
        "far": round(fp / (tp + fp), 4) if (tp + fp) else 0.0,
        "bias": round((tp + fp) / (tp + fn), 4) if (tp + fn) else 0.0,
        "cells_evaluated": int(valid.sum()),
        "cells_excluded_nodata": int((~valid).sum()),
    }


def coherence(mask: np.ndarray) -> dict:
    """Is this mask shaped like a flood, or like speckle?

    A flood is one connected corridor: water arrives from one place and spreads
    along the valley. Scattered radar change - wet soil, monsoon ponding,
    harvested fields, speckle - is not connected to anything. Counting the
    8-connected components separates the two without any hydraulics, and it is
    the check that decides whether the observation is a flood extent at all.
    """
    from collections import deque

    mask = np.asarray(mask, bool)
    H, W = mask.shape
    seen = np.zeros_like(mask)
    nbr = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
           (0, 1), (1, -1), (1, 0), (1, 1))
    sizes = []
    for y in range(H):
        for x in range(W):
            if mask[y, x] and not seen[y, x]:
                q = deque([(y, x)])
                seen[y, x] = True
                n = 0
                while q:
                    cy, cx = q.popleft()
                    n += 1
                    for dy, dx in nbr:
                        ny, nx = cy + dy, cx + dx
                        if (0 <= ny < H and 0 <= nx < W
                                and mask[ny, nx] and not seen[ny, nx]):
                            seen[ny, nx] = True
                            q.append((ny, nx))
                sizes.append(n)
    if not sizes:
        return {"cells": 0, "components": 0, "largest": 0,
                "largest_frac": 0.0, "singleton_frac": 0.0, "median": 0}
    sizes = np.array(sorted(sizes, reverse=True))
    total = int(sizes.sum())
    return {
        "cells": total,
        "components": len(sizes),
        "largest": int(sizes[0]),
        "largest_frac": round(float(sizes[0]) / total, 4),
        "singleton_frac": round(float((sizes <= 2).sum()) / len(sizes), 4),
        "median": int(np.median(sizes)),
    }


def load_observation(run_dir: Path):
    """Sentinel-1 change-detection mask + the validity mask, on the run's grid."""
    from importlib import import_module
    from shared.io import read_grid, read_meta

    sar = import_module("modules.06_gee_validation.sar")
    meta = read_meta(run_dir)
    bbox = tuple(meta["domain"]["bbox"])
    _d, grid = read_grid(run_dir, "max_depth")

    pre_db, post_db, info = sar.fetch_s1_pair(
        bbox=bbox, site=SITE,
        pre_start=PRE_WINDOW[0], pre_end=PRE_WINDOW[1],
        post_start=POST_WINDOW[0], post_end=POST_WINDOW[1],
        grid=grid,
    )
    obs, extra = sar.flood_extent_from_change(pre_db, post_db)

    # CHECK 1 and 2: the raster must hold real backscatter, over enough of the
    # domain. A zero-filled array is no-data, not dry ground.
    valid = (np.isfinite(pre_db) & np.isfinite(post_db)
             & (np.abs(post_db) > 1e-6) & (np.abs(pre_db) > 1e-6))
    coverage = float(valid.mean())
    return np.asarray(obs, bool), valid, coverage, {**info, **(extra or {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/annamayya_severity.py")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from shared.io import read_grid

    # Discover the runs and read what each ACTUALLY contains, rather than
    # trusting the order they were submitted in. run_id is allocated from the
    # folders that exist, so a concurrent submission can collide - one did
    # during this experiment, and this is the check that caught it.
    runs = {}
    anomalies = []
    for d in sorted((REPO_ROOT / "outputs").glob("cheyyeruprojectannamayya_overtop_fast_*")):
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        s, dom, res, t = m["scenario"], m["domain"], m["results"], m["time"]
        rec = {
            "run_id": d.name,
            "level": s.get("reservoir_level_frac"),
            "breach_width_m": s.get("breach_width_m"),
            "peak_cumecs": res.get("peak_discharge_cumecs"),
            "area_km2": res.get("flood_area_km2"),
            "cellsize_m": round(float(dom["cellsize_m"])),
            "shape": (int(dom["nx"]), int(dom["ny"])),
            "end_hr": float(t["end_hr"]),
            "dir": d,
        }
        # CHECK 5: identical configuration except the severity.
        if rec["cellsize_m"] != 60 or rec["end_hr"] != 24.0:
            anomalies.append(f"{d.name}: {rec['cellsize_m']} m / {rec['end_hr']:g} h "
                             f"- not the 60 m / 24 h sweep configuration, excluded")
            continue
        lvl = rec["level"]
        if lvl in runs:
            anomalies.append(f"{d.name}: duplicate level {lvl}, keeping "
                             f"{runs[lvl]['run_id']}")
            continue
        runs[lvl] = rec

    if not runs:
        raise SystemExit("no runs at the sweep configuration (60 m, 24 h)")

    ref = runs[max(runs)]
    obs, valid, coverage, info = load_observation(ref["dir"])
    if coverage < MIN_COVERAGE:
        raise SystemExit(f"observation covers only {100*coverage:.1f}% of the "
                         f"domain - refusing to score against mostly no-data")

    rows = []
    for lvl in sorted(runs, reverse=True):
        r = runs[lvl]
        # CHECK 4: identical grid, or the comparison is meaningless.
        depth, _g = read_grid(r["dir"], "max_depth")
        depth = np.asarray(depth, float)
        if depth.shape != obs.shape:
            anomalies.append(f"{r['run_id']}: grid {depth.shape} != observation "
                             f"{obs.shape}, excluded")
            continue
        sim = depth >= WET_THRESHOLD_M
        rows.append({**r, **masked_metrics(sim, obs, valid)})

    missing = [f"{int(100*l)}%" for l in (1.0, 0.75, 0.5, 0.25) if l not in runs]

    if args.json:
        print(json.dumps({"event": EVENT, "coverage": coverage,
                          "rows": [{k: v for k, v in r.items() if k != "dir"}
                                   for r in rows],
                          "missing": missing, "anomalies": anomalies}, indent=1))
        return 0

    print(f"\n{EVENT}")
    print("INVERSE MODELLING - which breach severity is the observation "
          "consistent with?")
    print(f"  observation: Sentinel-1 3-scene composite {POST_WINDOW[0]}..{POST_WINDOW[1]}, "
          f"threshold {info.get('threshold_db')} dB")
    print(f"  domain imaged: {100*coverage:.1f}%   metrics computed over "
          f"{int(valid.sum()):,} valid cells, {int((~valid).sum()):,} excluded as no-data")
    print(f"  all runs: 60 m grid, 24 h, 40 km reach, wet >= {WET_THRESHOLD_M} m\n")

    hdr = (f"  {'release':>8} {'peak m3/s':>10} {'area km2':>9} {'hits':>6} "
           f"{'false':>7} {'miss':>6} {'CSI':>7} {'POD':>6} {'FAR':>6} {'bias':>7}")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['level']*100:>7.0f}% {r['peak_cumecs']:>10,.0f} "
              f"{r['area_km2']:>9.2f} {r['hits']:>6,} {r['false_alarms']:>7,} "
              f"{r['misses']:>6,} {r['csi']:>7.4f} {r['pod']:>6.3f} "
              f"{r['far']:>6.3f} {r['bias']:>7.2f}")

    if missing:
        print(f"\n  NOT RUN: {', '.join(missing)} - reported as missing rather "
              f"than silently dropped.")
    for a in anomalies:
        print(f"  ANOMALY: {a}")

    # CHECK 6: is the observation a flood at all? A dam-break inundation is one
    # connected corridor. Scattered change is something else, and if the
    # observation is not flood-shaped then no severity can match it and the
    # whole inversion is answering the wrong question.
    obs_shape = coherence(obs & valid)
    ref_sim, _g = read_grid(ref["dir"], "max_depth")
    sim_shape = coherence((np.asarray(ref_sim, float) >= WET_THRESHOLD_M) & valid)
    print("")
    print("  IS THE OBSERVATION FLOOD-SHAPED? (8-connected components)")
    print(f"    observed  : {obs_shape['cells']:>6,} cells in "
          f"{obs_shape['components']:>5,} components, largest "
          f"{obs_shape['largest']:,} ({100*obs_shape['largest_frac']:.1f}%), "
          f"median {obs_shape['median']} cell(s)")
    print(f"    simulated : {sim_shape['cells']:>6,} cells in "
          f"{sim_shape['components']:>5,} components, largest "
          f"{sim_shape['largest']:,} ({100*sim_shape['largest_frac']:.1f}%), "
          f"median {sim_shape['median']} cell(s)")
    print(f"    {100*obs_shape['singleton_frac']:.0f}% of the observed components "
          f"are 1-2 cells - the signature of speckle and scattered")
    print("    wet ground, not of a river corridor.")

    if rows:
        best_csi = max(rows, key=lambda r: r["csi"])
        best_bias = min(rows, key=lambda r: abs(r["bias"] - 1.0))
        print("")
        print(f"  Best OVERLAP   (highest CSI): {best_csi['level']*100:.0f}% release, "
              f"CSI {best_csi['csi']:.4f}, bias {best_csi['bias']:.2f}")
        print(f"  Best AREA MATCH (bias -> 1):  {best_bias['level']*100:.0f}% release, "
              f"CSI {best_bias['csi']:.4f}, bias {best_bias['bias']:.2f}")
        if best_csi["level"] != best_bias["level"]:
            b = best_bias
            sim_cells = b["hits"] + b["false_alarms"]
            obs_cells = b["hits"] + b["misses"]
            overlap = 100.0 * b["hits"] / max(obs_cells, 1)
            print("")
            print("  THE TWO CRITERIA DISAGREE, AND THAT IS THE RESULT.")
            print(f"  At {b['level']*100:.0f}% release the simulated wet area is "
                  f"{sim_cells:,} cells against {obs_cells:,} observed")
            print(f"  - the same amount of water to within "
                  f"{abs(b['bias']-1)*100:.0f}% - yet they overlap on only "
                  f"{b['hits']:,} cells, {overlap:.1f}%.")
            print("")
            print("  Matching the AMOUNT of water without matching WHERE it is")
            print("  means the two are not the same phenomenon. If the observation")
            print("  were the dam's flood, the severity that gets the area right")
            print("  would also get the location right. No tested severity is")
            print("  consistent with the observation in a SPATIAL sense, so this")
            print("  inversion is NOT IDENTIFIABLE from this data.")

    print(
        "\n  THIS IS NOT THE SYSTEM'S ACCURACY. It is a statement about the\n"
        "  EVENT's forcing, which was never published, inferred from one noisy\n"
        "  satellite composite. Model validation would need the real breach\n"
        "  parameters; we do not have them, so we cannot do it.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
