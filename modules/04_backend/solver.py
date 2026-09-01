"""
modules/04_backend/solver.py - the fast 2D flood solver.

This is the engine that runs live at the pitch. If it blows up there is no
demo, so it is built for robustness first and speed second.

TWO SCHEMES, AND WHY WE CARRY BOTH
----------------------------------

**scheme="swe"** (default) - the full 2D shallow-water equations in
conservative form, solved by a first-order finite-volume method with an HLL
approximate Riemann solver and Audusse hydrostatic reconstruction of the bed
source term.

    dh/dt    + d(hu)/dx           + d(hv)/dy           = 0
    d(hu)/dt + d(hu^2 + gh^2/2)/dx + d(huv)/dy         = -gh dz/dx - friction
    d(hv)/dt + d(huv)/dx           + d(hv^2+gh^2/2)/dy = -gh dz/dy - friction

    Harten, A., Lax, P.D. & van Leer, B. (1983), "On Upstream Differencing and
      Godunov-Type Schemes for Hyperbolic Conservation Laws", SIAM Review 25(1).
    Audusse, E., Bouchut, F., Bristeau, M.-O., Klein, R. & Perthame, B. (2004),
      "A Fast and Stable Well-Balanced Scheme with Hydrostatic Reconstruction
      for Shallow Water Flows", SIAM J. Sci. Comput. 25(6), 2050-2065.
    Toro, E.F. (2001), "Shock-Capturing Methods for Free-Surface Shallow
      Flows", Wiley.

**scheme="inertial"** - the local inertial (LISFLOOD-FP) approximation, which
drops the advection terms.

    Bates, P.D., Horritt, M.S. & Fewtrell, T.J. (2010), "A simple inertial
      formulation of the shallow water equations for efficient two-dimensional
      flood inundation modelling", Journal of Hydrology, 387(1-2), 33-45.

We implemented the operational approximation first, ran it against Ritter's
analytical dam break, and measured the front position **60% slow**. That is
acceptable for fluvial floodplain inundation, which is what the scheme was
designed for, and wrong for a dam break, which is a high-Froude problem where
advection carries the front. So the default is the full equations. Both stay
available and tests/test_ritter.py reports the measured error of each. That
measurement is the honest version of "we chose a numerical scheme", and it is
worth more in Q&A than either solver on its own.

Everything is SI: depth m, velocity m/s, unit discharge m2/s, time seconds
internally and hours on disk.

Owner: person 4 / captain. Contract questions come here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

import numpy as np

try:
    from numba import njit, prange

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover - numba is a hard dependency
    HAVE_NUMBA = False
    prange = range  # type: ignore[assignment]

    def njit(*args, **kwargs):  # type: ignore[misc]
        def wrap(fn):
            return fn

        return wrap(args[0]) if args and callable(args[0]) else wrap


from shared.contract import (
    CFL_DEFAULT,
    DEFAULT_MANNING_N,
    GRAVITY,
    MAX_TIMESTEP_S,
    MIN_TIMESTEP_S,
    WET_THRESHOLD_M,
)

DRY_EPS = 1e-4
"""m. Below this a cell is numerically dry: velocity is forced to zero and it
enters a Riemann problem only as a dry state. Distinct from WET_THRESHOLD_M
(0.05 m), which is the *reporting* threshold in the contract."""

FLOW_DEPTH_EPS = 1e-3
"""m. Face flow depth below which the inertial scheme carries no flux."""

FROUDE_MAX = 4.0
"""Velocity limiter. On a drying front a cell can hold 5 cm of water and a
large momentum, and h -> 0 then reports 30-40 m/s, which is numerical, not
physical. Capping the cell Froude number at this value is standard practice in
operational 2D solvers (HEC-RAS 2D and TUFLOW both ship an equivalent control).

Fr = 4 sits well above anything real - a dam-break front runs at Fr of roughly
1 to 2 - so this only trims the thin-film artefact. The number of cells limited
in a run is COUNTED and written into meta.json under
results.froude_limited_cells. If that count is large the run is telling you the
grid is too coarse, and we report it rather than hiding it."""

INFLOW_DEPTH_STEP_M = 0.25
"""Most depth the breach inflow may add to one cell in a single timestep, m.

Not a tuning constant so much as a positivity guard: if a step dumps several
metres into a cell, its neighbours over-drain within that step, hit the
depth clamp, and the clamp invents water to keep them non-negative. 0.25 m is
well under the depth at which the shallow-water fluxes carry it away, and it
only binds during the breach peak - it costs nothing for the rest of the run."""

ARRIVAL_UNSET = -1.0
"""Sentinel for "this cell has never been wet".

