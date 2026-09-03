# Validation report

**Summary in one line: this project is strongly verified and weakly validated, and we publish both.**

Verification asks *did we solve the equations correctly*. Validation asks *does the answer match the
real world*. They are not the same question and the honest answers are very different.

Every number here is reproducible from a file in this repository. Where a result is bad, it is
printed at the same size as the good ones.

---

## 1. Verification — strong

All of these run in `integration/run_all.py` (23/23) and
`modules/04_backend/tests/test_solver_physics.py` (23 tests).

| Test | What it proves | Result | Acceptable? |
|---|---|---|---|
| Ritter analytical dam break | The solver reproduces the exact solution for an idealised instantaneous dam break | RMSE **0.218 m** over the rarefaction | Yes |
| Lake at rest | Well-balancedness — still water on uneven terrain stays still | **2.9e-06 m** spurious velocity | Yes |
| Closed-basin mass | No water created or destroyed in a sealed domain | **0.000%** | Yes |
| Real channel, dam break | Mass conservation on real conditioned terrain | **0.000%** | Yes |
| Real channel, blockage | Same, blockage scenario | **−0.000%** | Yes |
| No boundary inflow | No water enters through the edges | **+0.0000%** | Yes |
| Violent inflow | 30,000 m³/s into 5 cells does not break conservation | **+0.0000%** | Yes |
| Flat terrain | Historic weak point, improved by the inflow timestep limit | **−0.03%** | Yes |

**Interpretation.** The hydraulics are sound. A juror probing whether the numerics are trustworthy
should be pointed here first — an analytical comparison is the strongest evidence a flood model can
offer about itself, and most projects do not attempt one.

---

## 2. Validation against observed floods — weak, on both reaches

### 2.1 Chungthang / Teesta, Sikkim — inconclusive

- **Event:** Teesta GLOF, October 2023
- **Observation:** Sentinel-1 GRD via Google Earth Engine, VV, descending, change detection against a
  pre-event median
- **Run:** `chungthangdam_overtop_fast_002` → `validation.json`

| Metric | Value |
|---|---|
| CSI | **0.0075** |
| POD | 0.013 |
| FAR | 0.982 |
| bias | 0.70 |

**Why it failed, measured not guessed.** Median terrain slope in the reach is 30°, and the flood
corridor is one to three cells wide at 90 m resolution. Sentinel-1 physically cannot resolve a
channel that narrow in terrain that steep; radar shadow in a gorge is also dark and misclassifies as
water. A slope-threshold sensitivity sweep is published in the same `validation.json` and CSI never
exceeds 0.02 at **any** threshold.

**What we did not do:** tune the threshold until the number looked better. The sweep exists precisely
so that choice is visible.

### 2.2 Annamayya / Cheyyeru, Andhra Pradesh — better, still not sufficient

`AGENTS.md` Part 1 said the correct fix was to validate on a low-gradient reach rather than tune. So
we did.

- **Event:** Annamayya (Cheyyeru Project) earthfill embankment failure, **19 November 2021** — a real
  dam break, inside the Sentinel-1 era, on a floodplain
- **Dam:** `AP01MH0129`, from the CWC register — 25 m, 63.16 MCM
- **Run:** `cheyyeruprojectannamayya_overtop_fast_001` → `validation.json`

| | Chungthang gorge | Annamayya floodplain |
|---|---|---|
| Wet area simulated | 10.4 km² | **100.6 km²** |
| CSI | 0.0075 | **0.0268** |
| POD | 0.013 | **0.217** |
| FAR | 0.982 | 0.970 |
| bias | 0.70 | **7.31** |

**What improved and why it matters.** POD rose **seventeenfold** — we now detect 22% of observed
water instead of 1%. That confirms the gorge result was a *resolution* problem, not a model problem.
It is a real diagnostic finding.

**What is still wrong, stated plainly.** Bias 7.31 means **12,416 simulated wet cells against 1,698
observed**. We are over-predicting extent by a large factor. The dominant reason is a scenario
mismatch we cannot remove:

1. We simulate a **full reservoir** and a complete breach. The real failure's severity is unknown.
2. We compare **maximum extent over 24 hours** against **one satellite pass**, days after the event,
   when water had receded.
3. Sentinel-1 change detection removes permanent water — and any flooding already present before the
   event window.

**Is this scientifically defensible?** As a *diagnosis*, yes: it isolates resolution as the gorge's
problem and scenario severity as the floodplain's. As *evidence the model predicts real flood extent
accurately*, **no** — and we do not present it that way.

**We have no strong observational validation and we claim none.**

---

## 2.3 Grid convergence - measured, and it changes what we should quote

A third question sits between verification and validation: how much of the answer is the MESH?
Measured on two independent sites in [`CONVERGENCE.md`](CONVERGENCE.md).

| refinement | max depth | flood area |
|---|---|---|
| 90 m -> 60 m | -5.9% / +3.2% | +7.7% / +3.3% |
| **60 m -> 45 m** | **+1.1% / +0.6%** | +1.0% / +5.7% |

**Maximum depth converges by 60 m.** The 60->45 m change is an order of magnitude smaller than the
coarse-grid changes, so depth at 60 m is within about 1% of its grid-independent value.

**90 m - the resolution every published run here uses - is not in the converged range.** Refining
from it still moves depth by 3 to 6%, which is pure discretisation error carried by every depth
figure in this document.

