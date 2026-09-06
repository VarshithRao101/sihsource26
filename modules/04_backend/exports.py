"""
modules/04_backend/exports.py - what the operator actually walks away with.

Deliverable (iii) of problem statement 26161 ends "Output should be converted
to .shp or .Kml file." That sentence was satisfied for a long time by writing
the flood polygons out with five attributes attached, which is a file of the
right extension and very little else. A district administrator who opens that
in Google Earth sees a grey blob with no legend, no arrival times, no names,
and no way to tell a knife-edge volume from a solid one.

So the three exports are built here instead, and each one is a package rather
than a geometry dump:

    KML       styled by depth band, with the barrier, every affected
              settlement as a placemark carrying its arrival time, depth and
              hazard class, the roads that get cut, and a Document description
              that states the provenance and the limitations before anyone
              scrolls to the polygons.
    Shapefile a zip: the extent, the settlements as points, plus README.txt,
              meta.json, impact.json, uncertainty.json and hydrograph.csv. A
              GIS cell gets the layers AND the numbers behind them in one
              download.
    GeoJSON   the same polygons with real attributes and a top-level block of
              provenance, rather than the raw contract file.

THE ONE RULE HERE. Nothing in this file computes a result. Every number it
writes was read out of the run folder, and where a figure is an assumption or
a knife edge the export says so in the same place it shows the figure. An
export that quietly drops the caveat is worse than no export, because it is
the artefact that leaves the building.

Owner: captain (module 04).
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Depth banding - one definition, used by all three formats
# --------------------------------------------------------------------------
#
# The break points are the ones the impact model already uses to classify
# hazard, so the colours on a map and the words in the impact table cannot
# disagree. WET_THRESHOLD_M (0.05 m) is the bottom of the first band by
# definition: below it a cell is dry.

DEPTH_BANDS: list[tuple[float, float, str, str]] = [
    # low, high, label, KML colour (aabbggrr - KML is NOT rrggbb)
    (0.05, 0.5, "0.05 - 0.5 m  shallow", "7af0e6b4"),
    (0.5, 1.5, "0.5 - 1.5 m  wading depth", "8cf0c878"),
    (1.5, 3.0, "1.5 - 3 m  ground floor lost", "9ae6a032"),
    (3.0, 6.0, "3 - 6 m  first floor lost", "a5d2641e"),
    (6.0, 1e9, "over 6 m  structural", "b4a01414"),
]

HAZARD_COLOUR = {
    "extreme": "ff1414c8",
    "severe": "ff1e46e6",
    "significant": "ff28a0f0",
    "moderate": "ff50d2f0",
    "low": "ff78e6f0",
}


def _band_for(depth_m: float | None) -> tuple[str, str]:
    """(label, KML colour) for a depth. Unknown depth gets the shallow band."""
    d = float(depth_m or 0.0)
    for low, high, label, colour in DEPTH_BANDS:
        if low <= d < high:
            return label, colour
    return DEPTH_BANDS[0][2], DEPTH_BANDS[0][3]


def _xml(text: Any) -> str:
    """XML-escape. KML carries Indian place names, so this is not optional."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _num(v: Any, dp: int = 2, dash: str = "not computed") -> str:
    if v is None:
        return dash
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------
# The honesty block - assembled once, written into all three exports
# --------------------------------------------------------------------------


