"""
modules/04_backend/runner.py - scenario in, contract-valid run folder out.

This is the one function the API calls, the CLI calls, the integration test
calls and the ML training loop calls. There is exactly one code path from a
scenario to a run folder, so there is exactly one place a bug can hide.

    scenario  ->  terrain  ->  breach params  ->  hydrograph  ->  solver
              ->  grids + extent + packed.png + meta.json  ->  validator

Owner: person 4 / captain.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from shared.contract import WET_THRESHOLD_M, hazard_class
from shared.geo import Grid, bbox_downstream
from shared.hydro import (
    BreachParams,
    breach_hydrograph,
    breach_parameter_ensemble,
    peak_outflow_regressions,
)
from shared.io import (
    build_meta,
    hydrograph_volume_m3,
    make_run_id,
    next_sequence,
    write_extent,
    write_grid,
    write_hydrograph,
    write_json,
    write_meta,
    write_packed_png,
)

# Module folders start with a digit, so a plain `import` statement cannot name
# them. importlib can, and so can a relative import from inside the package.
from .scenario import ScenarioSpec
from .solver import SolverConfig, SolverResult, run_solver


# ==========================================================================
# Terrain
# ==========================================================================


class TerrainProvider(Protocol):
    """What the solver needs from module 01.

    Module 01 (geodata) implements this against real DEMs. Until it does - and
    for every test forever - SyntheticTerrain implements it against a generated
    valley. The solver cannot tell the difference, which is the point.
    """

    def get_terrain(
        self, bbox: tuple[float, float, float, float], cellsize_m: float
    ) -> tuple[np.ndarray, np.ndarray, Grid]:
        """Returns (dem_m, manning_n, grid). DEM may contain NaN for no-data."""
        ...

    @property
    def source(self) -> str:
        """One of shared.contract.DEM_SOURCES."""
        ...


class SyntheticTerrain:
    """A generated V-valley. Honest about what it is: dem_source = SYNTHETIC,
    which the frontend renders with a banner and the demo refuses to ship."""

    source = "SYNTHETIC"

    def __init__(self, seed: int = 26161):
        self.seed = seed

    def get_terrain(self, bbox, cellsize_m):
        from shared.fake import synthetic_valley

        grid = Grid.from_bbox_cellsize(bbox, cellsize_m)
        dem = synthetic_valley(grid, seed=self.seed)
        manning = np.full(grid.shape, 0.035, dtype=np.float32)
        # Channel cells get a lower n than the valley walls, which is what a
        # real Manning raster from land cover looks like.
        channel = dem < (np.percentile(dem, 20))
        manning[channel] = 0.030
        return dem.astype(np.float64), manning.astype(np.float64), grid


# ==========================================================================
# The run
# ==========================================================================


def _dem_meta(terrain, grid: Grid) -> dict:
    """The dem block for meta.json.

    A provider that knows how it conditioned the terrain says so itself -
    module 01's RealTerrain reports its pit-filling and bed estimation. Anything
    simpler gets an honest default. Never claim conditioning that did not happen,
    and never claim "none" for conditioning that did.
    """
    if hasattr(terrain, "dem_meta"):
        block = dict(terrain.dem_meta(grid))
        block.setdefault("native_resolution_m", round(grid.cellsize_m(), 1))
        return block
    return {
        "source": terrain.source,
        "native_resolution_m": round(grid.cellsize_m(), 1),
        "bathymetry": "none",
        "conditioning": "none",
    }


def resolve_breach(spec: ScenarioSpec) -> tuple[BreachParams, dict[str, BreachParams]]:
    """Breach geometry for this scenario, plus the full three-regression ensemble.

    The ensemble is always computed even though only one drives the run - it is
    what the uncertainty band in modules/07_ml is built from, and it costs
    microseconds.
    """
    ensemble = breach_parameter_ensemble(
        spec.water_volume_m3, spec.site.dam_height_m, spec.failure_mode
    )
    chosen = ensemble.get(spec.breach_regression) or ensemble["froehlich2008"]

    # A human override replaces the regression but is recorded as an override,
    # so nobody later mistakes a typed-in number for a computed one.
    if spec.breach_width_m is not None or spec.formation_time_hr is not None:
        chosen = BreachParams(
            bottom_width_m=(
                max(
                    spec.breach_width_m
                    - chosen.side_slope_h_per_v * spec.site.dam_height_m,
                    0.1 * spec.breach_width_m,
                )
                if spec.breach_width_m is not None
                else chosen.bottom_width_m
            ),
            average_width_m=(
                spec.breach_width_m
                if spec.breach_width_m is not None
                else chosen.average_width_m
            ),
            side_slope_h_per_v=chosen.side_slope_h_per_v,
            depth_m=chosen.depth_m,
            formation_time_hr=(
                spec.formation_time_hr
                if spec.formation_time_hr is not None
                else chosen.formation_time_hr
            ),
            source=chosen.source + " (user override)",
        )
    return chosen, ensemble


def find_inflow_cells(
    grid: Grid, dem: np.ndarray, lat: float, lon: float, width_m: float
) -> list[tuple[int, int]]:
    """The cells the breach outflow is injected into.

    Takes the row containing the dam, walks `width_m` either side of the dam
    column, and keeps the cells closest to the channel bed. Injecting into a
    valley wall instead of the channel is a classic way to get a flood that
    goes nowhere, so we snap to the lowest cells rather than trusting the
    dam coordinate to be pixel-accurate on a 90 m grid.
    """
    row, col = grid.rowcol(lon, lat)
    half = max(int(round(0.5 * width_m / grid.cellsize_m())), 1)

    lo = max(col - max(half, 3), 0)
    hi = min(col + max(half, 3) + 1, grid.nx)
    band = dem[row, lo:hi]

    if not np.isfinite(band).any():
        return [(row, col)]

    # Take the `2*half+1` lowest cells in the band - the channel.
    n_take = min(2 * half + 1, hi - lo)
    order = np.argsort(np.where(np.isfinite(band), band, np.inf))
    return [(row, int(lo + k)) for k in order[:n_take]]


def run_scenario(
    spec: ScenarioSpec,
    outputs_dir: str | Path = "outputs",
    terrain: TerrainProvider | None = None,
    run_id: str | None = None,
    keep_frames: bool = False,
    write_png: bool = True,
    progress: Callable[[dict], None] | None = None,
    exposure: dict | None = None,
) -> Path:
    """Run one scenario end to end and write a contract-valid run folder.

    Args:
        spec: the scenario. Validated first - a bad scenario raises before any
            expensive work happens.
        outputs_dir: parent of the run folder.
        terrain: module 01's provider, or SyntheticTerrain if None.
        run_id: override the generated {site}_{scenario}_{engine}_{nnn}.
        keep_frames: also write frames/depth_XXXX.tif - true solver output, as
            opposed to the packed.png reconstruction.
        write_png: write the browser texture.
        progress: forwarded to the solver; this is what the WebSocket streams.
        exposure: optional settlements/roads from module 01, used to build
            impact.json. Shape: {"settlements": [{name, lat, lon, population}]}.

    Returns:
        Path to the run folder.

    Raises:
        ValueError: if the scenario is invalid.
        RuntimeError: if the solve goes unstable.
    """
    spec.require_valid()
    t_wall = time.perf_counter()

    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    terrain = terrain or SyntheticTerrain()

    if run_id is None:
        seq = next_sequence(
            outputs_dir, spec.site_slug, spec.scenario_slug, spec.engine
        )
        run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)
    run_dir = outputs_dir / run_id

    # ---- 1. terrain ----------------------------------------------------
    bbox = spec.bbox
    dem, manning, grid = terrain.get_terrain(bbox, spec.cellsize_m)

    # ---- 2. breach and hydrograph --------------------------------------
    breach, ensemble = resolve_breach(spec)

    # ---- river blockage: a natural dam, not an engineered one ----------
    # The problem statement asks for blockage as well as dam break, and it is
    # a different problem: nobody published the storage, so we read it off the
    # DEM, and the debris breaches by different regressions than a compacted
    # embankment. Everything downstream of here is identical - a blockage
    # flood routes like any other flood, because it is one.
    blockage_block = None
    effective_capacity_m3 = spec.capacity_m3
    if spec.failure_mode == "blockage_breach":
        from .blockage import prepare_blockage

        acc = None
        if hasattr(terrain, "last_products") and terrain.last_products:
            acc = terrain.last_products.get("accumulation")

        breach, blockage = prepare_blockage(
            dem=dem,
            grid=grid,
            lat=spec.site.lat,
            lon=spec.site.lon,
            blockage_height_m=spec.blockage_height_m,
            accumulation=acc,
            inflow_cumecs=spec.inflow_cumecs or None,
        )
        blockage_block = blockage.as_dict()
        effective_capacity_m3 = blockage.impounded_volume_mcm * 1e6

    t_hr, q_cumecs = breach_hydrograph(
        breach,
        dam_height_m=(
            spec.blockage_height_m
            if spec.failure_mode == "blockage_breach"
            else spec.site.dam_height_m
        ),
        capacity_m3=effective_capacity_m3,
        reservoir_level_frac=(
            1.0 if spec.failure_mode == "blockage_breach" else spec.reservoir_level_frac
        ),
        failure_mode=spec.failure_mode,
        inflow_cumecs=spec.inflow_cumecs,
        duration_hr=spec.end_hr,
        output_step_hr=min(spec.output_step_hr, 0.05),
        storage_exponent=spec.storage_exponent,
    )

    # ---- 3. solve ------------------------------------------------------
    inflow_cells = find_inflow_cells(
        grid, dem, spec.site.lat, spec.site.lon, breach.average_width_m
    )
    config = SolverConfig(
        dx_m=grid.cellsize_m(),
        end_hr=spec.end_hr,
        scheme=spec.scheme,  # type: ignore[arg-type]
        manning_n=spec.manning_n,
        output_step_hr=spec.output_step_hr,
        keep_frames=keep_frames,
    )
    result = run_solver(
        dem,
        config,
        inflow_hydrograph=(t_hr, q_cumecs),
        inflow_cells=inflow_cells,
        manning_grid=manning,
        progress=progress,
    )

    # ---- 4. write the contract files -----------------------------------
    write_grid(run_dir, "max_depth", result.max_depth, grid, "maximum water depth")
    write_grid(run_dir, "arrival_time", result.arrival_time, grid, "first wetting")
    write_grid(run_dir, "time_of_peak", result.time_of_peak, grid, "time of max depth")
    write_grid(run_dir, "max_velocity", result.max_velocity, grid, "max depth-avg speed")
    write_grid(run_dir, "duration", result.duration, grid, "hours wet")

    dv = (result.max_depth * result.max_velocity).astype(np.float32)
    write_grid(run_dir, "max_dv", dv, grid, "depth x velocity hazard product")

    write_hydrograph(run_dir, t_hr, q_cumecs)
    write_extent(run_dir, result.max_depth, grid)

    packed_max = None
    if write_png:
        _, packed_max = write_packed_png(
            run_dir,
            result.arrival_time,
            result.time_of_peak,
            result.max_depth,
            result.duration,
            spec.end_hr,
        )

    if keep_frames and result.frames:
        frames_dir = run_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        for k, frame in enumerate(result.frames):
            write_grid(frames_dir, f"depth_{k:04d}", frame, grid, "depth")
        write_json(
            run_dir,
            "frames_index.json",
            {
                "count": len(result.frames),
                "times_hr": [round(t, 4) for t in result.frame_times_hr],
                "note": "true solver output, not the packed.png reconstruction",
            },
        )

    if exposure:
        write_json(run_dir, "impact.json", build_impact(run_id, grid, result, exposure))

        # Evacuation routing, when module 01 gave us road geometry. Soft import
        # for the same reason as the damage model: a missing route plan is a
        # gap in the output, a crash here would be a lost run.
        if exposure.get("roads"):
            try:
                from importlib import import_module

                _ev = import_module("modules.07_ml.evacuation")
                _ev.plan_evacuation(run_dir, exposure)
            except Exception:
                pass

    # ---- 5. the uncertainty block (schema 2.0) --------------------------
    write_json(run_dir, "uncertainty.json", build_uncertainty(spec, ensemble, q_cumecs))

    # ---- 6. meta.json --------------------------------------------------
    wet = result.max_depth >= WET_THRESHOLD_M
    results_block = {
        "peak_discharge_cumecs": round(float(q_cumecs.max()), 1),
        "max_depth_m": round(float(result.max_depth.max()), 2),
        "max_velocity_ms": round(float(result.max_velocity.max()), 2),
        "flood_area_km2": round(float(wet.sum()) * grid.cell_area_m2() / 1e6, 2),
        "released_volume_mcm": round(hydrograph_volume_m3(t_hr, q_cumecs) / 1e6, 3),
        "runtime_s": round(result.runtime_s, 2),
        "wall_time_s": round(time.perf_counter() - t_wall, 2),
        "mass_balance_err_pct": round(result.mass_balance_err_pct, 4),
        "solver_steps": result.n_steps,
        "min_timestep_s": round(result.min_dt_s, 5),
        "scheme": result.scheme,
        # The full volume ledger, not just the error. If mass is going missing
        # these three numbers say where: into the domain, out of the boundary,
        # or still standing on the ground at the end.
        "volume_in_mcm": round(result.volume_in_m3 / 1e6, 4),
        "volume_out_mcm": round(result.volume_out_m3 / 1e6, 4),
        "volume_stored_mcm": round(result.volume_stored_m3 / 1e6, 4),
        "froude_limited_cells": int(result.froude_limited_cells),
    }
    if packed_max is not None:
        results_block["packed_depth_max_m"] = round(packed_max, 3)

    meta = build_meta(
        run_id=run_id,
        engine=spec.engine,
        grid=grid,
        site={
            "name": spec.site.name,
            "river": spec.site.river,
            "state": spec.site.state,
            "lat": spec.site.lat,
            "lon": spec.site.lon,
            "dam_height_m": spec.site.dam_height_m,
            "reservoir_capacity_mcm": spec.site.reservoir_capacity_mcm,
            "source": spec.site.source,
        },
        scenario=spec.to_meta_scenario(breach),
        time_block={
            "start_hr": 0.0,
            "end_hr": spec.end_hr,
            "output_step_hr": spec.output_step_hr,
        },
        results=results_block,
        module="04_backend",
        dem=_dem_meta(terrain, grid),
        is_fake=(terrain.source == "SYNTHETIC"),
        notes=spec.notes,
    )
    if blockage_block is not None:
        meta["blockage"] = blockage_block
    meta["domain"]["reach_length_km"] = spec.reach_length_km
    write_meta(run_dir, meta)

    return run_dir


# ==========================================================================
# Derived products
# ==========================================================================


def build_uncertainty(spec: ScenarioSpec, ensemble: dict, q_cumecs: np.ndarray) -> dict:
    """The honesty block. Schema 2.0.

    Three breach regressions and four peak-outflow regressions, all shown, none
    averaged into a single confident number. The spread IS the answer. Module
    07's Monte Carlo widens this with a proper parameter sweep; this is the
    zero-cost version that every run carries.
    """
    peaks = peak_outflow_regressions(spec.water_volume_m3, spec.site.dam_height_m)
    widths = {k: round(v.average_width_m, 1) for k, v in ensemble.items()}
    times = {k: round(v.formation_time_hr, 4) for k, v in ensemble.items()}

    peak_vals = list(peaks.values())
    return {
        "note": (
            "Breach parameters carry roughly a factor-of-two uncertainty on peak "
            "flow (Froehlich 2008 reports the scatter explicitly). These are the "
            "independent estimates, unaveraged. Our routed peak is one point "
            "inside this envelope, not the truth."
        ),
        "routed_peak_cumecs": round(float(q_cumecs.max()), 1),
        "breach_width_m_by_regression": widths,
        "formation_time_hr_by_regression": times,
        "peak_discharge_cumecs_by_regression": {
            k: round(v, 1) for k, v in peaks.items()
        },
        "peak_envelope_cumecs": [round(min(peak_vals), 1), round(max(peak_vals), 1)],
        "envelope_ratio": round(max(peak_vals) / max(min(peak_vals), 1e-9), 2),
        "dem_vertical_uncertainty_m": {
            "FABDEM": 1.4,
            "COP30": 1.7,
            "SRTM": 6.0,
            "note": (
                "RMSE figures from Hawker et al. (2022), Environmental Research "
                "Letters 17(2), 024016 for FABDEM/COP30, and the SRTM mission "
                "specification for SRTM. Bathymetry is not measured at all."
            ),
        },
    }


SETTLEMENT_RADIUS_M = 250.0
"""Half-width of the footprint sampled around a settlement node, metres.

