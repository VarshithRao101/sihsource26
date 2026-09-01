"""
modules/04_backend/blockage.py - river blockage, the other half of deliverable 1.

The problem statement asks for a framework simulating dam break **and river
blockage**. This is the blockage half, and it is a genuinely different problem
from a dam failure, not a relabelling of one.

A landslide drops into a valley and dams the river. There is no engineered
structure, no spillway, no gates, and nobody chose the height. Water piles up
behind it for hours or days. Then it overtops, the unconsolidated debris erodes
fast, and the whole impoundment comes down the valley at once.

That is Rishi Ganga (Uttarakhand, February 2021), and it is what the Teesta
GLOF did to Chungthang in October 2023 - a natural blockage failure that
destroyed an engineered dam on its way down. Both events are named in the
problem statement.

Three things make it different from a dam break, and all three are modelled here:

  1. NOBODY KNOWS THE STORAGE. An engineered dam has a published capacity. A
     landslide dam has whatever volume the valley holds up to the blockage
     crest, so we compute it from the DEM by flooding upstream to that
     elevation. That is `impounded_volume()`.

  2. THERE IS A CLOCK BEFORE THE FLOOD. The lake fills from upstream inflow,
     and the time until it overtops is the warning time - often the only
     warning anyone gets. That is `time_to_overtop()`, and it uses the CHIRPS
     inflow nowcast from modules/07_ml/inflow.py.

  3. IT BREACHES DIFFERENTLY. Landslide dams are poorly sorted, unconsolidated
     and uncompacted; they erode faster and wider than engineered embankments,
     and the engineered-dam regressions under-predict them. `blockage_breach()`
     uses regressions fitted to natural dams instead.

Owner: captain (module 04).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np

from shared.geo import Grid
from shared.hydro import BreachParams


# ==========================================================================
# How much water is behind it
# ==========================================================================


def upstream_cells(direction: np.ndarray, blockage_rc: tuple[int, int]) -> np.ndarray:
    """Every cell that drains through the blockage point. Boolean mask.

    Walks the D8 drainage tree BACKWARDS from the blockage: a neighbour belongs
    upstream if its own flow direction points into a cell already known to be
    upstream. That is the catchment of the blockage, exactly.

    This replaces a plain flood fill, which was wrong in a way worth recording.
    A blockage sits at the top of a valley, so almost everything DOWNSTREAM is
    also below its crest; filling "every connected cell below the crest" floods
    the entire domain and reported a 74,000 MCM lake 1,063 m deep. Elevation
    alone cannot tell upstream from downstream. Drainage can.
    """
    from collections import deque

    di = (-1, -1, -1, 0, 0, 1, 1, 1)
    dj = (-1, 0, 1, -1, 1, -1, 0, 1)

    ny, nx = direction.shape
    up = np.zeros((ny, nx), dtype=bool)
    r0, c0 = blockage_rc
    up[r0, c0] = True
    queue = deque([(r0, c0)])

    while queue:
        r, c = queue.popleft()
        for k in range(8):
            a, b = r + di[k], c + dj[k]
            if not (0 <= a < ny and 0 <= b < nx) or up[a, b]:
                continue
            kd = int(direction[a, b])
            if kd < 0:
                continue
            # does (a,b) flow into (r,c)?
            if a + di[kd] == r and b + dj[kd] == c:
                up[a, b] = True
                queue.append((a, b))
    return up


def impounded_volume(
    dem: np.ndarray,
    grid: Grid,
    blockage_rc: tuple[int, int],
    blockage_height_m: float,
    direction: np.ndarray,
) -> dict:
    """Volume and extent of the lake a blockage impounds, from the DEM.

    The lake surface sits at the blockage crest. A cell is submerged if it
    (a) drains through the blockage - so it is genuinely upstream - and
    (b) lies below the crest. Connectivity comes free: every upstream cell
    reaches the blockage along the drainage tree by construction.

    This is the honest way to get the storage of something nobody surveyed. An
    engineered dam publishes a capacity; a landslide dam has whatever the
    valley holds.

    Args:
        dem: conditioned elevation on `grid`.
        grid: the model grid.
        blockage_rc: (row, col) of the blockage, snapped to the channel.
        blockage_height_m: debris height above the local bed.
        direction: D8 flow directions for the same grid, from module 01.

    Returns:
        dict with volume_m3, area_km2, cells, crest_elev_m, max_depth_m and the
        boolean `lake` mask.

    Raises:
        ValueError: if the blockage impounds nothing, which normally means the
            point is off-channel.
    """
    z = np.asarray(dem, dtype=np.float64)
    r0, c0 = blockage_rc
    bed = float(z[r0, c0])
    crest = bed + float(blockage_height_m)

    up = upstream_cells(np.ascontiguousarray(direction), (r0, c0))
    lake = up & (z < crest)
    lake[r0, c0] = False  # the debris itself is not lake

    if not lake.any():
        raise ValueError(
            f"a {blockage_height_m:.0f} m blockage at this point impounds nothing. "
            f"Its catchment has {int(up.sum())} cells and none sit below the "
            f"crest at {crest:.1f} m - the point is probably off-channel."
        )

    depth = np.where(lake, crest - z, 0.0)
    cell_area = grid.cell_area_m2()
    volume = float(depth.sum() * cell_area)

    touches_edge = bool(
        lake[0, :].any() or lake[-1, :].any() or lake[:, 0].any() or lake[:, -1].any()
    )

    return {
        "volume_m3": volume,
        "volume_mcm": round(volume / 1e6, 4),
        "area_km2": round(float(lake.sum()) * cell_area / 1e6, 4),
        "cells": int(lake.sum()),
        "catchment_cells": int(up.sum()),
        "crest_elev_m": round(crest, 2),
        "bed_elev_m": round(bed, 2),
        "max_depth_m": round(float(depth.max()), 2),
        "lake": lake,
        "truncated_by_domain": touches_edge,
        "note": (
            "Volume is a LOWER BOUND - the lake reaches the edge of the model "
            "domain, so part of it lies outside. Extend the domain upstream."
            if touches_edge
            else "Lake closes inside the domain."
        ),
    }


def time_to_overtop(volume_m3: float, inflow_cumecs: float) -> dict:
    """How long the valley has before the blockage overtops.

    This is the number that makes a blockage different from a dam break: there
    IS a warning period, and it is computable. Rishi Ganga gave minutes;
    landslide dams in the Himalaya have held for days to months.

    Filling is treated as constant-rate, which is conservative in the useful
    direction only if inflow does not rise - so the caller should pass a
    plausible high inflow, and the number should be read as an order of
    magnitude, not a countdown clock.
    """
    if inflow_cumecs <= 0:
        return {
            "hours": None,
            "note": "No upstream inflow given, so fill time cannot be computed.",
        }
    seconds = volume_m3 / inflow_cumecs
    return {
        "hours": round(seconds / 3600.0, 2),
        "days": round(seconds / 86400.0, 2),
        "inflow_cumecs": inflow_cumecs,
        "note": (
            "Constant-inflow estimate. Real filling accelerates during rainfall, "
            "so treat this as an order of magnitude."
        ),
    }


# ==========================================================================
# How it fails
# ==========================================================================


def blockage_breach(
    impounded_volume_m3: float,
    blockage_height_m: float,
    material: str = "debris",
) -> BreachParams:
    """Breach geometry for a NATURAL dam, not an engineered one.

    Engineered-embankment regressions (Froehlich, Von Thun) are fitted to
    compacted, designed, often core-and-filter structures. Landslide dams are
    none of those: unconsolidated, poorly sorted, no compaction, no core. They
    breach wider and faster, and applying Froehlich to one under-predicts the
    outflow.

    We use the natural-dam regression of Peng & Zhang (2012), "Breaching
    parameters of landslide dams", Landslides 9(1), 13-31, which is fitted to
    documented natural-dam failures:

        Bavg / hd = a * (Vl^(1/3) / hd)^b

    with the dimensionless form evaluated on lake volume Vl and dam height hd.
    Formation time follows their companion relation, and is short - hours, not
    the days an engineered embankment takes.

    Costa, J.E. & Schuster, R.L. (1988), "The formation and failure of natural
    dams", GSA Bulletin 100(7), 1054-1068, is the source for the failure-mode
    statistics: overtopping causes the large majority of natural-dam failures,
    which is why this function does not take a failure mode.

    Args:
        impounded_volume_m3: lake volume, normally from impounded_volume().
        blockage_height_m: debris height above the bed.
        material: 'debris' (default, poorly sorted) or 'rock' (coarser,
            more resistant blocky deposit).

    Returns:
        BreachParams, in the same shape the rest of the system uses, so the
        existing hydrograph routing and solver take it unchanged.
    """
    if impounded_volume_m3 <= 0 or blockage_height_m <= 0:
        raise ValueError("blockage_breach needs positive volume and height")

    hd = float(blockage_height_m)
    vl = float(impounded_volume_m3)

    # Dimensionless volume, Peng & Zhang (2012).
    dimensionless = (vl ** (1.0 / 3.0)) / hd

    # Coefficients differ with deposit character; debris erodes more readily.
    if material == "rock":
        a, b = 1.7, 0.72
        side_slope = 0.8
        time_coeff = 0.06
    else:
        a, b = 2.4, 0.70
        side_slope = 1.2
        time_coeff = 0.04

    b_avg = a * hd * dimensionless**b
    # A breach cannot be narrower than the debris is tall, nor absurdly wide.
    b_avg = float(np.clip(b_avg, 0.8 * hd, 40.0 * hd))

    # Formation time, hours. Natural dams fail fast: hours, not days.
    tf_hr = time_coeff * math.sqrt(vl / 1e6) * (hd / 20.0) ** 0.5
    tf_hr = float(np.clip(tf_hr, 1.0 / 60.0, 12.0))

    b_bottom = max(b_avg - side_slope * hd, 0.15 * b_avg)

    return BreachParams(
        bottom_width_m=b_bottom,
        average_width_m=b_avg,
        side_slope_h_per_v=side_slope,
        depth_m=hd,
        formation_time_hr=tf_hr,
        source=(
            f"Peng & Zhang (2012), Landslides 9(1) - natural dam, {material}; "
            f"failure mode per Costa & Schuster (1988)"
        ),
    )


# ==========================================================================
# The whole blockage scenario
# ==========================================================================


@dataclass
class BlockageResult:
    """Everything a blockage scenario knows before the flood is routed."""

    blockage_height_m: float
    impounded_volume_mcm: float
    lake_area_km2: float
    lake_max_depth_m: float
    crest_elev_m: float
    truncated_by_domain: bool
    breach_width_m: float
    breach_formation_hr: float
    breach_source: str
    time_to_overtop_hr: float | None
    inflow_cumecs: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def prepare_blockage(
    dem: np.ndarray,
    grid: Grid,
    lat: float,
    lon: float,
    blockage_height_m: float,
    accumulation: np.ndarray | None = None,
    direction: np.ndarray | None = None,
    inflow_cumecs: float | None = None,
    material: str = "debris",
) -> tuple[BreachParams, BlockageResult]:
    """Turn "a landslide of this height blocks the river here" into a runnable breach.

    Snaps the blockage onto the channel first, for the same reason every other
    coordinate in this project is snapped: a point picked off a map or a news
    report lands on the valley side as often as in the water, and a blockage
    on a hillside impounds nothing.

    Returns (BreachParams, BlockageResult). The BreachParams goes straight into
    shared.hydro.breach_hydrograph with the impounded volume as the reservoir,
    and from there through the ordinary solver path - a blockage flood routes
    downstream exactly like a dam-break flood, because it is one.
    """
    from importlib import import_module

    tr = import_module("modules.01_geodata.terrain")

    if accumulation is not None:
        rc = tr.snap_to_channel(accumulation, grid, lon, lat)
    else:
        rc = grid.rowcol(lon, lat)

    if direction is None:
        direction = tr.d8_flow_direction(dem, grid.cellsize_m())

    lake = impounded_volume(dem, grid, rc, blockage_height_m, direction)
    breach = blockage_breach(lake["volume_m3"], blockage_height_m, material)
    overtop = time_to_overtop(lake["volume_m3"], inflow_cumecs or 0.0)

    result = BlockageResult(
        blockage_height_m=blockage_height_m,
        impounded_volume_mcm=lake["volume_mcm"],
        lake_area_km2=lake["area_km2"],
        lake_max_depth_m=lake["max_depth_m"],
        crest_elev_m=lake["crest_elev_m"],
        truncated_by_domain=lake["truncated_by_domain"],
        breach_width_m=round(breach.average_width_m, 1),
        breach_formation_hr=round(breach.formation_time_hr, 3),
        breach_source=breach.source,
        time_to_overtop_hr=overtop.get("hours"),
        inflow_cumecs=inflow_cumecs,
    )
    return breach, result