def caveats(meta: dict, uncertainty: dict | None, impact: dict | None) -> list[str]:
    """Everything about this run a reader must be told, in plain sentences.

    Assembled from what the run folder actually recorded. A caveat that does
    not apply to this run is not emitted - there is no boilerplate here - and
    one that does apply cannot be omitted, because the export reads it from
    the same JSON the console reads.
    """
    out: list[str] = []
    dem = meta.get("dem") or {}
    dom = meta.get("domain") or {}
    sc = meta.get("scenario") or {}
    res = meta.get("results") or {}
    blk = meta.get("blockage") or {}

    if meta.get("is_fake", False):
        out.append(
            "SYNTHETIC RUN. This did not model real terrain and must not be "
            "used as a result."
        )

    out.append(
        f"Terrain is {dem.get('source', 'unknown')} at "
        f"{_num(dem.get('native_resolution_m'), 0)} m native resolution, solved on a "
        f"{_num(dom.get('cellsize_m'), 0)} m grid "
        f"({dom.get('nx', '?')} x {dom.get('ny', '?')} cells). "
        f"There is no survey of the riverbed under the water: bathymetry is "
        f"'{dem.get('bathymetry', 'unknown')}'."
    )

    # Two modes have no barrier at all - a controlled release and a routed
    # river flood - and uncertainty.json says so instead of publishing breach
    # regressions computed on a placeholder height. Read its own note rather
    # than assuming every run breached something.
    breached = not (uncertainty or {}).get("scenario") in ("gated_release",
                                                           "river_flood")
    if breached and sc.get("breach_param_source"):
        out.append(
            f"Breach geometry from {sc['breach_param_source']}. Published "
            f"breach regressions disagree with each other by up to a factor of "
            f"ten; uncertainty.json carries all of them unaveraged."
        )
    if breached and uncertainty and uncertainty.get("envelope_ratio"):
        env = uncertainty.get("peak_envelope_cumecs") or []
        if len(env) == 2:
            out.append(
                f"Peak discharge envelope across the published regressions is "
                f"{_num(env[0], 0)} to {_num(env[1], 0)} m3/s, a factor of "
                f"{_num(uncertainty['envelope_ratio'], 1)}. The routed peak of "
                f"{_num(res.get('peak_discharge_cumecs'), 0)} m3/s is one point "
                f"inside that envelope, not the truth."
            )
    if uncertainty and uncertainty.get("note") and not breached:
        out.append(str(uncertainty["note"]))

    if blk.get("volume_is_knife_edge"):
        out.append(
            "IMPOUNDED VOLUME IS NOT IDENTIFIABLE. Rounding the conditioned "
            f"DEM by a quarter of a millimetre moves this lake from "
            f"{_num(blk.get('impounded_volume_mcm'))} to "
            f"{_num(blk.get('volume_mcm_under_perturbed_dem'))} MCM "
            f"({_num(blk.get('volume_swing_pct'), 0)}%), because it reroutes "
            "the drainage that defines the catchment. Read the volume, and "
            "every discharge derived from it, as an order of magnitude."
        )
    elif blk.get("volume_swing_pct") is not None:
        out.append(
            f"Impounded volume is stable: a quarter-millimetre DEM "
            f"perturbation moves it {_num(blk.get('volume_swing_pct'), 1)}%."
        )

    if impact:
        totals = impact.get("totals") or {}
        by_src = totals.get("population_by_source") or {}
        if by_src.get("class_default"):
            out.append(
                f"{by_src['class_default']:,} of the "
                f"{_num(totals.get('population_affected'), 0)} people counted "
                "carry a settlement-class DEFAULT population, not a measured "
                "one - WorldPop found no mapped buildings there."
            )
        if totals.get("damage_curve_source"):
            out.append(str(totals["damage_curve_source"]))

    mb = res.get("mass_balance_err_pct")
    if mb is not None:
        out.append(
            f"Mass balance error over the whole solve: {_num(mb, 3)}%. "
            "This measures the numerics, not the realism."
        )

    out.append(
        "No observed flood extent has been compared against this run unless a "
        "validation.json accompanies it. This is a scenario, not a hindcast."
    )
    return out


def headline(meta: dict, impact: dict | None) -> list[tuple[str, str]]:
    """The numbers a reader wants first, as (label, value) pairs."""
    res = meta.get("results") or {}
    site = meta.get("site") or {}
    sc = meta.get("scenario") or {}
    tot = (impact or {}).get("totals") or {}
    rows = [
        ("Site", site.get("name", "-")),
        ("River", site.get("river") or "-"),
        ("State", site.get("state") or "-"),
        ("Failure mode", sc.get("failure_mode", "-")),
        ("Peak discharge", f"{_num(res.get('peak_discharge_cumecs'), 0)} m3/s"),
        ("Maximum depth", f"{_num(res.get('max_depth_m'))} m"),
        ("Maximum velocity", f"{_num(res.get('max_velocity_ms'))} m/s"),
        ("Flooded area", f"{_num(res.get('flood_area_km2'))} km2"),
        ("Released volume", f"{_num(res.get('released_volume_mcm'), 3)} MCM"),
        ("Simulated", f"{_num((meta.get('time') or {}).get('end_hr'), 1)} hours "
                      f"from failure"),
    ]
    if tot:
        rows += [
            ("Settlements affected", _num(tot.get("settlements_affected"), 0)),
            ("People affected", _num(tot.get("population_affected"), 0)),
            ("Roads cut", f"{_num(tot.get('roads_cut_km'))} km"),
            ("Damage", f"Rs {_num(tot.get('damage_inr_crore'))} crore"),
        ]
    return rows


