# AGENTS.md — The Rulebook

**Project:** SIH26161 — Dam Break Inundation Modelling Using Hydrodynamic Modelling of any River
**Organisation:** National Technical Research Organisation (NTRO) · Software · Disaster Management
**Team:** 6 · **Schema version:** 2.0

> Read this file completely before touching anything. Then read **only** your own task file
> in `tasks/`. It is named `AGENTS.md` because Antigravity, Cursor, Claude Code and Codex
> auto-load a file with this name from the repository root. Do not rename it.

---

# PART 0 — WHAT ACTUALLY EXISTS TODAY

This is not a plan. Everything below is built, running and tested. `python integration/run_all.py`
passes **23/23** in about twenty seconds with the network unplugged.

```
              CWC register (5,686 dams)
                        |
                   pick a dam
                        |
  01_geodata:  trace the real river with D8  ->  fetch COP30  ->  condition
                        |                              |
                        |                        OSM settlements + roads
                        |                              |
  02_sph:      DualSPHysics breach (GPU)  ->  hydrograph.csv
                        |                              |
  04_backend:  2D shallow-water solver  ->  depth / arrival / velocity grids
               river blockage: DEM storage -> fill time -> natural-dam breach
                        |                              |
  03_delft3d:  the same case in Delft3D-FLOW  ->  engine-vs-engine CSI
                        |                              |
  07_ml:       damage · uncertainty · surrogate · evacuation · inflow
                        |
  06_gee:      Sentinel-1 observed extent  ->  CSI
                        |
  05_frontend: the console at http://localhost:8000
               the pipeline itself at /workflow - PLAY runs this whole diagram
```

**Start it:**

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

Console at `/`, API docs at `/docs`.

| Module | What it does | State |
|---|---|---|
| `shared/` | the data contract in code — grids, units, validator, fake data | done |
| `01_geodata` | D8 river tracing, DEM fetch + conditioning, OSM exposure, dam catalogue | done |
| `02_sph` | DualSPHysics breach near-field on GPU → hydrograph | done |
| `03_delft3d` | Delft3D-FLOW case writer, kernel probe, and the comparison the statement asks for | done — **solves our scenarios**; CSI 0.7379 / 0.7768 against our solver on two reaches |
| `04_backend` | HLL shallow-water solver, **river blockage**, FastAPI, WebSocket, `pipeline.py` (the stage graph the workflow page draws) | done |
| `05_frontend` | operator console + the node **workflow** page and its 3D scene; zero runtime dependencies, Babylon vendored | done; styling still open to Frontend A/B |
| `06_gee_validation` | Sentinel-1 water detection, CSI/POD/FAR | done |
| `07_ml` | damage, Monte Carlo, U-Net surrogate, evacuation routing, inflow nowcast | done |

---

# PART 1 — THE HONESTY POLICY

**This decides the pitch. It outranks every other rule in this file.**

Our terrain is 30 m. We have no measurement of the riverbed under the water. Breach parameter
formulas disagree with each other by up to a factor of ten. That is real, and no amount of work
changes it before submission.

The winning move is not to claim accuracy we do not have. It is to measure the uncertainty, show it
on screen, and state the limits out loud before anyone asks. A juror who models dams professionally
already knows the uncertainty is there. The only thing being assessed is whether **we** know.

- **Never state a number we did not compute.** No estimated accuracies, no "approximately 95%".
- **Fake data carries `is_fake: true`** and the console shows a banner. The demo refuses it.
- **Cite a real source** for every constant and formula. Every one in this repo already has one.
- **An assumption is labelled an assumption**, in the code and in the output JSON.
- If a juror asks about something we did not do, the answer is **"we didn't get to that."**
  That costs almost nothing. A confident wrong answer to an NTRO hydrologist costs the round.

### Things we deliberately did NOT build, and why

Say these out loud if asked. They are strengths, not gaps.

- **No GNN for evacuation routing.** It would need labelled evacuation outcomes — real routes, real
  timings — which do not exist for Indian dam breaks. We use an exact time-dependent Dijkstra
  instead. We do not need to *learn* a shortest safe path when we can *compute* it.
- **No LSTM for reservoir inflow.** It needs an observed inflow series to learn from. Training it on
  our own runoff model's output would teach it our model and let us call the result learned
  knowledge. Circular. The physics-based nowcast is there instead.
