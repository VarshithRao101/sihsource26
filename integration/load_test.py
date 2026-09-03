"""
integration/load_test.py - how much data can this thing actually take?

The problem statement says the program "should support the large volume of
data". Until now our answer was "the largest run so far was 437 x 343 cells",
which is not an answer to that question - it is a note about what we happened
to try. This finds the ceiling and the shape of the curve up to it, so the
answer becomes a number somebody measured.

    python integration/load_test.py                  # the standard sweep
    python integration/load_test.py --max-cells 4000000
    python integration/load_test.py --quick          # three small sizes

What it measures, per grid size, on the synthetic valley so no download or
network is involved and the only variable is size:

    build_s        making the terrain
    solve_s        the shallow-water solve itself
    write_s        writing the five GeoTIFFs, extent, hydrograph and texture
    peak_rss_mb    high-water memory of this process
    cells_per_s    solver throughput - the number that says whether the
                   scaling is linear or falling over
    packed_kb      size of the browser texture, which is what the dashboard
                   actually has to load

Every row is a REAL run through runner.run_scenario - the same code path the
API uses - and every row is validated against the contract before it counts.
A size that solves but fails the validator is reported as a failure, because a
run that does not validate does not exist.

Results land in docs/LOAD_TEST.md and outputs/.loadtest/ so the number in the
requirement mapping has something behind it.

Owner: captain (integration).
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import time
import tracemalloc
from dataclasses import dataclass, asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "outputs" / ".loadtest"
REPORT = REPO / "docs" / "LOAD_TEST.md"

# Square-ish grids from small to large. The reach length is what drives cell
# count for a fixed cell size, so the sweep varies the reach and holds the cell
# size at the 90 m we validate against.
DEFAULT_SIZES_KM = [10, 20, 40, 80, 120, 180, 250, 350]
QUICK_SIZES_KM = [10, 20, 40]


@dataclass
class Row:
    reach_km: float
    cellsize_m: float
    nx: int
    ny: int
    cells: int
    end_hr: float
    build_s: float
    solve_s: float
    write_s: float
    total_s: float
    peak_rss_mb: float
    cells_per_s: float
    steps: int
    packed_kb: float
    run_bytes: int
    mass_err_pct: float
    validates: bool
    error: str = ""


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def one(reach_km: float, cellsize_m: float, end_hr: float, keep: bool) -> Row:
    """Run one size end to end and time every phase of it."""
    # Module folders start with a digit, so a plain import statement cannot
    # name them. importlib can.
    from importlib import import_module

    from shared.io import read_meta
    from shared.validate import validate_run

    _runner = import_module("modules.04_backend.runner")
    _scen = import_module("modules.04_backend.scenario")
    SyntheticTerrain, run_scenario = _runner.SyntheticTerrain, _runner.run_scenario
    ScenarioSpec, SiteSpec = _scen.ScenarioSpec, _scen.SiteSpec

    from shared.io import make_run_id

    # The size goes in the SITE name, not the run id: run ids have a contract
    # shape ({site}_{scenario}_{engine}_{nnn}) and the validator enforces it.
    site = SiteSpec(
        name=f"LoadTest{int(reach_km)}km",
        lat=27.6,
        lon=88.65,
        dam_height_m=60.0,
        reservoir_capacity_mcm=50.0,
        source="synthetic - load test",
    )
    spec = ScenarioSpec(
        site=site,
        failure_mode="overtopping",
        reach_length_km=reach_km,
        cellsize_m=cellsize_m,
        end_hr=end_hr,
    )
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, 1)
    run_dir = OUT_DIR / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)

    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()

    phases: dict[str, float] = {}

    def progress(u: dict) -> None:
        # runner announces its stages; use them to split build from solve.
        node = u.get("node")
        if node and node not in phases:
            phases[node] = time.perf_counter()

    terrain = SyntheticTerrain()
    t_build0 = time.perf_counter()
    try:
        run_scenario(
            spec,
            outputs_dir=OUT_DIR,
            terrain=terrain,
            run_id=run_id,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 - a failure at size IS the result
        peak = tracemalloc.get_traced_memory()[1] / 1e6
        tracemalloc.stop()
        return Row(
            reach_km=reach_km, cellsize_m=cellsize_m, nx=0, ny=0, cells=0,
            end_hr=end_hr, build_s=0, solve_s=0, write_s=0,
            total_s=round(time.perf_counter() - t0, 2), peak_rss_mb=round(peak, 1),
            cells_per_s=0, steps=0, packed_kb=0, run_bytes=0, mass_err_pct=0,
            validates=False, error=f"{type(exc).__name__}: {exc}",
        )

    total_s = time.perf_counter() - t0
    peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()

    meta = read_meta(run_dir)
    dom, res = meta["domain"], meta["results"]
    nx, ny = int(dom["nx"]), int(dom["ny"])
    cells = nx * ny

    solve_s = float(res.get("runtime_s", 0.0))
    build_s = max(phases.get("solve", t_build0) - t_build0, 0.0)
    write_s = max(total_s - build_s - solve_s, 0.0)
    steps = int(res.get("solver_steps", 0))

    rep = validate_run(run_dir)
    packed = run_dir / "packed.png"
    row = Row(
        reach_km=reach_km,
        cellsize_m=cellsize_m,
        nx=nx, ny=ny, cells=cells,
        end_hr=end_hr,
        build_s=round(build_s, 2),
        solve_s=round(solve_s, 2),
        write_s=round(write_s, 2),
        total_s=round(total_s, 2),
        peak_rss_mb=round(peak_mb, 1),
        # Cell-updates per second: cells x timesteps / solve seconds. This is
        # the throughput figure that says whether we scale or fall over.
        cells_per_s=round(cells * steps / max(solve_s, 1e-9), 0),
        steps=steps,
        packed_kb=round(packed.stat().st_size / 1024, 1) if packed.exists() else 0.0,
        run_bytes=_dir_bytes(run_dir),
        mass_err_pct=round(float(res.get("mass_balance_err_pct", 0.0)), 4),
        validates=bool(rep.ok),
        error="" if rep.ok else "; ".join(rep.errors)[:200],
    )
    if not keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    return row


def report(rows: list[Row], cellsize_m: float, end_hr: float) -> str:
    ok = [r for r in rows if r.validates]
    biggest = max(ok, key=lambda r: r.cells) if ok else None

    lines = [
        "# Load test — how much data this actually takes",
        "",
        "Generated by `python integration/load_test.py`. Every row is a real run",
        "through `runner.run_scenario`, the same path the API uses, on the",
        "synthetic valley so that size is the only variable and nothing is",
        "downloaded. A row only counts as a pass if `shared.validate` accepts the",
        "run folder it produced.",
        "",
        f"Cell size **{cellsize_m:g} m**, simulated duration **{end_hr:g} h**.",
        "",
        "| reach km | grid | cells | solve s | write s | total s | peak MB | cell-updates/s | packed.png KB | run folder | mass err % | ok |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        grid = f"{r.nx}×{r.ny}" if r.cells else "—"
        lines.append(
            f"| {r.reach_km:g} | {grid} | {r.cells:,} | {r.solve_s:g} | {r.write_s:g} "
            f"| {r.total_s:g} | {r.peak_rss_mb:g} | {r.cells_per_s:,.0f} "
            f"| {r.packed_kb:g} | {r.run_bytes / 1e6:.1f} MB | {r.mass_err_pct:g} "
            f"| {'yes' if r.validates else 'NO — ' + r.error} |"
        )

    lines += ["", "## What this says", ""]
    if biggest:
        lines += [
            f"- Largest grid that ran and validated: **{biggest.nx}×{biggest.ny} = "
            f"{biggest.cells:,} cells**, {biggest.total_s:g} s end to end, "
            f"{biggest.peak_rss_mb:g} MB peak.",
            f"- The browser texture for it is **{biggest.packed_kb:g} KB**, which is "
            f"what the dashboard downloads to draw the whole time-varying flood.",
        ]
        thr = [r.cells_per_s for r in ok if r.cells_per_s > 0]
        if len(thr) >= 2:
            lines.append(
                f"- Solver throughput across the sweep: "
                f"{min(thr):,.0f} to {max(thr):,.0f} cell-updates per second. "
                "Flat throughput means the cost is linear in cells; a fall at the "
                "top means we are out of cache or memory."
            )
    failed = [r for r in rows if not r.validates]
    if failed:
        lines.append(
            f"- **Ceiling found.** {len(failed)} size(s) did not produce a valid run: "
            + "; ".join(f"{r.reach_km:g} km — {r.error}" for r in failed)
        )
    else:
        lines.append(
            "- **No ceiling was reached in this sweep.** The largest size tried "
            "still validated, so the limit is above it and is not yet measured."
        )
    lines += [
        "",
        "Nothing here is extrapolated. Sizes that were not run are not reported.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/load_test.py")
    ap.add_argument("--cellsize", type=float, default=90.0)
    ap.add_argument("--end-hr", type=float, default=2.0,
                    help="short on purpose - this measures size, not duration")
    ap.add_argument("--max-cells", type=int, default=2_000_000,
                    help="stop climbing once a grid exceeds this")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the run folders")
    ap.add_argument("--sizes", type=float, nargs="*", default=None)
    args = ap.parse_args(argv)

    sizes = args.sizes or (QUICK_SIZES_KM if args.quick else DEFAULT_SIZES_KM)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("SIH26161 load test — real runs, synthetic valley, no network\n")
    print(f"cell size {args.cellsize:g} m, {args.end_hr:g} h simulated, "
          f"stopping above {args.max_cells:,} cells\n")

    rows: list[Row] = []
    for reach in sizes:
        print(f"  {reach:>5g} km ... ", end="", flush=True)
        row = one(reach, args.cellsize, args.end_hr, args.keep)
        rows.append(row)
        if row.validates:
            print(f"{row.nx}x{row.ny} = {row.cells:,} cells, "
                  f"solve {row.solve_s:g}s, peak {row.peak_rss_mb:g} MB, "
                  f"{row.cells_per_s:,.0f} cell-updates/s")
        else:
            print(f"FAILED — {row.error}")
            break
        if row.cells >= args.max_cells:
            print(f"\n  reached the {args.max_cells:,} cell budget; stopping.")
            break

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report(rows, args.cellsize, args.end_hr), encoding="utf-8")
    (OUT_DIR / "load_test.json").write_text(
        json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8"
    )
    print(f"\nwrote {REPORT.relative_to(REPO)}")
    return 0 if all(r.validates for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
