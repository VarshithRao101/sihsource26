"""
integration/validate_annamayya.py - a real dam failure, against what the
satellite saw.

    python integration/validate_annamayya.py --run <run_id>

THE EVENT. The Annamayya (Cheyyeru Project) earthfill embankment, Andhra
Pradesh, failed on 19 November 2021 during heavy rainfall - 25 m high, 63.16
MCM, dam AP01MH0129 in the CWC register. It is the validation case this project
uses because it is a REAL Indian dam break, it falls inside the Sentinel-1 era
so an observation exists, and it is on a floodplain: the Teesta gorge case could
not be observed at all, because a flood corridor one to three cells wide in 30
degree terrain is below what Sentinel-1 can resolve.

WHAT THIS COMPARES. Our simulated maximum extent over 24 hours against
Sentinel-1 change detection between a pre-event median and the post-event
scenes. It reports CSI, POD, FAR and bias, and it writes the caveats into
validation.json alongside them, because a CSI quoted without its caveats is a
claim we cannot defend.

WHAT IT IS NOT. It is not a measure of how accurate the model is in general. It
is one event, one reach, one satellite pass, and the mismatch between a
full-reservoir worst case and an unknown real breach severity is baked into the
number. Read docs/VALIDATION.md before quoting anything from here.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The observation windows used for the 90 m validation, kept identical so the
# two runs differ only in resolution.
EVENT = "Annamayya (Cheyyeru) dam failure, 19 November 2021"
SITE = "annamayya"
PRE_WINDOW = ("2021-09-15", "2021-10-31")
POST_WINDOW = ("2021-11-19", "2021-12-05")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/validate_annamayya.py")
    ap.add_argument("--run", required=True, help="run id in outputs/")
    ap.add_argument("--compare-to", default=None,
                    help="an earlier run id to print alongside, e.g. the 90 m one")
    ap.add_argument("--force", action="store_true",
                    help="refetch the SAR pair instead of using the cache")
    args = ap.parse_args(argv)

    from importlib import import_module
    sar = import_module("modules.06_gee_validation.sar")

    run_dir = REPO_ROOT / "outputs" / args.run
    if not (run_dir / "meta.json").exists():
        raise SystemExit(f"no such run: {run_dir}")

    print(f"Validating {args.run}")
    print(f"  event      {EVENT}")
    print(f"  pre-event  {PRE_WINDOW[0]} to {PRE_WINDOW[1]}")
    print(f"  post-event {POST_WINDOW[0]} to {POST_WINDOW[1]}")
    print("  fetching Sentinel-1 via Earth Engine ...\n")

    out = sar.validate_run(
        run_dir=run_dir, site=SITE,
        pre_window=PRE_WINDOW, post_window=POST_WINDOW,
        event_name=EVENT, force=args.force,
    )

    m = out["metrics"]
    obs = out["observation"]
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    cell = meta["domain"]["cellsize_m"]

    rows = [(args.run, cell, m, out)]
    if args.compare_to:
        p = REPO_ROOT / "outputs" / args.compare_to / "validation.json"
        q = REPO_ROOT / "outputs" / args.compare_to / "meta.json"
        if p.exists() and q.exists():
            old = json.loads(p.read_text(encoding="utf-8"))
            oldmeta = json.loads(q.read_text(encoding="utf-8"))
            rows.insert(0, (args.compare_to,
                            oldmeta["domain"]["cellsize_m"],
                            old["metrics"], old))

    print(f"  {'run':<46} {'cell':>6} {'CSI':>8} {'POD':>7} {'FAR':>7} {'bias':>7}")
    print(f"  {'-'*46} {'-'*6} {'-'*8} {'-'*7} {'-'*7} {'-'*7}")
    for rid, cs, mm, _ in rows:
        print(f"  {rid[:46]:<46} {cs:>5.0f}m {mm['csi']:>8.4f} {mm['pod']:>7.4f} "
              f"{mm['far']:>7.4f} {mm['bias']:>7.2f}")

    print(f"\n  simulated wet cells   {out['simulated_wet_cells']:,}")
    print(f"  observed wet cells    {out['observed_wet_cells']:,}")
    print(f"  threshold             {obs.get('threshold_db')} dB "
          f"({obs.get('threshold_method')})")
    print(f"  scenes                {obs.get('n_scenes_pre')} pre, "
          f"{obs.get('n_scenes_post')} post, {obs.get('orbit_pass')}")

    print("\n  CAVEATS - read these with the number, never without:")
    for c in out.get("caveats", []):
        print(f"    - {c}")

    print(
        "\n  This is one event on one reach against one satellite pass. It is a\n"
        "  diagnosis, not a general accuracy figure, and docs/VALIDATION.md says\n"
        "  so at more length.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