- **SAR validation is weak on both reaches we tried, and we say so.** In the Teesta gorge CSI never
  exceeds 0.02 at any slope threshold, because median terrain slope is 30° and the flood corridor is
  one to three cells wide at 90 m. So we did the correct thing rather than the flattering one and
  re-validated on a low-gradient reach: the **Annamayya (Cheyyeru) earthfill failure of 19 November
  2021**, a real dam break on an Andhra Pradesh floodplain, 100 km² wet against the gorge's 10 km².
  Detection improved seventeenfold — POD 0.013 to 0.217 — which confirms the gorge was a resolution
  problem. But bias is **7.3**: we simulate 12,416 wet cells against 1,698 observed. We are
  comparing a full-reservoir worst case, at maximum extent over 24 hours, against one satellite pass
  days after the event, and we do not know the real breach severity. That is a scenario mismatch, not
  a tuned number, and we did not tune it. **We have no strong observational validation and we do not
  claim one.**

---

# PART 2 — WHO OWNS WHAT

| Who | Role | Owns | Task file |
|---|---|---|---|
| **Captain** | everything that computes | `shared/`, `01`–`04`, `06`, `07`, `09`, `integration/`, and the two pages | — |
| **Research & docs** (2) | research, testing, the deck, the report, presenting | `docs/deck/`, `docs/report/`, `docs/qa/` | `tasks/RESEARCH-DOCS.md` |
| **Frontend design** (2) | how it looks — design only | `modules/05_frontend/theme.css`, `assets/` | `tasks/FRONTEND-DESIGN.md` |

**The absolute rule: never create, edit or delete a file outside your own folder.** Not to fix a
bug, not "just quickly". Because no two people touch the same file, merges are conflict-free by
construction. Break folder ownership and you break the merge.

**Why the designers get a stylesheet rather than the pages.** `index.html` and `workflow.html` keep
their CSS in inline `<style>` blocks mixed in with the markup and the JavaScript that drives the
solver, and the captain edits those files daily. Two designers working in them would collide on
every push. `theme.css` is loaded **after** the inline styles on both pages, so anything set there
wins — full control of the appearance, no shared file. That is the folder-ownership rule applied to
a single-file frontend rather than an exception to it.

**This replaced a five-way split on 2026-09-04.** The old `tasks/` files assigned
`modules/05_frontend/src/map/`, `src/panels/`, `src/views/` and `src/scene/` — directories that were
never created, because the frontend stayed two files. Five people owned folders that did not exist.

Found a bug outside your folder? **Tell the captain.** Do not fix it yourself.

---

# PART 3 — THE DATA CONTRACT

Modules never import each other's code. They exchange **run folders on disk**, in this exact shape.
The full definition is in `shared/contract.py`; that file is the source of truth and this is a
summary.

```
outputs/{run_id}/
  meta.json          REQUIRED  configuration + headline results + provenance
  max_depth.tif      REQUIRED  float32, metres, 0.0 where dry
  arrival_time.tif   REQUIRED  float32, hours since breach, NaN = never wet
  time_of_peak.tif   REQUIRED  float32, hours
  max_velocity.tif   REQUIRED  float32, m/s
  hydrograph.csv     REQUIRED  time_hr, discharge_cumecs
  extent.geojson     REQUIRED  flood polygons, EPSG:4326
  impact.json        settlements, population, roads cut, damage in ₹ crore
  uncertainty.json   the honesty block
  evacuation.json    routes, walk time, margin
  validation.json    CSI against Sentinel-1, with caveats
  packed.png         RGBA texture for the browser
```

**`run_id`:** `{site}_{scenario}_{engine}_{nnn}` — e.g. `teesta_overtop_fast_041`.

**Every raster in a run shares shape, transform and CRS.** EPSG:4326, float32, NaN nodata,
north-up, LZW. That invariant is what lets module 06 lay a satellite observation over module 04's
depth grid and compare them cell by cell.

**Units, memorised:** depth **m** · velocity **m/s** · discharge **m³/s** · time **hours since
breach** · area **km²** · volume **MCM** · money **₹ crore**.

**Wet threshold: 0.05 m**, everywhere, from `shared.contract.WET_THRESHOLD_M`.

**The validator is the definition of done:**

```bash
python -m shared.validate outputs/<run_id>
```