250 m is about the radius of a small Indian village and is under three cells at
90 m posting. It is an assumption, it is reported in impact.json as
settlement_sample_radius_m, and it is deliberately not tuned per site."""


def build_impact(
    run_id: str, grid: Grid, result: SolverResult, exposure: dict
) -> dict:
    """Sample the flood grids at real settlement locations.

    `exposure` comes from module 01 and holds REAL places from OSM or the
    Census. This function never invents a name, a coordinate or a population -
    it only reads the flood grids at points somebody else established.
    """
    settlements_out = []
    total_pop = 0

    for s in exposure.get("settlements", []):
        lat, lon = float(s["lat"]), float(s["lon"])
        if not grid.contains(lon, lat):
            continue
        r, c = grid.rowcol(lon, lat)

        # A settlement is an area, not a pixel. OSM gives us one node at the
        # nominal centre of a village that may be several hundred metres
        # across, and the node routinely sits on the road or the temple rather
        # than on the bank. Testing that single cell against a 90 m grid makes
        # the answer a coin-flip: Chungthang came out UNAFFECTED while lying
        # 130 m - one and a half cells - from 8 m of water.
        #
        # So we sample a footprint and take its worst cell. The radius is
        # recorded in impact.json; it is an assumption and it is declared.
        rad = max(int(SETTLEMENT_RADIUS_M / grid.cellsize_m()), 1)
        r_lo, r_hi = max(r - rad, 0), min(r + rad + 1, grid.ny)
        c_lo, c_hi = max(c - rad, 0), min(c + rad + 1, grid.nx)

        depth_win = result.max_depth[r_lo:r_hi, c_lo:c_hi]
        if depth_win.size == 0:
            continue
        local = int(np.argmax(depth_win))
        dr, dc = local // depth_win.shape[1], local % depth_win.shape[1]
        depth = float(depth_win[dr, dc])
        if depth < WET_THRESHOLD_M:
            continue

        rw, cw = r_lo + dr, c_lo + dc
        vel = float(result.max_velocity[rw, cw])
        arr = float(result.arrival_time[rw, cw])
        pop = int(s.get("population", 0) or 0)
        total_pop += pop
        settlements_out.append(
            {
                "name": s["name"],
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "population": pop,
                "arrival_hr": round(arr, 3) if np.isfinite(arr) else None,
                "max_depth_m": round(depth, 2),
                "max_velocity_ms": round(vel, 2),
                "hazard_class": hazard_class(depth, vel),
            }
        )

    settlements_out.sort(key=lambda s: (s["arrival_hr"] is None, s["arrival_hr"]))

    # --- roads cut, from module 01's OSM road geometry ------------------
    roads_out: list[dict] = []
    if exposure.get("roads"):
        try:
            from importlib import import_module

            _ex = import_module("modules.01_geodata.exposure")
            roads_out = _ex.roads_cut(exposure["roads"], result.max_depth, grid)
        except Exception:
            roads_out = []
    roads_cut_km = round(sum(r["length_cut_km"] for r in roads_out), 2)

    # --- money, from module 07's depth-damage curves --------------------
    # Imported softly: module 04 must still produce a valid run folder if the
    # ML layer is absent. A missing damage figure is a gap; a broken run is a
    # failure. The contract makes damage optional for exactly this reason.
    damage_block: dict = {}
    try:
        from importlib import import_module

        _dm = import_module("modules.07_ml.damage")
        dmg = _dm.total_damage(settlements_out, roads_cut_km=roads_cut_km)
        settlements_out = dmg["settlements"]
        damage_block = {
            "damage_inr_crore": dmg["damage_inr_crore"],
            "damage_breakdown_inr_crore": dmg["damage_breakdown_inr_crore"],
            "houses_affected": dmg["houses_affected"],
            "damage_curve_source": dmg["damage_curve_source"],
        }
    except Exception:
        damage_block = {}

    wet = result.max_depth >= WET_THRESHOLD_M
    return {
        "run_id": run_id,
        "totals": {
            "settlements_affected": len(settlements_out),
            "population_affected": total_pop,
            "roads_cut_km": roads_cut_km,
            "settlement_sample_radius_m": SETTLEMENT_RADIUS_M,
            **damage_block,
            "flood_area_km2": round(float(wet.sum()) * grid.cell_area_m2() / 1e6, 2),
        },
        "settlements": settlements_out,
        "roads": roads_out,
        "infrastructure": [],
        "note": (
            "Population and place names are read from the exposure layer produced "
            "by module 01. Depths, velocities and arrival times are sampled from "
            "the solver grids at those points. Nothing here is estimated by hand."
        ),
    }
