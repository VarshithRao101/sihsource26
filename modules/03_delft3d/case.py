"""
modules/03_delft3d/case.py - turn one of our scenarios into a Delft3D-FLOW case.

    UNVERIFIED. THIS HAS NEVER BEEN RUN AGAINST A KERNEL.

    There is no Delft3D kernel on this machine - engine.py reports it absent -
    so every line below is written from the file formats and never executed.
    AGENTS.md Part 1 does not allow us to call this working, and nothing in the
    repository presents it as such: compare_engines.py still reports Delft3D
    absent, and the workflow node still never turns green. What this file buys
    is that the day a kernel exists, the case is one command away instead of a
    day away.

    The formats below are NOT from memory. They were read off Deltares' own
    f34 example (examples/delft3d4/11_standard_netcdf in github.com/Deltares/
    Delft3D), and the places where memory would have been wrong are marked.

Delft3D-FLOW reads a keyword file plus a handful of attribute files:

    <run>.mdf     keyword = value control file; filenames are wrapped in #...#
    <run>.grd     RGFGRID curvilinear grid - we write a rectilinear one
    <run>.enc     grid enclosure: a closed polygon in (m,n) INDEX space
    <run>.dep     bed level, POSITIVE DOWN, at MNKmax resolution
    <run>.bnd     open boundary sections
    <run>.bct     the time series feeding those boundaries
    <run>.src     discharge source locations, in (m,n)
    <run>.dis     discharge time series
    config_d_hydro.xml   what d_hydro.exe actually reads; it names the mdf

FOUR CONVENTIONS THAT ARE SILENT WHEN WRONG. Each was checked against f34.

  1. MNKmax IS THE GRID PLUS ONE. f34.grd declares "14 21" and f34.mdf declares
     "MNKmax = 15 22 5". Delft3D carries a dummy row and column. f34.dep then
     holds 330 values = 15 x 22, NOT 14 x 21. Get this backwards and the depth
     array is read with the wrong stride: the run completes and the bed is
     sheared, which looks like a modelling result rather than a bug.

  2. DEPTH IS POSITIVE DOWN. The .dep file stores depth below the reference
     level, so it is the NEGATIVE of our elevation. A sign error here inverts
     the terrain and water runs up the valley walls.

  3. TIME IS IN Tunit, NOT SECONDS. Tunit = #M# means Tstart, Tstop, Dt, Flmap
     and every table's time column are MINUTES since Itdate. Passing seconds
     gives a run 60x too long, which is the same failure mode SFINCS had with
     its datetime tstart.

  4. GRIDS ARE WRITTEN BOTTOM-UP. RGFGRID's N index increases northward and our
     rasters are north-up, so every array is flipped on the way out and flipped
     back on the way in - exactly as modules/09_sfincs/case.py does.

WHAT IS DELIBERATELY SIMPLE, and would be the first thing to revisit once a
kernel can actually run it: one water-level boundary, placed on whichever domain
edge holds the lowest bed, held at that edge's own bed level. That is the
crudest defensible outflow condition. The rim treatment SFINCS gets with msk=3
has no one-line equivalent here, and inventing a more elaborate boundary that
has never been tested would be worse than a simple one that has not.

Owner: captain.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# Delft3D's own drying/flooding threshold. Ours is 0.05 m from
# shared.contract.WET_THRESHOLD_M, and f34 happens to use the same value.
# Keeping them equal is what makes an extent comparison mean anything.
DRYFLC_M = 0.05

# The dummy value f34.dep uses for cells outside the enclosure.
DEP_DUMMY = -999.0

ITDATE = "2026-01-01"
REFERENCE_TIME = "20260101"


@dataclass
class Delft3DCase:
    """Where the case was written and what went into it."""

    case_dir: Path
    run_id: str
    nx: int
    ny: int
    mmax: int
    nmax: int
    dx_m: float
    end_hr: float
    dt_minutes: float
    manning: float
    src_mn: tuple[int, int]
    peak_dis_cumecs: float
    boundary_edge: str
    boundary_level_m: float

    def as_dict(self) -> dict:
        return {
            "engine": "Delft3D-FLOW (Delft3D 4, structured)",
            "case_dir": str(self.case_dir),
            "run_id": self.run_id,
            "our_grid": [self.nx, self.ny],
            "mnkmax": [self.mmax, self.nmax, 1],
            "dx_m": round(self.dx_m, 3),
            "end_hr": self.end_hr,
            "dt_minutes": self.dt_minutes,
            "manning": self.manning,
            "source_mn": list(self.src_mn),
            "peak_discharge_cumecs": round(self.peak_dis_cumecs, 1),
            "boundary_edge": self.boundary_edge,
            "boundary_level_m": round(self.boundary_level_m, 3),
            "dryflc_m": DRYFLC_M,
            "verified": False,
            "note": (
                "Written from the Deltares f34 example formats and NEVER RUN - "
                "no Delft3D kernel exists on the machine that produced this. "
                "Not evidence of a Delft3D result."
            ),
        }


# --------------------------------------------------------------------------
# The attribute files
# --------------------------------------------------------------------------


def _fmt_e(v: float) -> str:
    """The 7-decimal exponential the MDF uses: 5.0000000e-002."""
    return f"{v: .7e}"


def _write_grd(path: Path, nx: int, ny: int, dx_m: float) -> None:
    """RGFGRID grid file: nx by ny grid points on a local Cartesian metre grid.

    Values are written 5 per line as in f34, x block first then y block, with
    ETA= naming the row. Row 1 is the SOUTHERNMOST, so the caller's north-up
    array is flipped before it ever gets here.
    """
    lines = [
        "* Generated by modules/03_delft3d/case.py - SIH26161",
        "* Local Cartesian grid in metres, 1:1 with our own solver grid.",
        "Coordinate System = Cartesian",
        "Missing Value     =          0.000",
        f"{nx:>8}{ny:>8}",
        " 0.000000000000000000E+00 0.000000000000000000E+00 0.000000000000000000E+00",
    ]

    def block(values_for_row) -> None:
        for n in range(ny):
            row = values_for_row(n)
            head = f" ETA={n + 1:>5}"
            for i in range(0, len(row), 5):
                chunk = "".join(f"{v: .18E}" for v in row[i:i + 5])
                lines.append((head if i == 0 else " " * len(head)) + chunk)

    block(lambda n: [m * dx_m for m in range(nx)])          # x, constant per row
    block(lambda n: [n * dx_m for _ in range(nx)])          # y, constant per row

    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def _write_enc(path: Path, mmax: int, nmax: int) -> None:
    """Grid enclosure: a closed rectangle in (m,n) index space.

    f34's enclosure is traced in MNKmax indices - it reaches (15,22) on a grid
    whose .grd says 14 21 - so this uses mmax/nmax too rather than nx/ny.
    """
    ring = [(1, 1), (mmax, 1), (mmax, nmax), (1, nmax), (1, 1)]
    lines = []
    for i, (m, n) in enumerate(ring):
        tag = ""
        if i == 0:
            tag = "   *** begin external enclosure"
        elif i == len(ring) - 1:
            tag = "   *** end external grid enclosure"
        lines.append(f"{m:>6}{n:>6}{tag}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def _write_dep(path: Path, dem: np.ndarray, mmax: int, nmax: int) -> None:
    """Bed level, POSITIVE DOWN, at (mmax x nmax) with a dummy row and column.

    dem is north-up elevation in metres. Delft3D wants depth below datum, so
    every value is negated, and the extra row/column that MNKmax adds is filled
    with the dummy f34 uses. Twelve values per line, as in f34.dep.
    """
    ny, nx = dem.shape
    grid = np.full((nmax, mmax), DEP_DUMMY, dtype=np.float64)

    flipped = np.flipud(dem)                       # north-up -> Delft3D bottom-up
    depth = np.where(np.isfinite(flipped), -flipped, DEP_DUMMY)
    grid[:ny, :nx] = depth

    out = []
    for n in range(nmax):
        row = grid[n]
        for i in range(0, mmax, 12):
            out.append("".join(f"{v:12.3f}" for v in row[i:i + 12]))
    path.write_text("\n".join(out) + "\n", encoding="ascii", newline="\n")


def _write_src(path: Path, m: int, n: int) -> None:
    """One discharge source at (m,n), layer 1.

    f34's line is:  (14,2)               Y     14      2      1    N
    """
    label = f"({m},{n})"
    path.write_text(
        f"{label:<20} Y {m:>6} {n:>6} {1:>6}    N\n",
        encoding="ascii", newline="\n",
    )


def _table(name: str, location: str, param: str, unit: str,
           times_min: np.ndarray, values: np.ndarray) -> str:
    """Delft3D's table block, the format f34.dis uses for .dis and .bct alike."""
    head = [
        f"table-name           '{name}'",
        "contents             'regular  '",
        f"location             '{location:<20}'",
        "time-function        'non-equidistant'",
        f"reference-time       {REFERENCE_TIME}",
        "time-unit            'minutes'",
        "interpolation        'linear'",
        "parameter            'time                '                     unit '[min]'",
        f"parameter            '{param:<20}'                     unit '{unit}'",
        f"records-in-table     {len(times_min)}",
    ]
    body = [f"{t:.7e} {v:.7e}" for t, v in zip(times_min, values)]
    return "\n".join(head + body) + "\n"