# --------------------------------------------------------------------------
# KML
# --------------------------------------------------------------------------


def build_kml(
    run_id: str,
    meta: dict,
    extent: dict,
    impact: dict | None,
    evacuation: dict | None,
    uncertainty: dict | None,
) -> str:
    """A styled KML that explains itself when it opens.

    Written by hand rather than through a driver because a driver writes
    geometry and nothing else: no styling, no folders, no balloon, no legend.
    Google Earth is what a district office actually has open, and what it shows
    on the first click is the whole value of this file.
    """
    site = meta.get("site") or {}
    res = meta.get("results") or {}
    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append("<Document>")
    parts.append(f"<name>{_xml(site.get('name', run_id))} - modelled flood extent</name>")

    # The description balloon. This is the first thing anyone reads, so the
    # headline numbers and the caveats are both in it, in that order.
    desc = ["<![CDATA[", "<h3>Modelled flood extent</h3>", "<table>"]
    for label, value in headline(meta, impact):
        desc.append(f"<tr><td><b>{_xml(label)}</b></td><td>{_xml(value)}</td></tr>")
    desc.append("</table>")
    desc.append("<h4>What this is, and what it is not</h4><ul>")
    for c in caveats(meta, uncertainty, impact):
        desc.append(f"<li>{_xml(c)}</li>")
    desc.append("</ul>")
    desc.append(f"<p><i>Run {_xml(run_id)}, engine {_xml(meta.get('engine'))}, "
                f"created {_xml(meta.get('created_utc'))}. "
                f"Problem statement 26161, NTRO.</i></p>")
    desc.append("]]>")
    parts.append("<description>" + "\n".join(desc) + "</description>")

    # Styles: one per depth band, one per hazard class, one for cut roads.
    for i, (_, _, label, colour) in enumerate(DEPTH_BANDS):
        parts.append(
            f'<Style id="band{i}"><LineStyle><color>ff{colour[2:]}</color>'
            f"<width>1</width></LineStyle>"
            f"<PolyStyle><color>{colour}</color></PolyStyle></Style>"
        )
    for name, colour in HAZARD_COLOUR.items():
        parts.append(
            f'<Style id="haz_{name}"><IconStyle><color>{colour}</color>'
            f"<scale>1.1</scale><Icon><href>"
            f"http://maps.google.com/mapfiles/kml/shapes/caution.png"
            f"</href></Icon></IconStyle></Style>"
        )
    parts.append(
        '<Style id="barrier"><IconStyle><scale>1.3</scale><Icon><href>'
        "http://maps.google.com/mapfiles/kml/shapes/water.png"
        "</href></Icon></IconStyle></Style>"
    )
    parts.append(
        '<Style id="cutroad"><LineStyle><color>ff0000ff</color>'
        "<width>3</width></LineStyle></Style>"
    )

    # --- the barrier itself -------------------------------------------------
    if site.get("lat") is not None and site.get("lon") is not None:
        parts.append("<Folder><name>Source</name>")
        parts.append(
            "<Placemark><styleUrl>#barrier</styleUrl>"
            f"<name>{_xml(site.get('name', 'source'))}</name>"
            "<description><![CDATA["
            f"<b>{_xml((meta.get('scenario') or {}).get('failure_mode', ''))}</b><br/>"
            f"height {_xml(_num(site.get('dam_height_m'), 1))} m, "
            f"source {_xml(site.get('source', ''))}<br/>"
            f"peak outflow {_xml(_num(res.get('peak_discharge_cumecs'), 0))} m3/s"
            "]]></description>"
            f"<Point><coordinates>{float(site['lon']):.6f},"
            f"{float(site['lat']):.6f},0</coordinates></Point></Placemark>"
        )
        parts.append("</Folder>")

    # --- the extent, banded by depth ---------------------------------------
    parts.append("<Folder><name>Flood extent</name>")
    parts.append("<description>Polygons are the modelled maximum extent over "
                 "the whole simulation, not a snapshot at one instant."
                 "</description>")
    for feat in extent.get("features", []):
        props = feat.get("properties") or {}
        depth = props.get("max_depth_m", props.get("min_depth_m"))
        label, _ = _band_for(depth)
        band_i = next(
            (i for i, (lo, hi, _, _) in enumerate(DEPTH_BANDS)
             if lo <= float(depth or 0) < hi),
            0,
        )
        area = props.get("area_km2")
        parts.append(
            f'<Placemark><styleUrl>#band{band_i}</styleUrl>'
            f"<name>{_xml(label)}</name>"
            "<description><![CDATA["
            f"area {_xml(_num(area, 4))} km2<br/>"
            f"wet threshold {_xml(_num(props.get('min_depth_m'), 2))} m"
            "]]></description>"
            + _geom_kml(feat.get("geometry") or {})
            + "</Placemark>"
        )
    parts.append("</Folder>")

    # --- settlements --------------------------------------------------------
    evac_by_name = {
        s.get("name"): s for s in ((evacuation or {}).get("settlements") or [])
    }
    settlements = (impact or {}).get("settlements") or []
    if settlements:
        parts.append("<Folder><name>Settlements affected</name>")
        for s in settlements:
            ev = evac_by_name.get(s.get("name")) or {}
            haz = str(s.get("hazard_class", "moderate"))
            body = [
                "<![CDATA[",
                f"<b>Water arrives</b> {_xml(_num(s.get('arrival_hr'), 2))} hours "
                "after failure<br/>",
                f"<b>Maximum depth</b> {_xml(_num(s.get('max_depth_m')))} m<br/>",
                f"<b>Maximum velocity</b> {_xml(_num(s.get('max_velocity_ms')))} "
                "m/s<br/>",
                f"<b>Hazard class</b> {_xml(haz)}<br/>",
                f"<b>Population</b> {_xml(_num(s.get('population'), 0))} "
                f"({_xml(s.get('population_source', 'unknown'))})<br/>",
                f"<b>Houses affected</b> {_xml(_num(s.get('houses_affected'), 0))}"
                "<br/>",
            ]
            if ev.get("note"):
                body.append(f"<p><i>{_xml(ev['note'])}</i></p>")
            body.append("]]>")
            parts.append(
                f'<Placemark><styleUrl>#haz_{haz if haz in HAZARD_COLOUR else "moderate"}'
                f'</styleUrl><name>{_xml(s.get("name", "unnamed"))}</name>'
                "<description>" + "".join(body) + "</description>"
                f"<Point><coordinates>{float(s['lon']):.6f},"
                f"{float(s['lat']):.6f},0</coordinates></Point></Placemark>"
            )
        parts.append("</Folder>")

    # --- roads cut ----------------------------------------------------------
    roads = (impact or {}).get("roads") or []
    named = [r for r in roads if r.get("name")]
    if roads:
        parts.append("<Folder><name>Roads cut</name>")
        parts.append(
            "<description><![CDATA["
            f"{len(roads)} road segments are inundated, "
            f"{_xml(_num(((impact or {}).get('totals') or {}).get('roads_cut_km')))} km "
            "in total. Geometry is not carried in this export - the segment "
            "list is in impact.json, and the shapefile package carries it. "
            f"Named roads: {_xml(', '.join(r['name'] for r in named[:12]) or 'none')}"
            "]]></description>"
        )
        parts.append("</Folder>")

    parts.append("</Document></kml>")
    return "\n".join(parts)


