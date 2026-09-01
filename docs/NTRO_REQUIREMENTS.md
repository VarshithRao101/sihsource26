# NTRO requirement mapping

Problem Statement **26161**, deliverable by deliverable, against what exists in this repository.
The official text is in [`PROBLEM_STATEMENT.md`](PROBLEM_STATEMENT.md) and is the source of truth.

Every row names the file that implements it and the evidence that it works. Where something is
missing it says so plainly — nothing here is estimated.

**Status key:** ✅ complete and verified · 🟡 implemented, needs real-world validation ·
🔴 not implemented · ⚪ not requested

---

## Deliverable (i)

> *Creation of generalized modelling framework to predict / simulate dam break / river blockage
> analysis providing the necessary inputs on the basis of sudden water surge as well as loss and
> damage analysis using 'Smooth Particle Hydrodynamics model and Delf3D model'.*

| Clause | Implementation | Evidence | Status |
|---|---|---|---|
| **Dam break** | `modules/04_backend/solver.py` — 2D shallow water, HLL Riemann, Audusse well-balanced | Ritter analytical RMSE **0.218 m**; lake-at-rest **2.9e-06 m**; mass **0.000%** | ✅ |
| **River blockage** | `modules/04_backend/blockage.py` — storage read off the DEM, fill time from upstream inflow, natural-dam breach regressions (Peng & Zhang 2012) | `latatapovanntpc_blockage_fast_001` — 6.32 MCM lake, 280.3 m breach, validates clean | ✅ |
| **Water release** | `shared/hydro.py::gated_release_hydrograph` — orifice gates (Fread 1988) + broad-crested spillway. **No breach regression: the dam does not fail** | `cheyyeruprojectannamayya_gated_fast_001` — 8,069 m³/s capped at the register's design capacity | ✅ |
| **Sudden water surge** | `shared/hydro.py` — Froehlich (2008), Von Thun & Gillette (1990), MacDonald & Langridge-Monopolis (1984), all computed, none averaged | `uncertainty.json` — 4.0× spread published per run | ✅ |
| **Loss and damage** | `modules/07_ml/damage.py` — JRC Huizinga et al. (2017) Asia depth-damage curves + Clausen & Clark (1990) velocity aggravation | `impact.json` — damage in ₹ crore, buildings/roads/cropland split | 🟡 replacement values are **stated assumptions**, not measured |
| **SPH model** | `modules/02_sph/breach.py` — DualSPHysics v5.4 on GPU | 99,000 particles; agrees with the weir equation to **5%** | 🟡 cross-check, not independent validation |
| **Delft3D model** | `modules/03_delft3d/engine.py` — detection only | Absence is **measured**, not asserted; gate-enforced | 🔴 **licence not granted** |
| **"compare the scenario"** | `integration/compare_engines.py` | Compares our solver, weir equation, DualSPHysics and four empirical regressions. **The Delft3D row is empty** | 🟡 incomplete while Delft3D is absent |

---

## Deliverable (ii)

> *Building a customized tool/ framework so that it is possible to generate a flood inundation
> simulation scenario using different input datasets.*

| Clause | Implementation | Evidence | Status |
|---|---|---|---|
| Customisable scenarios | `modules/04_backend/api.py::RunRequest` | 17 parameters: failure mode, breach regression, reservoir level, gate opening, reach length, cell size, duration, scheme, roughness, terrain source | ✅ |
| Different input datasets | `modules/01_geodata/provider.py` | COP30 · SRTM · NASADEM · ALOS · FABDEM, any bbox on earth. NTRO's dataset link names ASTER/SRTM — **SRTM is supported** | ✅ |
| Satellite imagery as model input | `modules/01_geodata/roughness.py` | ESA WorldCover → per-cell Manning *n*. Imagery feeds the model, not just the validation | ✅ |
| Hydrological data | `modules/07_ml/inflow.py` | CHIRPS rainfall → SCS runoff → routing nowcast | 🟡 no **observed** inflow series obtained |

---

## Deliverable (iii)

> *Developing a Dashboard for providing modelling input and output visualization framework (GUI). The
> program should support the large volume of data. Output should be converted to .shp or .Kml file.*

| Clause | Implementation | Evidence | Status |
|---|---|---|---|
| Dashboard — **input** | `modules/05_frontend/index.html` | Scenario form driven by `/api/enums`; cascading dam picker over 5,686 CWC dams | ✅ |
| Dashboard — **output visualisation** | same file | Flood map with time scrubber, 5 analysis charts, impact table, evacuation table, uncertainty panel, live WebSocket progress | ✅ |
| GUI | same | Single file, **zero dependencies, no build step**, works offline with OSM raster fallback | ✅ |
| **"large volume of data"** | `packed.png` RGBA texture — the whole time-varying flood in one image rather than per-frame rasters | Largest run to date **437 × 343 ≈ 150,000 cells**. **Never load-tested to a ceiling** | 🟡 untested claim |
| **.shp export** | `api.py` `/api/runs/{id}/export?format=shp` | Verified live: zip containing `.shp`, `.shx`, `.dbf`, `.prj`, `.cpg` | ✅ |
| **.kml export** | same, `format=kml` | Verified live: 27 KB, opens in Google Earth | ✅ |
| GeoJSON (bonus) | same, `format=geojson` | Verified live | ✅ |

