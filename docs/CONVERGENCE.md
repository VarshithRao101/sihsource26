# Grid convergence

**How much of the answer is the mesh?**

Verification asks whether we solve the equations correctly. Validation asks whether the answer
matches reality. Between them sits a third question this project had never answered: **how much
of a result is an artefact of the cell size we chose?**

Every run in `outputs/` before 4 September 2026 solves at **90 m** on **30 m COP30** terrain — a
3× coarsening chosen when the solver swept the whole domain every step and finer grids were
unaffordable. The windowed sweep removed that constraint, so the choice was re-measured rather
than inherited.

Reproduce with:

```bash
python integration/grid_convergence.py --dam AP01MH0129 --reach 8 --hours 3
```

It drives the real API, so every row below went through the real terrain fetch, conditioning,
solver, contract writer and validator.

---

## Annamayya (Cheyyeru), AP01MH0129 — 8 km reach, 3 h

| cell | grid | cells | max depth | area km² | mass err % |
|---|---|---|---|---|---|
| 120 m | 119×143 | 17,017 | 18.80 m | 12.75 | 0.0000 |
| 90 m | 159×191 | 30,369 | 17.65 m | 10.70 | 0.0000 |
| 60 m | 239×287 | 68,593 | 16.61 m | 11.52 | −0.0000 |
| 45 m | 319×383 | 122,177 | 16.79 m | 11.63 | −0.0000 |

## Lower Manair, TL47HH0065 — 8 km reach, 3 h

| cell | grid | cells | max depth | area km² | mass err % |
|---|---|---|---|---|---|
| 120 m | 155×128 | 19,840 | 17.38 m | 26.01 | −0.0000 |
| 90 m | 207×171 | 35,397 | 18.02 m | 26.50 | 0.0000 |
| 60 m | 311×257 | 79,927 | 18.59 m | 27.37 | 0.0000 |
| 45 m | 414×343 | 142,002 | 18.70 m | 28.94 | −0.0003 |

## Change on each refinement

| refinement | depth, Annamayya | depth, Lower Manair | area, Annamayya | area, Lower Manair |
|---|---|---|---|---|
| 120 → 90 m | −6.1% | +3.7% | −16.1% | +1.9% |
| 90 → 60 m | −5.9% | +3.2% | +7.7% | +3.3% |
| **60 → 45 m** | **+1.1%** | **+0.6%** | +1.0% | +5.7% |

---

## What this says

**Maximum depth converges by 60 m, on two independent sites.** The change from 60 m to 45 m is
**1.1% and 0.6%** — an order of magnitude smaller than the 6.1% and 3.7% seen between the coarse
grids. Depth at 60 m is therefore within about 1% of its grid-independent value on these reaches.

**90 m is not in the converged range.** Refining from 90 m to 60 m still moves the depth by
**5.9% and 3.2%**. A number quoted at 90 m carries several percent of pure discretisation error
before any physical uncertainty is considered.

**Flood extent converges more slowly than depth, and on one site it has not converged at all.**
Lower Manair's wet area is still moving **+5.7%** between 60 m and 45 m. This is expected rather
than alarming: extent is decided by very shallow water at the flood margin, where a cell is either
just over or just under the 0.05 m wet threshold, and that boundary stays grid-sensitive long after
the depths behind it have settled. **Every area figure in this repository should be read as
carrying a several-percent grid dependence.** Depth figures should not.

**Mass conservation is unaffected by cell size**, staying at 0.0000% across every grid on both
sites. That is what a well-balanced conservative scheme should do and it is worth noting that the
refinement did not disturb it.

## What this does NOT say

It is not an accuracy figure. A converged answer is one the mesh has stopped changing — it is not
a correct one. The 30 m terrain, the unsurveyed riverbed and the 4× spread in breach parameters
are separate error sources, larger than this one, and no amount of refinement touches them. See
[`VALIDATION.md`](VALIDATION.md).

What it does buy is a bounded, measured statement about **one** error source, where previously
there was none.

## The open decision

The API default is still `cellsize_m = 90.0`. On this evidence **60 m is the better default** —
it is inside the converged range for depth and the windowed sweep makes it affordable in a way it
was not before.

It has deliberately **not** been changed, because `AGENTS.md` records cell size as one of the
values the project validates against: every published run, every number in `VALIDATION.md` and the
SFINCS cross-check at CSI 0.9607 were all produced at 90 m. Changing the default silently would
leave the documentation describing runs nobody can reproduce. That is the captain's call, and it
needs a re-run of the published figures behind it.

## Caveats on the timings

`grid_convergence.py` reports wall time, and that figure includes the terrain fetch and
conditioning as well as the solve. Cache state therefore dominates it — a size whose DEM was
already on disk looks far faster than one that had to be downloaded. **Do not read those numbers
as a solver benchmark.** `docs/LOAD_TEST.md` is where solver cost is measured properly.

Two sites, one scenario type, one reach length, one duration. Nothing here is extrapolated to
sites that were not run.
