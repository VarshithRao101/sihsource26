"""
shared/geo.py - the Grid.

Every raster in a run folder has the same shape, the same transform and the
same CRS. That invariant is what lets module 04 write max_depth.tif, module 06
read it next to a Sentinel-1 observation, and module 05 stack them in a browser
without anybody resampling anything. This file is that invariant, in code.

Owner: captain / person 4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from shared.contract import DELIVERY_CRS

Bbox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)

EARTH_RADIUS_M = 6371008.8  # mean radius, IUGG


@dataclass(frozen=True)
class Grid:
    """A north-up, axis-aligned raster grid in EPSG:4326.

    Attributes:
        bbox: (min_lon, min_lat, max_lon, max_lat), degrees.
        nx: columns (longitude direction).
        ny: rows (latitude direction). Row 0 is the NORTH edge.
        crs: always EPSG:4326 on disk. Kept explicit so the validator can check.
    """

    bbox: Bbox
    nx: int
    ny: int
    crs: str = DELIVERY_CRS

    # -- geometry ----------------------------------------------------------

    @property
    def dx_deg(self) -> float:
        return (self.bbox[2] - self.bbox[0]) / self.nx

    @property
    def dy_deg(self) -> float:
        return (self.bbox[3] - self.bbox[1]) / self.ny

    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)

    @property
    def transform(self):
        """rasterio affine transform, north-up. Imported lazily so that modules
        which only need the geometry do not pull in rasterio."""
        from rasterio.transform import from_bounds

        return from_bounds(*self.bbox, self.nx, self.ny)

    @property
    def centre(self) -> tuple[float, float]:
        return (
            0.5 * (self.bbox[0] + self.bbox[2]),
            0.5 * (self.bbox[1] + self.bbox[3]),
        )

    def cellsize_m(self) -> float:
        """Approximate cell size in metres at the grid centre.

        Longitude degrees shrink with latitude; we report the geometric mean of
        the x and y cell size, which is what goes into meta.json.domain.cellsize_m.
        Modelling is done on this nominal size - for a 60 km reach at mid
        latitude the x/y anisotropy is a few percent and is absorbed into the
        DEM uncertainty, which is far larger.
        """
        _, lat = self.centre
        m_per_deg_lat = EARTH_RADIUS_M * math.pi / 180.0
        m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(lat))
        return math.sqrt((self.dx_deg * m_per_deg_lon) * (self.dy_deg * m_per_deg_lat))

    def cell_area_m2(self) -> float:
        return self.cellsize_m() ** 2

    # -- indexing ----------------------------------------------------------

    def rowcol(self, lon: float, lat: float) -> tuple[int, int]:
        """Grid cell containing a lon/lat. Clamped to the grid."""
        col = int((lon - self.bbox[0]) / self.dx_deg)
        row = int((self.bbox[3] - lat) / self.dy_deg)
        return (
            min(max(row, 0), self.ny - 1),
            min(max(col, 0), self.nx - 1),
        )

    def lonlat(self, row: int, col: int) -> tuple[float, float]:
        """Centre coordinate of a cell."""
        return (
            self.bbox[0] + (col + 0.5) * self.dx_deg,
            self.bbox[3] - (row + 0.5) * self.dy_deg,
        )

    def contains(self, lon: float, lat: float) -> bool:
        return (
            self.bbox[0] <= lon <= self.bbox[2]
            and self.bbox[1] <= lat <= self.bbox[3]
        )

    def coordinate_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """(lons, lats) 1-D arrays of cell centres."""
        lons = self.bbox[0] + (np.arange(self.nx) + 0.5) * self.dx_deg
        lats = self.bbox[3] - (np.arange(self.ny) + 0.5) * self.dy_deg
        return lons, lats

    # -- construction ------------------------------------------------------

    @classmethod
    def from_bbox_cellsize(cls, bbox: Bbox, cellsize_m: float) -> "Grid":
        """Build a grid covering bbox at approximately cellsize_m resolution."""
        min_lon, min_lat, max_lon, max_lat = bbox
        mid_lat = 0.5 * (min_lat + max_lat)
        m_per_deg_lat = EARTH_RADIUS_M * math.pi / 180.0
        m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(mid_lat))

        nx = max(int(round((max_lon - min_lon) * m_per_deg_lon / cellsize_m)), 2)
        ny = max(int(round((max_lat - min_lat) * m_per_deg_lat / cellsize_m)), 2)
        return cls(bbox=bbox, nx=nx, ny=ny)

    @classmethod
    def from_meta(cls, meta: dict) -> "Grid":
        """Rebuild the grid from a meta.json dict."""
        domain = meta["domain"]
        if "nx" in domain and "ny" in domain:
            return cls(
                bbox=tuple(domain["bbox"]),
                nx=int(domain["nx"]),
                ny=int(domain["ny"]),
                crs=domain.get("crs", DELIVERY_CRS),
            )
        return cls.from_bbox_cellsize(tuple(domain["bbox"]), float(domain["cellsize_m"]))

    def to_meta_domain(self, reach_length_km: float | None = None) -> dict:
        """The domain block of meta.json."""
        block = {
            "bbox": [round(v, 6) for v in self.bbox],
            "crs": self.crs,
            "cellsize_m": round(self.cellsize_m(), 3),
            "nx": self.nx,
            "ny": self.ny,
        }
        if reach_length_km is not None:
            block["reach_length_km"] = round(reach_length_km, 2)
        return block

    def matches(self, other: "Grid", tol: float = 1e-9) -> bool:
        """Same shape, same footprint, same CRS - the contract invariant."""
        return (
            self.nx == other.nx
            and self.ny == other.ny
            and self.crs == other.crs
            and all(abs(a - b) < tol for a, b in zip(self.bbox, other.bbox))
        )


# --------------------------------------------------------------------------
# Bounding boxes
# --------------------------------------------------------------------------


def bbox_around(lon: float, lat: float, radius_km: float) -> Bbox:
    """Square-ish bbox centred on a point, sized in kilometres."""
    d_lat = radius_km * 1000.0 / (EARTH_RADIUS_M * math.pi / 180.0)
    d_lon = d_lat / max(math.cos(math.radians(lat)), 1e-6)
    return (lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def bbox_downstream(
    dam_lon: float,
    dam_lat: float,
    reach_length_km: float,
    corridor_width_km: float = 12.0,
    bearing_deg: float = 180.0,
) -> Bbox:
    """Bbox covering a reach running away from a dam on a given bearing.

    Used when we know only the dam location and a rough flow direction - the
    initial domain guess before module 01 traces the real channel and tightens
    it. bearing_deg is compass (0 = north, 90 = east).
    """
    m_per_deg_lat = EARTH_RADIUS_M * math.pi / 180.0
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(dam_lat))

    theta = math.radians(bearing_deg)
    end_lat = dam_lat + (reach_length_km * 1000.0 * math.cos(theta)) / m_per_deg_lat
    end_lon = dam_lon + (reach_length_km * 1000.0 * math.sin(theta)) / m_per_deg_lon

    pad_lat = corridor_width_km * 500.0 / m_per_deg_lat
    pad_lon = corridor_width_km * 500.0 / m_per_deg_lon

    return (
        min(dam_lon, end_lon) - pad_lon,
        min(dam_lat, end_lat) - pad_lat,
        max(dam_lon, end_lon) + pad_lon,
        max(dam_lat, end_lat) + pad_lat,
    )


def bbox_area_km2(bbox: Bbox) -> float:
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = EARTH_RADIUS_M * math.pi / 180.0
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(mid_lat))
    return (
        (max_lon - min_lon) * m_per_deg_lon * (max_lat - min_lat) * m_per_deg_lat
    ) / 1e6


def clip_bbox(inner: Bbox, outer: Bbox) -> Bbox:
    return (
        max(inner[0], outer[0]),
        max(inner[1], outer[1]),
        min(inner[2], outer[2]),
        min(inner[3], outer[3]),
    )


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------


def utm_epsg(lon: float, lat: float) -> str:
    """EPSG code of the UTM zone containing a point.

    Model in UTM (metres, equal-area cells) and reproject on write. India spans
    UTM 42N to 47N, so this matters: a degree of longitude at 8 N is 110 km and
    at 35 N it is 91 km. Running a shallow-water solver on degrees without this
    correction bakes in a 20% velocity error along the length of the country.
    """
    zone = int((lon + 180.0) / 6.0) + 1
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)) / 1000.0


def polyline_length_km(coords: Iterable[tuple[float, float]]) -> float:
    pts = list(coords)
    return sum(
        haversine_km(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
        for i in range(len(pts) - 1)
    )