NOT NaN, deliberately. The accumulation kernel runs under fastmath=True, which
licenses the compiler to assume no NaNs exist - so the usual `x != x` NaN test
compiles to a constant False and every arrival time silently stays unset. That
bug shipped for exactly one test cycle before shared.validate caught it. The
sentinel is converted to NaN on the way out, where the contract wants it."""

Scheme = Literal["swe", "inertial"]


# ==========================================================================
# HLL Riemann solver
# ==========================================================================


@njit(cache=True, fastmath=True, inline="always")
def _wave_speeds(hL, uL, hR, uR, g):
    """Left and right signal speeds for the HLL flux.

    Two-rarefaction estimate, Toro (2001) section 10.5, with the dry-bed
    special cases. Getting these right is what makes the dam-break front
    travel at the correct 2*sqrt(g*h0) instead of something slower.
    """
    if hL <= DRY_EPS and hR <= DRY_EPS:
        return 0.0, 0.0
    if hL <= DRY_EPS:
        cR = math.sqrt(g * hR)
        return uR - 2.0 * cR, uR + cR
    if hR <= DRY_EPS:
        cL = math.sqrt(g * hL)
        return uL - cL, uL + 2.0 * cL

    cL = math.sqrt(g * hL)
    cR = math.sqrt(g * hR)
    u_star = 0.5 * (uL + uR) + cL - cR
    c_star = 0.5 * (cL + cR) + 0.25 * (uL - uR)

    sL = uL - cL
    if u_star - c_star < sL:
        sL = u_star - c_star
    sR = uR + cR
    if u_star + c_star > sR:
        sR = u_star + c_star
    return sL, sR


@njit(cache=True, fastmath=True, inline="always")
def _hll_flux(hL, uL, vL, hR, uR, vR, g):
    """HLL numerical flux normal to a face. Returns (F_h, F_hu, F_hv).

    uL/uR are the NORMAL velocity components, vL/vR the tangential ones. The
    tangential momentum is advected passively and upwinded by the mass flux -
    the standard treatment for 2D shallow water on a Cartesian grid.
    """
    if hL <= DRY_EPS and hR <= DRY_EPS:
        return 0.0, 0.0, 0.0

    sL, sR = _wave_speeds(hL, uL, hR, uR, g)

    fL_h = hL * uL
    fL_hu = hL * uL * uL + 0.5 * g * hL * hL
    fR_h = hR * uR
    fR_hu = hR * uR * uR + 0.5 * g * hR * hR

    if sL >= 0.0:
        f_h = fL_h
        f_hu = fL_hu
    elif sR <= 0.0:
        f_h = fR_h
        f_hu = fR_hu
    else:
        inv = 1.0 / (sR - sL)
        f_h = (sR * fL_h - sL * fR_h + sL * sR * (hR - hL)) * inv
        f_hu = (sR * fL_hu - sL * fR_hu + sL * sR * (hR * uR - hL * uL)) * inv

    f_hv = f_h * (vL if f_h >= 0.0 else vR)
    return f_h, f_hu, f_hv


# ==========================================================================
# Full shallow-water step
# ==========================================================================


@njit(cache=True, fastmath=True, parallel=True)
def _fluxes_x(h, hu, hv, z, active, g, open_edges, Fh, Fhu, Fhv, SL, SR):
    """HLL fluxes on every x-interface, plus the two Audusse pressure
    corrections. Parallel over rows; each row writes only its own slice.

    Interface j lies between cell (i, j-1) [left] and cell (i, j) [right].
    SL[i, j] is the correction owed to the left cell, SR[i, j] to the right.

    Returns the volume leaving through x-boundaries this step (before the dt
    factor is applied by the caller).
    """
    ny, nx = h.shape
    outflow = 0.0
    for i in prange(ny):
        for j in range(nx + 1):
            has_left = j > 0 and active[i, j - 1] == 1
            has_right = j < nx and active[i, j] == 1
            if not has_left and not has_right:
                Fh[i, j] = 0.0
                Fhu[i, j] = 0.0
                Fhv[i, j] = 0.0
                SL[i, j] = 0.0
                SR[i, j] = 0.0
                continue

            hl = 0.0; zl = 0.0; ul = 0.0; vl = 0.0
            hr = 0.0; zr = 0.0; ur = 0.0; vr = 0.0

            if has_left:
                hl = h[i, j - 1]
                zl = z[i, j - 1]
                if hl > DRY_EPS:
                    ul = hu[i, j - 1] / hl
                    vl = hv[i, j - 1] / hl
            if has_right:
                hr = h[i, j]
                zr = z[i, j]
                if hr > DRY_EPS:
                    ur = hu[i, j] / hr
                    vr = hv[i, j] / hr

            if not has_left:
                # West edge of the active domain: ghost cell mirrors the interior.
                hl = hr; zl = zr; vl = vr
                ul = ur if open_edges[3] == 1 else -ur
            if not has_right:
                hr = hl; zr = zl; vr = vl
                ur = ul if open_edges[2] == 1 else -ul

            zf = zl if zl > zr else zr
            hls = hl + zl - zf
            if hls < 0.0:
                hls = 0.0
            hrs = hr + zr - zf
            if hrs < 0.0:
                hrs = 0.0

            f_h, f_hu, f_hv = _hll_flux(hls, ul, vl, hrs, ur, vr, g)

            # Open boundaries are OUTFLOW-ONLY.
            #
            # The ghost cell mirrors the interior (zero-gradient), which is the
            # standard transmissive condition and lets a flood wave leave the
            # domain without reflecting. But mirroring is symmetric: if the
            # interior near the edge happens to be flowing INWARD, the ghost
            # cell obligingly supplies water to match, and the domain gains
            # volume out of nothing. On a steep gorge this never fires because
            # the flow is always downhill and outward. On flat terrain it fires
            # constantly - the Hirakud run took in 7,683 MCM, and ended holding
            # 56,015 MCM, having imported 48,332 MCM through its own edges.
            #
            # There is nothing outside the domain to supply that water, so any
            # inward boundary flux is an artefact. We zero it. Mass can leave,
            # never enter.
            # Clamp the MASS flux only. The momentum fluxes carry the
            # hydrostatic pressure that keeps a lake touching the edge in
            # balance; zeroing them drains the lake (16 m of surface drop in
            # the lake-at-rest test). Blocking mass transport is what we want;
            # deleting the pressure term is not.
            if not has_left and f_h > 0.0:
                f_h = 0.0
            elif not has_right and f_h < 0.0:
                f_h = 0.0

            Fh[i, j] = f_h
            Fhu[i, j] = f_hu
            Fhv[i, j] = f_hv
            # Audusse pressure correction. Sign matters and is easy to get
            # backwards: at rest the reconstructed depths are equal, so
            # F_hu = g/2 * h_star^2 on both faces, and the correction has to
            # cancel it back to g/2 * h_cell^2 for the cell to feel no net
            # force. Hence (h_cell^2 - h_star^2), not the other way round.
            # Inverted, this is invisible on a flat bed (Ritter passes) and
            # puts 100 m of spurious head into a lake at rest.
            SL[i, j] = 0.5 * g * (hl * hl - hls * hls)
            SR[i, j] = 0.5 * g * (hr * hr - hrs * hrs)

            if not has_left:
                outflow += -f_h
            if not has_right:
                outflow += f_h
    return outflow


@njit(cache=True, fastmath=True, parallel=True)
def _fluxes_y(h, hu, hv, z, active, g, open_edges, Gh, Ghu, Ghv, TL, TR):
    """Same as _fluxes_x, on y-interfaces. Parallel over columns.

    Interface i lies between cell (i-1, j) [north] and (i, j) [south]. The
    normal direction is +y = southward = increasing row index, so the normal
    velocity is v and the tangential one is u - which is why the flux tuple is
    unpacked in the swapped order.
    """
    ny, nx = h.shape
    outflow = 0.0
    for j in prange(nx):
        for i in range(ny + 1):
            has_up = i > 0 and active[i - 1, j] == 1
            has_dn = i < ny and active[i, j] == 1
            if not has_up and not has_dn:
                Gh[i, j] = 0.0
                Ghu[i, j] = 0.0
                Ghv[i, j] = 0.0
                TL[i, j] = 0.0
                TR[i, j] = 0.0
                continue

            hl = 0.0; zl = 0.0; ul = 0.0; vl = 0.0
            hr = 0.0; zr = 0.0; ur = 0.0; vr = 0.0

            if has_up:
                hl = h[i - 1, j]
                zl = z[i - 1, j]
                if hl > DRY_EPS:
                    ul = hv[i - 1, j] / hl
                    vl = hu[i - 1, j] / hl
            if has_dn:
                hr = h[i, j]
                zr = z[i, j]
                if hr > DRY_EPS:
                    ur = hv[i, j] / hr
                    vr = hu[i, j] / hr

            if not has_up:
                hl = hr; zl = zr; vl = vr
                ul = ur if open_edges[0] == 1 else -ur
            if not has_dn:
                hr = hl; zr = zl; vr = vl
                ur = ul if open_edges[1] == 1 else -ul

            zf = zl if zl > zr else zr
            hls = hl + zl - zf
            if hls < 0.0:
                hls = 0.0
            hrs = hr + zr - zf
            if hrs < 0.0:
                hrs = 0.0

            f_h, f_hv, f_hu = _hll_flux(hls, ul, vl, hrs, ur, vr, g)

            # Outflow-only, exactly as in _fluxes_x. See the note there.
            if not has_up and f_h > 0.0:
                f_h = 0.0
            elif not has_dn and f_h < 0.0:
                f_h = 0.0

            Gh[i, j] = f_h
            Ghu[i, j] = f_hu
            Ghv[i, j] = f_hv
            TL[i, j] = 0.5 * g * (hl * hl - hls * hls)
            TR[i, j] = 0.5 * g * (hr * hr - hrs * hrs)

            if not has_up:
                outflow += -f_h
            if not has_dn:
                outflow += f_h
    return outflow


@njit(cache=True, fastmath=True, parallel=True)
def _apply_swe(
    h, hu, hv, n, active, dx, dt, g, froude_max,
    Fh, Fhu, Fhv, SL, SR, Gh, Ghu, Ghv, TL, TR,
):
    """Divergence, then semi-implicit friction, then the Froude limiter.

    Cell (i, j) takes flux in through its west interface j and its north
    interface i, and loses flux through interface j+1 and interface i+1. The
    Audusse correction owed to a cell depends on which side of the interface it
    sits: SR on its west face, SL on its east face.

    Returns the number of cells the Froude limiter touched.
    """
    ny, nx = h.shape
    lam = dt / dx
    limited = 0

    for i in prange(ny):
        for j in range(nx):
            if active[i, j] == 0:
                h[i, j] = 0.0
                hu[i, j] = 0.0
                hv[i, j] = 0.0
                continue

            d_h = lam * (Fh[i, j] - Fh[i, j + 1] + Gh[i, j] - Gh[i + 1, j])
            hn = h[i, j] + d_h

            if hn <= DRY_EPS:
                # Drying cell. Any residual shows up in the mass balance; we do
                # not manufacture water to keep it wet.
                h[i, j] = hn if hn > 0.0 else 0.0
                hu[i, j] = 0.0
                hv[i, j] = 0.0
                continue

            d_hu = lam * (
                (Fhu[i, j] + SR[i, j])
                - (Fhu[i, j + 1] + SL[i, j + 1])
                + Ghu[i, j]
                - Ghu[i + 1, j]
            )
            d_hv = lam * (
                Fhv[i, j]
                - Fhv[i, j + 1]
                + (Ghv[i, j] + TR[i, j])
                - (Ghv[i + 1, j] + TL[i + 1, j])
            )

            un = (hu[i, j] + d_hu) / hn
            vn = (hv[i, j] + d_hv) / hn

            # Semi-implicit Manning friction:
            #   du/dt = -g n^2 u|u| / h^(4/3)
            # as u^(n+1) = u^n / (1 + dt g n^2 |u^n| / h^(4/3)) - unconditionally
            # stable, and it can never reverse the flow.
            speed = math.sqrt(un * un + vn * vn)
            if speed > 0.0:
                nij = n[i, j]
                cf = 1.0 + dt * g * nij * nij * speed / (hn ** (4.0 / 3.0))
                un /= cf
                vn /= cf
                speed /= cf

            # Froude limiter. See FROUDE_MAX. Counted, never silent.
            c = math.sqrt(g * hn)
            if speed > froude_max * c and speed > 0.0:
                scale = froude_max * c / speed
                un *= scale
                vn *= scale
                limited += 1

            h[i, j] = hn
            hu[i, j] = hn * un
            hv[i, j] = hn * vn

    return limited


@njit(cache=True, fastmath=True)
def _max_wave_speed(h, hu, hv, active, g):
    """max(|u| + sqrt(gh)) over the domain, for the CFL condition."""
    ny, nx = h.shape
    smax = 0.0
    for i in range(ny):
        for j in range(nx):
            if active[i, j] == 0:
                continue
            hij = h[i, j]
            if hij <= DRY_EPS:
                continue
            c = math.sqrt(g * hij)
            su = abs(hu[i, j] / hij) + c
            sv = abs(hv[i, j] / hij) + c
            s = su if su > sv else sv
            if s > smax:
                smax = s
    return smax


# ==========================================================================
# Local inertial step (Bates et al. 2010) - kept for comparison
# ==========================================================================


@njit(cache=True, fastmath=True)
def _step_inertial(h, z, n, qx, qy, active, dx, dt, g, flow_eps, open_edges):
    """One local-inertial step. Advection is dropped; see the module docstring.

    qx[i, j] is the flux between (i, j-1) and (i, j), positive eastward.
    qy[i, j] is the flux between (i-1, j) and (i, j), positive southward.
    """
    ny, nx = h.shape
    outflow = 0.0

    for i in range(ny):
        for j in range(1, nx):
            hl = h[i, j - 1]
            hr = h[i, j]
            zl = z[i, j - 1]
            zr = z[i, j]
            wl = hl + zl
            wr = hr + zr
            hf = (wl if wl > wr else wr) - (zl if zl > zr else zr)
            if hf <= flow_eps or active[i, j - 1] == 0 or active[i, j] == 0:
                qx[i, j] = 0.0
                continue
            slope = (wr - wl) / dx
            nf = 0.5 * (n[i, j - 1] + n[i, j])
            q = qx[i, j]
            qn = (q - g * hf * dt * slope) / (
                1.0 + g * dt * nf * nf * abs(q) / (hf ** (7.0 / 3.0))
            )
            if qn > 0.0:
                cap = hl * dx / dt
                if qn > cap:
                    qn = cap
            else:
                cap = -hr * dx / dt
                if qn < cap:
                    qn = cap
            qx[i, j] = qn

    for i in range(1, ny):
        for j in range(nx):
            hup = h[i - 1, j]
            hdn = h[i, j]
            zu = z[i - 1, j]
            zd = z[i, j]
            wu = hup + zu
            wd = hdn + zd
            hf = (wu if wu > wd else wd) - (zu if zu > zd else zd)
            if hf <= flow_eps or active[i - 1, j] == 0 or active[i, j] == 0:
                qy[i, j] = 0.0
                continue
            slope = (wd - wu) / dx
            nf = 0.5 * (n[i - 1, j] + n[i, j])
            q = qy[i, j]
            qn = (q - g * hf * dt * slope) / (
                1.0 + g * dt * nf * nf * abs(q) / (hf ** (7.0 / 3.0))
            )
            if qn > 0.0:
                cap = hup * dx / dt
                if qn > cap:
                    qn = cap
            else:
                cap = -hdn * dx / dt
                if qn < cap:
                    qn = cap
            qy[i, j] = qn

    # Normal-depth free outfall on open edges (the LISFLOOD-FP "FREE" boundary).
    for i in range(ny):
        qx[i, 0] = 0.0
        qx[i, nx] = 0.0
        if open_edges[3] == 1 and h[i, 0] > flow_eps and active[i, 0] == 1 and nx > 1:
            s = (z[i, 0] - z[i, 1]) / dx
            if s < 1e-4:
                s = 1e-4
            elif s > 0.1:
                s = 0.1
            q = -(1.0 / n[i, 0]) * h[i, 0] ** (5.0 / 3.0) * math.sqrt(s)
            cap = -h[i, 0] * dx / dt
            qx[i, 0] = q if q > cap else cap
            outflow += -qx[i, 0] * dx * dt
        if (
            open_edges[2] == 1
            and h[i, nx - 1] > flow_eps
            and active[i, nx - 1] == 1
            and nx > 1
        ):
            s = (z[i, nx - 1] - z[i, nx - 2]) / dx
            if s < 1e-4:
                s = 1e-4
            elif s > 0.1:
                s = 0.1
            q = (1.0 / n[i, nx - 1]) * h[i, nx - 1] ** (5.0 / 3.0) * math.sqrt(s)
            cap = h[i, nx - 1] * dx / dt
            qx[i, nx] = q if q < cap else cap
            outflow += qx[i, nx] * dx * dt

    for j in range(nx):
        qy[0, j] = 0.0
        qy[ny, j] = 0.0
        if open_edges[0] == 1 and h[0, j] > flow_eps and active[0, j] == 1 and ny > 1:
            s = (z[0, j] - z[1, j]) / dx
            if s < 1e-4:
                s = 1e-4
            elif s > 0.1:
                s = 0.1
            q = -(1.0 / n[0, j]) * h[0, j] ** (5.0 / 3.0) * math.sqrt(s)
            cap = -h[0, j] * dx / dt
            qy[0, j] = q if q > cap else cap
            outflow += -qy[0, j] * dx * dt
        if (
            open_edges[1] == 1
            and h[ny - 1, j] > flow_eps
            and active[ny - 1, j] == 1
            and ny > 1
        ):
            s = (z[ny - 1, j] - z[ny - 2, j]) / dx
            if s < 1e-4:
                s = 1e-4
            elif s > 0.1:
                s = 0.1
            q = (1.0 / n[ny - 1, j]) * h[ny - 1, j] ** (5.0 / 3.0) * math.sqrt(s)
            cap = h[ny - 1, j] * dx / dt
            qy[ny, j] = q if q < cap else cap
            outflow += qy[ny, j] * dx * dt

    fac = dt / dx
    for i in range(ny):
        for j in range(nx):
            if active[i, j] == 0:
                continue
            hn = h[i, j] + fac * (qx[i, j] - qx[i, j + 1] + qy[i, j] - qy[i + 1, j])
            h[i, j] = hn if hn > 0.0 else 0.0

    return outflow


# ==========================================================================
# Running maxima
# ==========================================================================


@njit(cache=True, fastmath=True)
def _accumulate(
    h, u, v, t_hr, dt_hr,
    max_depth, arrival_time, time_of_peak, max_velocity, duration,
    wet_threshold,
):
    """Update the four contract grids plus duration. Called every step."""
    ny, nx = h.shape
    for i in range(ny):
        for j in range(nx):
            hij = h[i, j]
            if hij < wet_threshold:
                continue
            speed = math.sqrt(u[i, j] * u[i, j] + v[i, j] * v[i, j])
            if hij > max_depth[i, j]:
                max_depth[i, j] = hij
                time_of_peak[i, j] = t_hr
            if speed > max_velocity[i, j]:
                max_velocity[i, j] = speed
            if arrival_time[i, j] < 0.0:  # ARRIVAL_UNSET; see the constant
                arrival_time[i, j] = t_hr
            duration[i, j] += dt_hr


# ==========================================================================
# Configuration and result
# ==========================================================================


@dataclass
class SolverConfig:
    """Everything the solver needs that is not terrain."""

    dx_m: float
    """Real cell size in METRES. The solver works in a locally metric frame.
    Never pass it degrees."""

    end_hr: float = 12.0
    scheme: Scheme = "swe"
    cfl: float = CFL_DEFAULT
    manning_n: float = DEFAULT_MANNING_N
    output_step_hr: float = 0.25
    open_edges: tuple[bool, bool, bool, bool] = (False, True, True, True)
    """N, S, E, W. Default closes the upstream (north) edge and lets water leave
    everywhere else."""
    max_steps: int = 2_000_000
    initial_dt_s: float = 0.5
    keep_frames: bool = False
    progress_every_steps: int = 100
    min_manning_n: float = 1e-3
    """Floor for the Manning raster. Lower than the 0.010 physical floor in
    contract.py so near-frictionless benchmarks run through the same code path
    as real terrain. Real runs use module 01's raster and never reach it."""

    froude_max: float = FROUDE_MAX
    """Cell Froude cap. See FROUDE_MAX for why it exists.

    Set it to `float("inf")` for idealised benchmarks. Ritter's rarefaction has
    a genuinely unbounded Froude number at the tip (u -> 2c0 while h -> 0), so
    limiting it there clips real physics: with the cap on, our measured Ritter
    front error goes from 22% to 30% and the depth RMSE from 0.07 m to 0.13 m.
    On real terrain the same cap removes a 35 m/s thin-film artefact. Both
    numbers are real; the cap is right for one problem and wrong for the other,
    so it is a setting rather than a constant."""


