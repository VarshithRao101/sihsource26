"""
modules/01_geodata/rivers.py - the searchable Indian river index.

    python -m modules.01_geodata.rivers build
    python -m modules.01_geodata.rivers search --q godavari
    python -m modules.01_geodata.rivers show godavari

WHY THIS EXISTS. Problem statement 26161 is titled "Dam Break Inundation
Modelling Using Hydrodynamic Modelling of any River". It asks for "dam break /
river blockage analysis", and FOUR OF THE FIVE events it names in its Background
are natural dams on rivers rather than engineered dam failures - Rishi Ganga,
Wapriyang, Phuktal, Kosi. The physics for that has been here all along:
domain.plan_domain traces the channel by D8 from any coordinate, and
failure_mode="blockage_breach" reads the impounded volume off the DEM. What was
missing was the way IN. Every entry point was a dam_id, so a river with no dam
on it could not be selected at all.

WHAT THIS IS NOT, and the distinction matters if a juror asks. This is not a
river NETWORK. We hold no channel geometry dataset - no HydroRIVERS, no OSM
waterways layer. This is an INDEX from river names to points whose coordinates
we actually have, and every one of those coordinates is a dam in the CWC
National Register of Large Dams 2019, read out of its "river" column. The
channel itself is still traced from the DEM at run time, which is what makes
the "any river" claim true: give the tracer a coordinate on water and it finds
the channel, whether or not that river is in this index.

So the index answers "where can I start on the Godavari?" and not "where does
the Godavari go". Four consequences, all stated rather than hidden:

  * A river with no large dam on it is NOT in here. The Rishi Ganga is not,
    because NRLD lists no dam on it. For those, enter a coordinate directly -
    that path is unchanged and is how latatapovanntpc_blockage_fast_001 was run.
  * Entries are grouped on name AND BASIN, because Indian river names repeat
    heavily. Grouping on the name alone produced a single "Ghataprabha" of 54
    points spanning 13 basins and latitudes 8.7 to 34.1 - Kerala to Kashmir.
    That is a name collision, not a river. With the basin it becomes 24 points
    in the Krishna basin, which is where the Ghataprabha actually is.
  * Spelling variants are NOT merged beyond case and spacing. NRLD writes
    "Ghataprabha", "Ghatprabha" and "GHARAPARBHA", and only the last two differ
    by letters, so they stay separate. Deciding which near-identical names mean
    one river needs a gazetteer we do not have, and guessing would be inventing
    geography. Every spelling seen is kept on the record so a search on any of
    them finds the river.
  * Even inside one basin, points can be hundreds of km apart.
    `points_may_not_be_one_channel` is set above a 400 km span so nobody assumes
    two points sit on one continuous reach. 21 of 2,229 rivers carry it, and on
    the Tapi and the Godavari it is simply true - they are that long.

Placeholder river names are dropped. NRLD uses "Local Nalla" 1,302 times as a
stand-in for "a small stream we did not name"; it is not a river and would sit
at the top of every search.

Owner: captain (module 01).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RIVERS_DIR = REPO_ROOT / "data" / "rivers"
CATALOGUE_JSON = RIVERS_DIR / "rivers.json"

SOURCE = "CWC National Register of Large Dams, 2019 (river column)"

# NRLD's stand-ins for "a stream we did not name". These are not rivers, and
# "Local Nalla" alone accounts for 1,302 dams - it would dominate every search.
_PLACEHOLDER_RE = re.compile(
    r"^(local\b.*|n\.?a\.?|na|nil|none|-+|unnamed.*|nala|nalla|nallah|nalah"
    r"|stream|river|canal|tank|drain|rivulet|stream\s*/\s*nala)$",
    re.IGNORECASE,
)

_CATALOGUE: list[dict] | None = None


def _clean(name: str) -> str:
    """Collapse whitespace and strip decoration, without changing the spelling.

    Deliberately conservative: case and spacing are normalised, letters are
    not. "Ghataprabha" and "Ghatprabha" stay distinct - see the module
    docstring.
    """
    s = re.sub(r"\s+", " ", (name or "").strip())
    s = s.strip(" .,;:-_/\\|()[]")
    return s


def _river_id(name: str) -> str:
    """A stable, URL-safe id. Same shape as a run_id's site component."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def is_placeholder(name: str) -> bool:
    """True for NRLD's non-names, which must not enter the index."""
    s = _clean(name)
    if not s:
        return True
    return bool(_PLACEHOLDER_RE.match(s))


