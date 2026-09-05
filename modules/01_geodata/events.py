"""
modules/01_geodata/events.py - the historic natural-dam events, as entry points.

    python -m modules.01_geodata.events list
    python -m modules.01_geodata.events show rishiganga2021

WHY THIS EXISTS. Problem statement 26161 names its failures in the Background,
and four of the five are natural dams rather than engineered ones. Until now
there was no way to *select* one: dams.py indexes the CWC register and rivers.py
indexes river names taken from that same register, so a river with no large dam
on it - the Rishi Ganga, the Tsarap Chu - could not be picked at all. rivers.py
says so in its own docstring. This is the third entry point, and it is the one
the problem statement actually asks for.

WHERE THESE NUMBERS COME FROM, stated plainly because it decides how they may
be used. They were supplied by the project team from published accounts of each
event. They are NOT read out of a dataset in this repository, they are NOT from
the CWC register, and every coordinate, height and volume here is APPROXIMATE -
the source table gave them with a tilde. Each record carries `is_approximate`
and `source` saying exactly that, and both travel into meta.json. Nobody should
quote a number from this file as a measurement.

WHAT A RUN FROM ONE OF THESE IS, AND IS NOT.

  * It IS the breach of a natural dam of the stated height at the stated place,
    routed downstream over a real DEM. That is the physics the statement asks
    for, on the reaches it names.
  * The impounded volume is READ OFF THE TERRAIN by blockage.py, not taken from
    the table. `reported_impoundment_mcm` is carried alongside so the two can be
    compared - a DEM that finds 6 MCM behind a barrier reported at 24 MCM is
    telling you something, and it is worth showing rather than hiding.
  * It is NOT a hindcast of what happened. We do not model the TRIGGER of any
    of these - not the rock-ice avalanche at Rishi Ganga, not the avalanche
    displacement wave at South Lhonak, not the sediment choking on the
    Wapriyang. `mechanism` records what was reported; the model starts at a
    barrier that is already there and fails.
  * It is NOT validated against the observed flood of that event. No observed
    extent has been obtained for any of these seven.

Owner: captain (module 01).
"""

from __future__ import annotations

import argparse
import json
import sys

SOURCE = (
    "supplied by the project team from published accounts of each event; "
    "approximate, not from a dataset held in this repository"
)

EVENTS: list[dict] = [
    {
        "id": "rishiganga2021",
        "name": "Rishi Ganga",
        "year": 2021,
        "river": "Rishiganga",
        "state": "Uttarakhand",
        "lat": 30.4832,
        "lon": 79.7321,
        "blockage_height_m": 30.0,
        "reported_impoundment_mcm": 0.8,
        "mechanism": "overtopping and catastrophic washout",
        "named_in_problem_statement": True,
    },
    {
        "id": "phuktal2015",
        "name": "Phuktal River",
        "year": 2015,
        "river": "Tsarap Chu / Phuktal",
        "state": "Ladakh",
        "lat": 33.2841,
        "lon": 77.1714,
        "blockage_height_m": 60.0,
        "reported_impoundment_mcm": 24.0,
        "mechanism": "overtopping after 110 days; reported lake 15 km long",
        "named_in_problem_statement": True,
    },
    {
        "id": "pareechu2005",
        "name": "Pareechu River",
        "year": 2005,
        "river": "Pareechu (draining to the Sutlej)",
        "state": "Himachal Pradesh",
        "lat": 31.9542,
        "lon": 78.6831,
        "blockage_height_m": 35.0,
        "reported_impoundment_mcm": 60.0,
        "mechanism": "piping / overtopping breach",
        "named_in_problem_statement": False,
    },
    {
        "id": "southlhonak2023",
        "name": "South Lhonak Lake",
        "year": 2023,
        "river": "Teesta headwaters",
        "state": "Sikkim",
        "lat": 27.9042,
        "lon": 88.1991,
        "blockage_height_m": 40.0,
        "reported_impoundment_mcm": 15.0,
        "mechanism": (
            "avalanche-induced displacement wave over a moraine dam; the "
            "40 m is moraine freeboard and the 15 MCM is what drained"
        ),
        "named_in_problem_statement": False,
    },
    {
        "id": "gohnatal1893",
        "name": "Gohna Tal",
        "year": 1893,
        "river": "Birahi Ganga",
        "state": "Uttarakhand",
        "lat": 30.3731,
        "lon": 79.4882,
        "blockage_height_m": 300.0,
        "reported_impoundment_mcm": 280.0,
        "mechanism": "progressive overtopping scour",
        "named_in_problem_statement": False,
    },
    {
        "id": "wapriyang2021",
        "name": "Wapriyang River",
        "year": 2021,
        "river": "Kameng tributary",
        "state": "Arunachal Pradesh",
        "lat": 27.4120,
        "lon": 92.8310,
        "blockage_height_m": 25.0,
        # No volume was reported - the source table says "variable backwater
        # pool". None here rather than a number nobody measured; the DEM
        # supplies the storage at run time either way.
        "reported_impoundment_mcm": None,
        "mechanism": "sediment-choked debris breach; variable backwater pool",
        "named_in_problem_statement": True,
    },
    {
        "id": "subansiri2023",
        "name": "Subansiri blockage",
        "year": 2023,
        "river": "Subansiri",
        "state": "Arunachal Pradesh / Assam",
        "lat": 27.5540,
        "lon": 94.2620,
        "blockage_height_m": 20.0,
        "reported_impoundment_mcm": None,
        "mechanism": "partial choke, backwater incision; storage is valley storage",
        "named_in_problem_statement": False,
    },
]