@dataclass
class SolverResult:
    """A completed run. Grids are float32 and contract-shaped."""

    max_depth: np.ndarray
    arrival_time: np.ndarray
    time_of_peak: np.ndarray
    max_velocity: np.ndarray
    duration: np.ndarray
    final_depth: np.ndarray

    volume_in_m3: float
    volume_out_m3: float
    volume_stored_m3: float
    mass_balance_err_pct: float

    runtime_s: float
    n_steps: int
    min_dt_s: float
    scheme: str = "swe"
    froude_limited_cells: int = 0
    """Total cell-steps the Froude limiter touched. Reported, never hidden -
    a large number means the grid is too coarse for the flow it is carrying."""
    frames: list[np.ndarray] = field(default_factory=list)
    frame_times_hr: list[float] = field(default_factory=list)

    @property
    def wet_cells(self) -> int:
        return int((self.max_depth >= WET_THRESHOLD_M).sum())

    def summary(self) -> str:
        return (
            f"scheme={self.scheme}  steps={self.n_steps}  "
            f"runtime={self.runtime_s:.1f}s  min_dt={self.min_dt_s:.4f}s  "
            f"wet={self.wet_cells}  max_depth={float(self.max_depth.max()):.2f}m  "
            f"max_vel={float(self.max_velocity.max()):.2f}m/s  "
            f"mass_err={self.mass_balance_err_pct:+.3f}%"
        )