**Flood extent converges more slowly, and on one site not at all**: Lower Manair's area is still
moving +5.7% between 60 m and 45 m. Extent is decided by very shallow water at the margin, where a
cell sits just either side of the 0.05 m threshold. Every AREA figure in this repository should be
read as carrying a several-percent grid dependence. Depth figures should not.

Mass conservation is unaffected by cell size - 0.0000% at every grid on both sites.

---

## 3. Cross-checks that are not validation

Useful, and honestly labelled as something less than validation.

| Cross-check | Result | What it does and does not prove |
|---|---|---|
| **Our solver vs SFINCS** | **CSI 0.9607** extent agreement; 10.39 vs 10.57 km²; 40.22 vs 37.58 m max depth | Two INDEPENDENT 2D engines, identical terrain, grid, forcing and wet threshold. The closest thing to external corroboration this project has. Still not validation against reality - both could be wrong the same way |
| SPH vs weir equation | within **5%** | Two independent methods agree on breach discharge. Does not prove either matches reality |
| Surrogate vs solver | CSI **0.909**, depth MAE 1.11 m, ~20 ms | The U-Net faithfully emulates **our solver**. Says nothing about reality |
| Hirakud vs empirical envelope | 265,799 m³/s inside 38,315–380,296 | Plausible, but near the top. **Not reviewed by a practising engineer** |
| Breach regression spread | 9,377–37,206 m³/s, **4.0×** | Quantifies how uncertain breach parameters genuinely are |

---

## 4. Not validated at all

Stated so nobody assumes otherwise.

| Component | Status |
|---|---|
| **Damage estimates** | Curves are real (Huizinga et al. 2017, JRC EUR 28552 EN). **Replacement values are project assumptions, not measured.** No comparison against observed losses |
| **Evacuation routes** | Exact time-dependent Dijkstra on the OSM road graph. No observed evacuation outcome to compare against — none exist for Indian dam breaks |
| **Delft3D** | Not installed. Reported absent, never estimated |
| **LSTM inflow** | Not built. No observed inflow series exists to train or test against |
| **Blockage lake volumes** | Read off the DEM. No surveyed landslide-dam volume to compare against |

---

## 5. Known limitations, with numbers

Mirrors `AGENTS.md` Part 4.

| Limitation | Number | Recorded in |
|---|---|---|
| Terrain resolution | 30 m COP30, ~90 m solver grid | `meta.json` → `dem` |
| **Grid dependence, depth** | 90 m is not converged: refining to 60 m moves max depth **3–6%**. Converged by 60 m (60→45 m is ~1%) | `docs/CONVERGENCE.md` |
| **Grid dependence, extent** | Area still moves **+5.7%** from 60 m to 45 m on one of two sites. Every area figure carries several percent of grid dependence | `docs/CONVERGENCE.md` |
| Bathymetry | none — bed is estimated | `meta.json` → `dem.bathymetry` |
| SAR validation, gorge | CSI 0.0075 | `validation.json` |
| SAR validation, floodplain | CSI 0.0268, bias 7.31 | `validation.json` |
| Populations, unmapped sites | 4 of 22 stay class defaults at Teesta | `impact.json` → `population_source` |
| WorldPop coverage | only 13.8% of the Teesta tile has data | measured from the clipped raster |
| SPH | near-field only, first ~60 s | `sph_meta.json` |
| Surrogate | emulator, not validated against real floods | `surrogate_metrics.json` |
| Breach parameter spread | up to 4× on peak discharge | `uncertainty.json` |
| Storage curve | k = 2.7 assumed, no surveyed curve | `meta.json` → `scenario.storage_curve` |
| Damage replacement values | assumptions | `impact.json` → `damage_curve_source` |
| Delft3D | absent | `compare_engines.py`, gate-enforced |

---

## 6. What would make this genuinely validated

In order of value, and honest about what each requires from outside the project:

1. **A Copernicus EMS rapid-mapping extent** for an Indian flood — a curated flood polygon rather
   than a raw backscatter threshold. Removes the SAR classifier as a source of error and would let us
   report a defensible CSI. *Needs: a matching EMS activation.*
2. **A documented dam-break event with known breach parameters** — validating a worst-case scenario
   against an event of unknown severity is what bias 7.31 is measuring. *Needs: a post-event
   engineering report.*
3. **An observed inflow series** from India-WRIS or CWC. Unblocks the LSTM and lets us validate the
   rainfall-runoff nowcast. *Needs: a data download.*
4. **A hydraulics engineer's review of Hirakud.** *Needs: ten minutes of an expert's time.*
5. **Delft3D**, to complete the engine comparison the problem statement asks for. *Needs: compiling
   the Delft3D 4 kernel (`d3d4-suite`) — GPLv3, no licence, Intel oneAPI in a Docker devcontainer.
   The FM licence we requested was never answered, but FM is not the model the statement names.*

---

## 7. Reproducing everything here

```bash
.venv\Scripts\python.exe integration\run_all.py
```
23/23, about twenty seconds, works with the network unplugged.

```bash
.venv\Scripts\python.exe -m pytest modules\04_backend\tests -q
```
23 tests.

```bash
.venv\Scripts\python.exe -m shared.validate outputs\chungthangdam_overtop_fast_002
```
All seven runs on disk pass with zero errors and zero warnings.

```bash
.venv\Scripts\python.exe integration\compare_engines.py --capacity 5 --height 60
```
The engine and regression spread, with Delft3D reported absent.