---

## Deliverable (iv)

> *Additionally, developing a framework for near real time flood analysis through Google Earth Engine
> with the help of open source data.*

| Clause | Implementation | Evidence | Status |
|---|---|---|---|
| GEE framework | `modules/06_gee_validation/sar.py` | Sentinel-1 GRD, VV, descending, change detection against pre-event median | ✅ |
| Observed flood extent | same | CSI / POD / FAR against our simulated extent, with caveats attached | 🟡 see `VALIDATION.md` — weak on both reaches tried |
| Near-real-time **input** | `modules/07_ml/inflow.py` | CHIRPS rainfall → reservoir inflow nowcast. GEE used in **both** directions | ✅ |
| Open-source data | — | Sentinel-1, CHIRPS, COP30, ESA WorldCover, OSM, WorldPop, CWC NRLD. All open. Full list in `data/SOURCES.md` | ✅ |

---

## Deliverable (v)

> *Simulation needs to be done by taking the any river and Dam data (open source) of India during the
> final demonstration of the software.*

| Clause | Implementation | Evidence | Status |
|---|---|---|---|
| Any Indian dam | `modules/01_geodata/dams.py` | **5,686 dams**, 29 states, parsed from the CWC National Register of Large Dams 2019 | ✅ |
| Open-source dam data | same | CWC NRLD 2019 + GRanD | ✅ |
| Any river | `modules/01_geodata/domain.py` | D8 drainage tracing from the DEM — no pre-built river list needed | ✅ |
| **Proven on unseen sites** | — | **Hirakud** (Mahanadi, Odisha) and **Annamayya** (Cheyyeru, Andhra Pradesh), both first try, both 0.000% mass error | ✅ |
| Live demonstration | `docs/DEMO_RUNBOOK.md` | Needs internet for a **new** dam — the DEM must be fetched and we refuse to substitute synthetic terrain. Cached sites run fully offline | 🟡 mitigated by pre-caching |

---

## Binding sentences in the Description

Not numbered deliverables, but requirements all the same.

| Phrase | Where we stand | Status |
|---|---|---|
| *"automatically carry out the simulation modelling"* | No code editing between the operator's request and the result — proven twice on dams never run before | ✅ |
| *"using hydrological data, DEM and satellite imagery"* | DEM ✅ · satellite imagery ✅ (WorldCover → roughness, Sentinel-1) · hydrological 🟡 (modelled rainfall-runoff, no observed series) | 🟡 |
| *"identify the inundated area due to flash flood in the lower catchment"* | Downstream reach is the modelled domain — depth, arrival, velocity, duration grids + extent polygons | ✅ |
| *"in case of dam break or water release"* | Both implemented as **separate physics**. See deliverable (i) | ✅ |
| *"and compare the scenario"* | `compare_engines.py`, incomplete while Delft3D is absent | 🟡 |
| HADR framing (stated twice) | Named villages, arrival times, roads cut, evacuation routes with walk-time margins, and settlements with **no safe route** | ✅ |

---

## The events NTRO names

Four of the five failures in their Background are **natural dams**, not engineered ones.

| Event | Type | Covered? |
|---|---|---|
| Rishi Ganga, Uttarakhand, Feb 2021 | natural lake | ✅ `latatapovanntpc_blockage_fast_001` models a landslide dam on the Dhauliganga at Lata Tapovan (CWC coordinate) — the reach destroyed in that event |
| Wapriyang, Nov 2021 | natural dam | 🟡 same physics, not run as a named case |
| Phuktal / Sumdo, J&K, Mar 2015 | natural dam | 🟡 same physics, not run |
| Kosi, 2008 | embankment breach | 🟡 same physics, not run |
| Kashmir / Assam, 2014 | flood | ⚪ not a dam-failure case |

---

## Not requested

Recorded so nobody assumes these were required. The official text contains **no** requirement for
any of them.

| | Status |
|---|---|
| Machine learning / AI | ⚪ Not requested. Four models built anyway — XGBoost damage, Monte Carlo + GP uncertainty, U-Net surrogate (CSI 0.909, ~900× faster), Sentinel-1 water classifier. **Presented as bonus, never as a deliverable** |
| Quantum computing | ⚪ Not requested. Not built |

---

## Scorecard

| Deliverable | Status |
|---|---|
| (i) framework: dam break · blockage · water release · surge · loss & damage · SPH | ✅ |
| (i) Delft3D and the engine comparison | 🔴 **licence not granted** |
| (ii) customisable tool, different datasets | ✅ |
| (iii) dashboard + .shp/.kml | ✅ (load-test outstanding) |
| (iv) near-real-time GEE | ✅ |
| (v) any Indian river and dam, live | ✅ |

**One deliverable clause outstanding, and it is a procurement problem rather than an engineering
one.** Everything else is implemented, and every implementation names the file that proves it.