# ==========================================================================
# The driver
# ==========================================================================


def run_solver(
    dem: np.ndarray,
    config: SolverConfig,
    inflow_hydrograph: tuple[np.ndarray, np.ndarray] | None = None,
    inflow_cells: list[tuple[int, int]] | None = None,
    initial_depth: np.ndarray | None = None,
    manning_grid: np.ndarray | None = None,
    active_mask: np.ndarray | None = None,
    progress: Callable[[dict], None] | None = None,
) -> SolverResult:
    """Route a flood across a DEM.

    Args:
        dem: bed elevation in metres, shape (ny, nx). NaN cells are switched off.
        config: see SolverConfig. `dx_m` must be the true cell size in metres.
        inflow_hydrograph: (time_hr, discharge_cumecs) injected at `inflow_cells`.
            Exactly the hydrograph.csv the contract defines, whether it came
            from shared.hydro or from module 02's SPH run.
        inflow_cells: cells the inflow is split evenly across. For a dam break
            this is the row of cells spanning the breach.
        initial_depth: starting water depth, e.g. a filled reservoir.
        manning_grid: per-cell Manning's n; falls back to config.manning_n.
        active_mask: True where the solver may put water.
        progress: called every config.progress_every_steps with
            {step, t_hr, dt_s, pct, wet_cells, max_depth_m}. This is what the
            WebSocket streams to the browser while the solve is running.

    Returns:
        SolverResult - four contract grids, duration, and an honest mass balance.

    Raises:
        RuntimeError: if the timestep collapses below MIN_TIMESTEP_S. We abort
            rather than produce numbers nobody should trust.
    """
    t_start = time.perf_counter()

    z = np.ascontiguousarray(dem, dtype=np.float64)
    if z.ndim != 2:
        raise ValueError(f"dem must be 2-D, got shape {z.shape}")
    ny, nx = z.shape

    active = np.ones((ny, nx), dtype=np.uint8)
    if active_mask is not None:
        active &= np.ascontiguousarray(active_mask, dtype=np.uint8)
    nan_cells = ~np.isfinite(z)
    if nan_cells.any():
        if nan_cells.all():
            raise ValueError("dem is entirely NaN")
        active[nan_cells] = 0
        z = np.where(nan_cells, float(np.nanmax(z)) + 1000.0, z)

    n_grid = (
        np.ascontiguousarray(manning_grid, dtype=np.float64)
        if manning_grid is not None
        else np.full((ny, nx), config.manning_n, dtype=np.float64)
    )
    n_grid = np.clip(n_grid, config.min_manning_n, 0.200)

    # Refuse a non-finite roughness field rather than silently destroying water.
    # Every kernel below is compiled with fastmath=True, so a NaN does not
    # propagate visibly - it makes a comparison take the wrong branch and the
    # cell gets zeroed. The failure then shows up as a mass-balance error
    # thousands of steps later with nothing pointing at the cause. Fail here,
    # loudly, naming the module that has to fix it.
    n_bad = int((~np.isfinite(n_grid)).sum())
    if n_bad:
        raise ValueError(
            f"manning_grid has {n_bad} non-finite cells "
            f"({100.0 * n_bad / n_grid.size:.1f}% of the grid). The solver "
            f"cannot run on this: NaN roughness silently zeroes wet cells under "
            f"fastmath. Module 01 must fill land-cover no-data before returning "
            f"a roughness raster."
        )

    h = (
        np.array(initial_depth, dtype=np.float64)
        if initial_depth is not None
        else np.zeros((ny, nx), dtype=np.float64)
    )
    h = np.ascontiguousarray(h)
    h[active == 0] = 0.0

    hu = np.zeros((ny, nx), dtype=np.float64)
    hv = np.zeros((ny, nx), dtype=np.float64)
    qx = np.zeros((ny, nx + 1), dtype=np.float64)
    qy = np.zeros((ny + 1, nx), dtype=np.float64)

    # Face-flux scratch, allocated once. Five arrays per direction: the three
    # HLL flux components and the two Audusse pressure corrections.
    Fh = np.zeros((ny, nx + 1), dtype=np.float64)
    Fhu = np.zeros((ny, nx + 1), dtype=np.float64)
    Fhv = np.zeros((ny, nx + 1), dtype=np.float64)
    SL = np.zeros((ny, nx + 1), dtype=np.float64)
    SR = np.zeros((ny, nx + 1), dtype=np.float64)
    Gh = np.zeros((ny + 1, nx), dtype=np.float64)
    Ghu = np.zeros((ny + 1, nx), dtype=np.float64)
    Ghv = np.zeros((ny + 1, nx), dtype=np.float64)
    TL = np.zeros((ny + 1, nx), dtype=np.float64)
    TR = np.zeros((ny + 1, nx), dtype=np.float64)

    max_depth = np.zeros((ny, nx), dtype=np.float64)
    arrival_time = np.full((ny, nx), ARRIVAL_UNSET, dtype=np.float64)
    time_of_peak = np.full((ny, nx), np.nan, dtype=np.float64)
    max_velocity = np.zeros((ny, nx), dtype=np.float64)
    duration = np.zeros((ny, nx), dtype=np.float64)

    open_edges = np.array([int(b) for b in config.open_edges], dtype=np.uint8)
    dx = float(config.dx_m)
    cell_area = dx * dx
    use_swe = config.scheme == "swe"

    if inflow_hydrograph is not None:
        hyd_t = np.asarray(inflow_hydrograph[0], dtype=np.float64)
        hyd_q = np.asarray(inflow_hydrograph[1], dtype=np.float64)
    else:
        hyd_t = None
        hyd_q = None
    cells = list(inflow_cells or [])

    volume_in = float(h.sum()) * cell_area
    volume_out = 0.0

    t_s = 0.0
    end_s = config.end_hr * 3600.0
    min_dt_seen = float(config.initial_dt_s)
    step = 0
    froude_limited = 0

    frames: list[np.ndarray] = []
    frame_times: list[float] = []
    next_frame_s = 0.0

    while t_s < end_s and step < config.max_steps:
        # ---- adaptive timestep ----------------------------------------
        if use_swe:
            smax = _max_wave_speed(h, hu, hv, active, GRAVITY)
            dt = config.cfl * dx / smax if smax > 1e-9 else config.initial_dt_s
        else:
            h_max = float(h.max())
            dt = (
                config.cfl * dx / math.sqrt(GRAVITY * h_max)
                if h_max > FLOW_DEPTH_EPS
                else config.initial_dt_s
            )
        # The wave-speed CFL above says how fast information CROSSES a cell.
        # It says nothing about how fast we are POURING water into one, and the
        # breach inflow is concentrated into a handful of cells. A 312 m wide
        # natural-dam breach delivering 31,000 m3/s into five 90 m cells raises
        # each by metres in a single step; neighbours then over-drain, go
        # negative, and the positivity clamp manufactures water. That showed up
        # as a mass error of -2.5%, over the contract tolerance, on the first
        # river-blockage run - and it never appeared for engineered dams,
        # whose breaches are narrower and slower.
        #
        # So cap dt by the inflow as well: never add more than
        # INFLOW_DEPTH_STEP_M of depth to an inflow cell in one step.
        if hyd_t is not None and cells:
            q_peek = float(np.interp(t_s / 3600.0, hyd_t, hyd_q, left=0.0, right=0.0))
            if q_peek > 0.0:
                dt_inflow = INFLOW_DEPTH_STEP_M * cell_area * len(cells) / q_peek
                if dt_inflow < dt:
                    dt = dt_inflow

        if dt > MAX_TIMESTEP_S:
            dt = MAX_TIMESTEP_S
        if t_s + dt > end_s:
            dt = end_s - t_s
        if dt < MIN_TIMESTEP_S:
            raise RuntimeError(
                f"timestep collapsed to {dt:.2e} s at t = {t_s / 3600:.4f} hr. "
                f"The solve is unstable - nothing it produced should be trusted."
            )
        if dt < min_dt_seen:
            min_dt_seen = dt

        # ---- inject the breach hydrograph -----------------------------
        if hyd_t is not None and cells:
            q_now = float(np.interp(t_s / 3600.0, hyd_t, hyd_q, left=0.0, right=0.0))
            if q_now > 0.0:
                vol = q_now * dt
                per_cell_depth = (vol / len(cells)) / cell_area
                for (i, j) in cells:
                    if active[i, j]:
                        h[i, j] += per_cell_depth
                volume_in += vol

        # ---- one step -------------------------------------------------
        if use_swe:
            out_x = _fluxes_x(h, hu, hv, z, active, GRAVITY, open_edges, Fh, Fhu, Fhv, SL, SR)
            out_y = _fluxes_y(h, hu, hv, z, active, GRAVITY, open_edges, Gh, Ghu, Ghv, TL, TR)
            volume_out += (out_x + out_y) * dx * dt
            froude_limited += _apply_swe(
                h, hu, hv, n_grid, active, dx, dt, GRAVITY, config.froude_max,
                Fh, Fhu, Fhv, SL, SR, Gh, Ghu, Ghv, TL, TR,
            )
        else:
            volume_out += _step_inertial(
                h, z, n_grid, qx, qy, active, dx, dt, GRAVITY, FLOW_DEPTH_EPS, open_edges
            )

        t_s += dt
        step += 1
        t_hr = t_s / 3600.0

        # ---- cell-centred velocities for the running maxima -----------
        safe_h = np.maximum(h, 0.01)
        wet_now = h > DRY_EPS
        if use_swe:
            u = np.where(wet_now, hu / safe_h, 0.0)
            v = np.where(wet_now, hv / safe_h, 0.0)
        else:
            u = np.where(wet_now, 0.5 * (qx[:, :-1] + qx[:, 1:]) / safe_h, 0.0)
            v = np.where(wet_now, 0.5 * (qy[:-1, :] + qy[1:, :]) / safe_h, 0.0)

        _accumulate(
            h, u, v, t_hr, dt / 3600.0,
            max_depth, arrival_time, time_of_peak, max_velocity, duration,
            WET_THRESHOLD_M,
        )

        if config.keep_frames and t_s >= next_frame_s:
            frames.append(h.astype(np.float32).copy())
            frame_times.append(t_hr)
            next_frame_s += config.output_step_hr * 3600.0

        if progress is not None and step % config.progress_every_steps == 0:
            progress(
                {
                    "step": step,
                    "t_hr": round(t_hr, 4),
                    "dt_s": round(dt, 4),
                    "pct": round(100.0 * t_s / end_s, 2),
                    "wet_cells": int((h >= WET_THRESHOLD_M).sum()),
                    "max_depth_m": round(float(h.max()), 3),
                    # Live volume ledger. The dashboard shows this as a running
                    # mass-balance readout, and it is the only way to see a
                    # conservation bug while it is happening rather than
                    # inferring it from the final number.
                    "volume_in_mcm": round(volume_in / 1e6, 4),
                    "volume_out_mcm": round(volume_out / 1e6, 4),
                    "volume_stored_mcm": round(float(h.sum()) * cell_area / 1e6, 4),
                    "min_depth_m": round(float(h.min()), 6),
                }
            )

    volume_stored = float(h.sum()) * cell_area
    denom = volume_in if volume_in > 1.0 else 1.0
    mass_err_pct = 100.0 * (volume_in - volume_out - volume_stored) / denom

    # ---- contract clean-up --------------------------------------------
    md = max_depth.astype(np.float32)
    md[md < WET_THRESHOLD_M] = 0.0
    wet = md >= WET_THRESHOLD_M
    # Sentinel -> NaN, which is what the contract stores for "never wet".
    arrival_time[arrival_time < 0.0] = np.nan
    at = arrival_time.astype(np.float32)
    tp = time_of_peak.astype(np.float32)
    mv = max_velocity.astype(np.float32)
    du = duration.astype(np.float32)
    at[~wet] = np.nan
    tp[~wet] = np.nan
    mv[~wet] = 0.0
    du[~wet] = 0.0

    return SolverResult(
        max_depth=md,
        arrival_time=at,
        time_of_peak=tp,
        max_velocity=mv,
        duration=du,
        final_depth=h.astype(np.float32),
        volume_in_m3=volume_in,
        volume_out_m3=volume_out,
        volume_stored_m3=volume_stored,
        mass_balance_err_pct=mass_err_pct,
        runtime_s=time.perf_counter() - t_start,
        n_steps=step,
        min_dt_s=min_dt_seen,
        scheme=config.scheme,
        froude_limited_cells=froude_limited,
        frames=frames,
        frame_times_hr=frame_times,
    )


def warm_up_jit(size: int = 24) -> float:
    """Compile the numba kernels on a tiny problem.

    Call this at API startup. The first call to a numba function pays a 3-6
    second compile and you do not want that happening while a juror watches a
    progress bar. Returns the compile time in seconds.
    """
    t0 = time.perf_counter()
    z = np.tile(np.linspace(10.0, 0.0, size)[:, None], (1, size))
    h0 = np.zeros((size, size))
    h0[0, size // 2] = 2.0
    for scheme in ("swe", "inertial"):
        run_solver(
            z,
            SolverConfig(dx_m=10.0, end_hr=0.005, scheme=scheme, initial_dt_s=0.05),
            initial_depth=h0.copy(),
        )
    return time.perf_counter() - t0
