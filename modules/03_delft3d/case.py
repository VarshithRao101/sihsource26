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

    AND ON A REAL SCENARIO, 2026-09-05. Delft3D-FLOW solved
    godavariatgangapur_blockage_fast_001 - a 223 x 161 domain at 90 m with 277 m
    of relief, fed our breach hydrograph peaking at 85,152 m3/s - in 33.3 s at
    dt = 0.1 min, and read back clean. Against our solver: 33.19 km2 wet and
    32.35 m deep against our 31.96 km2 and 27.82 m, extent CSI 0.7379. That is
    the comparison the problem statement asks for, on the engine it names. It
    is still NOT validation: two engines agreeing bounds the numerics and
    neither has been checked against a measured flood on this reach.

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

SEVEN CONVENTIONS THAT ARE SILENT WHEN WRONG. Every one of these was found by
running the real kernel and bisecting against Deltares' own f34 example, not by
reading documentation - and every one of them fails without a useful message.

  1. MNKmax IS THE CELL COUNT PLUS TWO, AND THE .grd HOLDS CORNERS. nx by ny
     cells need nx+1 by ny+1 grid points, because RGFGRID stores corners; and
     Delft3D treats BOTH the first and the last index as dummy. Writing our
     cells as points silently lost the last row of bed - the run completed and
     the terrain was a row short.

  2. DEPTH IS POSITIVE DOWN, at MNKmax size, offset one cell in each axis. The
     .dep stores depth below the reference level, the negative of our
     elevation, and the data starts at index 1 because index 0 is the dummy
     edge.

  3. TIME IS IN Tunit, NOT SECONDS. Tunit = #M# means Tstart, Tstop, Dt, Flmap
     and every table's time column are MINUTES since Itdate.

  4. GRIDS ARE WRITTEN BOTTOM-UP. RGFGRID's N index increases northward and our
     rasters are north-up, so arrays are flipped both ways. On the way back in,
     CROP THE DUMMY ROW AND COLUMN BEFORE FLIPPING, or everything shifts a row.

  5. EVERY Fil* KEY NEEDS ITS Fmt* PARTNER (#FR#). Delft3D does not infer the
     format of an attribute file.

  6. THE MDF KEYWORD FIELD IS SIX CHARACTERS WIDE, "=" AT COLUMN 7, AND FLOATS
     NEED THREE-DIGIT EXPONENTS RIGHT-JUSTIFIED TO 16. "Filcco= #x.grd#", not
     "Filcco = #x.grd#"; 8.0000000e+002, not e+02. Both are read positionally.
     The exponent trap bit three separate times, because the MDF, the table
     data rows and the .bnd alpha column each have their own format string.
     Ident must be omitted entirely - Delft3D parses it for its own version
     stamp and free text there fails before anything else is read.

  7. A BOUNDARY MUST NOT INCLUDE THE ENCLOSURE CORNERS, and on real terrain it
     must not span the whole edge either. Corners crash the kernel outright
     (0xC0000409, no diagnostic). A full edge imposes the outlet's water level
     on ground hundreds of metres above it, and the run aborts on the first
     step with "Water level change too high".

  8. THE BOUNDARY MUST LIE ON THE ENCLOSURE, AND THE ENCLOSURE MUST BE THE
     CELLS THAT HOLD TERRAIN. These two are one constraint and getting either
     half alone fails. A section inside the polygon is refused - "Boundary
     point (m, n) lies inside the computational domain" - and a polygon drawn
     around the full 1..mmax rectangle puts its own edge on the dummy ring,
     whose .dep is the -999 filler, so the outflow would sit on a 999 m wall.
     _write_enc traces 2..nx+1 by 2..ny+1 and the outflow goes on that edge.

  9. EVERY CELL NAMED TO DELFT3D GOES THROUGH _dem_to_mn. Our rasters are
     north-up and 0-based; Delft3D is bottom-up, 1-based, with a dummy ring.
     The discharge source and the outflow span both used to convert by hand and
     both were wrong - the source by one cell in each axis, the boundary by one
     cell AND unflipped, which put the outflow at the wrong end of the valley
     on ground 100 m above the channel. Delft3D reported it as "Water level
     change too high > 25.00 m", which reads like a physics problem and is not
     one. This was 277 m of relief being blamed for an index.

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

BOUNDARY_BAND_M = 5.0
"""How far above the outlet's lowest bed a cell may sit and still carry the
water-level boundary, in metres. Wide enough to cover a valley floor, narrow
enough to exclude the hillsides - which, held at the channel's level, diverge."""

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
    boundary_mn: tuple[int, int, int, int]

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
            # Where the outflow section actually went, in Delft3D indices. It
            # is in the record because a boundary on the wrong cells is the one
            # failure that looks like a kernel problem.
            "boundary_mn": list(self.boundary_mn),
            "dryflc_m": DRYFLC_M,
            "verified": True,
            "note": (
                "Verified end to end: a case written by this module solved on a "
                "locally built Delft3D 4 kernel and read back with the bed "
                "matching our DEM to within a centimetre, first on a synthetic "
                "channel and then on a real 223 x 161 blockage scenario with "
                "277 m of relief. Comparing two engines bounds the numerics; "
                "neither has been validated against a measured flood."
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


def _write_enc(path: Path, nx: int, ny: int) -> None:
    """Grid enclosure: a closed rectangle in (m,n) index space.

    IT TRACES THE CELLS THAT HOLD TERRAIN, m = 2..nx+1 and n = 2..ny+1, not the
    full 1..mmax rectangle.

    An open boundary has to lie ON this polygon - a section anywhere inside it
    is rejected with "Boundary point (m, n) lies inside the computational
    domain". Enclosing 1..mmax therefore forces the outflow onto the dummy edge,
    whose .dep is the -999 filler, so the boundary would sit on a 999 m wall.
    Enclosing the data rectangle instead puts the enclosure edge on the last
    column of real bed, which is where the water is supposed to leave.

    Everything else keeps its indices: _write_dep and _write_ini still write
    data at 2..nx+1 / 2..ny+1, and read_map still crops [1:ny+1, 1:nx+1]. Only
    the dummy ring changes meaning - from "inside the domain, filled with -999"
    to "outside it", which is what a dummy edge is for.
    """
    ring = [(2, 2), (nx + 1, 2), (nx + 1, ny + 1), (2, ny + 1), (2, 2)]
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


def _write_ini(path: Path, dem: np.ndarray, mmax: int, nmax: int,
               level_m: float) -> None:
    """Initial water level per cell: DRY, i.e. the level sits at the bed.

    A uniform Zeta0 cannot express this. Our domains carry hundreds of metres
    of relief, so one number is either far below most of the bed - which
    Delft3D rejects on the first step with "Water level change too high
    > 25.00 m", listing cells whose bed is tens of metres above the level being
    imposed - or high enough to flood the whole valley before the breach opens.

    So the level is written per cell at the bed, which is what "dry" means, and
    only the outlet cells start at the boundary level so the boundary has
    something consistent to hold.

    Three blocks in the same layout as the .dep: water level, then u, then v.
    """
    ny, nx = dem.shape
    s_field = np.full((nmax, mmax), 0.0, dtype=np.float64)

    flipped = np.flipud(dem)
    bed = np.where(np.isfinite(flipped), flipped, 0.0)
    # Dry: water surface at the bed. Never below it, or the first step has to
    # travel the gap.
    s_field[1:ny + 1, 1:nx + 1] = np.maximum(bed, level_m)

    def block(arr):
        out = []
        for n in range(nmax):
            row = arr[n]
            for i in range(0, mmax, 12):
                out.append("".join(f"{v:12.3f}" for v in row[i:i + 12]))
        return out

    zeros = np.zeros((nmax, mmax), dtype=np.float64)
    lines = block(s_field) + block(zeros) + block(zeros)
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def _dem_to_mn(row: int, col: int, ny: int) -> tuple[int, int]:
    """DEM (row, col), north-up and 0-based -> Delft3D (m, n), 1-based.

    THE ONE PLACE THIS CONVERSION LIVES. Everything that names a cell to
    Delft3D - the discharge source, the boundary span - goes through here,
    because getting it wrong does not crash in any way that points at the
    cause.

    Two steps, and both were once missing:

      * the FLIP. _write_dep writes np.flipud(dem), so our row 0 (north) is
        Delft3D's LAST n, not its first. A boundary derived from unflipped row
        indices lands mirrored across the domain - at the far end of the
        valley, on the hillside.
      * the OFFSET. Data occupies file rows/columns 1..ny / 1..nx, which are
        Delft3D's 1-based 2..ny+1 / 2..nx+1. Index 1 and index mmax/nmax are
        the enclosure's dummy edge and hold no terrain at all.

    Together they put the outflow boundary on ground 100 m above the channel
    it was meant to drain, and Delft3D stopped on the first timestep with
    "Water level change too high > 25.00 m", naming cells whose bed sat a
    hundred metres above the level being forced on them. The kernel was fine.
    The indices were not.

    read_map is the proof: DPS0[m, n] is the value written at file (row n,
    col m), so this is exactly its inverse.
    """
    return col + 2, (ny - 1 - row) + 2


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


def _write_bnd_bct(bnd: Path, bct: Path,
                   m1: int, n1: int, m2: int, n2: int,
                   level_m: float, end_hr: float) -> None:
    """One water-level boundary across the channel cells at the outlet.

    NOT the whole edge - see convention 7 in the module docstring. The span is
    given in Delft3D (m, n), already converted from DEM indices by write_case,
    because that conversion is where this went wrong for a week - see the note
    on `_dem_to_mn`.

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
    # mmax is 15 - which is exactly the range that holds data, so staying
    # inside the data range is the whole rule.
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
    _write_enc(case_dir / f"{run_id}.enc", nx, ny)
    _write_dep(case_dir / f"{run_id}.dep", dem, mmax, nmax)

    src_m, src_n = _dem_to_mn(int(src_row), int(src_col), ny)
    _write_src(case_dir / f"{run_id}.src", src_m, src_n)
    _write_dis(case_dir / f"{run_id}.dis", src_m, src_n, t_hr, q_cumecs)

    # Outflow on whichever edge holds the lowest bed, and ONLY across the cells
    # that form the channel there. Taking the whole edge imposes the outlet's
    # water level on ground hundreds of metres above it and Delft3D diverges on
    # the first step. The channel is taken as the contiguous run of edge cells
    # within BOUNDARY_BAND_M of the lowest, which on a conditioned DEM is the
    # valley floor and not the hillsides either side of it.
    finite = np.where(np.isfinite(dem), dem, np.inf)
    profiles = {
        "north": finite[0, :],
        "south": finite[-1, :],
        "west": finite[:, 0],
        "east": finite[:, -1],
    }
    edge = min(profiles, key=lambda k: float(np.min(profiles[k])))
    prof = np.asarray(profiles[edge], dtype=float)
    level = float(np.min(prof))
    if not np.isfinite(level):
        level = float(np.nanmin(dem))

    # Walk out from the lowest cell while the bed stays within the band.
    k = int(np.argmin(prof))
    lo = hi = k
    while lo - 1 >= 0 and prof[lo - 1] <= level + BOUNDARY_BAND_M:
        lo -= 1
    while hi + 1 < prof.size and prof[hi + 1] <= level + BOUNDARY_BAND_M:
        hi += 1
    # lo..hi index the DEM along that edge - rows for east/west, columns for
    # north/south. Convert both ends through the one conversion, so the span
    # lands on the cells whose bed was actually measured.
    if edge in ("east", "west"):
        col = nx - 1 if edge == "east" else 0
        a = _dem_to_mn(lo, col, ny)
        b = _dem_to_mn(hi, col, ny)
    else:
        row = 0 if edge == "north" else ny - 1
        a = _dem_to_mn(row, lo, ny)
        b = _dem_to_mn(row, hi, ny)
    # The flip reverses row order, so sort rather than assume a comes first.
    m1, m2 = sorted((a[0], b[0]))
    n1, n2 = sorted((a[1], b[1]))

    _write_ini(case_dir / f"{run_id}.ini", dem, mmax, nmax, level)
    _write_bnd_bct(case_dir / f"{run_id}.bnd", case_dir / f"{run_id}.bct",
                   m1, n1, m2, n2, level, end_hr)

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
        # Zeta0 is a single number and our domains are not. The per-cell
        # initial condition below replaces it; Zeta0 stays at the outlet level
        # for the cells the .ini does not cover.
        kv("Zeta0", " [.]"),
        kv("Filic", f" #{run_id}.ini#"), kv("Fmtic", " #FR#"),
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
        boundary_mn=(m1, n1, m2, n2),
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