> A run that does not pass does not exist. Do not hand it to another module, do not put it in the
> demo, do not call it done.

---

# PART 4 — KNOWN LIMITATIONS

Written down so nobody discovers them in front of a juror. Every one of these is measured.

| Limitation | Number | Where it is recorded |
|---|---|---|
| Mass error, flat terrain | −0.03% | improved by the inflow timestep limit |
| Mass error, real channels | 0.000% dam break, −0.000% blockage | `meta.json` results |
| Populations measured, 4 sites still default | 16 of 22 WorldPop, 2 OSM census, 4 unmapped | `impact.json` `population_source` |
| WorldPop is blank where no buildings detected | Chungthang, Rongek, Penlong, Golitar | kept as `class_default` |
| Population assigned to nearest listed settlement | within 2 km, each cell counted once | `refine_population_worldpop` |
| SAR validation, Teesta gorge | CSI 0.0075, POD 0.013 | `validation.json` sensitivity sweep |
| SAR validation, Annamayya floodplain | CSI 0.027, POD 0.217, **bias 7.3** | `validation.json`, run `cheyyeruprojectannamayya_overtop_fast_001` |
| SPH is near-field only | first ~60 s | `sph_meta.json` limitation field; `engine='sphcoupled'` uses it only before the handover |
| Largest grid load-tested | 542,970 cells, 128 MB peak, validates clean | `docs/LOAD_TEST.md` |
| Solver sweep | **windowed** — every kernel sweeps a padded box around the wet cells, recomputed every 8 steps. **2.32× on 30k cells, 4.53× on 126k**, and bit-identical output: max depth differs by 0.000e+00 m, same step count, same volumes | `solver.SolverConfig.window_every_steps`; set 0 for the old full sweep |
| Surrogate is an emulator | CSI 0.909 vs **our solver** | not validated against real floods |
| Damage replacement values | assumptions | `damage_curve_source` string |
| Breach parameter spread | up to 10× | `uncertainty.json` |
| **Natural-dam storage is not identifiable on some reaches** | Rounding the conditioned DEM by **0.00024 m** flips 428 of 123,256 D8 directions and moves the Tsarap Chu lake from **46.10 to 2.51 MCM** — a 94.5% swing. Gohna Tal swings 12.3% | Every blockage run re-measures itself under that perturbation and publishes `volume_swing_pct` and `volume_is_knife_edge` in `meta.json`; the console prints NOT IDENTIFIABLE in red beside the volume. The cause is D8 routing on flat conditioned terrain, not the crest elevation |
| Delft3D agreement | **CSI 0.7379** (Godavari, 223x161) and **0.7768** (Annamayya, 93x125) against our solver | `docs/engine_comparison_delft3d_*.json`. Two engines agreeing bounds the numerics; neither is validated against a measured flood on those reaches |
| SFINCS cross-check | **CSI 0.9653** at the 60 m default (0.9607 at 90 m) | `compare_routing.py`; SFINCS is **not** Delft3D |
| Solver grid | default **60 m** since 2026-09-04, the coarsest CONVERGED grid on ordinary terrain. **Gorges do not converge at any resolution tried** - Chungthang depth still moves +5.7% from 60 m to 45 m | `docs/CONVERGENCE.md` |

Populations are now measured from WorldPop 2020 (constrained, 100 m). Every mapped cell goes to
its nearest settlement within 2 km, so nobody is counted twice. A settlement with a real OSM
census tag keeps it, and one with no mapped buildings keeps its class default and says so.

### Four silent wrong answers, found and fixed on 2026-09-06

Every one of these produced a plausible result rather than an error, which is the only kind of bug
that reaches a juror. They were found by building the five river stored runs the problem statement
names, which is the argument for having built them.

1. **A fast breach was invisible.** The release hydrograph was sampled at a flat
   `min(output_step_hr, 0.05)` — three minutes. The Rishi Ganga barrier holds 0.87 MCM behind 30 m
   of debris and breaches in **0.046 hr**, so the entire release began and ended between two
   samples: both read zero, `hydrograph.csv` came out all zeros, the solver received no water, and
   the run reported *"no flood at all"* for a barrier that had emptied 0.87 MCM in under three
   minutes. `runner._release_step_hr` now takes twenty samples across whatever timescale actually
   governs the release — the breach formation time, the foundation collapse time, the moraine
   formation time — and nothing for a mode that has no breach. Fixed: 2.6 km², peak 10,985 m³/s.