def _span_km(lats: list[float], lons: list[float]) -> float:
    """Rough diagonal of the bounding box, in km. A sanity check, not geodesy."""
    import math

    dlat = (max(lats) - min(lats)) * 110.6
    mid = math.radians((max(lats) + min(lats)) / 2.0)
    dlon = (max(lons) - min(lons)) * 111.3 * math.cos(mid)
    return round(math.hypot(dlat, dlon), 1)


def build(write: bool = True) -> list[dict]:
    """Derive the river index from the dam catalogue.

    GROUPED ON (river name, BASIN), and the basin half is not optional. Indian
    river names repeat heavily across the country, and grouping on the name
    alone merged rivers that have nothing to do with each other: "Ghataprabha"
    came out as one 54-point river spanning THIRTEEN basins and a bounding box
    from latitude 8.7 to 34.1 - Kerala to Kashmir. That is not a river, it is a
    name collision, and routing a flood down it would have been meaningless.

    Basin is a real hydrological unit and NRLD carries it per dam. Two dams on
    a same-named river in the same basin are almost certainly on one river; in
    different basins they are certainly not. So the Godavari stays one entry
    across four states, and "Ghataprabha" correctly becomes several.

    Points are ordered north to south, which on most Indian rivers is roughly
    upstream to downstream and is at least stable between builds.
    """
    from importlib import import_module

    dams = import_module("modules.01_geodata.dams").load_catalogue()

    grouped: dict[tuple[str, str], dict] = {}
    for d in dams:
        raw = d.get("river") or ""
        if is_placeholder(raw):
            continue
        if not d.get("has_coords"):
            continue
        name = _clean(raw)
        nid = _river_id(name)
        if not nid:
            continue
        basin = _clean(d.get("basin") or "")
        key = (nid, _river_id(basin))

        rec = grouped.setdefault(
            key,
            {
                "name_id": nid,
                "basin": basin,
                # Every raw spelling seen, so the display name can be the one
                # NRLD uses most often rather than whichever row came first.
                "spellings": {},
                "states": set(),
                "points": [],
                "source": SOURCE,
            },
        )
        rec["spellings"][name] = rec["spellings"].get(name, 0) + 1
        if d.get("state"):
            rec["states"].add(d["state"])
        rec["points"].append(
            {
                "name": d["name"],
                "lat": d["lat"],
                "lon": d["lon"],
                "state": d.get("state", ""),
                "dam_id": d.get("id"),
                "dam_height_m": d.get("height_m"),
            }
        )

    out: list[dict] = []
    for (nid, bid), rec in grouped.items():
        pts = sorted(rec["points"], key=lambda p: -p["lat"])
        lats = [p["lat"] for p in pts]
        lons = [p["lon"] for p in pts]
        span = _span_km(lats, lons)
        # NRLD spells the same river several ways. Show the commonest, and
        # keep the rest so a search on any of them still finds it.
        display = max(rec["spellings"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        out.append(
            {
                "id": f"{nid}-{bid}" if bid else nid,
                "name": display,
                "spellings": sorted(rec["spellings"]),
                "basin": rec["basin"],
                "states": sorted(rec["states"]),
                "point_count": len(pts),
                "bbox": [min(lons), min(lats), max(lons), max(lats)],
                "span_km": span,
                # Even inside one basin a name can repeat. 400 km is far enough
                # apart that we say so rather than let the operator assume the
                # points are on one continuous channel.
                "points_may_not_be_one_channel": span > 400.0,
                "points": pts,
                "source": rec["source"],
            }
        )

    # Most points first: a river the register knows well is the one an operator
    # is most likely to want, and it is also the one we can place a blockage on
    # with the most choice.
    out.sort(key=lambda r: (-r["point_count"], r["name"]))

    if write:
        RIVERS_DIR.mkdir(parents=True, exist_ok=True)
        with open(CATALOGUE_JSON, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
    return out


def load_catalogue() -> list[dict]:
    """Read the index, building it once if it has never been built."""
    global _CATALOGUE
    if _CATALOGUE is None:
        if not CATALOGUE_JSON.exists():
            _CATALOGUE = build(write=True)
        else:
            with open(CATALOGUE_JSON, "r", encoding="utf-8") as fh:
                _CATALOGUE = json.load(fh)
    return _CATALOGUE


def states() -> list[str]:
    """Every state that has at least one indexed river."""
    out: set[str] = set()
    for r in load_catalogue():
        out.update(r["states"])
    return sorted(out)


def search(
    q: str | None = None,
    state: str | None = None,
    min_points: int = 1,
    limit: int = 200,
) -> list[dict]:
    """Filter the index. Returns summaries - call get() for the points."""
    rows = load_catalogue()
    if state:
        rows = [r for r in rows if state in r["states"]]
    if q:
        needle = q.lower()
        rows = [
            r for r in rows
            if needle in r["name"].lower()
            or needle in (r["basin"] or "").lower()
            or any(needle in s.lower() for s in r.get("spellings", []))
        ]
    rows = [r for r in rows if r["point_count"] >= min_points]
    return [{k: v for k, v in r.items() if k != "points"} for r in rows[:limit]]


def get(river_id: str) -> dict | None:
    """One river, with every point we hold on it.

    Matched against the stored id as-is. Do NOT put it through _river_id():
    that strips non-alphanumerics, and the id's own "name-basin" hyphen is one
    of them, so "godavari-godavari" would be looked up as "godavarigodavari"
    and never match anything.
    """
    rid = (river_id or "").strip().lower()
    for r in load_catalogue():
        if r["id"] == rid:
            return r
    return None


def point(river_id: str, index: int = 0) -> dict | None:
    """One coordinate on a river, by position in its point list."""
    r = get(river_id)
    if r is None or not r["points"]:
        return None
    if not 0 <= index < len(r["points"]):
        return None
    return r["points"][index]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.01_geodata.rivers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="derive the index from the dam catalogue")

    s = sub.add_parser("search", help="find a river")
    s.add_argument("--q")
    s.add_argument("--state")
    s.add_argument("--min-points", type=int, default=1)
    s.add_argument("--limit", type=int, default=25)

    sh = sub.add_parser("show", help="one river and its points")
    sh.add_argument("river_id")

    args = ap.parse_args(argv)

    if args.cmd == "build":
        rows = build()
        pts = sum(r["point_count"] for r in rows)
        print(f"{len(rows):,} rivers, {pts:,} points -> {CATALOGUE_JSON}")
        print(f"source: {SOURCE}")
        print("\ntop by points held:")
        for r in rows[:12]:
            print(f"  {r['name'][:28]:<28} {r['point_count']:>4}  "
                  f"{r['basin'][:34]:<34} {', '.join(r['states'])[:26]}")
        return 0

    if args.cmd == "search":
        rows = search(q=args.q, state=args.state,
                      min_points=args.min_points, limit=args.limit)
        if not rows:
            print("no rivers matched")
            return 1
        print(f"{'river':<30} {'pts':>4}  states")
        print(f"{'-' * 30} {'-' * 4}  {'-' * 40}")
        for r in rows:
            print(f"  {r['name'][:28]:<28} {r['point_count']:>4}  "
                  f"{', '.join(r['states'])[:40]}")
        return 0

    r = get(args.river_id)
    if r is None:
        print(f"unknown river {args.river_id!r}")
        return 1
    print(f"{r['name']}  ({r['id']})")
    print(f"  states  {', '.join(r['states'])}")
    print(f"  basins  {', '.join(r['basins'])}")
    print(f"  bbox    {r['bbox']}")
    print(f"  source  {r['source']}")
    print(f"\n  {r['point_count']} point(s), north to south:")
    for i, p in enumerate(r["points"]):
        print(f"    [{i:>2}] {p['name'][:32]:<32} {p['lat']:>9.4f} {p['lon']:>9.4f}  "
              f"{p['state']}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main())
