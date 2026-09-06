"""
Delft3D index conventions, tested without the kernel.

These are the two mistakes that cost the most on this module, and neither one
announces itself: Delft3D accepted the files, started solving, and stopped with
a message about water levels that pointed at physics rather than at indices.
Both are now one function and one polygon, so both can be checked in
milliseconds instead of by running a solver.

    * a cell named to Delft3D must be flipped north-up -> bottom-up AND offset
      past the dummy ring
    * the enclosure must trace the cells that hold terrain, because an open
      boundary has to lie on it and the dummy ring holds -999
"""

from __future__ import annotations

from importlib import import_module

import numpy as np

case = import_module("modules.03_delft3d.case")


def test_dem_to_mn_is_the_inverse_of_write_dep():
    """The mapping must agree with where _write_dep actually puts each value.

    Written the way it was found: put a unique number in every DEM cell, write
    the .dep, read it back as Delft3D would (file row n, col m) and check the
    value at _dem_to_mn(row, col) is the one from that cell.
    """
    ny, nx = 5, 7
    dem = np.arange(ny * nx, dtype=float).reshape(ny, nx)
    mmax, nmax = nx + 2, ny + 2

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.dep"
        case._write_dep(path, dem, mmax, nmax)
        values = [float(v) for v in path.read_text().split()]
        grid = np.array(values).reshape(nmax, mmax)   # [file row n, file col m]

    for row in range(ny):
        for col in range(nx):
            m, n = case._dem_to_mn(row, col, ny)
            # .dep holds depth, the negative of elevation, and Delft3D's 1-based
            # (m, n) is the 0-based file position plus one.
            assert grid[n - 1, m - 1] == -dem[row, col], (row, col, m, n)


def test_dem_to_mn_flips_north_up():
    """Row 0 is the NORTH edge and must land on the LARGEST n, not the smallest.

    Without this the outflow boundary was mirrored to the far end of the valley
    - onto ground 100 m above the channel it was meant to drain.
    """
    ny = 10
    assert case._dem_to_mn(0, 0, ny)[1] == ny + 1
    assert case._dem_to_mn(ny - 1, 0, ny)[1] == 2


def test_dem_to_mn_skips_the_dummy_ring():
    """Index 1 and index mmax/nmax are the enclosure's dummy edge, not cells."""
    m, n = case._dem_to_mn(3, 0, 10)
    assert m == 2, "column 0 is Delft3D m=2; m=1 is the dummy edge"


def test_enclosure_traces_the_cells_that_hold_terrain():
    """The polygon must be 2..nx+1 by 2..ny+1.

    Enclosing the full 1..mmax rectangle puts the enclosure edge - and therefore
    any open boundary - on the dummy ring, whose bed is the -999 filler.
    """
    import tempfile
    from pathlib import Path

    nx, ny = 7, 5
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.enc"
        case._write_enc(path, nx, ny)
        pts = [tuple(int(v) for v in line.split()[:2])
               for line in path.read_text().strip().splitlines()]

    assert pts[0] == pts[-1], "the enclosure must be a closed ring"
    ms = {m for m, _ in pts}
    ns = {n for _, n in pts}
    assert min(ms) == 2 and max(ms) == nx + 1
    assert min(ns) == 2 and max(ns) == ny + 1


def test_boundary_lands_on_the_enclosure_edge():
    """A whole case: the outflow span must sit on the enclosure, on real bed.

    A section inside the polygon is refused with "Boundary point (m, n) lies
    inside the computational domain", which is the error this pins.
    """
    import tempfile
    from pathlib import Path

    ny, nx = 20, 30
    # A valley falling to the east, so the outlet edge is the last column.
    dem = np.tile(np.linspace(500.0, 400.0, nx), (ny, 1))
    dem += np.abs(np.arange(ny) - ny // 2)[:, None] * 20.0   # valley walls

    with tempfile.TemporaryDirectory() as td:
        c = case.write_case(
            Path(td), dem, dx_m=90.0, src_row=ny // 2, src_col=1,
            t_hr=np.array([0.0, 1.0]), q_cumecs=np.array([0.0, 500.0]),
            end_hr=1.0,
        )
        m1, n1, m2, n2 = c.boundary_mn

    assert c.boundary_edge == "east"
    assert m1 == m2 == nx + 1, "an east boundary sits on the last column of bed"
    assert 2 <= n1 <= n2 <= ny + 1, "and inside the enclosure's n range"
    # The source is a cell that holds terrain, not a dummy.
    sm, sn = c.src_mn
    assert 2 <= sm <= nx + 1 and 2 <= sn <= ny + 1
