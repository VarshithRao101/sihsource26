"""
modules/01_geodata/domain.py - work out where the river actually goes.

The naive way to pick a model domain is to draw a corridor on a compass bearing
from the dam. We did that first and it fails on real terrain: over a 40 km reach
below Chungthang the Teesta swings far enough east that a 10 km straight
corridor loses the river after 12 km. Everything past that point in the box is
ridge, the minimum elevation along the domain RISES from 1,174 m to 2,824 m, and
the flood piles up against a mountain instead of reaching the villages.

So we find the river first, then draw the box around it:

    1. fetch a coarse DEM over a generous square around the dam
    2. condition it and compute D8 drainage
    3. snap the dam onto the channel (published dam coordinates land on the
       abutment as often as in the water)
    4. walk the drainage downstream for the requested reach length
    5. take the bounding box of that traced path, buffered by the corridor width

This is the function that makes "point it at a dam it has never seen and it
sets itself up" a true statement rather than a slogan.

Owner: captain (module 01).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from shared.geo import Grid, bbox_around, polyline_length_km


@dataclass
class DomainPlan:
    """Where to model, and the river path the decision was based on."""

    bbox: tuple[float, float, float, float]
    path_lonlat: list[tuple[float, float]]
    dam_lonlat: tuple[float, float]
    """The dam AFTER snapping to the channel. Use this as the release point."""
    snapped_by_m: float
    traced_length_km: float
    drop_m: float
    """Elevation lost from the dam to the end of the trace. A dam-break needs
    this to be positive and substantial; near zero means the trace failed."""
    scout_cellsize_m: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "bbox": [round(v, 6) for v in self.bbox],
            "dam_lonlat": [round(v, 6) for v in self.dam_lonlat],
            "snapped_by_m": round(self.snapped_by_m, 1),
            "traced_length_km": round(self.traced_length_km, 2),
            "drop_m": round(self.drop_m, 1),
            "scout_cellsize_m": self.scout_cellsize_m,
            "path_points": len(self.path_lonlat),
            "notes": self.notes,
        }


def plan_domain(
    lat: float,
    lon: float,
    site: str,
    reach_length_km: float = 60.0,
    corridor_width_km: float = 10.0,
    source: str = "COP30",
    scout_cellsize_m: float = 180.0,
    snap_radius_m: float = 900.0,
) -> DomainPlan:
    """Trace the river below a dam and return the domain that contains it.

    The scout pass runs at 180 m rather than the solver's 90 m. Drainage
    direction is scale-tolerant - the channel is in the same place at either
    resolution - and 180 m makes the scout roughly four times cheaper, which
    matters because this runs before every new site.

    Args:
        lat, lon: published dam coordinates.
        site: slug for the DEM cache.
        reach_length_km: how far downstream to model.
        corridor_width_km: floodplain buffer either side of the traced channel.
        source: DEM to scout with.
        scout_cellsize_m: scout resolution.
        snap_radius_m: how far to look for the channel around the dam.

    Returns:
        DomainPlan.

    Raises:
        RuntimeError: if the trace goes nowhere, which means the dam is not on a
            drainage line the DEM can see. Better to say so than to hand the
            solver a domain with no river in it.
    """
    from . import terrain as tr

    notes: list[str] = []

    # A square wide enough that the river cannot leave it before the reach ends,
    # whichever way it bends.
    scout_bbox = bbox_around(lon, lat, radius_km=reach_length_km)
    scout_grid = Grid.from_bbox_cellsize(scout_bbox, scout_cellsize_m)

    dem_path = tr.fetch_dem(scout_bbox, site=f"{site}_scout", source=source)
    dem = tr.load_local_dem(dem_path, scout_bbox, scout_grid)

    if np.isnan(dem).any():
        from .provider import _fill_voids

        dem = _fill_voids(dem)
        notes.append("scout DEM had voids; filled by nearest neighbour")

    filled = tr.fill_depressions(dem)
    direction = tr.d8_flow_direction(filled, scout_grid.cellsize_m())
    acc = tr.flow_accumulation(direction)

    # --- snap the dam onto the channel --------------------------------
    r0, c0 = scout_grid.rowcol(lon, lat)
    r_snap, c_snap = tr.snap_to_channel(acc, scout_grid, lon, lat, snap_radius_m)
    snap_lon, snap_lat = scout_grid.lonlat(r_snap, c_snap)

    from shared.geo import haversine_km

    snapped_by_m = haversine_km(lon, lat, snap_lon, snap_lat) * 1000.0
    if snapped_by_m > 50.0:
        notes.append(
            f"dam snapped {snapped_by_m:.0f} m onto the channel "
            f"(contributing area {acc[r_snap, c_snap]:,.0f} cells vs "
            f"{acc[r0, c0]:,.0f} at the published point)"
        )

    # --- follow the water ---------------------------------------------
    path_rc = tr.trace_downstream(
        direction, scout_grid, (r_snap, c_snap), max_length_km=reach_length_km
    )
    path_lonlat = tr.path_to_lonlat(path_rc, scout_grid)

    if len(path_lonlat) < 5:
        raise RuntimeError(
            f"could not trace a river below {site} at {lat}, {lon}: the drainage "
            f"path is {len(path_lonlat)} cells long. Either the coordinates are "
            f"not on a river, or the DEM is too coarse to resolve the channel here."
        )

    traced_km = polyline_length_km(path_lonlat)
    drop_m = float(filled[r_snap, c_snap] - filled[path_rc[-1][0], path_rc[-1][1]])

    if traced_km < 0.5 * reach_length_km:
        notes.append(
            f"trace ran {traced_km:.1f} km of the {reach_length_km:.0f} km requested "
            f"before leaving the scout box or reaching a sink"
        )
    if drop_m < 10.0:
        notes.append(
            f"only {drop_m:.1f} m of fall along the traced reach - check the dam "
            f"coordinates, a dam-break needs a gradient"
        )

    bbox = tr.bbox_from_path(path_lonlat, corridor_width_km)

    return DomainPlan(
        bbox=bbox,
        path_lonlat=path_lonlat,
        dam_lonlat=(snap_lon, snap_lat),
        snapped_by_m=snapped_by_m,
        traced_length_km=traced_km,
        drop_m=drop_m,
        scout_cellsize_m=scout_cellsize_m,
        notes=notes,
    )


def river_geojson(plan: DomainPlan, name: str = "traced channel") -> dict:
    """The traced path as GeoJSON - written to data/rivers/{site}/network.geojson
    and drawn on the dashboard so a juror can see the modelled river."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(x, 6), round(y, 6)] for x, y in plan.path_lonlat],
                },
                "properties": {
                    "name": name,
                    "length_km": round(plan.traced_length_km, 2),
                    "drop_m": round(plan.drop_m, 1),
                    "source": "D8 trace over conditioned COP30",
                },
            }
        ],
    }
