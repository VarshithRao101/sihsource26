"""
modules/07_ml/montecarlo.py - the uncertainty band.

The rulebook's honesty policy says the winning move is not to claim accuracy we
do not have, but to measure the uncertainty and put it on screen. This is the
module that does the measuring.

Breach parameters carry roughly a factor-of-two uncertainty on peak flow. That
is not a hedge, it is the documented scatter in the source regressions - the
three we carry disagree with each other by a factor of 10 on Hirakud. Running
the deterministic scenario once and quoting the number is therefore false
precision. Running it a few thousand times across the plausible parameter
space, and quoting a band, is the honest version of the same answer.

    python -m modules.07_ml.montecarlo --capacity 5 --height 60 --n 4000

Cheap by construction: the uncertainty lives almost entirely in the breach and
the reservoir, both of which are captured by the level-pool routing in
shared.hydro. That is milliseconds per sample, so a 4,000-member ensemble costs
seconds rather than the weeks a full 2D ensemble would take. What it does NOT
propagate is terrain and roughness uncertainty into the 2D extent - say so.

Owner: captain (module 07).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from shared.hydro import (
    BreachParams,
    breach_hydrograph,
    breach_parameter_ensemble,
    peak_outflow_regressions,
)
from shared.io import hydrograph_volume_m3

REPO_ROOT = Path(__file__).resolve().parents[2]

# The factor-of-two band on breach geometry. Froehlich (2008) reports the
# prediction scatter of his own regression explicitly, and the spread between
# the three regressions we carry is of the same order. We sample a
# multiplicative factor over this range rather than assuming a published sigma
# we cannot verify - and the range is stated in the output, so a reviewer can
# disagree with it and re-run rather than having to trust it.
WIDTH_FACTOR_RANGE = (0.5, 2.0)
TIME_FACTOR_RANGE = (0.5, 2.0)

# Storage-elevation exponent. V = V_full * (h/H)^k. k = 1 is a vertical-walled
# tank, k = 3 a cone; real valley impoundments sit between. We have no surveyed
# curve for an arbitrary dam, so this is sampled, not assumed.
STORAGE_EXPONENT_RANGE = (2.0, 3.2)


@dataclass
class EnsembleResult:
    peaks_cumecs: np.ndarray
    volumes_mcm: np.ndarray
    widths_m: np.ndarray
    formation_hr: np.ndarray
    regressions_used: list[str]

    def percentiles(self, qs=(5, 25, 50, 75, 95)) -> dict:
        return {
            f"p{q}": round(float(np.percentile(self.peaks_cumecs, q)), 1) for q in qs
        }


def run_ensemble(
    capacity_mcm: float,
    dam_height_m: float,
    failure_mode: str = "overtopping",
    reservoir_level_frac: float = 1.0,
    n: int = 4000,
    duration_hr: float = 12.0,
    seed: int = 26161,
) -> EnsembleResult:
    """Monte Carlo over breach geometry, formation time and storage shape.

    Each member: pick one of the three regressions at random, perturb its
    width and formation time by an independent factor in the stated range,
    sample a storage exponent, and route the reservoir down through the breach.

    Picking the regression rather than averaging them is deliberate. Averaging
    three disagreeing models produces a fourth number that none of them
    supports and hides the disagreement; sampling preserves it in the spread.
    """
    rng = np.random.default_rng(seed)
    capacity_m3 = capacity_mcm * 1e6
    water_volume_m3 = capacity_m3 * reservoir_level_frac

    base = breach_parameter_ensemble(water_volume_m3, dam_height_m, failure_mode)  # type: ignore[arg-type]
    names = list(base)

    peaks = np.empty(n)
    volumes = np.empty(n)
    widths = np.empty(n)
    times = np.empty(n)

    pick = rng.integers(0, len(names), n)
    wf = rng.uniform(*WIDTH_FACTOR_RANGE, n)
    tf = rng.uniform(*TIME_FACTOR_RANGE, n)
    ke = rng.uniform(*STORAGE_EXPONENT_RANGE, n)

    for i in range(n):
        src = base[names[pick[i]]]
        b = BreachParams(
            bottom_width_m=max(src.bottom_width_m * wf[i], 1.0),
            average_width_m=max(src.average_width_m * wf[i], 1.0),
            side_slope_h_per_v=src.side_slope_h_per_v,
            depth_m=src.depth_m,
            formation_time_hr=max(src.formation_time_hr * tf[i], 1.0 / 60.0),
            source=src.source,
        )
        t, q = breach_hydrograph(
            b,
            dam_height_m=dam_height_m,
            capacity_m3=capacity_m3,
            reservoir_level_frac=reservoir_level_frac,
            failure_mode=failure_mode,  # type: ignore[arg-type]
            duration_hr=duration_hr,
            dt_s=10.0,
            output_step_hr=0.1,
            storage_exponent=float(ke[i]),
        )
        peaks[i] = float(q.max())
        volumes[i] = hydrograph_volume_m3(t, q) / 1e6
        widths[i] = b.average_width_m
        times[i] = b.formation_time_hr

    return EnsembleResult(peaks, volumes, widths, times, names)


def fit_gp_surrogate(res: EnsembleResult, seed: int = 26161):
    """Gaussian Process from (breach width, formation time) to peak discharge.

    Why a GP rather than a bigger ensemble: it gives a PREDICTIVE VARIANCE, not
    just a fitted value. That means the dashboard can answer "what if the
    breach is 140 m wide" with a mean and an honest error bar, without running
    another thousand routings, and the error bar widens automatically in
    regions the ensemble sampled sparsely - which is exactly where we should be
    least confident.

    Trained on a subsample: a GP is O(n^3) in the number of training points and
    500 is plenty for a smooth two-input response surface.
    """
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

    rng = np.random.default_rng(seed)
    n_fit = min(500, len(res.peaks_cumecs))
    sel = rng.choice(len(res.peaks_cumecs), n_fit, replace=False)

    X = np.column_stack([res.widths_m[sel], res.formation_hr[sel]])
    y = np.log(np.maximum(res.peaks_cumecs[sel], 1.0))  # log: peaks span decades

    x_mu, x_sd = X.mean(0), X.std(0) + 1e-9
    Xs = (X - x_mu) / x_sd

    kernel = ConstantKernel(1.0) * RBF([1.0, 1.0]) + WhiteKernel(1e-2)
    gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True, random_state=seed)
    gp.fit(Xs, y)

    def predict(width_m: float, formation_hr: float) -> dict:
        xs = (np.array([[width_m, formation_hr]]) - x_mu) / x_sd
        mu, sd = gp.predict(xs, return_std=True)
        return {
            "peak_cumecs": round(float(np.exp(mu[0])), 1),
            "p10_cumecs": round(float(np.exp(mu[0] - 1.2816 * sd[0])), 1),
            "p90_cumecs": round(float(np.exp(mu[0] + 1.2816 * sd[0])), 1),
        }

    return predict, {"n_fit": n_fit, "kernel": str(gp.kernel_)}


def summarise(
    capacity_mcm: float,
    dam_height_m: float,
    failure_mode: str = "overtopping",
    reservoir_level_frac: float = 1.0,
    n: int = 4000,
    seed: int = 26161,
) -> dict:
    """The uncertainty block, ready to merge into a run's uncertainty.json."""
    res = run_ensemble(
        capacity_mcm, dam_height_m, failure_mode, reservoir_level_frac, n, seed=seed
    )
    regs = peak_outflow_regressions(capacity_mcm * 1e6 * reservoir_level_frac, dam_height_m)
    pcts = res.percentiles()

    band_ratio = pcts["p95"] / max(pcts["p5"], 1e-9)

    return {
        "method": (
            "Monte Carlo over breach regression choice, breach width, formation "
            "time and reservoir storage exponent, routed through level-pool "
            "hydraulics. Terrain and roughness uncertainty are NOT propagated "
            "into the 2D extent - this band is on discharge only."
        ),
        "n_members": n,
        "sampling": {
            "regressions": res.regressions_used,
            "width_factor_range": list(WIDTH_FACTOR_RANGE),
            "formation_time_factor_range": list(TIME_FACTOR_RANGE),
            "storage_exponent_range": list(STORAGE_EXPONENT_RANGE),
        },
        "peak_discharge_cumecs": pcts,
        "peak_band_ratio_p95_over_p5": round(band_ratio, 2),
        "released_volume_mcm": {
            "p5": round(float(np.percentile(res.volumes_mcm, 5)), 2),
            "p50": round(float(np.percentile(res.volumes_mcm, 50)), 2),
            "p95": round(float(np.percentile(res.volumes_mcm, 95)), 2),
        },
        "empirical_regressions_cumecs": {k: round(v, 1) for k, v in regs.items()},
        "honest_statement": (
            f"Peak discharge is between {pcts['p5']:,.0f} and {pcts['p95']:,.0f} m3/s "
            f"across the plausible breach parameter space - a factor of "
            f"{band_ratio:.1f}. Any single quoted peak is one draw from this."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.07_ml.montecarlo")
    ap.add_argument("--capacity", type=float, required=True, help="MCM")
    ap.add_argument("--height", type=float, required=True, help="m")
    ap.add_argument("--mode", default="overtopping")
    ap.add_argument("--level", type=float, default=1.0)
    ap.add_argument("--n", type=int, default=4000)
    ap.add_argument("--gp", action="store_true", help="also fit the GP surrogate")
    args = ap.parse_args(argv)

    out = summarise(args.capacity, args.height, args.mode, args.level, args.n)
    print(json.dumps(out, indent=2))

    if args.gp:
        res = run_ensemble(args.capacity, args.height, args.mode, args.level, args.n)
        predict, info = fit_gp_surrogate(res)
        print("\nGP surrogate:", json.dumps(info))
        w = float(np.median(res.widths_m))
        t = float(np.median(res.formation_hr))
        print(f"  at width {w:.0f} m, formation {t:.2f} hr -> {json.dumps(predict(w, t))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