def _write_dis(path: Path, m: int, n: int,
               t_hr: np.ndarray, q_cumecs: np.ndarray) -> None:
    """The breach hydrograph, in MINUTES because Tunit is #M#."""
    times_min = np.asarray(t_hr, float) * 60.0
    q = np.maximum(np.asarray(q_cumecs, float), 0.0)
    path.write_text(
        _table("Discharge : 1", f"({m},{n})",
               "flux/discharge rate", "[m**3/s]", times_min, q),
        encoding="ascii", newline="\n",
    )


def _write_bnd_bct(bnd: Path, bct: Path, mmax: int, nmax: int,
                   edge: str, level_m: float, end_hr: float) -> None:
    """One water-level boundary along a whole domain edge, held constant.

    f34's .bnd line is:
        SEA BOUNDARY         Z H     2    22    14    22       0.00
    name(20) type(Z=water level) forcing m1 n1 m2 n2 alpha. f34 uses H for
    harmonic, which needs a .bch; T takes a time series from a .bct, which is
    what a constant outflow level is easiest to express as.
    """
    spans = {
        "south": (1, 1, mmax, 1),
        "north": (1, nmax, mmax, nmax),
        "west": (1, 1, 1, nmax),
        "east": (mmax, 1, mmax, nmax),
    }
    m1, n1, m2, n2 = spans[edge]
    name = "OUTFLOW"
    bnd.write_text(
        f"{name:<20} Z T {m1:>5} {n1:>5} {m2:>5} {n2:>5}    0.0000000e+00\n",
        encoding="ascii", newline="\n",
    )

    # A boundary section is forced at BOTH ends, so the table carries two
    # parameters - A and B - not one.
    times_min = np.array([0.0, end_hr * 60.0])
    head = [
        "table-name           'Boundary Section : 1'",
        "contents             'Uniform          '",
        f"location             '{name:<20}'",
        "time-function        'non-equidistant'",
        f"reference-time       {REFERENCE_TIME}",
        "time-unit            'minutes'",
        "interpolation        'linear'",
        "parameter            'time                '                     unit '[min]'",
        "parameter            'water elevation (z)  end A'               unit '[m]'",
        "parameter            'water elevation (z)  end B'               unit '[m]'",
        f"records-in-table     {len(times_min)}",
    ]
    body = [f"{t:.7e} {level_m:.7e} {level_m:.7e}" for t in times_min]
    bct.write_text("\n".join(head + body) + "\n", encoding="ascii", newline="\n")