def _geom_kml(geom: dict) -> str:
    """GeoJSON geometry to KML. Polygons and MultiPolygons only - the extent
    file contains nothing else, and silently accepting a type we do not draw
    would produce an empty placemark rather than an error."""
    gtype = geom.get("type")
    if gtype == "Polygon":
        return _polygon_kml(geom.get("coordinates") or [])
    if gtype == "MultiPolygon":
        inner = "".join(_polygon_kml(p) for p in (geom.get("coordinates") or []))
        return f"<MultiGeometry>{inner}</MultiGeometry>"
    return ""


def _polygon_kml(rings: list) -> str:
    if not rings:
        return ""
    def ring(coords: list) -> str:
        return " ".join(f"{c[0]:.6f},{c[1]:.6f},0" for c in coords)
    out = ["<Polygon><tessellate>1</tessellate>"]
    out.append("<outerBoundaryIs><LinearRing><coordinates>"
               + ring(rings[0]) + "</coordinates></LinearRing></outerBoundaryIs>")
    for hole in rings[1:]:
        out.append("<innerBoundaryIs><LinearRing><coordinates>"
                   + ring(hole) + "</coordinates></LinearRing></innerBoundaryIs>")
    out.append("</Polygon>")
    return "".join(out)


# --------------------------------------------------------------------------
# GeoJSON
# --------------------------------------------------------------------------


