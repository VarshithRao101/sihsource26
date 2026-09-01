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
passes **22/22** in about twenty seconds with the network unplugged.

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
  07_ml:       damage · uncertainty · surrogate · evacuation · inflow
                        |
  06_gee:      Sentinel-1 observed extent  ->  CSI
                        |
  05_frontend: the console at http://localhost:8000
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
| `03_delft3d` | far-field routing | **absent — engine check reports it, never estimates** |
| `04_backend` | HLL shallow-water solver, **river blockage**, FastAPI, WebSocket | done |
| `05_frontend` | plain operator console, zero dependencies | **handover to Frontend A/B** |
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

| Person | Role | Owns | Task file |
|---|---|---|---|
| **Captain** | everything that computes | `shared/`, `01`, `02`, `04`, `06`, `07`, `integration/` | — |
| **Frontend A** | map, scene, timeline | `modules/05_frontend/src/map/`, `src/scene/` | `tasks/FRONTEND-A.md` |
| **Frontend B** | panels, tables, forms | `modules/05_frontend/src/panels/`, `src/views/` | `tasks/FRONTEND-B.md` |
| **Docs A** | the deck | `docs/deck/` | `tasks/DOCS-A.md` |
| **Docs B** | the written report | `docs/report/` | `tasks/DOCS-B.md` |
| **QA** | breaking it before a juror does | `docs/qa/` | `tasks/QA.md` |

**The absolute rule: never create, edit or delete a file outside your own folder.** Not to fix a
bug, not "just quickly". Because no two people touch the same file, merges are conflict-free by
construction. Break folder ownership and you break the merge.

Two people share `modules/05_frontend/`, so the split goes one level deeper — see the two frontend
task files. `src/api.ts` and `src/types.ts` are **Frontend A owns, Frontend B may not edit**.

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
| SPH is near-field only | first ~60 s | `sph_meta.json` limitation field |
| Surrogate is an emulator | CSI 0.909 vs **our solver** | not validated against real floods |
| Damage replacement values | assumptions | `damage_curve_source` string |
| Breach parameter spread | up to 10× | `uncertainty.json` |
| Delft3D | absent | reported as absent, never estimated |

Populations are now measured from WorldPop 2020 (constrained, 100 m). Every mapped cell goes to
its nearest settlement within 2 km, so nobody is counted twice. A settlement with a real OSM
census tag keeps it, and one with no mapped buildings keeps its class default and says so.

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
