"""
shared/fake.py - synthetic runs, so that nobody is ever blocked.

    python -m shared.fake --run-id synthetic_overtop_fast_001

Generates a complete, contract-valid run folder with a plausible synthetic
flood on a synthetic V-shaped valley. Use it from day one: the frontend builds
the whole dashboard against fake floods before any solver exists, Delft3D
develops against a fake hydrograph before SPH runs.

Every run this produces carries **is_fake: true**. The demo refuses to load
those, the frontend shows a SYNTHETIC DATA banner, and the validator warns.
That is the mechanism that stops fabricated numbers reaching a juror. Do not
remove it, do not flip the flag by hand.

Owner: captain / person 4.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from shared.contract import (
    DEFAULT_MANNING_N,
    GRAVITY,
    SCHEMA_VERSION,
    WET_THRESHOLD_M,
)
from shared.geo import Grid
from shared.hydro import breach_hydrograph, froehlich_2008
from shared.io import (
    build_meta,
    hydrograph_volume_m3,
    write_extent,
    write_grid,
    write_hydrograph,
    write_json,
    write_meta,
    write_packed_png,
)


# --------------------------------------------------------------------------
# Synthetic terrain
# --------------------------------------------------------------------------


def synthetic_valley(
    grid: Grid,
    crest_elev_m: float = 1600.0,
    slope: float = 0.008,
    valley_depth_m: float = 120.0,
    valley_halfwidth_cells: float = 12.0,
    meander_amplitude_cells: float = 18.0,
    seed: int = 26161,
) -> np.ndarray:
    """A meandering V-shaped valley descending north to south.

    Not a real DEM and never presented as one. It exists so that a solver can
    be exercised, a shader can be lit up and a dashboard can be built before
    module 01 has downloaded a single tile.

    Returns elevation in metres, shape == grid.shape.
    """
    rng = np.random.default_rng(seed)
    ny, nx = grid.shape
    rows = np.arange(ny)[:, None]
    cols = np.arange(nx)[None, :]

    cellsize = grid.cellsize_m()

    # Channel centreline meanders about the middle column.
    centre = nx / 2.0 + meander_amplitude_cells * np.sin(2.0 * np.pi * rows / (ny / 2.6))

    # Regional slope: elevation drops going south (increasing row index).
    regional = crest_elev_m - slope * rows * cellsize

    # V-shaped incision, capped so the valley walls are flat plateau.
    dist = np.abs(cols - centre)
    incision = valley_depth_m * np.clip(1.0 - dist / valley_halfwidth_cells, 0.0, 1.0)

    # Gentle valley-wall rise plus low-amplitude noise for texture.
    walls = 0.35 * valley_depth_m * np.clip(
        (dist - valley_halfwidth_cells) / (0.35 * nx), 0.0, 1.0
    )
    noise = rng.normal(0.0, 0.8, size=(ny, nx))

    return (regional - incision + walls + noise).astype(np.float32)


# --------------------------------------------------------------------------
# Synthetic flood
# --------------------------------------------------------------------------


def _kinematic_flood(
    grid: Grid,
    dem: np.ndarray,
    peak_q_cumecs: float,
    end_hr: float,
    manning_n: float = DEFAULT_MANNING_N,
) -> dict[str, np.ndarray]:
    """A cheap kinematic-wave sketch of a flood down the synthetic valley.

    This is NOT a solver. It gives grids with the right shape, the right units
    and physically ordered values (water arrives later further downstream,
    peaks after it arrives, is deeper in the channel than on the walls) so that
    everything downstream of the contract can be built and tested. Anything
    that consumes it must respect is_fake.
    """
    ny, nx = grid.shape
    cellsize = grid.cellsize_m()

    # Height above the local channel bed, per row - the channel is the minimum.
    channel_bed = dem.min(axis=1, keepdims=True)
    hand = dem - channel_bed  # "height above nearest drainage", crudely

    # Downstream distance from the dam, at the north edge.
    dist_m = np.arange(ny)[:, None] * cellsize * np.ones((1, nx))

    # Attenuate the flood with distance: a real dam-break wave loses peak
    # discharge downstream as it spreads. Exponential decay over ~40 km.
    attenuation = np.exp(-dist_m / 40_000.0)

    # Stage that the wave reaches, above the channel bed.
    stage = (
        (peak_q_cumecs / 1000.0) ** 0.4 * 9.0 * attenuation
    )  # metres, tuned to give O(10 m) near the dam
    depth = np.clip(stage - hand, 0.0, None).astype(np.float32)
    depth[depth < WET_THRESHOLD_M] = 0.0

    wet = depth >= WET_THRESHOLD_M

    # Celerity of the front: sqrt(g*h) with a floor, so arrival grows downstream.
    celerity = np.sqrt(GRAVITY * np.maximum(depth, 0.5))
    front_speed = np.maximum(celerity.mean(axis=1, keepdims=True) * 0.55, 0.5)
    arrival_hr = (dist_m / front_speed) / 3600.0

    arrival = np.where(wet, arrival_hr, np.nan).astype(np.float32)
    arrival = np.clip(arrival, 0.0, end_hr).astype(np.float32)
    arrival[~wet] = np.nan

    # Peak lags arrival by a fraction of the remaining window.
    lag = 0.15 * end_hr * (0.4 + 0.6 * np.clip(dist_m / (ny * cellsize), 0, 1))
    peak = np.where(wet, np.minimum(arrival + lag, end_hr), np.nan).astype(np.float32)

    # Velocity from Manning on the local slope.
    slope = np.gradient(dem, cellsize, axis=0)
    slope = np.clip(-slope, 1e-4, 0.2)
    velocity = np.where(
        wet, (1.0 / manning_n) * depth ** (2.0 / 3.0) * np.sqrt(slope), 0.0
    ).astype(np.float32)
    velocity = np.clip(velocity, 0.0, 18.0)

    duration = np.where(wet, np.clip(end_hr - arrival, 0.0, end_hr), 0.0).astype(np.float32)

    return {
        "max_depth": depth,
        "arrival_time": arrival,
        "time_of_peak": peak,
        "max_velocity": velocity,
        "duration": duration,
        "max_dv": (depth * velocity).astype(np.float32),
    }


def _synthetic_impact(
    run_id: str, grid: Grid, grids: dict[str, np.ndarray], rng: np.random.Generator
) -> dict:
    """A synthetic impact table.

    Place names here are deliberately obvious placeholders. The contract says
    settlement names must be real, from OSM or the Census - so a fake run is
    the one place they are not, and they are named so that nobody can mistake
    one for a real village on a screenshot.
    """
    depth = grids["max_depth"]
    arrival = grids["arrival_time"]
    wet = depth >= WET_THRESHOLD_M
    rows, cols = np.nonzero(wet)
    if rows.size == 0:
        return {"run_id": run_id, "totals": {}, "settlements": []}

    n = min(12, rows.size)
    pick = rng.choice(rows.size, size=n, replace=False)
    settlements = []
    total_pop = 0
    for k, idx in enumerate(sorted(pick, key=lambda i: rows[i])):
        r, c = int(rows[idx]), int(cols[idx])
        lon, lat = grid.lonlat(r, c)
        pop = int(rng.integers(180, 4200))
        total_pop += pop
        d = float(depth[r, c])
        a = float(arrival[r, c]) if np.isfinite(arrival[r, c]) else 0.0
        v = float(grids["max_velocity"][r, c])
        from shared.contract import hazard_class

        settlements.append(
            {
                "name": f"SYNTHETIC-PLACE-{k + 1:02d}",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "population": pop,
                "arrival_hr": round(a, 3),
                "max_depth_m": round(d, 2),
                "hazard_class": hazard_class(d, v),
            }
        )

    area_km2 = float(wet.sum()) * grid.cell_area_m2() / 1e6
    return {
        "run_id": run_id,
        "totals": {
            "settlements_affected": len(settlements),
            "population_affected": total_pop,
            "buildings_affected": int(total_pop * 0.11),
            "roads_cut_km": round(area_km2 * 0.42, 1),
            "bridges_affected": int(rng.integers(0, 5)),
            "cropland_ha": round(area_km2 * 18.0, 1),
            "damage_inr_crore": None,
            "damage_curve_source": "SYNTHETIC - no damage curve applied",
        },
        "settlements": settlements,
        "roads": [],
        "infrastructure": [],
        "note": "Generated by shared.fake. Every value here is synthetic.",
    }


# --------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------


def generate_fake_run(
    run_id: str = "synthetic_overtop_fast_001",
    outputs_dir: str | Path = "outputs",
    nx: int = 220,
    ny: int = 320,
    dam_height_m: float = 60.0,
    capacity_mcm: float = 5.0,
    end_hr: float = 12.0,
    failure_mode: str = "overtopping",
    seed: int = 26161,
    write_png: bool = True,
    write_impact: bool = True,
) -> Path:
    """Write a complete, contract-valid, obviously synthetic run folder.

    The hydrograph is real physics - shared.hydro.breach_hydrograph with the
    Froehlich (2008) breach - because a fake hydrograph with the wrong shape
    teaches modules 03 and 04 the wrong thing. Only the flood grids are faked.

    Returns:
        Path to the run folder.
    """
    t_start = time.perf_counter()
    rng = np.random.default_rng(seed)

    run_dir = Path(outputs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # A domain roughly the size of a 60 km Himalayan reach.
    grid = Grid(bbox=(88.30, 27.20, 88.62, 27.75), nx=nx, ny=ny)

    dem = synthetic_valley(grid, seed=seed)

    capacity_m3 = capacity_mcm * 1e6
    breach = froehlich_2008(capacity_m3, dam_height_m, failure_mode)  # type: ignore[arg-type]
    t_hr, q_cumecs = breach_hydrograph(
        breach,
        dam_height_m=dam_height_m,
        capacity_m3=capacity_m3,
        failure_mode=failure_mode,  # type: ignore[arg-type]
        duration_hr=end_hr,
    )

    grids = _kinematic_flood(grid, dem, float(q_cumecs.max()), end_hr)

    for name, arr in grids.items():
        write_grid(run_dir, name, arr, grid, description=f"SYNTHETIC {name}")

    write_hydrograph(run_dir, t_hr, q_cumecs)
    write_extent(run_dir, grids["max_depth"], grid)

    packed_max = None
    if write_png:
        _, packed_max = write_packed_png(
            run_dir,
            grids["arrival_time"],
            grids["time_of_peak"],
            grids["max_depth"],
            grids["duration"],
            end_hr,
        )

    if write_impact:
        write_json(run_dir, "impact.json", _synthetic_impact(run_id, grid, grids, rng))

    wet = grids["max_depth"] >= WET_THRESHOLD_M
    results = {
        "peak_discharge_cumecs": round(float(q_cumecs.max()), 1),
        "max_depth_m": round(float(grids["max_depth"].max()), 2),
        "flood_area_km2": round(float(wet.sum()) * grid.cell_area_m2() / 1e6, 2),
        "released_volume_mcm": round(hydrograph_volume_m3(t_hr, q_cumecs) / 1e6, 3),
        "runtime_s": round(time.perf_counter() - t_start, 2),
        "mass_balance_err_pct": 0.0,
    }
    if packed_max is not None:
        results["packed_depth_max_m"] = round(packed_max, 3)

    meta = build_meta(
        run_id=run_id,
        engine="fast",
        grid=grid,
        site={
            "name": "SYNTHETIC SITE (not a real dam)",
            "river": "SYNTHETIC",
            "state": "SYNTHETIC",
            "lat": 27.7400,
            "lon": 88.4600,
            "dam_height_m": dam_height_m,
            "reservoir_capacity_mcm": capacity_mcm,
            "source": "shared.fake",
        },
        scenario={
            "failure_mode": failure_mode,
            "reservoir_level_frac": 1.0,
            "breach_width_m": round(breach.average_width_m, 1),
            "breach_side_slope": breach.side_slope_h_per_v,
            "formation_time_hr": round(breach.formation_time_hr, 3),
            "breach_param_source": breach.source,
            "storage_curve": "power law, k = 2.7",
        },
        time_block={"start_hr": 0.0, "end_hr": end_hr, "output_step_hr": 0.25},
        results=results,
        module="shared.fake",
        dem={
            "source": "SYNTHETIC",
            "native_resolution_m": round(grid.cellsize_m(), 1),
            "bathymetry": "none",
            "conditioning": "synthetic V-valley, no conditioning",
        },
        is_fake=True,
        notes=(
            "Synthetic run from shared.fake. Grids are a kinematic sketch, not a "
            "solver result. The hydrograph is real level-pool routing. "
            "Never show this to a juror as a result."
        ),
    )
    write_meta(run_dir, meta)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.fake",
        description="Generate a contract-valid synthetic run folder.",
    )
    parser.add_argument("--run-id", default="synthetic_overtop_fast_001")
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--nx", type=int, default=220)
    parser.add_argument("--ny", type=int, default=320)
    parser.add_argument("--dam-height", type=float, default=60.0)
    parser.add_argument("--capacity-mcm", type=float, default=5.0)
    parser.add_argument("--end-hr", type=float, default=12.0)
    parser.add_argument(
        "--failure-mode",
        default="overtopping",
        choices=["overtopping", "piping", "gated_release", "blockage_breach"],
    )
    parser.add_argument("--seed", type=int, default=26161)
    args = parser.parse_args(argv)

    run_dir = generate_fake_run(
        run_id=args.run_id,
        outputs_dir=args.outputs,
        nx=args.nx,
        ny=args.ny,
        dam_height_m=args.dam_height,
        capacity_mcm=args.capacity_mcm,
        end_hr=args.end_hr,
        failure_mode=args.failure_mode,
        seed=args.seed,
    )
    print(f"wrote {run_dir}  (schema {SCHEMA_VERSION}, is_fake = true)")
    print(f"validate it:  python -m shared.validate {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