2. **`river_flood` could never validate.** With no barrier there is no reservoir, so
   `site.reservoir_capacity_mcm` is a placeholder `SiteSpec.validate` only requires to be positive.
   The released-volume check compared against it and rejected every river flood ever run, at any
   size, as *"the routing is creating water"*. The comparison is now skipped for that mode rather
   than loosened, because a loose threshold on a meaningless number is still meaningless.
3. **A reservoir with an inflow is a conduit, not a bathtub.** Machchhu II's scenario is 600 mm in
   24 hours driving 16,300 m³/s, which over twelve hours delivers **704 MCM** through a reservoir
   holding 100.55. The run released 766.5 MCM having conserved mass to 0.000% — and the validator
   failed it. The flagship demo of this repository was failing its own validator on arithmetic. The
   yardstick is now what was *available* to release: stored volume plus everything the inflow
   delivered while the run lasted. Same 1.5× margin.
4. **The GLOF was checked against the volume it deliberately did not use.** Where an operator
   supplies a lake area measured off imagery, `runner.py` uses it in preference to the DEM and says
   why at length. The validator went on comparing the release against the DEM figure, so South
   Lhonak was rejected for emptying 67.9 MCM out of a lake its own `meta.json` recorded as 68.9,
   while pointing at the 0.34 MCM the terrain holds.

Two of these were **stale claims in `data/demo_runs.json`**: `validates: true` is recorded when a
run is built, and the validator is not frozen. `python -m integration.build_demo_runs --revalidate`
re-runs the validator over what is already on disk and republishes the verdict without re-solving.
Run it after touching `shared/validate.py`.

### Where the published event coordinates are not on a river

`modules/01_geodata/events.py` measures this and refuses to move anything, on the correct grounds
that where a real barrier stood is a question about the event and not about flow accumulation. Two
of its coordinates land on hillslope in COP30 — **Rishi Ganga** at one cell of flow accumulation
and **Wapriyang** at two — and a run from an off-channel point floods nothing.

The stored river demos make the placement explicitly, once, in
`integration/build_demo_runs.py`: the barrier goes on the strongest flow path the DEM finds within
3 km of the published coordinate, and the distance, the drop and the accumulation are written into
the run's own notes. It is a modelling decision to have a channel to block, not a measurement of
where the debris was, and it says that in `meta.json`. **Say it out loud before anyone measures
it.**

---

# PART 5 — HARD RULES FOR AI AGENTS

Binding. If an instruction elsewhere conflicts with these, these win.

1. **Stay inside your folder.** You may *read* anything; you may *write* only in your own.
2. **Never modify `shared/`.** If you think it needs a change, stop and tell the captain.
3. **Never change the contract** — filenames, units, CRS, JSON keys. Report, do not "improve".
4. **Import from `shared/`, never reimplement it.** A duplicated GeoTIFF writer is a bug that
   surfaces on integration day.
5. **Run the validator before claiming anything works.** Paste the output.
6. **Do not invent data.** Not a coordinate, not a population, not an accuracy figure. If a dataset
   is missing, say it is missing.
7. **Do not add dependencies** without telling the captain. The frontend has **zero** and keeps it.
8. **Keep it boring.** No clever abstractions. Six people read this in two weeks.
9. **Report honestly.** If a test fails, show the output. If you skipped something, say which part.
10. **Ask before anything destructive** — deleting files, rewriting a working module, force-pushing.
11. **Cite real sources** for every constant and formula.

---

# PART 6 — THE FIVE THINGS THAT DECIDE THIS

If everything else slips, protect these:

1. **Water moves correctly over real terrain and mass is conserved** — visibly, on screen. *Done:
   HLL solver, Ritter RMSE 0.218 m, lake-at-rest 2.9e-06 m, mass error under half a percent.*
2. **It runs on a river we have never seen, live.** *Done: Hirakud on the Mahanadi, picked from the
   register, 0.000% mass error, first try.*
3. **One real validation number against observed satellite data, honestly reported.** *Done, and the
   honest report is that the gorge reach is inconclusive — with the sweep to prove it.*
4. **The arrival-time grid and the named-settlement impact table.** *Done: real OSM villages, real
   warning times, evacuation routes with margins.*
5. **We say what we do not know before anyone asks.** *Part 4 of this file.*