def _write_config(path: Path, mdf_name: str) -> None:
    """config_d_hydro.xml - what d_hydro.exe reads. It names flow2d3d and the mdf."""
    path.write_text(
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        '<deltaresHydro xmlns="http://schemas.deltares.nl/deltaresHydro">\n'
        "    <documentation>\n"
        "        Generated by modules/03_delft3d/case.py - SIH26161\n"
        "    </documentation>\n"
        "    <control>\n"
        "        <sequence>\n"
        "            <start>myNameFlow</start>\n"
        "        </sequence>\n"
        "    </control>\n"
        '    <flow2D3D name="myNameFlow">\n'
        "        <library>flow2d3d</library>\n"
        f"        <mdfFile>{mdf_name}</mdfFile>\n"
        "    </flow2D3D>\n"
        "</deltaresHydro>\n",
        encoding="ascii", newline="\n",
    )


# --------------------------------------------------------------------------
# The case
# --------------------------------------------------------------------------


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
    dt_minutes: float = 0.5,
    map_interval_min: float = 15.0,
    run_id: str = "sih",
) -> Delft3DCase:
    """Write a complete Delft3D-FLOW case. Returns what was written.

    Args:
        case_dir: written here; created if absent.
        dem: elevation, north-up, NaN where no data.
        dx_m: cell size in metres, square cells.
        src_row, src_col: the cell the breach discharge enters, DEM indexing.
        t_hr, q_cumecs: the hydrograph, from shared.hydro.
        end_hr: run duration.
        manning: uniform Manning n. Roumet is set to #M# so Ccofu/Ccofv are
            Manning values rather than f34's White-Colebrook ones.
        dt_minutes: computational timestep. Delft3D is implicit (ADI) so this is
            far larger than our explicit solver's, but it is NOT unconditional -
            too large and the run degrades rather than crashing.
        map_interval_min: how often to write a map field.
    """
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    ny, nx = dem.shape
    mmax, nmax = nx + 1, ny + 1          # convention 1 - see the module docstring

    _write_grd(case_dir / f"{run_id}.grd", nx, ny, dx_m)
    _write_enc(case_dir / f"{run_id}.enc", mmax, nmax)
    _write_dep(case_dir / f"{run_id}.dep", dem, mmax, nmax)

    # Delft3D indexes (m,n) from 1, m across and n up. Our row index counts from
    # the north, so it is flipped into Delft3D's northward n.
    src_m = int(src_col) + 1
    src_n = (ny - 1 - int(src_row)) + 1
    _write_src(case_dir / f"{run_id}.src", src_m, src_n)
    _write_dis(case_dir / f"{run_id}.dis", src_m, src_n, t_hr, q_cumecs)

    # Outflow on whichever edge holds the lowest bed - the crude but defensible
    # choice documented at the top of this file.
    finite = np.where(np.isfinite(dem), dem, np.inf)
    edges = {
        "north": float(np.min(finite[0, :])),
        "south": float(np.min(finite[-1, :])),
        "west": float(np.min(finite[:, 0])),
        "east": float(np.min(finite[:, -1])),
    }
    edge = min(edges, key=edges.get)
    level = edges[edge]
    if not np.isfinite(level):
        level = float(np.nanmin(dem))
    _write_bnd_bct(case_dir / f"{run_id}.bnd", case_dir / f"{run_id}.bct",
                   mmax, nmax, edge, level, end_hr)

    stop_min = end_hr * 60.0
    mdf = f"""Ident  = #Delft3D-FLOW written by SIH26161#
Commnt =
Runtxt = #SIH26161 dam break / river blockage#
         #UNVERIFIED CASE - never run against a kernel#
Filcco = #{run_id}.grd#
Anglat =  0.0000000e+000
Grdang =  0.0000000e+000
Filgrd = #{run_id}.enc#
MNKmax = {mmax} {nmax} 1
Thick  =  1.0000000e+002
Commnt =
Fildep = #{run_id}.dep#
Commnt =
Itdate = #{ITDATE}#
Tunit  = #M#
Tstart = {_fmt_e(0.0)}
Tstop  = {_fmt_e(stop_min)}
Dt     = {dt_minutes}
Tzone  = 0
Commnt =
Zeta0  = {_fmt_e(level)}
Commnt =
Filbnd = #{run_id}.bnd#
FilbcT = #{run_id}.bct#
Commnt =
Ag     =  9.8130000e+000
Rhow   =  1.0000000e+003
Commnt =
Roumet = #M#
Ccofu  = {_fmt_e(manning)}
Ccofv  = {_fmt_e(manning)}
Xlo    =  0.0000000e+000
Vicouv =  1.0000000e+000
Dicouv =  1.0000000e+001
Htur2d = #N#
Irov   = 0
Commnt =
Iter   =      2
Dryflp = #YES#
Dpsopt = #MAX#
Dpuopt = #MIN#
Dryflc = {_fmt_e(DRYFLC_M)}
Dco    = -9.9999900e+002
Tlfsmo = {_fmt_e(0.0)}
Forfuv = #Y#
Forfww = #N#
Sigcor = #N#
Trasol = #Cyclic-method#
Momsol = #Cyclic#
Commnt =
Filsrc = #{run_id}.src#
Fildis = #{run_id}.dis#
Commnt =
Flmap  = {_fmt_e(0.0)} {map_interval_min:g} {_fmt_e(stop_min)}
Flhis  = {_fmt_e(0.0)} {map_interval_min:g} {_fmt_e(stop_min)}
Flpp   = {_fmt_e(0.0)} 0 {_fmt_e(0.0)}
Flrst  = 0
Commnt =
Online = #N#
FlNcdf = #map his#
Commnt =
"""
    (case_dir / f"{run_id}.mdf").write_text(mdf, encoding="ascii", newline="\n")
    _write_config(case_dir / "config_d_hydro.xml", f"{run_id}.mdf")

    return Delft3DCase(
        case_dir=case_dir,
        run_id=run_id,
        nx=nx, ny=ny, mmax=mmax, nmax=nmax,
        dx_m=dx_m,
        end_hr=end_hr,
        dt_minutes=dt_minutes,
        manning=manning,
        src_mn=(src_m, src_n),
        peak_dis_cumecs=float(np.max(q_cumecs)) if len(q_cumecs) else 0.0,
        boundary_edge=edge,
        boundary_level_m=level,
    )


