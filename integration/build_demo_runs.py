"""
integration/build_demo_runs.py - the four runs the console loads instantly.

    python -m integration.build_demo_runs            # build any that are missing
    python -m integration.build_demo_runs --force    # rebuild all four
    python -m integration.build_demo_runs --list     # what is on disk now

WHY THIS EXISTS. A solve takes minutes on real terrain. In front of a panel
that is the whole demonstration gone, and the honest fix is not to fake the
solve - it is to have already done it. These four are REAL runs through
runner.run_scenario, the same path the API uses, on real COP30 terrain, and
they land in outputs/ as ordinary contract-valid run folders. The console's
"Load a stored run" button cycles them one click at a time.

WHY THESE FOUR. Not because they are the four prettiest floods. Each one is
the case that shows something the other three cannot:

  1. Machchhu II, overtopping. A real Indian dam-break with a documented
     outcome, so the result can be argued with rather than admired. It is the
     one case here where "is this right" has an answer.
  2. Lower Manair, spillway blockage. The only case that produces a time to
     overtop - the hours between the outlets failing and the first water over
     the crest. That number is the reason the mode exists.
  3. South Lhonak, moraine outburst. A natural dam with no published storage,
     so the volume is read off the DEM; and the Sikkim 2023 failure is the
     event the problem statement's background is about.
  4. Idukki, foundation failure. A 169 m arch dam, which is the one class of
     structure the breach regressions in shared/hydro.py do not describe. It is
     here to show a case where using them would have been wrong.

Everything is committed except the run folders themselves, which are large and
regenerable - run this once on a machine with network access to OpenTopography
and the console finds them.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"
MANIFEST = REPO_ROOT / "data" / "demo_runs.json"


# --------------------------------------------------------------------------
# The four
# --------------------------------------------------------------------------
#
# `dam_id` is a catalogue id, so the height, storage, crest length and design
# spillway all come from the register rather than from this file. The only
# numbers here are the ones that describe the SCENARIO - what failed, how full
# it was, how far and how long to look.

DEMOS = [
    {
        "key": "machhu_overtopping",
        "label": "Machchhu II, 1979 - overtopping",
        "why": (
            "Morbi, Gujarat, 11 August 1979. 600 mm in 24 hours produced an "
            "inflow near three times the spillway's design capacity, the water "
            "passed the earthen flanks and 2,100 m of embankment went. This is "
            "the one case in the set with a documented outcome to argue with."
        ),
        "dam_id": "GJ04MH0498",
        "failure_mode": "overtopping",
        "spec": {
            "reservoir_level_frac": 1.0,
            "reach_length_km": 60.0,
            "end_hr": 12.0,
            "inflow_cumecs": 16300.0,
        },
    },
    {
        "key": "manair_spillway_blockage",
        "label": "Lower Manair - spillway blocked",
        "why": (
            "The outlets are gone and the inflow keeps arriving. The number "
            "this case produces and no other case can is the time to overtop: "
            "the hours between the blockage and the first water over the "
            "crest, which is the warning the operator actually has."
        ),
        "dam_id": "TL47HH0065",
        "failure_mode": "spillway_blockage",
        "spec": {
            "reach_length_km": 40.0,
            "end_hr": 12.0,
            "inflow_cumecs": 4000.0,
            "residual_spillway_frac": 0.15,
            "blockage_start_level_frac": 0.85,
        },
    },
    {
        "key": "lhonak_glof",
        "label": "South Lhonak, 2023 - moraine outburst",
        "why": (
            "North Sikkim, 3-4 October 2023. The lake grew from 1.12 to "
            "1.63 km2 between 2016 and 2023 and then drained into the Teesta. "
            "A natural dam: no published storage exists, so the impounded "
            "volume comes from the 2023 lake area through Huggel et al. "
            "(2002) - because a 30 m DEM cannot resolve this basin, finding "
            "0.34 MCM behind the moraine against the ~25.7 MCM that actually "
            "drained. Both numbers are in meta.json. The breach stops at the "
            "bedrock sill rather than cutting through the whole ridge."
        ),
        "dam_id": "HISTSOUTHLHONAK2023",
        "failure_mode": "glof_moraine",
        "spec": {
            "reach_length_km": 60.0,
            "end_hr": 12.0,
            "moraine_height_m": 40.0,
            "glof_breach_width_m": 25.0,
            # The 2023 lake area. Supplying it is a deliberate statement that
            # the DEM has not seen this lake: at the published coordinate a
            # 30 m DEM holds 0.34 MCM behind the moraine, against roughly
            # 25.7 MCM that the outburst actually released. Both figures are
            # published in meta.json under glof_moraine so the disagreement is
            # visible. Neither is a measurement of this lake.
            "lake_area_km2": 1.63,
        },
    },
    {
        "key": "idukki_foundation",
        "label": "Idukki - foundation failure of an arch dam",
        "why": (
            "A 169 m double-curvature arch dam in Kerala. This is the one "
            "class of structure the breach regressions in shared/hydro.py do "
            "not describe - Froehlich, Von Thun and MacDonald are all fitted "
            "to earthfill embankments, and an arch dam that loses its "
            "foundation is displaced whole rather than eroded. The opening "
            "here is geometry under critical-flow control, and meta.json says "
            "no regression was applied."
        ),
        "dam_id": "KL29VH0027",
        "failure_mode": "foundation_failure",
        "spec": {
            "reservoir_level_frac": 1.0,
            "reach_length_km": 50.0,
            "end_hr": 12.0,
            "foundation_breach_frac": 0.8,
            "collapse_time_min": 2.0,
        },
    },
]


# --------------------------------------------------------------------------


def _resolve_dam(demo: dict) -> dict:
    from importlib import import_module

    cat = import_module("modules.01_geodata.dams")
    if demo.get("dam_id"):
        dam = cat.get(demo["dam_id"])
        if dam is None:
            raise SystemExit(
                f"{demo['key']}: no dam with id {demo['dam_id']!r} in the catalogue"
            )
        return dam
    hits = cat.search(q=demo["dam_search"], limit=5)
    if not hits:
        raise SystemExit(f"{demo['key']}: nothing matched {demo['dam_search']!r}")
    return hits[0]


def _spec_for(demo: dict):
    """A ScenarioSpec built the same way the API builds one."""
    # import_module, not `from modules.04_backend...`: the package name starts
    # with a digit, so it is not a legal Python identifier in an import
    # statement. Every module in this repository reaches it this way.
    from importlib import import_module

    _sc = import_module("modules.04_backend.scenario")
    ScenarioSpec, SiteSpec = _sc.ScenarioSpec, _sc.SiteSpec

    dam = _resolve_dam(demo)
    natural = dam.get("kind") == "natural"

    site = SiteSpec(
        name=dam["name"],
        lat=float(dam["lat"]),
        lon=float(dam["lon"]),
        river=dam.get("river") or "",
        state=dam.get("state") or "",
        dam_height_m=float(dam["height_m"]),
        # A natural dam has no published storage. runner.py replaces this with
        # what the terrain holds; it is 1.0 only because validate() wants it
        # positive, and nothing downstream reads it.
        reservoir_capacity_mcm=1.0 if natural else float(dam["gross_storage_mcm"]),
        source=dam.get("source") or "CWC NRLD 2019",
        kind=dam.get("kind", "engineered"),
        crest_length_m=float(dam["length_m"]) if dam.get("length_m") else None,
        height_source=dam.get("height_source", ""),
    )

    spec = ScenarioSpec(
        site=site,
        failure_mode=demo["failure_mode"],
        design_spillway_cumecs=(
            float(dam["spillway_cumecs"]) if dam.get("spillway_cumecs") else None
        ),
        dem_source="COP30",
        notes=demo["why"],
        tags=["demo", demo["key"]],
        **demo["spec"],
    )
    spec.require_valid()
    return spec, dam


def build_one(demo: dict) -> dict:
    """Solve one demo on real terrain, through the API's own path."""
    from importlib import import_module

    from shared.io import make_run_id, next_sequence, read_meta
    from shared.validate import validate_run

    gd = import_module("modules.01_geodata")
    rn = import_module("modules.04_backend.runner")

    spec, dam = _spec_for(demo)
    reach_km = spec.reach_length_km

    print(f"  {demo['label']}")
    print(f"    site {spec.site.name} ({spec.site.lat:.4f}, {spec.site.lon:.4f}) "
          f"h={spec.site.dam_height_m} m kind={spec.site.kind}")

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
    print(f"    -> {run_id}  {wall:.0f}s  "
          f"{res.get('flood_area_km2', 0):.1f} km2  "
          f"peak {res.get('peak_outflow_cumecs', 0):,.0f} m3/s  "
          f"valid={report.ok}")
    if not report.ok:
        for e in report.errors:
            print(f"       ERROR {e}")

    return {
        "key": demo["key"],
        "run_id": run_id,
        "label": demo["label"],
        "why": demo["why"],
        "failure_mode": demo["failure_mode"],
        "site": spec.site.name,
        "validates": report.ok,
        "runtime_s": round(wall, 1),
    }


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["runs"]


