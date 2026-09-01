"""
integration/compare_engines.py - the three-engine comparison table.

    python integration/compare_engines.py --site chungthang --capacity 5 --height 60

The problem statement asks for SPH and Delft3D. Most teams will show one flood
map. The thing that separates a modelling framework from a picture is running
more than one engine on the same problem and putting the numbers side by side -
including where they disagree, and why.

    SPH (DualSPHysics)   the breach itself, first minute, GPU particles
    level-pool routing   reservoir drawdown over hours (shared.hydro)
    empirical regression four independent peak-outflow formulae
    fast solver          2D shallow water over real terrain
    Delft3D FM           far-field routing  [not installed]

They do NOT measure the same thing, and the table says so per row rather than
pretending four numbers are four attempts at one answer. That distinction is
the whole point: quoting an SPH near-field peak next to a routed peak as though
they were comparable would be the sort of confident wrongness that loses a
round with an NTRO hydrologist.

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

from shared.hydro import (  # noqa: E402
    breach_hydrograph,
    breach_parameter_ensemble,
    peak_outflow_regressions,
)
from shared.io import hydrograph_volume_m3  # noqa: E402


def instantaneous_weir_peak(
    bottom_width_m: float, head_m: float, side_slope: float = 1.0
) -> float:
    """Broad-crested weir discharge through a fully-open breach, m3/s.

    This is the number SPH should be compared against, NOT the routed peak.
    SPH starts with the breach already open, so it models the same instant this
    formula does. The routed peak is lower because the breach grows over
    minutes and the reservoir drains as it goes.
    """
    return 1.7 * bottom_width_m * head_m**1.5 + 1.1 * side_slope * head_m**2.5


def sph_result(case_name: str) -> dict | None:
    """Peak and duration from a finished DualSPHysics case, or None."""
    try:
        from importlib import import_module

        sph = import_module("modules.02_sph.breach")
        case_dir = sph.CASES_DIR / case_name
        if not (case_dir / "out" / "flow.csv").exists():
            return None
        t_hr, q = sph.hydrograph_from_flow(case_dir, case_name)
        case = json.loads((case_dir / "case.json").read_text())
        return {
            "peak_cumecs": float(q.max()),
            "simulated_s": float(t_hr[-1] * 3600.0),
            "dp_m": case["dp"],
            "particles": int(
                case["reservoir_length_m"]
                * case["channel_width_m"]
                * case["water_depth_m"]
                / case["dp"] ** 3
            ),
            "breach_width_m": case["breach_bottom_width_m"],
            "head_m": case["water_depth_m"],
        }
    except Exception:
        return None


def compare(
    capacity_mcm: float,
    dam_height_m: float,
    failure_mode: str = "overtopping",
    sph_case: str | None = None,
    run_dir: str | Path | None = None,
) -> dict:
    """Assemble the comparison. Every row states what it measures."""
    capacity_m3 = capacity_mcm * 1e6
    ensemble = breach_parameter_ensemble(capacity_m3, dam_height_m, failure_mode)  # type: ignore[arg-type]
    froehlich = ensemble["froehlich2008"]

    t, q = breach_hydrograph(
        froehlich, dam_height_m=dam_height_m, capacity_m3=capacity_m3,
        failure_mode=failure_mode, duration_hr=12.0,  # type: ignore[arg-type]
    )
    routed_peak = float(q.max())
    routed_volume = hydrograph_volume_m3(t, q) / 1e6

    regs = peak_outflow_regressions(capacity_m3, dam_height_m)

    rows = [
        {
            "engine": "level-pool routing (shared.hydro)",
            "measures": "reservoir drawdown through a growing breach, over hours",
            "peak_cumecs": round(routed_peak, 1),
            "note": f"released {routed_volume:.2f} MCM over 12 h",
        }
    ]

    weir = instantaneous_weir_peak(froehlich.bottom_width_m, dam_height_m)
    rows.append(
        {
            "engine": "weir equation, breach fully open",
            "measures": "instantaneous discharge through the final breach",
            "peak_cumecs": round(weir, 1),
            "note": "the fair comparison for SPH - no breach growth, no drawdown",
        }
    )

    sph = sph_result(sph_case) if sph_case else None
    if sph:
        agreement = 100.0 * abs(sph["peak_cumecs"] - weir) / max(weir, 1e-9)
        rows.append(
            {
                "engine": "SPH (DualSPHysics v5.4, GPU)",
                "measures": "near-field particle flow through the open breach",
                "peak_cumecs": round(sph["peak_cumecs"], 1),
                "note": (
                    f"{sph['particles']:,} particles at dp={sph['dp_m']} m, "
                    f"{sph['simulated_s']:.0f} s simulated; "
                    f"{agreement:.0f}% from the weir equation"
                ),
            }
        )

    for name, value in sorted(regs.items(), key=lambda kv: kv[1]):
        rows.append(
            {
                "engine": f"empirical regression: {name}",
                "measures": "peak outflow fitted to documented historical failures",
                "peak_cumecs": round(value, 1),
                "note": "independent of our hydraulics entirely",
            }
        )

    if run_dir:
        try:
            from shared.io import read_meta

            meta = read_meta(run_dir)
            res = meta["results"]
            rows.append(
                {
                    "engine": "fast solver, 2D shallow water",
                    "measures": "inundation over real terrain; peak is its inflow",
                    "peak_cumecs": res.get("peak_discharge_cumecs"),
                    "note": (
                        f"{res.get('flood_area_km2')} km2 flooded, mass error "
                        f"{res.get('mass_balance_err_pct')}%, "
                        f"{res.get('runtime_s')} s runtime"
                    ),
                }
            )
        except Exception:
            pass

    # Absence is measured, not assumed. modules/03_delft3d/engine.py looks for
    # the D-Flow FM kernel and reports what it actually found - which is not the
    # same question as whether the Deltares licence manager is installed.
    from importlib import import_module

    d3d = import_module("modules.03_delft3d.engine").status()
    rows.append(
        {
            "engine": "Delft3D FM",
            "measures": "far-field routing",
            "peak_cumecs": None,
            "note": d3d["summary"] + " Reported as absent, not estimated.",
            "engine_check": d3d,
        }
    )

    peaks = [r["peak_cumecs"] for r in rows if r["peak_cumecs"]]
    return {
        "site": {"capacity_mcm": capacity_mcm, "dam_height_m": dam_height_m,
                 "failure_mode": failure_mode},
        "breach": {
            "source": froehlich.source,
            "bottom_width_m": round(froehlich.bottom_width_m, 1),
            "average_width_m": round(froehlich.average_width_m, 1),
            "formation_time_hr": round(froehlich.formation_time_hr, 3),
        },
        "rows": rows,
        "spread": {
            "min_cumecs": round(min(peaks), 1),
            "max_cumecs": round(max(peaks), 1),
            "ratio": round(max(peaks) / max(min(peaks), 1e-9), 2),
        },
        "reading": (
            "These rows are NOT four estimates of one quantity. Instantaneous "
            "breach discharge is necessarily larger than a routed peak, and an "
            "empirical regression predicts a different thing again. Compare "
            "like with like: SPH against the weir equation, routed against the "
            "regressions."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/compare_engines.py")
    ap.add_argument("--capacity", type=float, default=5.0, help="MCM")
    ap.add_argument("--height", type=float, default=60.0, help="m")
    ap.add_argument("--mode", default="overtopping")
    ap.add_argument("--sph-case", default="chungthang")
    ap.add_argument("--run", default=None, help="a run folder for the fast solver row")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out = compare(args.capacity, args.height, args.mode, args.sph_case, args.run)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    b = out["breach"]
    print(
        f"\nThree-engine comparison - {args.capacity} MCM behind a {args.height} m dam, "
        f"{args.mode}"
    )
    print(
        f"Breach: {b['average_width_m']} m average width, forms in "
        f"{b['formation_time_hr'] * 60:.0f} min  [{b['source']}]\n"
    )
    w = max(len(r["engine"]) for r in out["rows"])
    print(f"{'engine':<{w}}  {'peak m3/s':>12}   measures")
    print("-" * (w + 16 + 60))
    for r in out["rows"]:
        peak = f"{r['peak_cumecs']:,.0f}" if r["peak_cumecs"] else "n/a"
        print(f"{r['engine']:<{w}}  {peak:>12}   {r['measures']}")
        print(f"{'':<{w}}  {'':>12}   {r['note']}")
    print(
        f"\nspread across everything quoted: {out['spread']['min_cumecs']:,.0f} to "
        f"{out['spread']['max_cumecs']:,.0f} m3/s ({out['spread']['ratio']}x)"
    )
    print(f"\n{out['reading']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