def run_case(case_dir: Path | str, kernel: Path | str,
             timeout_s: int = 7200) -> dict:
    """Run d_hydro in `case_dir`. Never raises on solver failure.

    d_hydro takes the config XML, not the mdf. It also needs flow2d3d.dll
    beside it - engine.detect() checks for exactly that and refuses to call a
    lone d_hydro installed.
    """
    case_dir = Path(case_dir)
    log_path = case_dir / "delft3d.log"

    proc = subprocess.run(
        [str(kernel), "config_d_hydro.xml"],
        cwd=str(case_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(text, encoding="utf-8", errors="replace")

    produced = sorted(
        p.name for p in case_dir.iterdir()
        if p.suffix.lower() in (".nc", ".dat", ".def")
    )
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "log": str(log_path),
        "outputs": produced,
        "tail": "\n".join(text.strip().splitlines()[-15:]),
    }


def read_map(case_dir: Path | str, run_id: str = "sih",
             dem: np.ndarray | None = None) -> dict:
    """Read trim-<run_id>.nc back onto OUR north-up grid.

    FlNcdf = #map his# makes Delft3D write netCDF rather than its NEFIS .dat /
    .def pair, which needs a NEFIS reader we do not have. Grids come back
    bottom-up and are flipped, and when `dem` is supplied the bed Delft3D echoes
    is compared against it - the same orientation check module 09 does, which
    catches a mirrored flood immediately rather than after it looks plausible.
    """
    import xarray as xr

    case_dir = Path(case_dir)
    nc = case_dir / f"trim-{run_id}.nc"
    if not nc.exists():
        raise FileNotFoundError(f"{nc} - Delft3D produced no netCDF map output")

    with xr.open_dataset(nc) as ds:
        # S1 is water level, DPS the bed. Both are (time, n, m).
        s1 = np.asarray(ds["S1"].values)
        dps = np.asarray(ds["DPS"].values)
        if dps.ndim == 3:
            dps = dps[0]

    bed = -np.flipud(dps).astype(np.float64)          # positive down -> elevation
    levels = np.flipud(np.nanmax(s1, axis=0)).astype(np.float32)

    ny, nx = (dem.shape if dem is not None else bed.shape)
    bed = bed[:ny, :nx]
    levels = levels[:ny, :nx]

    if dem is not None:
        valid = np.isfinite(dem) & np.isfinite(bed)
        if valid.any():
            err = float(np.nanmax(np.abs(bed[valid] - dem[valid])))
            if err > 0.01:
                raise ValueError(
                    f"Delft3D bed differs from our DEM by up to {err:.3f} m - "
                    "the grid is flipped, sheared or misaligned, so the flood "
                    "would be too. Check MNKmax against the .grd dimensions."
                )

    depth = np.where(np.isfinite(levels) & np.isfinite(bed), levels - bed, 0.0)
    depth = np.where(depth > 0, depth, 0.0).astype(np.float32)
    return {
        "max_depth_m": depth,
        "max_water_level_m": levels,
        "wet_cells": int((depth >= DRYFLC_M).sum()),
        "max_depth_max_m": float(depth.max()) if depth.size else 0.0,
    }