def build_geojson(
    run_id: str,
    meta: dict,
    extent: dict,
    impact: dict | None,
    uncertainty: dict | None,
) -> dict:
    """The extent with attributes worth having, plus provenance at the top.

    The contract file in the run folder is deliberately minimal and stays that
    way - this is the derived product, and the two must not be confused, so
    the top-level `run` block names the run it came from.
    """
    site = meta.get("site") or {}
    res = meta.get("results") or {}
    sc = meta.get("scenario") or {}
    dom = meta.get("domain") or {}
    dem = meta.get("dem") or {}

    feats = []
    for feat in extent.get("features", []):
        props = dict(feat.get("properties") or {})
        depth = props.get("max_depth_m", props.get("min_depth_m"))
        props["depth_band"] = _band_for(depth)[0]
        props["run_id"] = run_id
        props["site"] = site.get("name")
        props["failure_mode"] = sc.get("failure_mode")
        props["engine"] = meta.get("engine")
        props["is_fake"] = meta.get("is_fake", False)
        feats.append({**feat, "properties": props})

    return {
        "type": "FeatureCollection",
        "crs": extent.get("crs", {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        }),
        # Foreign members are legal in GeoJSON and every reader ignores what it
        # does not know, so the provenance rides along without breaking a
        # single consumer.
        "run": {
            "run_id": run_id,
            "created_utc": meta.get("created_utc"),
            "engine": meta.get("engine"),
            "is_fake": meta.get("is_fake", False),
            "site": site,
            "scenario": sc,
            "domain": dom,
            "dem": dem,
            "results": res,
            "impact_totals": (impact or {}).get("totals"),
        },
        "caveats": caveats(meta, uncertainty, impact),
        "produced_by": (
            "SIH26161 dam-break inundation framework, NTRO problem statement "
            "26161. Derived from extent.geojson in the run folder; the run "
            "folder is the source of truth."
        ),
        "features": feats,
    }


# --------------------------------------------------------------------------
# Shapefile package
# --------------------------------------------------------------------------


def readme_text(run_id: str, meta: dict, impact: dict | None,
                uncertainty: dict | None) -> str:
    """The text file that ships inside the shapefile zip.

    A shapefile has a ten-character field-name limit and no room for a
    paragraph, so everything that will not fit in a DBF column goes here.
    """
    lines = [
        "=" * 72,
        f"  {run_id}",
        "  Dam Break Inundation Modelling - SIH problem statement 26161, NTRO",
        "=" * 72,
        "",
        "WHAT IS IN THIS ZIP",
        "",
        "  extent.shp/.dbf/.shx/.prj   modelled maximum flood extent, EPSG:4326",
        "  settlements.shp/...         affected settlements, one point each",
        "  hydrograph.csv              discharge at the source, m3/s vs hours",
        "  impact.json                 settlements, roads, population, damage",
        "  uncertainty.json            the breach-parameter spread, unaveraged",
        "  meta.json                   the complete run record",
        "  README.txt                  this file",
        "",
        "HEADLINE RESULTS",
        "",
    ]
    for label, value in headline(meta, impact):
        lines.append(f"  {label:<24} {value}")
    lines += ["", "WHAT THIS IS, AND WHAT IT IS NOT", ""]
    for c in caveats(meta, uncertainty, impact):
        # Wrap by hand; a README that needs a word-wrapping terminal is a
        # README nobody reads.
        words, line = c.split(), "  -"
        for w in words:
            if len(line) + len(w) + 1 > 74:
                lines.append(line)
                line = "   "
            line += " " + w
        lines.append(line)
        lines.append("")
    lines += [
        "FIELD NAMES",
        "",
        "  A shapefile DBF field name cannot exceed ten characters, so several",
        "  are abbreviated. extent: area_km2, min_depth, depth_band, run_id,",
        "  site, fail_mode, engine, is_fake. settlements: name, pop, pop_src,",
        "  arrival_hr, max_depth, max_vel, hazard, houses, damage_inr.",
        "",
        "  Full-precision values for every one of them are in impact.json and",
        "  meta.json, which are in this zip for exactly that reason.",
        "",
        f"  Run created {meta.get('created_utc', 'unknown')}, "
        f"engine {meta.get('engine', 'unknown')}.",
        "",
    ]
    return "\n".join(lines)


