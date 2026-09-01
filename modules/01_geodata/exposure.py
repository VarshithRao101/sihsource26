"""
modules/01_geodata/exposure.py - who and what is downstream.

This is the module that turns a flood map into an evacuation decision. A depth
raster tells a hydrologist something; "Chungthang, 1,240 people, water in
7 minutes" tells a district magistrate what to do.

Everything here comes from OpenStreetMap and WorldPop. Nothing is invented -
the contract forbids it, and a made-up village name in front of an NTRO juror
who knows the district is the fastest way to lose the round.

    settlements(bbox, site)   real named places with population
    roads(bbox, site)         road network for the roads-cut analysis
    build_exposure(...)       both, cached, in the shape runner.build_impact wants

Caching matters more than it looks: it is downloaded once, written to
data/exposure/{site}/, and the live demo then runs with no network at all.
Never demo something that needs a conference-hall wifi connection.

Owner: captain (module 01).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from shared.geo import Grid

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPOSURE_DIR = REPO_ROOT / "data" / "exposure"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

PLACE_RANKS = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 2}

# Fallback population when OSM has a named place but no population tag.
# These are order-of-magnitude class medians, not measurements, and every
# settlement that uses one is marked population_source = "class_default" so the
# UI and the impact table can say so.
POPULATION_BY_CLASS = {
    "city": 150_000,
    "town": 20_000,
    "suburb": 8_000,
    "village": 1_500,
    "hamlet": 250,
}

HIGHWAY_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")


# ==========================================================================
# Overpass
# ==========================================================================


def _overpass(query: str, timeout_s: int = 180) -> dict | None:
    """Run an Overpass QL query, trying the mirrors in turn.

    The User-Agent is not optional. Overpass answers the default
    `python-requests/x.y` agent with **406 Not Acceptable** and no explanation,
    which looks exactly like "there are no villages here" if you only check for
    an exception. Identify the client honestly - it is also the polite thing to
    do to a free volunteer-run service.

    Returns None rather than raising: exposure matters, but a network hiccup
    must never take down a run whose hydraulics already succeeded.
    """
    import requests

    headers = {
        "User-Agent": (
            "SIH26161-dam-break-inundation/1.0 "
            "(academic hackathon; flood early-warning research)"
        )
    }
    for url in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(
                url, data={"data": query}, headers=headers, timeout=timeout_s
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            time.sleep(1.0)
    return None


def fetch_settlements(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Named places inside bbox, from OpenStreetMap.

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat).

    Returns:
        List of {name, lat, lon, population, place_class, population_source,
        osm_id}. Empty list if Overpass is unreachable.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    query = f"""
    [out:json][timeout:120];
    (
      node["place"~"^(city|town|village|hamlet|suburb)$"]["name"]
          ({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out body;
    """
    data = _overpass(query)
    if not data:
        return []

    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        place_class = tags.get("place", "village")

        pop_raw = tags.get("population")
        if pop_raw and str(pop_raw).replace(",", "").strip().isdigit():
            population = int(str(pop_raw).replace(",", "").strip())
            pop_source = "osm:population"
        else:
            population = POPULATION_BY_CLASS.get(place_class, 1_000)
            pop_source = "class_default"

        out.append(
            {
                "name": name,
                "lat": float(el["lat"]),
                "lon": float(el["lon"]),
                "population": population,
                "place_class": place_class,
                "population_source": pop_source,
                "osm_id": str(el.get("id", "")),
            }
        )

    out.sort(key=lambda s: (PLACE_RANKS.get(s["place_class"], 9), -s["population"]))
    return out


def fetch_roads(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Major roads inside bbox, with geometry, from OpenStreetMap.

    Returns list of {osm_id, name, highway, coords: [[lon, lat], ...]}.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    classes = "|".join(HIGHWAY_CLASSES)
    query = f"""
    [out:json][timeout:180];
    (
      way["highway"~"^({classes})$"]({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out geom;
    """
    data = _overpass(query)
    if not data:
        return []

    roads = []
    for el in data.get("elements", []):
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        tags = el.get("tags", {})
        roads.append(
            {
                "osm_id": str(el.get("id", "")),
                "name": tags.get("name") or tags.get("ref") or "",
                "highway": tags.get("highway", ""),
                "coords": [[float(p["lon"]), float(p["lat"])] for p in geom],
            }
        )
    return roads


# ==========================================================================
# Population refinement
# ==========================================================================


def refine_population_worldpop(
    settlements: list[dict], site: str, radius_km: float = 2.0
) -> list[dict]:
    """Replace class-default populations with a WorldPop sum where available.

    WorldPop publishes 100 m population-count rasters. Summing the cells within
    a radius of a place node is a far better estimate than a class median, and
    it is a real measurement rather than an assumption. Requires
    data/exposure/{site}/population.tif; silently leaves settlements alone if
    the raster is not there.

    Source: WorldPop (2020), "Global High Resolution Population Denominators",
    University of Southampton, doi:10.5258/SOTON/WP00660.
    """
    pop_path = EXPOSURE_DIR / site / "population.tif"
    if not pop_path.exists():
        return settlements

    try:
        import rasterio
        from rasterio.warp import transform as warp_transform
    except Exception:
        return settlements

    with rasterio.open(pop_path) as src:
        band = src.read(1)
        nodata = src.nodata
        for s in settlements:
            xs, ys = warp_transform("EPSG:4326", src.crs, [s["lon"]], [s["lat"]])
            try:
                row, col = src.index(xs[0], ys[0])
            except Exception:
                continue
            # radius in pixels, from the raster's own resolution
            px = max(int(radius_km * 1000.0 / abs(src.transform.a) / 111_320 * 111_320), 1)
            r0, r1 = max(row - px, 0), min(row + px + 1, src.height)
            c0, c1 = max(col - px, 0), min(col + px + 1, src.width)
            if r0 >= r1 or c0 >= c1:
                continue
            window = band[r0:r1, c0:c1].astype(np.float64)
            if nodata is not None:
                window = window[window != nodata]
            window = window[np.isfinite(window) & (window >= 0)]
            if window.size == 0:
                continue
            s["population"] = int(round(float(window.sum())))
            s["population_source"] = "worldpop2020"
    return settlements


# ==========================================================================
# Roads cut
# ==========================================================================


def roads_cut(
    roads: list[dict], max_depth: np.ndarray, grid: Grid, threshold_m: float = 0.30
) -> list[dict]:
    """Which roads the flood cuts, and over what length.

    A road is "cut" where water on it exceeds `threshold_m`. 0.30 m is the
    depth at which a small vehicle loses traction and stalls - Australian
    Disaster Resilience Guideline 7-3 (2017) hazard class H2, and the same
    figure behind every "Turn Around Don't Drown" campaign.

    Length is measured by walking each way's vertices and accumulating the
    great-circle distance of segments whose midpoint is flooded.
    """
    from shared.geo import haversine_km

    out = []
    for road in roads:
        coords = road["coords"]
        cut_km = 0.0
        for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
            mid_lon, mid_lat = 0.5 * (lon1 + lon2), 0.5 * (lat1 + lat2)
            if not grid.contains(mid_lon, mid_lat):
                continue
            r, c = grid.rowcol(mid_lon, mid_lat)
            depth = float(max_depth[r, c])
            if depth >= threshold_m:
                cut_km += haversine_km(lon1, lat1, lon2, lat2)
        if cut_km > 0:
            out.append(
                {
                    "osm_id": road["osm_id"],
                    "name": road["name"],
                    "highway": road["highway"],
                    "length_cut_km": round(cut_km, 3),
                }
            )
    out.sort(key=lambda r: -r["length_cut_km"])
    return out


# ==========================================================================
# The cached bundle
# ==========================================================================


def build_exposure(
    bbox: tuple[float, float, float, float],
    site: str,
    force: bool = False,
    with_roads: bool = True,
) -> dict:
    """Settlements + roads for a site, downloaded once and cached.

    Returns the dict runner.build_impact expects:
        {"settlements": [...], "roads": [...], "source": "..."}

    Cached at data/exposure/{site}/exposure.json. Delete that file or pass
    force=True to refresh.
    """
    folder = EXPOSURE_DIR / site
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / "exposure.json"

    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    settlements = fetch_settlements(bbox)
    settlements = refine_population_worldpop(settlements, site)
    roads_list = fetch_roads(bbox) if with_roads else []

    bundle = {
        "site": site,
        "bbox": list(bbox),
        "settlements": settlements,
        "roads": roads_list,
        "source": "OpenStreetMap via Overpass API, ODbL",
        "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Never cache an empty download. A failed fetch and a genuinely empty
    # bbox look identical on disk, and caching the failure means every later
    # run silently reports zero people at risk - the worst possible way for
    # this to break, because it fails quietly and in the safe-looking direction.
    if not settlements:
        raise RuntimeError(
            f"no settlements returned for {site} over {bbox}. Overpass may be "
            f"rate-limiting or unreachable - the result was NOT cached, so "
            f"simply run again. If the bbox really is uninhabited, pass "
            f"exposure={{'settlements': []}} to run_scenario explicitly."
        )

    cache.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_geojson(folder / "settlements.geojson", settlements)
    return bundle


def _write_geojson(path: Path, settlements: list[dict]) -> None:
    """Also write settlements.geojson - the contract's data/ layout wants it,
    and it is what the frontend overlays."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {k: v for k, v in s.items() if k not in ("lat", "lon")},
            }
            for s in settlements
        ],
    }
    path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