def _decorate(e: dict) -> dict:
    """One event as the API serves it, with its caveats attached."""
    out = dict(e)
    out["failure_mode"] = "blockage_breach"
    out["is_approximate"] = True
    out["source"] = SOURCE
    out["modelled"] = (
        "The barrier is modelled as already in place and then breaching. The "
        "trigger is not modelled, the impounded volume is read off the DEM "
        "rather than taken from the reported figure, and no observed flood "
        "extent has been compared against the result."
    )
    return out


def all_events() -> list[dict]:
    return [_decorate(e) for e in EVENTS]


def get(event_id: str) -> dict | None:
    key = (event_id or "").strip().lower()
    for e in EVENTS:
        if e["id"] == key:
            return _decorate(e)
    return None


def check_point(event: dict, radius_km: float = 3.0,
                scout_cellsize_m: float = 180.0) -> dict:
    """Is this coordinate actually on a river the DEM can see?

    An approximate coordinate off a published account can land on the valley
    side rather than in the channel, and nothing downstream says so usefully:
    domain.plan_domain snaps within 900 m and then takes whatever it found, so
    a point 3 km off-channel becomes a run on a gully with a 352-cell catchment
    that impounds nothing. The Rishi Ganga coordinate in this file does exactly
    that - it sits at 3,749 m with a flow accumulation of ONE, and the nearest
    real channel is 2.96 km away and 1,613 m below it.

    So this measures the coordinate before anything is run, and reports what a
    better one would have to be. It does not move any coordinate: picking the
    barrier's true location is a decision about a real event, not something to
    infer from flow accumulation.

    IT DOES NOT PREDICT WHETHER THE RUN WILL FLOOD, and two earlier versions of
    this function tried to. The first compared the point against the biggest
    river within 3 km and called six of seven "off channel" - including three
    that had already produced clean runs, because in Himalayan terrain the trunk
    river is 3 km from almost everything and these barriers sit on tributaries.
    The second scored the catchment at the snapped cell, which is worse than it
    looks: Phuktal runs on 78 scout cells and impounds 46 MCM, while Pareechu
    has 1,004 and impounds 0.08 MCM. Storage behind a barrier is decided by the
    shape of the valley, not by the size of the catchment, so a threshold on
    accumulation is a curve fitted to seven points and dressed up as hydrology.

    So this reports two measured facts and stops:

        accumulation_cells   flow accumulation where the coordinate lands. 1 or
                             2 cells means hillslope - water arrives there from
                             nowhere - and that IS decisive: the tracer has
                             nothing to trace.
        snapped              the strongest flow path within plan_domain's 900 m
                             snap, which is what a run would actually model.

    Whether that barrier impounds anything is answered by running it -
    integration/run_events.py prints the volume blockage.py reads off the DEM
    beside the volume the event record reports.

    verdict: 'hillslope' when the coordinate itself is off any flow path,
    'on_flow_path' otherwise. Nothing more is claimed.
    """
    import numpy as np

    from shared.geo import Grid, bbox_around, haversine_km

    from . import terrain as tr

    lat, lon = float(event["lat"]), float(event["lon"])
    bbox = bbox_around(lon, lat, radius_km=20.0)
    grid = Grid.from_bbox_cellsize(bbox, scout_cellsize_m)
    dem = tr.load_local_dem(
        tr.fetch_dem(bbox, site=f"{event['id']}_scout", source="COP30"), bbox, grid
    )
    acc = tr.condition_dem(dem, grid.cellsize_m(), grid=grid)["accumulation"]

    col = int((lon - bbox[0]) / (bbox[2] - bbox[0]) * grid.nx)
    row = int((bbox[3] - lat) / (bbox[3] - bbox[1]) * grid.ny)
    row = max(0, min(grid.ny - 1, row))
    col = max(0, min(grid.nx - 1, col))

    def best_within(metres: float) -> tuple[int, int]:
        r = max(1, int(metres / grid.cellsize_m()))
        r0, c0 = max(0, row - r), max(0, col - r)
        sub = acc[r0:row + r + 1, c0:col + r + 1]
        dr, dc = np.unravel_index(int(np.argmax(sub)), sub.shape)
        return r0 + dr, c0 + dc

    def latlon_of(rr: int, cc: int) -> tuple[float, float]:
        return (
            bbox[3] - (rr + 0.5) / grid.ny * (bbox[3] - bbox[1]),
            bbox[0] + (cc + 0.5) / grid.nx * (bbox[2] - bbox[0]),
        )

    snap_radius_m = 900.0
    sr, sc = best_within(snap_radius_m)          # what plan_domain will use
    tr_, tc = best_within(radius_km * 1000)      # the trunk river, for context
    slat, slon = latlon_of(sr, sc)
    tlat, tlon = latlon_of(tr_, tc)
    snapped_acc = float(acc[sr, sc])

    # The only thing accumulation decides on its own: a cell that nothing drains
    # into is not on a river, whatever else is nearby.
    verdict = "hillslope" if float(acc[row, col]) <= 2 else "on_flow_path"

    return {
        "event_id": event["id"],
        "event": f"{event['name']} ({event['year']})",
        "given": [round(lat, 4), round(lon, 4)],
        "elevation_m": round(float(dem[row, col]), 1),
        "accumulation_cells": float(acc[row, col]),
        "snapped": {
            "latlon": [round(slat, 4), round(slon, 4)],
            "accumulation_cells": snapped_acc,
            "elevation_m": round(float(dem[sr, sc]), 1),
            "km_away": round(haversine_km(lon, lat, slon, slat), 2),
        },
        "trunk_channel": {
            "latlon": [round(tlat, 4), round(tlon, 4)],
            "accumulation_cells": float(acc[tr_, tc]),
            "elevation_m": round(float(dem[tr_, tc]), 1),
            "km_away": round(haversine_km(lon, lat, tlon, tlat), 2),
            "m_below_given": round(float(dem[row, col] - dem[tr_, tc]), 1),
        },
        "scout_cellsize_m": grid.cellsize_m(),
        "snap_radius_m": snap_radius_m,
        "verdict": verdict,
    }


