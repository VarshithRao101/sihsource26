"""
modules/07_ml/inflow.py - near-real-time reservoir inflow from satellite rainfall.

The problem statement asks for "near real-time flood analysis using Google
Earth Engine and open-source data". Module 06 does the observed-flood half of
that. This is the forward-looking half: how much water is arriving at the dam
right now, from rainfall that has already fallen.

    python -m modules.07_ml.inflow --lat 27.6003 --lon 88.6428 --days 30

Chain: CHIRPS daily rainfall over the upstream catchment -> SCS curve-number
runoff -> linear-reservoir routing -> inflow hydrograph in m3/s. Every step is
a published method with a citation, and the catchment area comes from our own
D8 flow accumulation rather than being typed in.

WHY THIS IS NOT AN LSTM, which the project plan called for. An LSTM forecasting
reservoir inflow has to be trained on OBSERVED inflow - a gauged time series at
the dam. No open series exists for these dams; India-WRIS publishes reservoir
levels for some, behind an interface that is a data-acquisition project in
itself. Training a recurrent net on rainfall paired with synthetic inflow would
teach it our own runoff model and then let us present it as learned knowledge,
which is circular and dishonest. The runoff model is here, uncircular, and the
LSTM slot stays open with a stated blocker: get an observed inflow series.
`fit_lstm()` below is the hook, and it refuses to run without real data.

Owner: captain (module 07).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
INFLOW_DIR = REPO_ROOT / "data" / "inflow"

CHIRPS = "UCSB-CHG/CHIRPS/DAILY"
"""Climate Hazards Group InfraRed Precipitation with Station data, 0.05 deg
daily. Funk, C. et al. (2015), "The climate hazards infrared precipitation with
stations - a new environmental record for monitoring extremes", Scientific
Data 2, 150066. Free, global, and available in Earth Engine with a few days'
latency - which is what "near real time" honestly means here."""

IMERG = "NASA/GPM_L3/IMERG_V07"
"""GPM IMERG half-hourly, ~4 hour latency. Use when the event is recent enough
that CHIRPS has not published; coarser calibration, much lower latency."""


# ==========================================================================
# Catchment
# ==========================================================================


def catchment_area_km2(lat: float, lon: float, site: str, scout_km: float = 60.0) -> dict:
    """Contributing area upstream of a dam, from our own D8 accumulation.

    Not typed in from a datasheet: the same drainage computation that traces
    the river downstream also counts how many cells drain INTO the dam. The
    dam is snapped to the channel first, for the same reason as everywhere else
    - a published coordinate on the abutment has a contributing area of three
    cells.
    """
    from importlib import import_module

    gd = import_module("modules.01_geodata")
    tr = gd.terrain
    from shared.geo import Grid, bbox_around

    bbox = bbox_around(lon, lat, radius_km=scout_km)
    grid = Grid.from_bbox_cellsize(bbox, 180.0)
    dem = tr.load_local_dem(
        tr.fetch_dem(bbox, site=f"{site}_scout", source="COP30"), bbox, grid
    )
    if np.isnan(dem).any():
        from importlib import import_module as _imp

        dem = _imp("modules.01_geodata.provider")._fill_voids(dem)
    filled = tr.fill_depressions(dem)
    direction = tr.d8_flow_direction(filled, grid.cellsize_m())
    acc = tr.flow_accumulation(direction)

    r, c = tr.snap_to_channel(acc, grid, lon, lat)
    cells = float(acc[r, c])
    area_km2 = cells * grid.cell_area_m2() / 1e6

    return {
        "area_km2": round(area_km2, 2),
        "cells": int(cells),
        "snapped_lonlat": list(grid.lonlat(r, c)),
        "scout_cellsize_m": round(grid.cellsize_m(), 1),
        "bbox": list(bbox),
    }


# ==========================================================================
# Rainfall
# ==========================================================================


def fetch_rainfall(
    bbox: tuple[float, float, float, float],
    start: str,
    end: str,
    collection: str = CHIRPS,
) -> tuple[list[str], np.ndarray]:
    """Catchment-mean daily rainfall, mm/day, from Earth Engine.

    Returns (dates, mm_per_day). Spatially averaged over the catchment bbox -
    a lumped model needs a lumped input, and distributing rainfall would imply
    a distributed runoff model we do not have.
    """
    from importlib import import_module

    sar = import_module("modules.06_gee_validation.sar")
    ee = sar.ee_init()

    region = ee.Geometry.Rectangle(list(bbox))
    coll = ee.ImageCollection(collection).filterDate(start, end).filterBounds(region)
    band = "precipitation"

    def daily(img):
        val = img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=5000, maxPixels=1e9
        ).get(band)
        return ee.Feature(None, {"date": img.date().format("YYYY-MM-dd"), "mm": val})

    feats = coll.map(daily).getInfo()["features"]
    dates, mm = [], []
    for f in feats:
        p = f["properties"]
        if p.get("mm") is None:
            continue
        dates.append(p["date"])
        mm.append(float(p["mm"]))
    return dates, np.asarray(mm, dtype=np.float64)


# ==========================================================================
# Rainfall -> runoff -> inflow
# ==========================================================================


def scs_runoff_mm(rain_mm: np.ndarray, curve_number: float = 75.0) -> np.ndarray:
    """SCS curve-number direct runoff, mm.

        S = 25400/CN - 254          potential retention, mm
        Q = (P - 0.2S)^2 / (P + 0.8S)   for P > 0.2S, else 0

    Source: USDA Soil Conservation Service (1972), National Engineering
    Handbook, Section 4: Hydrology. Still the standard lumped event-runoff
    method and the one an Indian irrigation department will recognise.

    CN = 75 is a mid-range value for forested/mixed hill catchment on
    moderately drained soil in average antecedent moisture. It is an
    ASSUMPTION - the single biggest lever on the answer - and it is reported
    with the result so it can be argued with.
    """
    s = 25400.0 / curve_number - 254.0
    p = np.maximum(np.asarray(rain_mm, dtype=np.float64), 0.0)
    ia = 0.2 * s
    q = np.where(p > ia, (p - ia) ** 2 / (p + 0.8 * s), 0.0)
    return q


def route_linear_reservoir(
    runoff_mm: np.ndarray, area_km2: float, k_days: float = 1.5
) -> np.ndarray:
    """Convert daily runoff depth to an inflow hydrograph, m3/s.

    A single linear reservoir: storage proportional to outflow, so the
    catchment response is an exponential recession with time constant k. It is
    the simplest defensible transformation from "rain fell" to "water arrives",
    and for a small mountain catchment k of 1-2 days is the right order.

    Source: Nash, J.E. (1957), "The form of the instantaneous unit
    hydrograph", IAHS Publication 45(3), 114-121.
    """
    depth_m = np.asarray(runoff_mm, dtype=np.float64) / 1000.0
    volume_m3 = depth_m * area_km2 * 1e6
    inflow = np.zeros_like(volume_m3)

    storage = 0.0
    dt_s = 86400.0
    k_s = max(k_days * 86400.0, dt_s)
    for i, v in enumerate(volume_m3):
        storage += v
        release = storage * (dt_s / k_s)
        storage -= release
        inflow[i] = release / dt_s
    return inflow


def nowcast(
    lat: float,
    lon: float,
    site: str,
    days: int = 30,
    end: str | None = None,
    curve_number: float = 75.0,
    k_days: float = 1.5,
    capacity_mcm: float | None = None,
) -> dict:
    """Recent rainfall over the catchment turned into a reservoir inflow series.

    Writes data/inflow/{site}_nowcast.json and returns it.
    """
    import datetime as _dt

    end_d = _dt.date.fromisoformat(end) if end else _dt.date.today()
    start_d = end_d - _dt.timedelta(days=days)

    catch = catchment_area_km2(lat, lon, site)
    dates, rain = fetch_rainfall(
        tuple(catch["bbox"]), start_d.isoformat(), end_d.isoformat()
    )
    if rain.size == 0:
        raise RuntimeError(
            f"CHIRPS returned no rainfall for {site} between {start_d} and {end_d}. "
            f"CHIRPS lags by several days; try an earlier end date or IMERG."
        )

    runoff = scs_runoff_mm(rain, curve_number)
    inflow = route_linear_reservoir(runoff, catch["area_km2"], k_days)

    total_inflow_mcm = float(inflow.sum() * 86400.0 / 1e6)
    payload = {
        "site": site,
        "window": [start_d.isoformat(), end_d.isoformat()],
        "catchment": catch,
        "method": {
            "rainfall": CHIRPS,
            "runoff": "SCS curve number (USDA SCS 1972, NEH-4)",
            "routing": "single linear reservoir (Nash 1957)",
            "curve_number": curve_number,
            "k_days": k_days,
        },
        "assumptions": (
            "Curve number and the routing constant are assumptions, not "
            "calibrated values - no observed inflow series exists for this dam "
            "to calibrate against. Treat the magnitude as indicative and the "
            "TIMING and RELATIVE changes as the useful signal."
        ),
        "series": [
            {"date": d, "rain_mm": round(float(r), 2), "runoff_mm": round(float(q), 2),
             "inflow_cumecs": round(float(i), 2)}
            for d, r, q, i in zip(dates, rain, runoff, inflow)
        ],
        "summary": {
            "days": len(dates),
            "total_rain_mm": round(float(rain.sum()), 1),
            "max_daily_rain_mm": round(float(rain.max()), 1),
            "peak_inflow_cumecs": round(float(inflow.max()), 1),
            "total_inflow_mcm": round(total_inflow_mcm, 2),
        },
    }

    if capacity_mcm:
        payload["summary"]["inflow_as_pct_of_capacity"] = round(
            100.0 * total_inflow_mcm / capacity_mcm, 1
        )

    INFLOW_DIR.mkdir(parents=True, exist_ok=True)
    (INFLOW_DIR / f"{site}_nowcast.json").write_text(json.dumps(payload, indent=2))
    return payload


def fit_lstm(*_args, **_kwargs):
    """Not implemented, deliberately. See the module docstring.

    An inflow LSTM needs an observed inflow series to learn from. Fitting one
    to output from the runoff model above would only teach it that model, and
    presenting the result as a learned forecast would be inventing evidence.

    To unblock: obtain a daily reservoir inflow or level series for the site
    (India-WRIS, the operating authority, or CWC), put it in
    data/inflow/{site}_observed.csv as date,inflow_cumecs, and this becomes a
    real supervised problem.
    """
    raise NotImplementedError(
        "No observed inflow series available. See the docstring for what is "
        "needed - this is a data-acquisition blocker, not a coding one."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.07_ml.inflow")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--site", default="teesta")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", default=None, help="YYYY-MM-DD, default today")
    ap.add_argument("--cn", type=float, default=75.0)
    ap.add_argument("--capacity", type=float, default=None, help="MCM")
    args = ap.parse_args(argv)

    out = nowcast(
        args.lat, args.lon, args.site, args.days, args.end, args.cn,
        capacity_mcm=args.capacity,
    )
    c, s = out["catchment"], out["summary"]
    print(f"catchment {c['area_km2']:,} km2 ({c['cells']:,} cells upstream)")
    print(f"window {out['window'][0]} to {out['window'][1]}")
    print(f"  total rain      {s['total_rain_mm']:>8} mm")
    print(f"  max daily rain  {s['max_daily_rain_mm']:>8} mm")
    print(f"  peak inflow     {s['peak_inflow_cumecs']:>8} m3/s")
    print(f"  total inflow    {s['total_inflow_mcm']:>8} MCM")
    if "inflow_as_pct_of_capacity" in s:
        print(f"  = {s['inflow_as_pct_of_capacity']}% of reservoir capacity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
