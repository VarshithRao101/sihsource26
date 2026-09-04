"""
modules/03_delft3d/case.py - turn one of our scenarios into a Delft3D-FLOW case.

    VERIFIED AGAINST A REAL KERNEL on 2026-09-04, and the limits of that are
    written down below because they matter.

    WHAT WAS VERIFIED. Delft3D 4 was compiled from source on this machine
    (d_hydro.exe + flow2d3d.dll, GPLv3, no licence), Deltares' own f34 example
    was run first as a control and succeeded, and then a case written by THIS
    module was run: it solved to completion - "0 errors", d_hydro shutting down
    normally - and read_map pulled the result back onto our north-up grid with
    the bed Delft3D echoed matching our own DEM to within a centimetre.

    WHAT WAS NOT. That was a small synthetic channel, not one of our real
    scenarios, and the result has NOT yet been compared against our own solver
    on a real dam break. Until it has, this proves the case format is right and
    nothing about whether the two engines agree.

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

  5. EVERY Fil* KEY NEEDS ITS Fmt* PARTNER. Delft3D does not infer the format
     of an attribute file: f34 pairs Filcco with Fmtcco= #FR#, Fildep with
     Fmtdep= #FR#, and so on for every file it names. Omitting them was the
     first thing that actually broke - the kernel aborted in "Initialisation
     Time Dep. Data" with forrtl severe (64), an internal formatted read
     failure, before reading a single cell. Found by running Deltares' own f34
     example as a control, which succeeded, and diffing its MDF against ours.

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
            "verified": True,
            "note": (
                "The case format is verified: a case written by this module "
                "solved on a locally built Delft3D 4 kernel and read back with "
                "the bed matching our DEM to within a centimetre. That was a "
                "synthetic channel; these two engines have NOT yet been "
                "compared on a real scenario."
            ),
        }


# --------------------------------------------------------------------------
# The attribute files
# --------------------------------------------------------------------------


def _e3(v: float) -> str:
    """A number with a THREE-DIGIT exponent, for the .dis and .bct data rows.

    Same trap as the MDF and a separate code path, which is why it bit twice:
    Fortran writes 8.0000000e+002, Python writes 8.0000000e+02, and Delft3D
    crashes on the short form with a stack buffer overrun (0xC0000409) rather
    than a readable error. Verified by bisection against f34's own tables.
    """
    mant, exp = f"{v:.7e}".split("e")
    return f"{mant}e{exp[0]}{int(exp[1:]):03d}"


def _fmt_e(v: float) -> str:
    """The MDF's numeric field, byte-for-byte as f34 writes it.

    THREE-DIGIT EXPONENT, RIGHT-JUSTIFIED TO 16. Fortran's E editing writes
    "5.5000000e+001"; Python's %e writes "5.5000000e+01". With the keyword
    field fixed at six characters the value that follows is read positionally
    too, so a two-digit exponent leaves the number one character short and the
    read fails or silently takes the wrong digits. f34 lines up exactly at 16:

        Ag    =  9.8130000e+000
        Dco   = -9.9999900e+002
    """
    mant, exp = f"{v:.7e}".split("e")
    return f"{mant}e{exp[0]}{int(exp[1:]):03d}".rjust(16)


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
    # OFFSET BY ONE IN BOTH AXES. Delft3D's first row and column are the dummy
    # edge of the enclosure, not cells. Writing our data from index 0 puts every
    # value one cell off in m and n - the run completes and the terrain is
    # shifted diagonally under the flood. Caught by read_map's bed check, which
    # compared Delft3D's echoed bed against our DEM and found 116 m of
    # disagreement.
    grid[1:ny + 1, 1:nx + 1] = depth

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
    body = ["".join(_fmt_e(x) for x in (t, v))
            for t, v in zip(times_min, values)]
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
    # A BOUNDARY SECTION MUST NOT INCLUDE THE ENCLOSURE'S CORNER POINTS.
    # Spanning the full edge (1 .. mmax) crashes the kernel outright with
    # 0xC0000409, a stack buffer overrun, and no diagnostic at all. f34 keeps
    # clear of them the same way: its boundary runs m = 2 .. 14 on a grid whose
    # mmax is 15. So every span starts at 2 and stops one short of the far edge.
    spans = {
        "south": (2, 1, mmax - 1, 1),
        "north": (2, nmax, mmax - 1, nmax),
        "west": (1, 2, 1, nmax - 1),
        "east": (mmax, 2, mmax, nmax - 1),
    }
    m1, n1, m2, n2 = spans[edge]
    name = "OUTFLOW"
    bnd.write_text(
        # The alpha column takes the same three-digit exponent as the rest.
        # This literal was the THIRD place the two-digit form hid: the MDF,
        # the table data rows, and here.
        f"{name:<20} Z T {m1:>5} {n1:>5} {m2:>5} {n2:>5}{_fmt_e(0.0)}\n",
        encoding="ascii", newline="\n",
    )

    # A boundary section is forced at BOTH ends, so the table carries two
    # parameters - A and B - not one.
    times_min = np.array([0.0, end_hr * 60.0])
    head = [
        "table-name           'Boundary Section : 1'",
        # 20 characters inside the quotes. Delft3D reads these name fields
        # positionally; a short one shifts the columns after it.
        "contents             'Uniform             '",
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
    body = ["".join(_fmt_e(x) for x in (t, level_m, level_m))
            for t in times_min]
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

    # CELLS VERSUS GRID POINTS. Our DEM is nx by ny CELLS. An RGFGRID .grd holds
    # the CORNERS, so nx by ny cells need (nx+1) by (ny+1) points, and MNKmax is
    # then two larger than the cell count - Delft3D treats BOTH the first and
    # the last index as dummy, not just the first.
    #
    # Writing the cells as points instead cost a row: Delft3D returned zeros for
    # the last row of bed, because there was nowhere for it to go. read_map's
    # bed check is what surfaced it.
    mmax, nmax = nx + 2, ny + 2

    _write_grd(case_dir / f"{run_id}.grd", nx + 1, ny + 1, dx_m)
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

    # THE KEYWORD FIELD IS SIX CHARACTERS WIDE AND "=" SITS AT COLUMN 7.
    # f34 writes "Filcco= #f34.grd#", not "Filcco = #f34.grd#". Delft3D reads
    # the key as a fixed-width field, so one extra space shifts every value one
    # character right and the numeric parse lands on the wrong characters. That
    # is exactly what forrtl severe (64) "internal formatted read" was - not a
    # bad value anywhere, every value one column out. Never hand-align these.
    def kv(key, value):
        return f"{key:<6}={value}"

    L = [
        # NO Ident LINE, DELIBERATELY. Delft3D parses that field for its own
        # version stamp - f34 carries "#Delft3D-FLOW  .03.02 3.41.06.10981#"
        # - and free text there fails the internal read with forrtl severe
        # (64) before anything else is looked at. Omitting it is accepted,
        # and is preferable to writing a version string we did not verify.
        kv("Commnt", " " * 30),
        kv("Runtxt", " #SIH26161 dam break / river blockage#"),
        kv("Filcco", f" #{run_id}.grd#"), kv("Fmtcco", " #FR#"),
        kv("Anglat", _fmt_e(0.0)), kv("Grdang", _fmt_e(0.0)),
        kv("Filgrd", f" #{run_id}.enc#"), kv("Fmtgrd", " #FR#"),
        kv("MNKmax", f" {mmax} {nmax} 1"),
        kv("Thick", _fmt_e(100.0)),
        kv("Commnt", " " * 30),
        kv("Fildep", f" #{run_id}.dep#"), kv("Fmtdep", " #FR#"),
        kv("Commnt", " " * 30),
        kv("Itdate", f" #{ITDATE}#"), kv("Tunit", " #M#"),
        kv("Tstart", _fmt_e(0.0)), kv("Tstop", _fmt_e(stop_min)),
        kv("Dt", f" {dt_minutes:g}"), kv("Tzone", " 0"),
        kv("Commnt", " " * 30),
        kv("Sub1", " #    #"), kv("Sub2", " #   #"),
        kv("Commnt", " " * 30),
        kv("Wnsvwp", " #N#"), kv("Wndint", " #Y#"),
        kv("Commnt", " " * 30),
        kv("Zeta0", _fmt_e(level)),
        kv("U0", " [.]"), kv("V0", " [.]"), kv("S0", " [.]"),
        kv("Commnt", " " * 30),
        kv("Filbnd", f" #{run_id}.bnd#"), kv("Fmtbnd", " #FR#"),
        kv("FilbcT", f" #{run_id}.bct#"), kv("FmtbcT", " #FR#"),
        kv("Commnt", " " * 30),
        kv("Ag", _fmt_e(9.813)), kv("Rhow", _fmt_e(1000.0)),
        kv("Alph0", " [.]"),
        kv("Tempw", _fmt_e(0.0)), kv("Salw", _fmt_e(0.0)),
        kv("Rouwav", " #    #"),
        kv("Wstres", f"{_fmt_e(2.5e-3)}{_fmt_e(0.0)}{_fmt_e(2.5e-3)}{_fmt_e(100.0)}"),
        kv("Rhoa", _fmt_e(1.0)), kv("Betac", _fmt_e(0.5)),
        kv("Equili", " #N#"), kv("Tkemod", " #Algebraic   #"),
        kv("Ktemp", " 0"), kv("Fclou", _fmt_e(0.0)),
        kv("Sarea", _fmt_e(0.0)), kv("Temint", " #Y#"),
        kv("Commnt", " " * 30),
        # Roumet #M# makes Ccofu/Ccofv Manning n rather than f34's
        # White-Colebrook coefficients.
        kv("Roumet", " #M#"),
        kv("Ccofu", _fmt_e(manning)), kv("Ccofv", _fmt_e(manning)),
        kv("Xlo", _fmt_e(0.0)),
        kv("Vicouv", _fmt_e(1.0)), kv("Dicouv", _fmt_e(10.0)),
        kv("Htur2d", " #N#"),
        kv("Vicoww", _fmt_e(1e-6)), kv("Dicoww", _fmt_e(1e-6)),
        kv("Irov", " 0"),
        kv("Commnt", " " * 30),
        kv("Iter", "      2"), kv("Dryflp", " #YES#"),
        # Dpsopt #DP# takes the bed AT THE CELL, as written. f34 uses #MAX#,
        # which derives each cell from its four corner depths - fine when the
        # .dep really holds corners, wrong for us because our DEM is
        # cell-centred, and it left a residual one-cell diagonal error of
        # exactly one row step plus one column step.
        kv("Dpsopt", " #DP#"), kv("Dpuopt", " #MIN#"),
        kv("Dryflc", _fmt_e(DRYFLC_M)), kv("Dco", _fmt_e(-999.999)),
        kv("Tlfsmo", _fmt_e(0.0)), kv("ThetQH", _fmt_e(0.0)),
        kv("Forfuv", " #Y#"), kv("Forfww", " #N#"), kv("Sigcor", " #N#"),
        kv("Trasol", " #Cyclic-method#"), kv("Momsol", " #Cyclic#"),
        kv("Commnt", " " * 30),
        kv("Filsrc", f" #{run_id}.src#"), kv("Fmtsrc", " #FR#"),
        kv("Fildis", f" #{run_id}.dis#"), kv("Fmtdis", " #FR#"),
        kv("Commnt", " " * 30),
        kv("SMhydr", " #YYYYY#"), kv("SMderv", " #YYYYYY#"),
        kv("SMproc", " #YYYYYYYYYY#"),
        kv("PMhydr", " #YYYYYY#"), kv("PMderv", " #YYY#"),
        kv("PMproc", " #YYYYYYYYYY#"),
        kv("SHhydr", " #YYYY#"), kv("SHderv", " #YYYYY#"),
        kv("SHproc", " #YYYYYYYYYY#"), kv("SHflux", " #YYYY#"),
        kv("PHhydr", " #YYYYYY#"), kv("PHderv", " #YYY#"),
        kv("PHproc", " #YYYYYYYYYY#"), kv("PHflux", " #YYYY#"),
        kv("Online", " #N#"), kv("Waqmod", " #N#"),
        kv("Flmap", f"{_fmt_e(0.0)} {map_interval_min:g} {_fmt_e(stop_min)}"),
        kv("Flhis", f"{_fmt_e(0.0)} {map_interval_min:g} {_fmt_e(stop_min)}"),
        kv("Flpp", f"{_fmt_e(0.0)} 0 {_fmt_e(0.0)}"),
        kv("Flrst", " 0"),
        kv("Commnt", " " * 30),
        kv("Addtim", " #Y#"),
        # Without this Delft3D writes NEFIS trim-*.dat/.def, which needs a
        # reader we do not have. read_map() looks for the netCDF first.
        kv("FlNcdf", " #map his#"),
        kv("Commnt", " " * 30),
    ]
    mdf = "\n".join(L) + "\n"
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
        # THE VARIABLE IS DPS0, NOT DPS, AND THE AXES ARE (M, N) NOT (N, M).
        # trim-*.nc carries S1 as (time, M, N) and the bed as DPS0 (M, N) -
        # Delft3D's own index order, which is the transpose of the (row, col)
        # our rasters use. Reading it without transposing gives a flood rotated
        # ninety degrees, which is the kind of wrong that still looks like a
        # result.
        s1 = np.asarray(ds["S1"].values)                  # (time, M, N)
        dps = np.asarray(ds["DPS0"].values)               # (M, N)

    # THE INVERSE OF _write_dep, IN THIS ORDER. Established by writing a .dep
    # whose every cell held a unique value n*100+m and reading back where each
    # one landed: DPS0[m, n] is exactly the value written at file (row n,
    # col m). So transpose first, then CROP OFF THE DUMMY ROW AND COLUMN, and
    # only then flip bottom-up to north-up. Flipping before cropping shifts the
    # data by one row and nothing lines up - which is what the bed check caught.
    ny, nx = (dem.shape if dem is not None else (dps.shape[1] - 1, dps.shape[0] - 1))

    bed_grid = -dps.T                                   # [n, m], nmax x mmax
    bed = np.flipud(bed_grid[1:ny + 1, 1:nx + 1]).astype(np.float64)

    lev_grid = np.nanmax(s1, axis=0).T                  # [n, m]
    levels = np.flipud(lev_grid[1:ny + 1, 1:nx + 1]).astype(np.float32)

    if dem is not None:
        valid = np.isfinite(dem) & np.isfinite(bed)
        if valid.any():
            err = float(np.nanmax(np.abs(bed[valid] - dem[valid])))
            if err > 0.01:
                raise ValueError(
                    f"Delft3D bed differs from our DEM by up to {err:.3f} m - "
                    "the grid is flipped, transposed or misaligned, so the "
                    "flood would be too. Check MNKmax against the .grd, and "
                    "that DPS0 was transposed and cropped before flipping."
                )

    depth = np.where(np.isfinite(levels) & np.isfinite(bed), levels - bed, 0.0)
    depth = np.where(depth > 0, depth, 0.0).astype(np.float32)
    return {
        "max_depth_m": depth,
        "max_water_level_m": levels,
        "wet_cells": int((depth >= DRYFLC_M).sum()),
        "max_depth_max_m": float(depth.max()) if depth.size else 0.0,
    }