def _outcome(event_id: str) -> str:
    """What the most recent run of this event actually impounded, if any.

    Read off disk, never predicted. The point of printing it beside the
    accumulation numbers is that the two do not track each other.
    """
    import json
    from pathlib import Path

    e = get(event_id)
    if e is None:
        return "unknown event"
    slug = "".join(
        ch for ch in f"{e['name']}{e['year']}".lower() if ch.isalnum()
    )
    runs = sorted((Path(__file__).resolve().parents[2] / "outputs").glob(f"{slug}_*/meta.json"))
    if not runs:
        return "not run yet"
    meta = json.loads(runs[-1].read_text(encoding="utf-8"))
    lake = (meta.get("blockage") or {}).get("impounded_volume_mcm")
    area = (meta.get("results") or {}).get("flood_area_km2")
    if not lake:
        return "run: impounds nothing"
    return f"run: {lake:,.2f} MCM lake, {area} km2 wet"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.01_geodata.events")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every historic event")
    s = sub.add_parser("show", help="one event")
    s.add_argument("event_id")
    c = sub.add_parser("check", help="are the coordinates on a real channel?")
    c.add_argument("event_id", nargs="?", help="default: every event")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        rows = [get(args.event_id)] if args.event_id else all_events()
        if rows == [None]:
            print(f"unknown event {args.event_id!r}", file=sys.stderr)
            return 1
        print(f"  {'event':<26} {'at pt':>7} {'snapped':>9} {'snap':>7}  "
              f"{'verdict':<13} measured by running it")
        print(f"  {'-'*26} {'-'*7} {'-'*9} {'-'*7}  {'-'*13} {'-'*28}")
        bad = 0
        for e in rows:
            try:
                r = check_point(e)
            except Exception as exc:  # noqa: BLE001
                print(f"  {e['name'][:26]:<26} check failed: "
                      f"{type(exc).__name__}: {exc}")
                bad += 1
                continue
            sn = r["snapped"]
            print(f"  {r['event'][:26]:<26} {r['accumulation_cells']:>7,.0f} "
                  f"{sn['accumulation_cells']:>9,.0f} {sn['km_away']:>6.2f}km  "
                  f"{r['verdict']:<13} {_outcome(e['id'])}")
            bad += r["verdict"] == "hillslope"
        print(f"\n  {len(rows) - bad}/{len(rows)} coordinates land on a flow path.")
        print("  'at pt' is flow accumulation where the coordinate lands - 1 or 2 cells is")
        print("  hillslope, water reaches it from nowhere. 'snapped' is the strongest flow")
        print("  path within plan_domain's 900 m snap, which is what a run models. Neither")
        print("  predicts the impounded volume - that is valley shape, and the last column")
        print("  is what running it measured.\n")
        return 0

    if args.cmd == "list":
        for e in all_events():
            vol = e["reported_impoundment_mcm"]
            print(
                f"  {e['id']:<18} {e['name']:<20} {e['year']}  "
                f"{e['lat']:.4f},{e['lon']:.4f}  {e['blockage_height_m']:>5.0f} m  "
                f"{'reported ' + format(vol, '.1f') + ' MCM' if vol else 'volume unreported'}"
                f"{'  [named by NTRO]' if e['named_in_problem_statement'] else ''}"
            )
        print(f"\n  {len(EVENTS)} events. Source: {SOURCE}")
        return 0

    e = get(args.event_id)
    if e is None:
        print(f"unknown event {args.event_id!r}", file=sys.stderr)
        return 1
    print(json.dumps(e, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
