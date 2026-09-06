"""
integration/run_events.py - simulate every historic natural-dam event.

    python integration/run_events.py                 # all of them
    python integration/run_events.py --only rishiganga2021 phuktal2015
    python integration/run_events.py --reach 30 --end 8

The problem statement names its failures in the Background and four of the five
are natural dams. modules/01_geodata/events.py holds those events as entry
points; this runs them, so "we cover the events NTRO named" is a table of runs
that validated rather than a claim that the physics would work.

Each run is a blockage_breach at the event's coordinate with the event's
reported debris height. The impounded volume is read off the DEM, and this
script prints it beside the volume the event record reports - the two are not
expected to match and the gap is worth seeing.

NOT A HINDCAST. The trigger is not modelled and no observed flood extent has
been compared. See the header of modules/01_geodata/events.py.

Needs the network on the first run of each event: the DEM has to be fetched.
Afterwards the conditioned terrain is cached under data/dem/.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"


def run_one(event: dict, reach_km: float, end_hr: float, cellsize_m: float) -> dict:
    """One event, through the same path the API uses. Returns a summary row."""
    from importlib import import_module

    from shared.io import read_meta
    from shared.validate import validate_run

    gd = import_module("modules.01_geodata")
    sc = import_module("modules.04_backend.scenario")
    rn = import_module("modules.04_backend.runner")

    site = sc.SiteSpec(
        name=f"{event['name']} {event['year']}",
        lat=float(event["lat"]),
        lon=float(event["lon"]),
        river=event["river"],
        state=event["state"],
        # Placeholders in blockage mode - runner replaces both. See the comment
        # in api.RunRequest.to_spec.
        dam_height_m=float(event["blockage_height_m"]),
        reservoir_capacity_mcm=1.0,
        source=event["source"],
    )
    spec = sc.ScenarioSpec(
        site=site,
        failure_mode="blockage_breach",
        blockage_height_m=float(event["blockage_height_m"]),
        reach_length_km=reach_km,
        cellsize_m=cellsize_m,
        end_hr=end_hr,
        dem_source="COP30",
        notes=(
            f"{event['name']} ({event['year']}), {event['mechanism']}. "
            f"Approximate coordinate and height - {event['source']}. Trigger "
            f"not modelled; no observed extent compared."
        ),
    )
    spec.require_valid()

    plan = gd.plan_domain(
        lat=site.lat, lon=site.lon, site=spec.site_slug,
        reach_length_km=reach_km, corridor_width_km=spec.corridor_width_km,
    )
    spec.site.lat, spec.site.lon = plan.dam_lonlat[1], plan.dam_lonlat[0]
    spec.domain_bbox = plan.bbox
    terrain = gd.RealTerrain(
        site=spec.site_slug, source="COP30",
        dam_lonlat=plan.dam_lonlat, reach_length_km=reach_km,
    )
    try:
        exposure = gd.exposure.build_exposure(plan.bbox, site=spec.site_slug)
    except Exception as exc:  # noqa: BLE001 - a flood map with no names is valid
        print(f"    exposure unavailable ({type(exc).__name__})")
        exposure = None

    from shared.io import make_run_id, next_sequence

    seq = next_sequence(OUTPUTS, spec.site_slug, spec.scenario_slug, spec.engine)
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)

    t0 = time.perf_counter()
    rn.run_scenario(spec, outputs_dir=OUTPUTS, terrain=terrain, run_id=run_id,
                    exposure=exposure)
    wall = time.perf_counter() - t0

    report = validate_run(OUTPUTS / run_id)
    meta = read_meta(OUTPUTS / run_id)
    res = meta.get("results", {})
    blockage = meta.get("blockage") or {}
    impact = {}
    ipath = OUTPUTS / run_id / "impact.json"
    if ipath.exists():
        impact = json.loads(ipath.read_text(encoding="utf-8"))

    return {
        "event_id": event["id"],
        "event": f"{event['name']} ({event['year']})",
        "named_by_ntro": event["named_in_problem_statement"],
        "run_id": run_id,
        "validates": report.ok,
        "errors": report.errors,
        "blockage_height_m": float(event["blockage_height_m"]),
        "reported_impoundment_mcm": event.get("reported_impoundment_mcm"),
        "dem_impoundment_mcm": blockage.get("impounded_volume_mcm"),
        "peak_discharge_cumecs": res.get("peak_discharge_cumecs"),
        "max_depth_m": res.get("max_depth_m"),
        "flood_area_km2": res.get("flood_area_km2"),
        "mass_balance_err_pct": res.get("mass_balance_err_pct"),
        "settlements": len(impact.get("settlements") or []),
        "people": impact.get("population_affected"),
        "wall_s": round(wall, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/run_events.py")
    ap.add_argument("--only", nargs="*", help="event ids; default is all of them")
    ap.add_argument("--reach", type=float, default=25.0, help="km downstream")
    ap.add_argument("--end", type=float, default=6.0, help="hours simulated")
    ap.add_argument("--cellsize", type=float, default=60.0, help="metres")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from importlib import import_module

    ev = import_module("modules.01_geodata.events")
    wanted = ev.all_events()
    if args.only:
        keep = {e.lower() for e in args.only}
        wanted = [e for e in wanted if e["id"] in keep]
        if not wanted:
            print(f"no event matches {args.only}", file=sys.stderr)
            return 1

    rows = []
    for i, event in enumerate(wanted, 1):
        print(f"[{i}/{len(wanted)}] {event['name']} ({event['year']}) - "
              f"{event['lat']:.4f}, {event['lon']:.4f}, "
              f"{event['blockage_height_m']:g} m debris")
        try:
            row = run_one(event, args.reach, args.end, args.cellsize)
        except Exception as exc:  # noqa: BLE001 - a failure is a result here
            print(f"    FAILED {type(exc).__name__}: {exc}")
            rows.append({"event_id": event["id"],
                         "event": f"{event['name']} ({event['year']})",
                         "named_by_ntro": event["named_in_problem_statement"],
                         "failed": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append(row)
        print(f"    {row['run_id']}  {'PASS' if row['validates'] else 'FAILED VALIDATION'}"
              f"  peak {row['peak_discharge_cumecs']:,.0f} m3/s"
              f"  {row['flood_area_km2']} km2  {row['wall_s']} s")

    out = {"events_run": len(rows), "rows": rows,
           "settings": {"reach_km": args.reach, "end_hr": args.end,
                        "cellsize_m": args.cellsize},
           "source": ev.SOURCE}
    if args.json:
        print(json.dumps(out, indent=1))
        return 0

    ok = [r for r in rows if r.get("validates")]
    print(f"\n{len(ok)}/{len(rows)} events simulated and validated\n")
    print(f"  {'event':<26} {'debris':>7} {'DEM lake':>9} {'reported':>9} "
          f"{'peak m3/s':>10} {'km2':>7}")
    print(f"  {'-'*26} {'-'*7} {'-'*9} {'-'*9} {'-'*10} {'-'*7}")
    for r in rows:
        if r.get("failed"):
            print(f"  {r['event'][:26]:<26} {r['failed'][:50]}")
            continue
        rep = r["reported_impoundment_mcm"]
        print(f"  {r['event'][:26]:<26} {r['blockage_height_m']:>6.0f}m "
              f"{(r['dem_impoundment_mcm'] or 0):>8.2f}  "
              f"{(f'{rep:.1f}' if rep else '     -'):>9} "
              f"{(r['peak_discharge_cumecs'] or 0):>10,.0f} "
              f"{(r['flood_area_km2'] or 0):>7.2f}")
    print("\n  DEM lake vs reported: the model uses the DEM figure. They are not")
    print("  expected to match - the reported volume is an approximate published")
    print("  number and the DEM one is what this terrain actually holds.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