def write_manifest(rows: list[dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {
                "runs": rows,
                "built_by": "python -m integration.build_demo_runs",
                "note": (
                    "Real runs through runner.run_scenario on COP30 terrain, "
                    "not recordings. The run folders are regenerable and are "
                    "not committed; rebuild them with the command above."
                ),
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m integration.build_demo_runs")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the manifest already points at a run")
    ap.add_argument("--list", action="store_true", help="show the manifest and stop")
    ap.add_argument("--only", default=None, help="build one demo by key")
    args = ap.parse_args(argv)

    existing = {r["key"]: r for r in load_manifest()}

    if args.list:
        if not existing:
            print("no manifest yet. Run without --list to build.")
            return 0
        for d in DEMOS:
            r = existing.get(d["key"])
            on_disk = r and (OUTPUTS / r["run_id"] / "meta.json").is_file()
            print(f"  {d['key']:<26} {'OK  ' if on_disk else 'MISSING'} "
                  f"{(r or {}).get('run_id', '-')}")
        return 0

    rows: list[dict] = []
    for d in DEMOS:
        if args.only and d["key"] != args.only:
            if d["key"] in existing:
                rows.append(existing[d["key"]])
            continue
        prev = existing.get(d["key"])
        if prev and not args.force and (OUTPUTS / prev["run_id"] / "meta.json").is_file():
            print(f"  {d['label']}\n    already built: {prev['run_id']}")
            rows.append(prev)
            continue
        try:
            rows.append(build_one(d))
        except Exception as exc:  # noqa: BLE001
            # One demo failing must not cost the other three. The manifest keeps
            # what worked and the console shows those.
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            if prev:
                rows.append(prev)

    write_manifest(rows)
    ok = sum(1 for r in rows if (OUTPUTS / r["run_id"] / "meta.json").is_file())
    print(f"\n  {ok}/{len(DEMOS)} demo runs on disk -> {MANIFEST}")
    return 0 if ok == len(DEMOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
