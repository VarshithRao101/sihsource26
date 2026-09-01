"""
modules/01_geodata/dams.py - the searchable Indian dam catalogue.

This is what makes "pick a dam and press run" real. Nothing is typed in by
hand: the catalogue is extracted from the Central Water Commission's National
Register of Large Dams 2019, which is the official list of every large dam in
India, and it carries exactly the fields the simulator needs - name, state,
river, nearest city, latitude, longitude, height above lowest foundation, and
gross storage capacity.

    python -m modules.01_geodata.dams build     # parse the PDF once
    python -m modules.01_geodata.dams search --state Sikkim

The output is data/dams/dams.geojson (map layer) and data/dams/dams.json
(search index). Both are small and committed, so nobody else has to re-parse
278 pages of PDF.

A note on districts. NRLD does not carry a district column - it has "Nearest
City". So the filter chain is State -> Nearest City -> Dam. Adding true
districts means a district boundary layer and a point-in-polygon join; the
hook for that is district_from_point() below, unimplemented until we have the
boundaries, because guessing a district from a city name would put wrong
administrative information in front of a district officer.

Owner: captain (module 01).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DAMS_DIR = REPO_ROOT / "data" / "dams"
NRLD_PDF = DAMS_DIR / "National Register of Large Dams, 2019.pdf"
CATALOGUE_JSON = DAMS_DIR / "dams.json"
CATALOGUE_GEOJSON = DAMS_DIR / "dams.geojson"

# The 20-column layout of the per-state dam tables in NRLD 2019.
COLUMNS = (
    "sr_no", "pic", "name", "operator", "lat_dms", "lon_dms", "year",
    "basin", "river", "nearest_city", "seismic_zone", "dam_type",
    "height_m", "length_m", "volume_content_m3", "gross_storage_m3",
    "reservoir_area_m2", "effective_storage_m3", "purpose", "spillway_cumecs",
)

# States and union territories, matched against the page header.
STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir",
    "Jammu & Kashmir", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar", "Chandigarh", "Dadra and Nagar Haveli",
    "Daman and Diu", "Delhi", "Lakshadweep", "Puducherry",
)


# ==========================================================================
# Parsing
# ==========================================================================


def parse_dms(text: str) -> float | None:
    """Convert a NRLD coordinate to decimal degrees.

    NRLD writes coordinates as degrees/minutes/seconds and the degree symbol
    survives PDF extraction as a replacement character, so the parser keys on
    the quote marks and the digits rather than on the symbol:

        13* 51 ' 42"   ->  13.861667

    Returns None when the field is blank or unparseable. None is correct here -
    a dam with no coordinate cannot be simulated, and inventing one would put a
    fictional dam on a map.
    """
    if not text:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if not nums:
        return None
    deg = float(nums[0])
    minutes = float(nums[1]) if len(nums) > 1 else 0.0
    seconds = float(nums[2]) if len(nums) > 2 else 0.0
    value = deg + minutes / 60.0 + seconds / 3600.0
    return round(value, 6)


def _num(text: str) -> float | None:
    """A NRLD numeric cell, or None. Blanks and '--' are common and meaningful."""
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    if cleaned in ("", "-", "--", "NA", "N.A."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _state_from_page(page_text: str) -> str | None:
    """The state a listing page belongs to, from its header line."""
    head = (page_text or "")[:200]
    for state in STATES:
        if state.lower() in head.lower():
            return "Odisha" if state == "Orissa" else state
    return None


def parse_nrld(pdf_path: Path = NRLD_PDF, start_page: int = 38) -> list[dict]:
    """Extract every dam row from the NRLD PDF.

    Args:
        pdf_path: the CWC register.
        start_page: the per-state listing begins around page 38; earlier pages
            are the summary tables, which have a different and incompatible
            layout.

    Returns:
        One dict per dam. Rows without a usable name are dropped; rows without
        coordinates are KEPT but flagged, because they are still real dams and
        the count matters when we say how many are in the catalogue.
    """
    import pdfplumber

    if not pdf_path.exists():
        raise FileNotFoundError(
            f"NRLD not found at {pdf_path}. Download it from https://damsafety.in/"
        )

    dams: list[dict] = []
    current_state: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_no in range(start_page, len(pdf.pages)):
            page = pdf.pages[page_no]
            state = _state_from_page(page.extract_text() or "")
            if state:
                current_state = state

            for table in page.extract_tables():
                if not table or len(table[0]) != len(COLUMNS):
                    continue
                for row in table:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    if cells[0] in ("Sr.No", "") or not cells[0][:1].isdigit():
                        continue

                    rec = dict(zip(COLUMNS, cells))
                    name = rec["name"].strip()
                    if not name or name in ("--", "-"):
                        continue

                    lat = parse_dms(rec["lat_dms"])
                    lon = parse_dms(rec["lon_dms"])

                    # India spans roughly 6-38 N and 68-98 E. A coordinate
                    # outside that is a parse failure, not a dam in the ocean.
                    if lat is not None and not (6.0 <= lat <= 38.0):
                        lat = None
                    if lon is not None and not (68.0 <= lon <= 98.0):
                        lon = None

                    gross = _num(rec["gross_storage_m3"])
                    dams.append(
                        {
                            "id": rec["pic"] or f"NRLD{len(dams):05d}",
                            "name": name,
                            "state": current_state or "",
                            "river": rec["river"],
                            "basin": rec["basin"],
                            "nearest_city": rec["nearest_city"],
                            "lat": lat,
                            "lon": lon,
                            "year": _num(rec["year"]),
                            "dam_type": rec["dam_type"],
                            "height_m": _num(rec["height_m"]),
                            "length_m": _num(rec["length_m"]),
                            "gross_storage_mcm": round(gross / 1e6, 4) if gross else None,
                            "purpose": rec["purpose"],
                            "spillway_cumecs": _num(rec["spillway_cumecs"]),
                            "has_coords": lat is not None and lon is not None,
                            "source": "CWC National Register of Large Dams, 2019",
                        }
                    )
    return dams


# ==========================================================================
# Building and loading the catalogue
# ==========================================================================


def build_catalogue(pdf_path: Path = NRLD_PDF) -> dict:
    """Parse the register and write dams.json + dams.geojson.

    Returns a summary dict; print it, because the counts are the honest
    statement of what the catalogue can and cannot do.
    """
    dams = parse_nrld(pdf_path)
    runnable = [d for d in dams if d["has_coords"] and d["height_m"] and d["gross_storage_mcm"]]

    DAMS_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_JSON.write_text(
        json.dumps({"dams": dams, "source": "CWC NRLD 2019"}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )

    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
                "properties": {k: v for k, v in d.items() if k not in ("lat", "lon")},
            }
            for d in dams
            if d["has_coords"]
        ],
    }
    CATALOGUE_GEOJSON.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    states = sorted({d["state"] for d in dams if d["state"]})
    return {
        "total_dams": len(dams),
        "with_coordinates": sum(1 for d in dams if d["has_coords"]),
        "simulatable": len(runnable),
        "states": len(states),
        "state_list": states,
        "json": str(CATALOGUE_JSON),
        "geojson": str(CATALOGUE_GEOJSON),
    }


_CACHE: list[dict] | None = None


def load_catalogue() -> list[dict]:
    """Every dam, from the built catalogue. Cached in process."""
    global _CACHE
    if _CACHE is None:
        if not CATALOGUE_JSON.exists():
            raise FileNotFoundError(
                f"{CATALOGUE_JSON} not built yet. Run:\n"
                f"  python -m modules.01_geodata.dams build"
            )
        _CACHE = json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))["dams"]
    return _CACHE


def states() -> list[str]:
    """States that have at least one simulatable dam."""
    return sorted({d["state"] for d in load_catalogue() if d["state"] and d["has_coords"]})


def cities(state: str) -> list[str]:
    """Nearest-city values within a state, for the second filter level."""
    return sorted(
        {
            d["nearest_city"].strip()
            for d in load_catalogue()
            if d["state"] == state and d["nearest_city"].strip() and d["has_coords"]
        }
    )


def search(
    state: str | None = None,
    city: str | None = None,
    q: str | None = None,
    simulatable_only: bool = True,
    limit: int = 500,
) -> list[dict]:
    """Filter the catalogue.

    Args:
        state: exact state name.
        city: exact nearest-city name.
        q: case-insensitive substring across name, river and city.
        simulatable_only: keep only dams with coordinates, a height and a
            storage capacity - the three things a scenario cannot run without.
        limit: cap the result size.

    Returns:
        Matching dams, largest reservoir first, so the interesting ones are on
        top rather than whichever happened to be listed first.
    """
    rows = load_catalogue()
    if state:
        rows = [d for d in rows if d["state"] == state]
    if city:
        rows = [d for d in rows if d["nearest_city"].strip().lower() == city.strip().lower()]
    if q:
        needle = q.lower()
        rows = [
            d
            for d in rows
            if needle in d["name"].lower()
            or needle in (d["river"] or "").lower()
            or needle in (d["nearest_city"] or "").lower()
        ]
    if simulatable_only:
        rows = [d for d in rows if d["has_coords"] and d["height_m"] and d["gross_storage_mcm"]]

    rows = sorted(rows, key=lambda d: -(d["gross_storage_mcm"] or 0.0))
    return rows[:limit]


def get(dam_id: str) -> dict | None:
    for d in load_catalogue():
        if d["id"] == dam_id:
            return d
    return None


def district_from_point(lat: float, lon: float) -> str | None:
    """Not implemented, on purpose.

    NRLD has no district column. Deriving one needs a district boundary layer
    and a point-in-polygon join - Survey of India or the Census 2011 district
    shapefile. Until that layer is on disk this returns None rather than
    guessing from the nearest city, because a wrong district in front of a
    district administrator is worse than no district at all.
    """
    return None


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m modules.01_geodata.dams")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="parse the NRLD PDF into the catalogue")

    s = sub.add_parser("search", help="query the catalogue")
    s.add_argument("--state")
    s.add_argument("--city")
    s.add_argument("-q")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--all", action="store_true", help="include non-simulatable dams")

    sub.add_parser("states", help="list states")

    args = parser.parse_args(argv)

    if args.cmd == "build":
        summary = build_catalogue()
        print(json.dumps({k: v for k, v in summary.items() if k != "state_list"}, indent=2))
        print("states:", ", ".join(summary["state_list"]))
        return 0

    if args.cmd == "states":
        for st in states():
            print(f"  {st}")
        return 0

    rows = search(
        state=args.state, city=args.city, q=args.q,
        simulatable_only=not args.all, limit=args.limit,
    )
    print(f"{len(rows)} dam(s)")
    for d in rows:
        print(
            f"  {d['name'][:34]:<34} {d['state'][:16]:<16} {d['river'][:16]:<16} "
            f"h={d['height_m'] or '?':>6}m  {d['gross_storage_mcm'] or '?':>10} MCM  "
            f"{d['lat']},{d['lon']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
