"""
modules/09_sfincs/case.py - turn one of our scenarios into a SFINCS case.

SFINCS reads a keyword/value control file plus a handful of ASCII grids, all
documented in SFINCS_v2.4.0_Galibier_release_manual_report.pdf sections 1.10 and
1.14. Nothing here is guessed; every file format below is from that manual.

    Output is netCDF (`outputformat = net`, the documented default). The binary
    writer stores only active cells in SFINCS' own compressed ordering, which
    needs the index file to decode; the ASCII writer crashes in this build
    ("unformatted I/O to unit open for formatted transfers"). netCDF carries its
    own x/y coordinates and is unambiguous.

    sfincs.inp   keyword = value control file (inputformat = asc, or SFINCS
                 defaults to binary and demands an index file we do not have)
    sfincs.dep   elevation at cell centres, m above a reference level
    sfincs.msk   0 inactive, 1 active, 2 water-level boundary, 3 outflow
    sfincs.src   discharge point locations, projected metres
    sfincs.dis   discharge time series, seconds since tref, m3/s

Two conventions worth writing down because getting either wrong is silent:

  * The map output interval keyword is `dtout`, NOT `dtmapout`. An unknown
    keyword is silently ignored and SFINCS then writes a snapshot every
    timestep - a two-hour run on a 60x40 grid produced 933 MB before it was
    killed. If an output file is growing without bound, check this first.

  * SFINCS grids are written bottom-up. The manual's example is
    `<zb x0,y0> <zb x1,y0>` on the first line, so row 0 is the SOUTHERNMOST
    row. Our rasters are north-up, so every grid is flipped on the way out and
    flipped back on the way in.

  * We build the case on a LOCAL Cartesian grid in metres (x0 = y0 = 0,
    dx = dy = our cell size) rather than reprojecting to UTM. SFINCS never needs
    the true CRS to compute, and this keeps a 1:1 cell correspondence with our
    own solver - which is the entire point, since the two are being compared.
    It is the same flat-earth approximation our solver already makes at 90 m
    over tens of kilometres.

Owner: captain.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# SFINCS treats anything drier than this as dry. Ours is 0.05 m
# (shared.contract.WET_THRESHOLD_M); keeping them equal is what makes an extent
# comparison between the two engines mean anything.
HUTHRESH_M = 0.05

TREF = "20260101 000000"


@dataclass
class SfincsCase:
    """Where the case was written and what went into it."""

    case_dir: Path
    nx: int
    ny: int
    dx_m: float
    end_hr: float
    manning: float
    src_xy_m: tuple[float, float]
    peak_dis_cumecs: float
    active_cells: int
    outflow_cells: int

    def as_dict(self) -> dict:
        return {
            "engine": "SFINCS",
            "case_dir": str(self.case_dir),
            "grid": [self.nx, self.ny],
            "dx_m": round(self.dx_m, 3),
            "end_hr": self.end_hr,
            "manning": self.manning,
            "source_xy_m": [round(v, 1) for v in self.src_xy_m],
            "peak_discharge_cumecs": round(self.peak_dis_cumecs, 1),
            "active_cells": self.active_cells,
            "outflow_cells": self.outflow_cells,
            "huthresh_m": HUTHRESH_M,
            "note": (
                "SFINCS is Deltares' open-source reduced-physics flood model. It "
                "is NOT Delft3D. Local Cartesian grid in metres, 1:1 with our own "
                "solver grid so the two can be compared cell by cell."
            ),
        }


def _write_grid_asc(path: Path, arr: np.ndarray, fmt: str) -> None:
    """Write a 2D grid bottom-up, one row per line, space separated."""
    flipped = np.flipud(arr)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        for row in flipped:
            fh.write(" ".join(fmt % v for v in row))
            fh.write("\n")


def build_mask(dem: np.ndarray) -> np.ndarray:
    """Active where the DEM is valid; the domain rim is outflow.

    msk = 3 on the rim lets water leave instead of piling up against a wall.
    A closed domain would conserve mass beautifully and model nothing.
    """
    msk = np.where(np.isfinite(dem), 1, 0).astype(np.int32)
    msk[0, :] = np.where(msk[0, :] == 1, 3, 0)
    msk[-1, :] = np.where(msk[-1, :] == 1, 3, 0)
    msk[:, 0] = np.where(msk[:, 0] == 1, 3, 0)
    msk[:, -1] = np.where(msk[:, -1] == 1, 3, 0)
    return msk


def write_case(
    case_dir: Path | str,
    dem: np.ndarray,
    dx_m: float,
    src_row: int,
    src_col: int,
    t_hr: np.ndarray,
    q_cumecs: np.ndarray,
    end_hr: float = 12.0,
    manning: float = 0.035,
    dtmapout_s: float = 900.0,
) -> SfincsCase:
    """Write a complete, runnable SFINCS case. Returns what was written.

    Args:
        case_dir: written here; created if absent.
        dem: elevation, north-up, NaN where no data.
        dx_m: cell size in metres, square cells.
        src_row, src_col: cell the breach discharge enters, in DEM indexing.
        t_hr, q_cumecs: the hydrograph, from shared.hydro.
        end_hr: run duration.
        manning: uniform roughness.
        dtmapout_s: map output interval.
    """
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    ny, nx = dem.shape
    filled = np.where(np.isfinite(dem), dem, -9999.0).astype(np.float64)
    msk = build_mask(dem)

    _write_grid_asc(case_dir / "sfincs.dep", filled, "%.3f")
    _write_grid_asc(case_dir / "sfincs.msk", msk, "%d")

    # Source point, in local metres at the cell centre. The DEM row index counts
    # from the north, so it is flipped to SFINCS' bottom-up y.
    x_m = (src_col + 0.5) * dx_m
    y_m = ((ny - 1 - src_row) + 0.5) * dx_m
    (case_dir / "sfincs.src").write_text(f"{x_m:.2f} {y_m:.2f}\n", encoding="ascii")

    lines = []
    for t, q in zip(np.asarray(t_hr, float), np.asarray(q_cumecs, float)):
        lines.append(f"{t * 3600.0:.1f} {max(float(q), 0.0):.3f}")
    (case_dir / "sfincs.dis").write_text("\n".join(lines) + "\n", encoding="ascii")

    # tstart/tstop are DATETIMES, not seconds. The manual's example is
    # `tstart = 20221116 180000`. Passing seconds here parses as a nonsense date
    # and the run never terminates - it sat at "0% complete" writing 621 MB.
    _t0dt = datetime.strptime(TREF, "%Y%m%d %H%M%S")
    _t1dt = _t0dt + timedelta(hours=float(end_hr))
    _t0 = _t0dt.strftime("%Y%m%d %H%M%S")
    _t1 = _t1dt.strftime("%Y%m%d %H%M%S")

    inp = f"""x0 = 0