def _csv_bytes(rows: list[dict], columns: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def build_shp_zip(
    run_dir: Path,
    run_id: str,
    meta: dict,
    extent: dict,
    impact: dict | None,
    uncertainty: dict | None,
    out_zip: Path,
    work_dir: Path,
) -> Path:
    """Write the shapefile delivery package and return the zip path."""
    import geopandas as gpd
    from shapely.geometry import Point, shape

    site = meta.get("site") or {}
    sc = meta.get("scenario") or {}

    # --- extent layer -------------------------------------------------------
    geoms, recs = [], []
    for feat in extent.get("features", []):
        props = feat.get("properties") or {}
        depth = props.get("max_depth_m", props.get("min_depth_m"))
        geoms.append(shape(feat["geometry"]))
        recs.append({
            "area_km2": props.get("area_km2"),
            "min_depth": props.get("min_depth_m"),
            "depth_band": _band_for(depth)[0],
            "run_id": run_id,
            "site": site.get("name"),
            "fail_mode": sc.get("failure_mode"),
            "engine": meta.get("engine"),
            "is_fake": bool(meta.get("is_fake", False)),
        })
    extent_gdf = gpd.GeoDataFrame(recs, geometry=geoms, crs="EPSG:4326")

    shp_dir = work_dir / run_id
    shp_dir.mkdir(parents=True, exist_ok=True)
    extent_gdf.to_file(shp_dir / "extent.shp", driver="ESRI Shapefile",
                       encoding="utf-8")

    # --- settlement layer ---------------------------------------------------
    settlements = (impact or {}).get("settlements") or []
    if settlements:
        pts, srecs = [], []
        for s in settlements:
            pts.append(Point(float(s["lon"]), float(s["lat"])))
            srecs.append({
                "name": s.get("name"),
                "pop": s.get("population"),
                "pop_src": s.get("population_source"),
                "arrival_hr": s.get("arrival_hr"),
                "max_depth": s.get("max_depth_m"),
                "max_vel": s.get("max_velocity_ms"),
                "hazard": s.get("hazard_class"),
                "houses": s.get("houses_affected"),
                "damage_inr": s.get("damage_inr"),
            })
        gpd.GeoDataFrame(srecs, geometry=pts, crs="EPSG:4326").to_file(
            shp_dir / "settlements.shp", driver="ESRI Shapefile", encoding="utf-8"
        )

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in sorted(shp_dir.iterdir()):
            zf.write(part, part.name)
        zf.writestr("README.txt",
                    readme_text(run_id, meta, impact, uncertainty))
        for name in ("meta.json", "impact.json", "uncertainty.json",
                     "hydrograph.csv", "validation.json", "evacuation.json"):
            src = run_dir / name
            if src.is_file():
                zf.write(src, name)
        if settlements:
            zf.writestr(
                "settlements.csv",
                _csv_bytes(settlements, [
                    "name", "lat", "lon", "population", "population_source",
                    "arrival_hr", "max_depth_m", "max_velocity_ms",
                    "hazard_class", "houses_affected", "damage_inr",
                ]),
            )
        roads = (impact or {}).get("roads") or []
        if roads:
            zf.writestr(
                "roads_cut.csv",
                _csv_bytes(roads, ["osm_id", "name", "highway", "length_cut_km"]),
            )
    return out_zip
