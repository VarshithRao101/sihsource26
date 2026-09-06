"""
integration/machhu1979.py - the Machhu-II failure of 11 August 1979, run through
the pipeline and compared against what was reported at the time.

    python integration/machhu1979.py
    python integration/machhu1979.py --reach 30 --end 24 --cellsize 60
    python integration/machhu1979.py --only event      # skip the control run

THE EVENT. The Machhu-II earthfill dam, on the Machhu in Saurashtra, Gujarat,
about 6 km upstream of Morvi (Morbi). On 11 August 1979 the flood arriving from
the 1,900 km2 catchment exceeded what the structure could pass, the earthen
flanks were overtopped between roughly 14:30 and 15:00 IST, eroded, and failed.
The reported wave at Morvi was 8-10 m and the town was submerged.

WHY THIS EVENT IS WORTH RUNNING. It is the one thing the Annamayya validation
could not give us: a documented DOWNSTREAM DEPTH. Annamayya has a satellite
extent and no depth, and the extent comparison is weak for reasons written up in
integration/validate_annamayya.py. Here the published account states a wave
height at a named town 6 km below the dam, and our solver writes a depth at that
town into impact.json. That is a like-for-like number, and it can disagree.

WHAT THIS IS.

  * The overtopping breach of Machhu-II as the CWC register describes the
    structure TODAY - 25.0 m, 100.55 MCM, dam GJ04MH0498 - routed over the real
    Machhu valley in COP30 terrain.
  * Run twice. The CONTROL is the ordinary full-reservoir breach the console
    would produce for this dam with no knowledge of 1979. The EVENT run adds the
    one thing the 1979 record supplies that the register cannot: the flood was
    still arriving while the dam broke, reported above 1.4e4 m3/s, and that
    inflow is carried through the level-pool routing.
  * The difference between the two runs is the entire contribution of the
    historical record. It is printed rather than folded in.

WHAT THIS IS NOT.

  * NOT a hindcast of the 1979 dam. The register describes the dam as REBUILT -
    year 1989, spillway 26,650 m3/s. The account of the failure gives the 1979
    design capacity as 5.7e3 m3/s, which is 4.7 times smaller. We do not have
    the 1979 embankment geometry, so we run the structure we have a record of
    and say which one that is. The rebuild's fivefold spillway increase is
    itself the clearest surviving statement of what went wrong.
  * NOT rainfall-driven. The account's rainfall series - 237 mm on 11 August,
    447 mm over 10-12 August, and the 1-to-72-hour depths - is not used to
    GENERATE the inflow here. modules/07_ml/inflow.py turns rainfall into inflow
    through CHIRPS, and CHIRPS starts in 1981. Running our runoff model on
    hand-typed 1979 depths and then calling the result agreement would be
    circular. The reported peak flood is used directly instead, as an input, and
    labelled as one.
  * NOT terrain from 1979. COP30 is a modern DEM of a valley that has been
    rebuilt and re-embanked since. Morbi's ground is not the ground the wave hit.
  * The trigger is not modelled. The run starts at a full reservoir with the
    flood already arriving.

WHERE THE 1979 NUMBERS COME FROM. Supplied by the project team from a published
meteorological and hydrological account of the failure. They are NOT read out of
a dataset in this repository and NOT from the CWC register. They travel into
meta.json as notes, and nothing in this file is used to tune anything.

Needs the network on the first run: the Machhu DEM has to be fetched. Afterwards
the conditioned terrain is cached under data/dem/machhuii/.

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

DAM_ID = "GJ04MH0498"

SOURCE = (
    "supplied by the project team from a published account of the 11 August "
    "1979 Machhu-II failure; approximate, not from a dataset held in this "
    "repository"
)

# Everything the published account states. Reported, not computed. Kept in one
# block so that every comparison below is against a number with a provenance
# string attached, and so nobody can quietly add a value that came from the run.
REPORTED = {
    "failure_date": "1979-08-11",
    "overtopping_window_ist": "14:30-15:00",
    "mechanism": "overtopping of the earthen flanks, erosion, breach",
    "catchment_km2": 1900.0,
    "river_length_km": 140.0,
    "mean_annual_rain_mm": 569.2,
    "rain_11_aug_mm": 237.0,
    "rain_10_to_12_aug_mm": 447.0,
    "design_peak_flood_cumecs": 5.7e3,
    "observed_peak_flood_cumecs": 1.4e4,   # "reported to exceed"
    "flood_wave_height_m": (8.0, 10.0),
    "distance_to_morvi_km": 6.0,
    "downstream_town": "Morbi",
    # Short-duration depths from the same account. Recorded because they are the
    # most useful part of it for anyone who later wires a real rainfall-runoff
    # chain to this event; NOT used by this script. See the docstring.
    "duration_rainfall_mm": {
        "1h": 141.0, "3h": 167.0, "6h": 208.0, "12h": 268.0, "18h": 312.0,
        "24h": 342.0, "36h": 364.0, "48h": 372.0, "72h": 447.0,
    },
    "source": SOURCE,
}

# Names the exposure layer might carry for the town 6 km below the dam.
MORBI_ALIASES = ("morbi", "morvi", "morbi city", "morbī")


def _dam() -> dict:
    from importlib import import_module

    cat = import_module("modules.01_geodata.dams")
    dam = cat.get(DAM_ID)
    if dam is None:
        raise RuntimeError(
            f"{DAM_ID} is not in the catalogue. Run "
            f"'python -m modules.01_geodata.dams build' first."
        )
    return dam


def build_spec(dam: dict, inflow_cumecs: float, reach_km: float, end_hr: float,
               cellsize_m: float, label: str):
    """One ScenarioSpec. `inflow_cumecs` is the only thing that differs."""
    from importlib import import_module

    sc = import_module("modules.04_backend.scenario")

    site = sc.SiteSpec(
        name=dam["name"],
        lat=float(dam["lat"]),
        lon=float(dam["lon"]),
        river=dam["river"] or "",
        state=dam["state"] or "",
        dam_height_m=float(dam["height_m"]),
        reservoir_capacity_mcm=float(dam["gross_storage_mcm"]),
        source="CWC NRLD 2019",
    )
    if label == "event":
        lo, hi = REPORTED["flood_wave_height_m"]
        notes = (
            "Machhu-II, 11 August 1979: overtopping of the earthen flanks "
            f"{REPORTED['overtopping_window_ist']} IST, wave reported "
            f"{lo:g}-{hi:g} m at Morvi "
            f"{REPORTED['distance_to_morvi_km']:g} km downstream. Reservoir "
            f"inflow held at the reported flood peak {inflow_cumecs:,.0f} m3/s "
            "through the breach. Structure geometry is the REBUILT dam as the "
            f"CWC register describes it (1989, spillway "
            f"{dam['spillway_cumecs']:,.0f} m3/s), not the 1979 dam (design "
            f"flood {REPORTED['design_peak_flood_cumecs']:,.0f} m3/s). Not a "
            f"hindcast; trigger and rainfall not modelled. {SOURCE}."
        )
    else:
        notes = (
            "Control: the ordinary full-reservoir overtopping breach of "
            "Machhu-II with no inflow, i.e. what the console produces for this "
            "dam from the register alone. Paired with the 1979 event run so the "
            "contribution of the reported flood inflow can be read off."
        )

    return sc.ScenarioSpec(
        site=site,
        failure_mode="overtopping",
        reservoir_level_frac=1.0,
        inflow_cumecs=float(inflow_cumecs),
        design_spillway_cumecs=dam.get("spillway_cumecs"),
        reach_length_km=reach_km,
        end_hr=end_hr,
        cellsize_m=cellsize_m,
        dem_source="COP30",
        notes=notes,
        tags=["machhu1979", label],
    )


def run_one(spec, reach_km: float) -> dict:
    """Through the same path the API and run_events.py use."""
    from importlib import import_module

    from shared.io import make_run_id, next_sequence, read_meta
    from shared.validate import validate_run

    gd = import_module("modules.01_geodata")
    rn = import_module("modules.04_backend.runner")

    spec.require_valid()

    plan = gd.plan_domain(
        lat=spec.site.lat, lon=spec.site.lon, site=spec.site_slug,
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
        print(f"    exposure unavailable ({type(exc).__name__}: {exc})")
        exposure = None

    seq = next_sequence(OUTPUTS, spec.site_slug, spec.scenario_slug, spec.engine)
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)

    t0 = time.perf_counter()
    rn.run_scenario(spec, outputs_dir=OUTPUTS, terrain=terrain, run_id=run_id,
                    exposure=exposure)
    wall = time.perf_counter() - t0

    report = validate_run(OUTPUTS / run_id)
    meta = read_meta(OUTPUTS / run_id)
    res = meta.get("results", {})

    impact = {}
    ipath = OUTPUTS / run_id / "impact.json"
    if ipath.exists():
        impact = json.loads(ipath.read_text(encoding="utf-8"))

    return {
        "run_id": run_id,
        "validates": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "inflow_cumecs": spec.inflow_cumecs,
        "peak_discharge_cumecs": res.get("peak_discharge_cumecs"),
        "max_depth_m": res.get("max_depth_m"),
        "max_velocity_ms": res.get("max_velocity_ms"),
        "flood_area_km2": res.get("flood_area_km2"),
        "released_volume_mcm": res.get("released_volume_mcm"),
        "mass_balance_err_pct": res.get("mass_balance_err_pct"),
        "breach_width_m": meta.get("scenario", {}).get("breach_width_m"),
        "formation_time_hr": meta.get("scenario", {}).get("formation_time_hr"),
        "cellsize_m": meta.get("domain", {}).get("cellsize_m"),
        "settlements": impact.get("settlements") or [],
        "totals": impact.get("totals") or {},
        "wall_s": round(wall, 1),
        "dam_lonlat": (spec.site.lon, spec.site.lat),
    }


def find_morbi(row: dict) -> dict | None:
    """The named town 6 km below the dam, if the exposure layer carries it.

    Matched on name, not on distance, so a nearby village cannot be silently
    promoted into the comparison. The distance is then MEASURED and printed,
    which is what lets the reader check the match against the reported 6 km.
    """
    from shared.geo import haversine_km

    lon, lat = row["dam_lonlat"]
    for s in row["settlements"]:
        if (s.get("name") or "").strip().lower() in MORBI_ALIASES:
            hit = dict(s)
            hit["distance_km"] = round(
                haversine_km(lon, lat, s["lon"], s["lat"]), 2
            )
            return hit
    return None


def deepest_near(row: dict, km_lo: float, km_hi: float) -> dict | None:
    """Deepest settlement in a distance band below the dam.

    A fallback for when the exposure layer has no place called Morbi. It answers
    a different question - "how deep does the model get anywhere in the band the
    town sits in" - and the output says so rather than presenting it as Morbi.
    """
    from shared.geo import haversine_km

    lon, lat = row["dam_lonlat"]
    band = []
    for s in row["settlements"]:
        d = haversine_km(lon, lat, s["lon"], s["lat"])
        if km_lo <= d <= km_hi and s.get("max_depth_m") is not None:
            hit = dict(s)
            hit["distance_km"] = round(d, 2)
            band.append(hit)
    if not band:
        return None
    return max(band, key=lambda s: s["max_depth_m"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python integration/machhu1979.py")
    ap.add_argument("--reach", type=float, default=30.0, help="km downstream")
    ap.add_argument("--end", type=float, default=24.0, help="hours simulated")
    ap.add_argument("--cellsize", type=float, default=60.0, help="metres")
    ap.add_argument("--only", choices=["control", "event"], default=None,
                    help="run just one of the two")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    dam = _dam()
    print("Machhu-II, 11 August 1979 - overtopping failure, Saurashtra, Gujarat")
    print(f"  register: {dam['name']}  {dam['height_m']} m  "
          f"{dam['gross_storage_mcm']} MCM  "
          f"spillway {dam['spillway_cumecs']:,.0f} m3/s  "
          f"({int(dam['year'])} rebuild)")
    print(f"  reported: design flood {REPORTED['design_peak_flood_cumecs']:,.0f} "
          f"m3/s, observed flood > "
          f"{REPORTED['observed_peak_flood_cumecs']:,.0f} m3/s, "
          f"catchment {REPORTED['catchment_km2']:,.0f} km2")
    print()

    plan = [
        ("control", 0.0),
        ("event", REPORTED["observed_peak_flood_cumecs"]),
    ]
    if args.only:
        plan = [p for p in plan if p[0] == args.only]

    rows: dict[str, dict] = {}
    for label, inflow in plan:
        print(f"[{label}] inflow {inflow:,.0f} m3/s, reach {args.reach:g} km, "
              f"{args.end:g} h, {args.cellsize:g} m cells")
        spec = build_spec(dam, inflow, args.reach, args.end, args.cellsize, label)
        try:
            row = run_one(spec, args.reach)
        except Exception as exc:  # noqa: BLE001 - a failure is a result here
            print(f"    FAILED {type(exc).__name__}: {exc}")
            rows[label] = {"failed": f"{type(exc).__name__}: {exc}"}
            continue
        rows[label] = row
        print(f"    {row['run_id']}  "
              f"{'PASS' if row['validates'] else 'FAILED VALIDATION'}  "
              f"peak {row['peak_discharge_cumecs']:,.0f} m3/s  "
              f"max depth {row['max_depth_m']:.2f} m  "
              f"{row['flood_area_km2']:.1f} km2  {row['wall_s']} s")
        if not row["validates"]:
            for e in row["errors"]:
                print(f"      ! {e}")

    out = {
        "event": "Machhu-II dam failure, 11 August 1979",
        "dam": dam,
        "reported": REPORTED,
        "settings": {"reach_km": args.reach, "end_hr": args.end,
                     "cellsize_m": args.cellsize},
        "runs": {k: {kk: vv for kk, vv in v.items() if kk != "settlements"}
                 for k, v in rows.items()},
        "morbi": {k: find_morbi(v) for k, v in rows.items() if not v.get("failed")},
        "band_5_8_km": {k: deepest_near(v, 5.0, 8.0)
                        for k, v in rows.items() if not v.get("failed")},
    }
    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return 0

    ok = [r for r in rows.values() if r.get("validates")]
    print(f"\n{len(ok)}/{len(rows)} runs validated against the contract\n")

    print("  MODELLED vs REPORTED")
    print(f"  {'quantity':<32} {'control':>12} {'1979 event':>12} {'reported':>18}")
    print(f"  {'-' * 32} {'-' * 12} {'-' * 12} {'-' * 18}")

    def cell(label, key, fmt):
        v = rows.get(label, {}).get(key)
        return fmt.format(v) if isinstance(v, (int, float)) else "-"

    def line(name, key, reported, fmt="{:,.1f}"):
        print(f"  {name:<32} {cell('control', key, fmt):>12} "
              f"{cell('event', key, fmt):>12} {reported:>18}")

    line("breach peak outflow, m3/s", "peak_discharge_cumecs",
         f">{REPORTED['observed_peak_flood_cumecs']:,.0f} in*", "{:,.0f}")
    line("max depth anywhere, m", "max_depth_m", "-", "{:.2f}")
    line("max velocity, m/s", "max_velocity_ms", "-", "{:.2f}")
    line("flood area, km2", "flood_area_km2", "-", "{:.1f}")
    line("released volume, MCM", "released_volume_mcm", "-", "{:.1f}")
    line("breach width, m", "breach_width_m", "-", "{:,.0f}")
    line("formation time, h", "formation_time_hr", "-", "{:.2f}")
    line("mass balance error, %", "mass_balance_err_pct", "-", "{:+.3f}")

    print()
    print("  * the reported figure is the INFLOW FLOOD arriving at the reservoir,")
    print("    not the breach outflow. They are different quantities and the table")
    print("    puts them side by side only because no breach outflow was measured.")

    print("\n  AT MORVI / MORBI, the one directly comparable number")
    lo, hi = REPORTED["flood_wave_height_m"]
    for label in ("control", "event"):
        row = rows.get(label)
        if not row or row.get("failed"):
            continue
        hit = find_morbi(row)
        if hit:
            print(f"    {label:<8} {hit['name']} at {hit['distance_km']} km: "
                  f"depth {hit['max_depth_m']:.2f} m, "
                  f"arrival {hit['arrival_hr']:.2f} h, "
                  f"{hit['max_velocity_ms']:.2f} m/s, {hit['hazard_class']}")
        else:
            alt = deepest_near(row, 5.0, 8.0)
            if alt:
                print(f"    {label:<8} no settlement named Morbi in the exposure "
                      f"layer. Deepest in the 5-8 km band is {alt['name']} at "
                      f"{alt['distance_km']} km: depth {alt['max_depth_m']:.2f} m, "
                      f"arrival {alt['arrival_hr']:.2f} h. NOT the same place.")
            else:
                print(f"    {label:<8} nothing wet in the 5-8 km band.")
    print(f"    reported wave height at Morvi: {lo:g}-{hi:g} m, "
          f"{REPORTED['distance_to_morvi_km']:g} km below the dam")
    print()
    print("  A depth and a wave height are not the same measurement. Our depth is")
    print("  water above the modern ground surface at the town; the reported 8-10 m")
    print("  is a wave height from a contemporary account, over 1979 ground, with")
    print("  no stated datum. Read the comparison as an order of magnitude.")
    print()
    print("  NOT A HINDCAST. The structure is the 1989 rebuild, the terrain is")
    print("  modern COP30, the rainfall is not modelled, and the trigger is not")
    print("  modelled. See the header of this file.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