y0 = 0
mmax = {nx}
nmax = {ny}
dx = {dx_m:.4f}
dy = {dx_m:.4f}
rotation = 0
tref = {TREF}
tstart = {_t0}
tstop = {_t1}
inputformat = asc
depfile = sfincs.dep
mskfile = sfincs.msk
srcfile = sfincs.src
disfile = sfincs.dis
manning = {manning}
huthresh = {HUTHRESH_M}
advection = 1
alpha = 0.5
theta = 1.0
dtout = {dtmapout_s:.0f}
dtmaxout = {end_hr * 3600.0:.0f}
dthisout = 0
outputformat = net
"""
    (case_dir / "sfincs.inp").write_text(inp, encoding="ascii", newline="\n")

    return SfincsCase(
        case_dir=case_dir,
        nx=nx,
        ny=ny,
        dx_m=dx_m,
        end_hr=end_hr,
        manning=manning,
        src_xy_m=(x_m, y_m),
        peak_dis_cumecs=float(np.max(q_cumecs)) if len(q_cumecs) else 0.0,
        active_cells=int((msk == 1).sum()),
        outflow_cells=int((msk == 3).sum()),
    )


def run_case(case_dir: Path | str, exe: Path | str, timeout_s: int = 3600) -> dict:
    """Run SFINCS in `case_dir`. Returns the outcome; never raises on solver failure."""
    case_dir = Path(case_dir)
    log_path = case_dir / "sfincs.log"

    proc = subprocess.run(
        [str(exe)],
        cwd=str(case_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(text, encoding="utf-8", errors="replace")

    produced = sorted(
        p.name for p in case_dir.iterdir() if p.suffix.lower() in (".dat", ".txt", ".nc")
    )
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "log": str(log_path),
        "outputs": produced,
        "tail": "\n".join(text.strip().splitlines()[-12:]),
    }


def read_map(case_dir: Path | str, dem: np.ndarray | None = None) -> dict:
    """Read sfincs_map.nc back onto OUR north-up grid.

    SFINCS writes bottom-up, so every grid is flipped on the way in. When `dem`
    is supplied the bed level SFINCS echoes back is compared against it, which
    catches an orientation mistake immediately instead of silently mirroring the
    flood. That check is cheap and the failure it prevents is invisible.

    Returns max depth and max water level as float32 arrays shaped like the DEM.
    """
    import xarray as xr

    case_dir = Path(case_dir)
    nc = case_dir / "sfincs_map.nc"
    if not nc.exists():
        raise FileNotFoundError(f"{nc} - SFINCS produced no map output")

    with xr.open_dataset(nc) as ds:
        hmax = np.flipud(np.asarray(ds["hmax"].values).squeeze()).astype(np.float32)
        zsmax = np.flipud(np.asarray(ds["zsmax"].values).squeeze()).astype(np.float32)
        zb = np.flipud(np.asarray(ds["zb"].values).squeeze()).astype(np.float64)

    if dem is not None:
        valid = np.isfinite(dem) & np.isfinite(zb)
        if valid.any():
            err = float(np.nanmax(np.abs(zb[valid] - dem[valid])))
            if err > 0.01:
                raise ValueError(
                    f"SFINCS bed level differs from our DEM by up to {err:.3f} m - "
                    "the grid is flipped or misaligned, so the flood would be too"
                )

    depth = np.where(np.isfinite(hmax) & (hmax > 0), hmax, 0.0).astype(np.float32)
    return {
        "max_depth_m": depth,
        "max_water_level_m": zsmax,
        "wet_cells": int((depth >= HUTHRESH_M).sum()),
        "max_depth_max_m": float(depth.max()) if depth.size else 0.0,
    }
