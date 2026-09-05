"""
modules/01_geodata/natural_dams.py - natural dams and moraine-dammed lakes.

    python -m modules.01_geodata.natural_dams build   # parse the PDF once
    python -m modules.01_geodata.natural_dams list
    python -m modules.01_geodata.natural_dams list --state Sikkim

WHY THIS EXISTS. dams.py indexes the CWC National Register of Large Dams, which
by definition contains only engineered dams. The problem statement is about the
sudden release of water from ANY impoundment, and four of the five failures it
names in its Background are natural: a landslide barrier, a moraine-dammed
glacial lake, a debris choke. None of those is in the CWC register and none of
them ever will be, because nobody registers a moraine.

For a while this repository handled that with a third picker - a short list of
seven historic events, sitting beside the dam picker and the river picker. That
was a mistake in the interface rather than in the data: an operator does not
think "am I looking at an engineered dam or a natural one", they think "there is
a body of water above a valley with people in it". A moraine dam IS a dam. So
this catalogue is merged into the dam catalogue (see dams.py load_catalogue) and
the historic events are merged into it too, with `kind` telling them apart.

WHERE THE NUMBERS COME FROM, and how far they can be trusted, because the two
groups in the source PDF are not equally solid:

  * GROUND-SURVEYED BENCHMARK (8 lakes). Heights are surveyed. `height_source`
    is "surveyed".
  * SATELLITE-MAPPED (48 lakes, by state). The source PDF says in its own
    closing note that the heights for this category are "assumed/estimated
    values". They are carried because a barrier height is what decides the
    impounded volume and an estimate beats refusing to model the lake at all -
    but `height_source` is "estimated" on every one of them, that flag reaches
    meta.json, and the UI shows it. Nobody should quote one as a measurement.

WHAT IS DELIBERATELY ABSENT. There is no storage capacity here, for any of the
56. A natural dam has no design storage, and inventing one would be the single
worst number this file could carry - it feeds the breach regression directly.
`gross_storage_mcm` is None throughout, and the run reads the impounded volume
off the terrain instead (modules/04_backend/blockage.py). That is slower and it
is the only defensible way to get the number.

River names are also absent unless the source named one. A glacial lake in the
Sikkim Himalaya drains into SOME channel, but which channel is a question for
the DEM, not for a guess in a data file.

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
SOURCE_PDF = DAMS_DIR / "natural_dam_coordinates_heights.pdf"
CATALOGUE_JSON = DAMS_DIR / "natural_dams.json"

SOURCE = (
    "Natural Dam / Lake Coordinates & Dam Heights, supplied reference dataset; "
    "benchmark group ground-surveyed, remainder satellite-mapped with "
    "assumed/estimated heights"
)

# Section headings in the PDF, mapped to the state they belong to. The first
# group is not a state - it is a quality tier that spans several states, so its
# members get their state from their coordinates instead (see _STATE_BOXES).
_SECTIONS = {
    "Ground-Surveyed Benchmark Natural Dams": (None, "surveyed"),
    "Sikkim": ("Sikkim", "estimated"),
    "Himachal Pradesh": ("Himachal Pradesh", "estimated"),
    "Uttarakhand": ("Uttarakhand", "estimated"),
    "Arunachal Pradesh": ("Arunachal Pradesh", "estimated"),
    "Ladakh & Jammu and Kashmir": ("Ladakh / Jammu and Kashmir", "estimated"),
}

# Crude lat/lon boxes, used ONLY to give the eight benchmark lakes a state,
# because the source lists them in a quality tier rather than by state. A box
# is enough for that and is not used for anything else - no run reads it.
_STATE_BOXES = (
    # (name,                       lat_min, lat_max, lon_min, lon_max)
    ("Himachal Pradesh",              30.3,   33.3,    75.5,   79.0),
    ("Uttarakhand",                   28.7,   31.5,    77.5,   81.1),
    ("Sikkim",                        27.0,   28.2,    88.0,   88.9),
    ("Arunachal Pradesh",             26.6,   29.5,    91.5,   97.5),
    ("Jammu and Kashmir",             32.2,   35.0,    73.8,   75.5),
    ("Ladakh / Jammu and Kashmir",    32.2,   36.0,    75.5,   80.0),
)

# A row is: index, name, "32.5160° N", "77.2210° E", height - one row per line.
# The degree sign does not survive extraction as a degree sign (it arrives as a
# replacement character), so the pattern accepts any run of non-space,
# non-digit characters between the number and its hemisphere letter rather
# than matching the character we wish were there.
_ROW = re.compile(
    r"^(\d{1,3})\s+"                      # No.
    r"(.+?)\s+"                           # Dam / Lake Name
    r"([\d.]+)\s*[^\s\d]*\s*([NS])\s+"    # Latitude
    r"([\d.]+)\s*[^\s\d]*\s*([EW])\s+"    # Longitude
    r"([\d.]+)\s*$",                      # Dam Height (m)
    re.MULTILINE,
)


def _state_from_point(lat: float, lon: float) -> str:
    for name, la0, la1, lo0, lo1 in _STATE_BOXES:
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return name
    return ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_pdf(pdf_path: Path = SOURCE_PDF) -> list[dict]:
    """Read every lake out of the supplied PDF.

    pdfplumber emits one table row per line, so the whole document is treated
    as one text blob and rows are matched line by line. The PDF has no ruling
    lines, so there is nothing to key a column parser off - the row shape
    itself (index, name, lat, lon, height) is what identifies a row.
    """
    import pdfplumber

    if not pdf_path.is_file():
        raise FileNotFoundError(f"{pdf_path} not found")

    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Split the blob at each section heading, keeping the heading with the
    # block that follows it, so every row knows which tier it came from.
    marks: list[tuple[int, str]] = []
    for heading in _SECTIONS:
        for m in re.finditer(rf"^{re.escape(heading)}\s*$", text, re.MULTILINE):
            marks.append((m.start(), heading))
    marks.sort()
    if not marks:
        raise ValueError(f"no known section headings found in {pdf_path}")

    rows: list[dict] = []
    seen: set[str] = set()
    for i, (pos, heading) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        state, height_source = _SECTIONS[heading]
        for m in _ROW.finditer(text[pos:end]):
            _, name, lat_s, ns, lon_s, ew, height_s = m.groups()
            name = name.strip()
            if not name or name.lower() in ("dam / lake name", "no."):
                continue
            lat = float(lat_s) * (-1 if ns == "S" else 1)
            lon = float(lon_s) * (-1 if ew == "W" else 1)

            dam_id = "NAT" + _slug(name).upper()[:16]
            if dam_id in seen:            # two lakes, one truncated slug
                dam_id = f"{dam_id}{len(seen):02d}"
            seen.add(dam_id)

            rows.append(
                {
                    "id": dam_id,
                    "name": name,
                    "state": state or _state_from_point(lat, lon),
                    # Not guessed. A glacial lake drains into some channel, but
                    # which one is a DEM question, not a data-file question.
                    "river": "",
                    "basin": "",
                    "nearest_city": "",
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "year": None,
                    "dam_type": "Natural - moraine / debris impoundment",
                    "height_m": float(height_s),
                    "height_source": height_source,
                    "length_m": None,
                    # None, on purpose. See the module docstring: a natural dam
                    # has no design storage and the DEM supplies the volume.
                    "gross_storage_mcm": None,
                    "purpose": "",
                    "spillway_cumecs": None,
                    "has_coords": True,
                    "kind": "natural",
                    "default_failure_mode": "glof_moraine",
                    "group": heading,
                    "source": SOURCE,
                }
            )
    return rows


def _historic_events() -> list[dict]:
    """The seven historic natural-dam failures, as catalogue rows.

    These used to live behind their own picker. They are natural dams with
    coordinates and a barrier height, which is exactly what this catalogue
    holds, so they belong in it - with `historic` set, because the extra thing
    they carry is that they are known to have failed, and a demonstration that
    picks one should be able to say so.
    """
    from . import events as ev

    rows = []
    for e in ev.EVENTS:
        rows.append(
            {
                "id": "HIST" + e["id"].upper(),
                "name": f"{e['name']} ({e['year']})",
                "state": e["state"],
                "river": e["river"],
                "basin": "",
                "nearest_city": "",
                "lat": e["lat"],
                "lon": e["lon"],
                "year": e["year"],
                "dam_type": "Natural - historic failure",
                "height_m": e["blockage_height_m"],
                "height_source": "reported",
                "length_m": None,
                "gross_storage_mcm": None,
                "reported_impoundment_mcm": e["reported_impoundment_mcm"],
                "purpose": "",
                "spillway_cumecs": None,
                "has_coords": True,
                "kind": "natural",
                "historic": True,
                "event_id": e["id"],
                "mechanism": e["mechanism"],
                "named_in_problem_statement": e["named_in_problem_statement"],
                "default_failure_mode": (
                    "glof_moraine" if "moraine" in e["mechanism"] else "blockage_breach"
                ),
                "group": "Historic natural-dam failures",
                "source": ev.SOURCE,
            }
        )
    return rows


def build(pdf_path: Path = SOURCE_PDF) -> dict:
    """Parse the PDF, fold in the historic events, write natural_dams.json."""
    lakes = parse_pdf(pdf_path)
    historic = _historic_events()
    rows = lakes + historic

    DAMS_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_JSON.write_text(
        json.dumps({"dams": rows, "source": SOURCE}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    by_group: dict[str, int] = {}
    for r in rows:
        by_group[r["group"]] = by_group.get(r["group"], 0) + 1
    return {
        "from_pdf": len(lakes),
        "historic_events": len(historic),
        "total": len(rows),
        "by_group": by_group,
        "surveyed_heights": sum(1 for r in rows if r["height_source"] == "surveyed"),
        "estimated_heights": sum(1 for r in rows if r["height_source"] == "estimated"),
        "json": str(CATALOGUE_JSON),
    }


_CACHE: list[dict] | None = None


def load_catalogue() -> list[dict]:
    """Every natural dam. Empty list if the catalogue has not been built.

    Returns empty rather than raising, because a missing natural-dam catalogue
    must not take the engineered dam picker down with it.
    """
    global _CACHE
    if _CACHE is None:
        if not CATALOGUE_JSON.exists():
            return []
        _CACHE = json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))["dams"]
    return _CACHE


def get(dam_id: str) -> dict | None:
    for d in load_catalogue():
        if d["id"] == dam_id:
            return d
    return None


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.01_geodata.natural_dams")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="parse the PDF and write natural_dams.json")
    ls = sub.add_parser("list", help="show the catalogue")
    ls.add_argument("--state", default=None)
    sh = sub.add_parser("show", help="one lake")
    sh.add_argument("dam_id")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        print(json.dumps(build(), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "show":
        row = get(args.dam_id)
        if row is None:
            print(f"unknown id {args.dam_id!r}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return 0

    rows = load_catalogue()
    if not rows:
        print("catalogue not built. Run:\n"
              "  python -m modules.01_geodata.natural_dams build", file=sys.stderr)
        return 1
    if args.state:
        rows = [r for r in rows if args.state.lower() in (r["state"] or "").lower()]
    print(f"  {'id':<20} {'name':<34} {'state':<26} {'height':>7}  source")
    print(f"  {'-'*20} {'-'*34} {'-'*26} {'-'*7}  {'-'*9}")
    for r in rows:
        print(f"  {r['id']:<20} {r['name'][:34]:<34} {(r['state'] or '?')[:26]:<26} "
              f"{r['height_m']:>6.0f}m  {r['height_source']}")
    print(f"\n  {len(rows)} natural dams. No storage capacity is carried for any of "
          f"them - the DEM supplies it.\n  Source: {SOURCE}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
