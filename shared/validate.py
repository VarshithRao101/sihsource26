"""
shared/validate.py - the definition of done.

    python -m shared.validate outputs/<run_id>

A run that does not pass this does not exist. Do not hand it to another module,
do not put it in the demo, do not call it done at standup.

The validator is deliberately strict and deliberately noisy. Every check here
corresponds to a way one of us has broken, or could break, somebody else's
module. Errors fail the run. Warnings are things a juror might ask about.

Owner: captain / person 4.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from shared.contract import (
    BATHYMETRY,
    DELIVERY_CRS,
    DEM_SOURCES,
    ENGINES,
    FAILURE_MODES,
    HYDROGRAPH_COLUMNS,
    MASS_BALANCE_TOLERANCE_PCT,
    OPTIONAL_GRIDS,
    RASTER_DTYPE,
    REQUIRED_FILES,
    REQUIRED_GRIDS,
    REQUIRED_META_KEYS,
    RUN_ID_PATTERN,
    WET_THRESHOLD_M,
)
from shared.io import get_dotted


@dataclass
class Report:
    """Result of validating one run folder."""

    run_dir: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def fact(self, msg: str) -> None:
        self.facts.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self, verbose: bool = True) -> str:
        lines = [f"run folder: {self.run_dir}"]
        if verbose and self.facts:
            lines.append("")
            lines += [f"  {f}" for f in self.facts]
        if self.warnings:
            lines.append("")
            lines += [f"  WARN   {w}" for w in self.warnings]
        if self.errors:
            lines.append("")
            lines += [f"  ERROR  {e}" for e in self.errors]
        lines.append("")
        verdict = "PASS" if self.ok else "FAIL"
        lines.append(
            f"{verdict}  -  {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------


def validate_run(run_dir: str | Path, strict_optional: bool = False) -> Report:
    """Validate one run folder against the contract.

    Args:
        run_dir: path to outputs/<run_id>.
        strict_optional: also require impact.json and packed.png. Use this on
            demo runs, where a missing impact table is a real problem.

    Returns:
        Report. Check .ok.
    """
    run_dir = Path(run_dir)
    rep = Report(run_dir=run_dir)

    if not run_dir.exists():
        rep.error(f"{run_dir} does not exist")
        return rep
    if not run_dir.is_dir():
        rep.error(f"{run_dir} is not a directory")
        return rep

    # -- files present --------------------------------------------------
    for fname in REQUIRED_FILES:
        if not (run_dir / fname).exists():
            rep.error(f"missing required file: {fname}")
    if strict_optional:
        for fname in ("impact.json", "packed.png"):
            if not (run_dir / fname).exists():
                rep.error(f"missing (strict mode): {fname}")

    # -- meta.json ------------------------------------------------------
    meta = None
    if (run_dir / "meta.json").exists():
        try:
            with open(run_dir / "meta.json", "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except json.JSONDecodeError as exc:
            rep.error(f"meta.json is not valid JSON: {exc}")

    if meta is not None:
        _check_meta(meta, run_dir, rep)

    # -- rasters --------------------------------------------------------
    ref_profile = _check_rasters(run_dir, rep)

    # -- physical sanity ------------------------------------------------
    if ref_profile is not None:
        _check_physics(run_dir, rep, meta)

    # -- hydrograph -----------------------------------------------------
    if (run_dir / "hydrograph.csv").exists():
        _check_hydrograph(run_dir, rep, meta)

    # -- extent ---------------------------------------------------------
    if (run_dir / "extent.geojson").exists():
        _check_extent(run_dir, rep)

    # -- impact ---------------------------------------------------------
    if (run_dir / "impact.json").exists():
        _check_impact(run_dir, rep)

    return rep


# --------------------------------------------------------------------------


def _check_meta(meta: dict, run_dir: Path, rep: Report) -> None:
    for key in REQUIRED_META_KEYS:
        if get_dotted(meta, key, _MISSING) is _MISSING:
            rep.error(f"meta.json missing required key: {key}")

    run_id = meta.get("run_id", "")
    if run_id and not re.match(RUN_ID_PATTERN, run_id):
        rep.error(
            f"run_id {run_id!r} does not match {{site}}_{{scenario}}_{{engine}}_{{nnn}}"
        )
    if run_id and run_id != run_dir.name:
        rep.error(f"run_id {run_id!r} does not match folder name {run_dir.name!r}")

    engine = meta.get("engine")
    if engine is not None and engine not in ENGINES:
        rep.error(f"engine {engine!r} not in {ENGINES}")

    mode = get_dotted(meta, "scenario.failure_mode")
    if mode is not None and mode not in FAILURE_MODES:
        rep.error(f"scenario.failure_mode {mode!r} not in {FAILURE_MODES}")

    dem_source = get_dotted(meta, "dem.source")
    if dem_source is not None and dem_source not in DEM_SOURCES:
        rep.error(f"dem.source {dem_source!r} not in {DEM_SOURCES}")

    bathy = get_dotted(meta, "dem.bathymetry")
    if bathy is not None and bathy not in BATHYMETRY:
        rep.error(f"dem.bathymetry {bathy!r} not in {BATHYMETRY}")

    crs = get_dotted(meta, "domain.crs")
    if crs is not None and crs != DELIVERY_CRS:
        rep.error(f"domain.crs is {crs!r}; the contract delivers in {DELIVERY_CRS}")

    if not isinstance(meta.get("is_fake"), bool):
        rep.error("is_fake must be a JSON boolean, not a string or a number")
    elif meta["is_fake"]:
        rep.warn("is_fake = true - this run must never appear in the live demo")

    bbox = get_dotted(meta, "domain.bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = bbox
        if not (min_lon < max_lon and min_lat < max_lat):
            rep.error(f"domain.bbox is not (min_lon, min_lat, max_lon, max_lat): {bbox}")
        if not (-180 <= min_lon <= 180 and -90 <= min_lat <= 90):
            rep.error(f"domain.bbox is outside valid lon/lat range: {bbox}")
    elif bbox is not None:
        rep.error(f"domain.bbox must be a 4-element list, got {bbox!r}")

    lat = get_dotted(meta, "site.lat")
    lon = get_dotted(meta, "site.lon")
    if isinstance(lat, (int, float)) and not -90 <= lat <= 90:
        rep.error(f"site.lat {lat} is not a latitude")
    if isinstance(lon, (int, float)) and not -180 <= lon <= 180:
        rep.error(f"site.lon {lon} is not a longitude")

    t0 = get_dotted(meta, "time.start_hr")
    t1 = get_dotted(meta, "time.end_hr")
    if isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 <= t0:
        rep.error(f"time.end_hr ({t1}) must be greater than time.start_hr ({t0})")

    mb = get_dotted(meta, "results.mass_balance_err_pct")
    if mb is None:
        rep.warn(
            "results.mass_balance_err_pct is absent - report it even when it is bad"
        )
    elif abs(float(mb)) > MASS_BALANCE_TOLERANCE_PCT:
        rep.error(
            f"mass balance error {mb}% exceeds the {MASS_BALANCE_TOLERANCE_PCT}% tolerance"
        )
    else:
        rep.fact(f"mass balance error   {float(mb):+.3f} %")

    runtime = get_dotted(meta, "results.runtime_s")
    if isinstance(runtime, (int, float)):
        rep.fact(f"runtime              {float(runtime):.1f} s")
    if engine:
        rep.fact(f"engine               {engine}")

    for suspicious in ("accuracy", "accuracy_pct", "confidence", "r2"):
        if suspicious in (meta.get("results") or {}):
            rep.warn(
                f"results.{suspicious} present - only keep it if you actually "
                f"computed it against observed data"
            )


_MISSING = object()


def _check_rasters(run_dir: Path, rep: Report):
    """Every grid: float32, 1 band, EPSG:4326, NaN nodata, identical geometry."""
    from shared.io import raster_profile

    ref = None
    ref_name = None
    names = list(REQUIRED_GRIDS) + [
        g for g in OPTIONAL_GRIDS if (run_dir / f"{g}.tif").exists()
    ]

    for name in names:
        path = run_dir / f"{name}.tif"
        if not path.exists():
            continue
        try:
            prof = raster_profile(path)
        except Exception as exc:
            rep.error(f"{name}.tif could not be opened: {exc}")
            continue

        if prof["dtype"] != RASTER_DTYPE:
            rep.error(f"{name}.tif dtype is {prof['dtype']}, contract requires {RASTER_DTYPE}")
        if prof["count"] != 1:
            rep.error(f"{name}.tif has {prof['count']} bands, contract requires 1")
        if prof["crs"] != DELIVERY_CRS:
            rep.error(f"{name}.tif CRS is {prof['crs']}, contract requires {DELIVERY_CRS}")
        if prof["nodata"] is None or not (
            isinstance(prof["nodata"], float) and math.isnan(prof["nodata"])
        ):
            rep.error(f"{name}.tif nodata is {prof['nodata']}, contract requires NaN")
        if prof["transform"][4] >= 0:
            rep.error(f"{name}.tif is not north-up (row 0 must be the north edge)")

        if ref is None:
            ref, ref_name = prof, name
            rep.fact(f"grid                 {prof['width']} x {prof['height']} cells")
        else:
            if (prof["width"], prof["height"]) != (ref["width"], ref["height"]):
                rep.error(
                    f"{name}.tif is {prof['width']}x{prof['height']} but "
                    f"{ref_name}.tif is {ref['width']}x{ref['height']} - "
                    f"every grid in a run must share shape"
                )
            if any(abs(a - b) > 1e-9 for a, b in zip(prof["transform"], ref["transform"])):
                rep.error(f"{name}.tif transform differs from {ref_name}.tif")

    return ref


def _check_physics(run_dir: Path, rep: Report, meta: dict | None) -> None:
    """Values a hydraulic engineer would refuse to look at."""
    from shared.io import read_grid

    try:
        depth, grid = read_grid(run_dir, "max_depth")
        arrival, _ = read_grid(run_dir, "arrival_time")
        peak, _ = read_grid(run_dir, "time_of_peak")
        vel, _ = read_grid(run_dir, "max_velocity")
    except Exception as exc:
        rep.error(f"could not read grids for the physics check: {exc}")
        return

    if np.any(np.isnan(depth)):
        rep.error("max_depth contains NaN - dry cells must be 0.0, not NaN")
    if np.any(depth < 0):
        rep.error(f"max_depth has negative values (min {float(np.nanmin(depth)):.3f} m)")
    if np.any(vel < 0):
        rep.error("max_velocity has negative values - it is a speed, not a component")

    finite_arr = arrival[np.isfinite(arrival)]
    if finite_arr.size and finite_arr.min() < -1e-6:
        rep.error(f"arrival_time has values before t=0 (min {finite_arr.min():.3f} hr)")

    end_hr = get_dotted(meta or {}, "time.end_hr")
    if end_hr is not None and finite_arr.size and finite_arr.max() > float(end_hr) + 1e-6:
        rep.error(
            f"arrival_time max {finite_arr.max():.3f} hr exceeds time.end_hr {end_hr}"
        )

    wet = depth >= WET_THRESHOLD_M
    dry_but_arrived = (~wet) & np.isfinite(arrival)
    if dry_but_arrived.any():
        rep.error(
            f"{int(dry_but_arrived.sum())} cells have an arrival time but depth "
            f"below the {WET_THRESHOLD_M} m wet threshold"
        )
    wet_never_arrived = wet & ~np.isfinite(arrival)
    if wet_never_arrived.any():
        rep.error(
            f"{int(wet_never_arrived.sum())} wet cells have no arrival time - "
            f"arrival_time must be finite wherever max_depth >= {WET_THRESHOLD_M}"
        )

    both = np.isfinite(arrival) & np.isfinite(peak)
    if both.any() and np.any(peak[both] < arrival[both] - 1e-6):
        n = int(np.sum(peak[both] < arrival[both] - 1e-6))
        rep.error(f"{n} cells peak before the water arrives")

    if wet.any():
        area_km2 = float(wet.sum()) * grid.cell_area_m2() / 1e6
        rep.fact(f"flooded area         {area_km2:.2f} km2 ({int(wet.sum())} cells)")
        rep.fact(f"max depth            {float(depth.max()):.2f} m")
        rep.fact(f"max velocity         {float(vel.max()):.2f} m/s")
        if float(vel.max()) > 30.0:
            rep.warn(
                f"max_velocity {float(vel.max()):.1f} m/s is very high - real dam-break "
                f"fronts rarely exceed ~20 m/s. Check the wet/dry treatment."
            )
        if float(depth.max()) > 200.0:
            rep.warn(f"max_depth {float(depth.max()):.1f} m is implausible for a valley")
    else:
        rep.error("no cells are wet - the run produced no flood at all")


def _check_hydrograph(run_dir: Path, rep: Report, meta: dict | None) -> None:
    from shared.io import hydrograph_volume_m3, read_hydrograph

    try:
        t, q = read_hydrograph(run_dir)
    except Exception as exc:
        rep.error(f"hydrograph.csv: {exc}")
        return

    if t.size < 2:
        rep.error("hydrograph.csv has fewer than two samples")
        return
    if abs(t[0]) > 1e-9:
        rep.error(f"hydrograph.csv must start at time_hr = 0.0, got {t[0]}")
    if np.any(np.diff(t) <= 0):
        rep.error("hydrograph.csv time_hr is not strictly increasing")
    if np.any(q < 0):
        rep.error("hydrograph.csv has negative discharge")
    if np.all(q == 0):
        rep.error("hydrograph.csv is all zeros - no water was ever released")

    vol_mcm = hydrograph_volume_m3(t, q) / 1e6
    rep.fact(f"peak discharge       {float(q.max()):,.0f} m3/s")
    rep.fact(f"released volume      {vol_mcm:.2f} MCM")

    # What "the reservoir" means depends on the scenario. For a river blockage
    # the water is behind a landslide dam whose volume was read off the DEM,
    # and the engineered dam's registered capacity is the wrong yardstick
    # entirely - a 1.5 MCM barrage can sit under a 6 MCM landslide lake.
    #
    # And for failure_mode='river_flood' there is NO impounded volume of any
    # kind. Nothing is emptied: a wave enters the top of the reach and the
    # released volume is the integral of that wave, which is set by the peak
    # discharge and the duration the operator asked for. site.reservoir_capacity
    # _mcm is a placeholder there - SiteSpec.validate() only wants it positive -
    # so comparing against it rejected every river flood ever run, at any size,
    # as "creating water". The comparison is skipped rather than loosened,
    # because a loose threshold on a meaningless number is still meaningless.
    mode = get_dotted(meta or {}, "scenario.failure_mode")
    if mode == "river_flood":
        rep.fact(
            "released volume unchecked - river_flood impounds nothing, so "
            "there is no capacity to compare it against"
        )
        cap, cap_label = None, ""
    else:
        # Order matters, and it is the order of what the run ACTUALLY emptied.
        #
        # A moraine-dammed lake that an operator measured off imagery overrides
        # the DEM, because a 30 m DEM routinely cannot see the basin at all -
        # runner.py says so at length and publishes both numbers. Checking the
        # release against the DEM figure the run deliberately did not use
        # rejected South Lhonak for emptying 67.9 MCM out of a lake it had
        # correctly recorded as 68.9, while pointing at the 0.34 MCM the
        # terrain holds. That is the validator marking a run wrong for doing
        # exactly what its own meta.json documents.
        cap = get_dotted(meta or {}, "glof_moraine.lake_volume_m3")
        cap_label = "moraine lake volume this run released from"
        if isinstance(cap, (int, float)) and cap > 0:
            cap = cap / 1e6
        else:
            cap = get_dotted(meta or {}, "blockage.impounded_volume_mcm")
            cap_label = "impounded landslide lake"
        if not (isinstance(cap, (int, float)) and cap > 0):
            cap = get_dotted(meta or {}, "site.reservoir_capacity_mcm")
            cap_label = "stated reservoir capacity"

    # A reservoir with an inflow arriving is a CONDUIT, not a bathtub, and
    # comparing what left it against what it holds is then simply the wrong
    # sum. Machchhu II is the case that proves it: 600 mm in 24 hours drove an
    # inflow of 16,300 m3/s, which over the twelve hours simulated delivers
    # 704 MCM through a reservoir that holds 100.55. The run released 766.5 MCM
    # and was rejected as "creating water" when in fact it had conserved mass
    # to 0.000% - the flagship demo of this repository, failing its own
    # validator on arithmetic rather than on physics.
    #
    # So the yardstick is what was AVAILABLE to release: the stored volume plus
    # everything the inflow delivered while the run lasted. The 1.5x margin is
    # unchanged and still catches genuine mass creation, which is what it is
    # for. Where there is no inflow the two forms are identical, so no existing
    # run's verdict moves except the ones that were wrong.
    inflow = get_dotted(meta or {}, "scenario.inflow_cumecs")
    end_hr = get_dotted(meta or {}, "time.end_hr")
    inflow_mcm = 0.0
    if isinstance(inflow, (int, float)) and isinstance(end_hr, (int, float)):
        inflow_mcm = max(0.0, float(inflow)) * float(end_hr) * 3600.0 / 1e6

    if isinstance(cap, (int, float)) and cap > 0:
        available = cap + inflow_mcm
        if inflow_mcm > 0:
            rep.fact(
                f"available to release  {available:,.2f} MCM "
                f"({cap:,.2f} stored + {inflow_mcm:,.2f} from inflow)"
            )
        if vol_mcm > 1.5 * available:
            supply = (
                f"the {cap_label} {cap} MCM plus {inflow_mcm:,.1f} MCM of "
                f"inflow over {end_hr} hours"
                if inflow_mcm > 0
                else f"the {cap_label} {cap} MCM"
            )
            rep.error(
                f"released volume {vol_mcm:.1f} MCM exceeds 1.5x {supply} "
                f"- the routing is creating water"
            )

    reported_peak = get_dotted(meta or {}, "results.peak_discharge_cumecs")
    if isinstance(reported_peak, (int, float)) and reported_peak > 0:
        rel = abs(reported_peak - float(q.max())) / float(q.max())
        if rel > 0.02:
            rep.error(
                f"meta results.peak_discharge_cumecs is {reported_peak:,.0f} but the "
                f"hydrograph peaks at {float(q.max()):,.0f}"
            )


def _check_extent(run_dir: Path, rep: Report) -> None:
    try:
        with open(run_dir / "extent.geojson", "r", encoding="utf-8") as fh:
            fc = json.load(fh)
    except json.JSONDecodeError as exc:
        rep.error(f"extent.geojson is not valid JSON: {exc}")
        return

    if fc.get("type") != "FeatureCollection":
        rep.error("extent.geojson must be a FeatureCollection")
        return
    feats = fc.get("features", [])
    if not feats:
        rep.error("extent.geojson has no features - nothing flooded")
        return

    total = 0.0
    for i, feat in enumerate(feats):
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            rep.error(f"extent.geojson feature {i} is a {geom.get('type')}, not a polygon")
        props = feat.get("properties") or {}
        if "area_km2" not in props:
            rep.error(f"extent.geojson feature {i} has no area_km2 property")
        else:
            total += float(props["area_km2"])
    rep.fact(f"extent polygons      {len(feats)} ({total:.2f} km2 total)")


def _check_impact(run_dir: Path, rep: Report) -> None:
    try:
        with open(run_dir / "impact.json", "r", encoding="utf-8") as fh:
            impact = json.load(fh)
    except json.JSONDecodeError as exc:
        rep.error(f"impact.json is not valid JSON: {exc}")
        return

    totals = impact.get("totals")
    if not isinstance(totals, dict):
        rep.error("impact.json needs a 'totals' object")
        return

    for key in ("settlements_affected", "population_affected"):
        if key not in totals:
            rep.error(f"impact.json totals missing {key}")

    if "damage_inr_crore" in totals and "damage_curve_source" not in totals:
        rep.error(
            "impact.json reports damage_inr_crore with no damage_curve_source - "
            "every money figure carries its curve citation"
        )

    settlements = impact.get("settlements", [])
    for i, s in enumerate(settlements):
        if not s.get("name"):
            rep.error(f"impact.json settlement {i} has no name - never ship an unnamed place")
        for key in ("lat", "lon", "population", "arrival_hr", "max_depth_m"):
            if key not in s:
                rep.error(f"impact.json settlement {s.get('name', i)!r} missing {key}")

    if settlements:
        rep.fact(
            f"settlements affected {len(settlements)} "
            f"({totals.get('population_affected', '?')} people)"
        )
        n_named = sum(1 for s in settlements if s.get("name"))
        if n_named != len(settlements):
            rep.error("some settlements are unnamed")


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.validate",
        description="Validate a run folder against the SIH26161 data contract.",
    )
    parser.add_argument("run_dir", nargs="+", help="one or more outputs/<run_id> folders")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also require impact.json and packed.png (use on demo runs)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="verdict lines only")
    args = parser.parse_args(argv)

    failed = 0
    for i, run_dir in enumerate(args.run_dir):
        rep = validate_run(run_dir, strict_optional=args.strict)
        if i:
            print()
        print(rep.render(verbose=not args.quiet))
        if not rep.ok:
            failed += 1

    if len(args.run_dir) > 1:
        print(f"\n{len(args.run_dir) - failed}/{len(args.run_dir)} run(s) passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
