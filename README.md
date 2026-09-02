# SIH26161 — Dam Break Inundation Modelling

Point it at any of 5,686 Indian dams and it tells you which villages flood, how deep, and **how
long they have**.

Double-click **`start_console.bat`**, or from a terminal:

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

Console at <http://localhost:8000>, API docs at `/docs`. Give it about twenty
seconds — it warms the solver JIT and loads the ML surrogate at boot so nobody
watches it happen mid-demo. **Keep the window open; closing it stops the server.**

Read **`AGENTS.md`** before changing anything. If you are on the team, read your file in `tasks/`.

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then fill it in
.venv\Scripts\python.exe -m shared.creds        # check what is missing
.venv\Scripts\python.exe integration\run_all.py # should print 18/18
```

`run_all.py` works with the network unplugged. Run it before every push.

---

## Everyday commands

```bash
# find a dam
.venv\Scripts\python.exe -m modules.01_geodata.dams search --state Odisha --limit 5

# check a result is contract-valid  (a run that fails this does not exist)
.venv\Scripts\python.exe -m shared.validate outputs\teesta_overtop_fast_041

# compare the engines
.venv\Scripts\python.exe integration\compare_engines.py --capacity 5 --height 60

# uncertainty band
.venv\Scripts\python.exe -m modules.07_ml.montecarlo --capacity 5 --height 60

# evacuation routes
.venv\Scripts\python.exe -m modules.07_ml.evacuation --run outputs\teesta_overtop_fast_051

# rainfall -> reservoir inflow, from satellite
.venv\Scripts\python.exe -m modules.07_ml.inflow --lat 27.6003 --lon 88.6428 --site teesta

# SPH breach on the GPU
.venv\Scripts\python.exe -m modules.02_sph.breach run --height 60 --width 57.6 --dp 2.0

# solver physics tests
.venv\Scripts\python.exe -m pytest modules\04_backend\tests -q
```

---

## Water release

NTRO asks for "dam break **or water release**". They are different events and do not share an
implementation. A release opens the gates on a structure that stays intact, so no breach regression
is used at all - the water leaves through the outlet works the dam was built with:

```
gate      Q = Cd A sqrt(2 g (y - y_invert))     orifice, Fread (1988)
spillway  Q = C L (y - y_crest)^1.5             broad-crested weir
```

Set `failure_mode="gated_release"` and `gate_opening_frac`. When the dam comes from the CWC register
its **design spillway capacity is used as the gate capacity** - a measured number rather than an
assumption - and the release is capped at it. Annamayya, 63.16 MCM behind a 25 m embankment:

| scenario | peak | volume released |
|---|---|---|
| dam break, overtopping | 11,325 m3/s | 63.15 MCM |
| gated release, gates 100% | 8,069 m3/s | 63.14 MCM |
| gated release, gates 25% | 2,609 m3/s | 63.14 MCM |

Same water, a quarter of the peak. That is the operational point of a controlled release, and it is
recorded under `meta.json` -> `gated_release` with the capacity source named.

## River blockage

A landslide dam, not an engineered one. Set `failure_mode="blockage_breach"` and
`blockage_height_m` on the scenario; the storage is read off the DEM (nobody published it), the fill
time comes from upstream inflow, and the breach uses natural-dam regressions:

```python
spec = ScenarioSpec(site=site, failure_mode="blockage_breach",
                    blockage_height_m=60.0, inflow_cumecs=60.0, ...)
```

A 60 m blockage on the Teesta impounds 5.95 MCM over 0.17 km², breaches 312 m wide in 10 minutes,
and takes 27.5 hours to fill — that fill time is the warning a blockage gives and a dam break does
not. Recorded under `meta.json` → `blockage`.

---

## Manual tasks still outstanding

Ordered by value per minute of your time. Nothing in the code is waiting on items 3–6.
Item 1 is done.

### ~~1. WorldPop population raster~~ — DONE

Populations are now measured. 16 of 22 Teesta settlements carry a WorldPop count labelled
`population_source: worldpop2020`; Gangtok and Mangan keep their real OSM census tags; four
(Chungthang, Rongek, Penlong, Golitar) keep `class_default` because WorldPop's constrained product
has **no mapped built-up area** within 2 km of them — it is blank wherever no buildings were
detected, and that includes some real high-altitude Sikkim villages. That gap is honest and stays
visible in the table.

To reproduce on a fresh clone (the raster is gitignored — 531 MB):

```bash
curl -L -o data/worldpop/ind_ppp_2020_constrained.tif   https://data.worldpop.org/GIS/Population/Global_2000_2020_Constrained/2020/BSGM/IND/ind_ppp_2020_constrained.tif
```

Then clip it to the site bbox into `data/exposure/{site}/population.tif` and delete
`data/exposure/{site}/exposure.json` so it refetches. `refine_population_worldpop` activates on its
own.

**Method:** every mapped 100 m cell is assigned to its **nearest** settlement within 2 km, so each
person is counted once. Summing a box around each place instead double-counted anyone living
between two villages — it produced 83,966 people across the refined settlements when the whole tile
only holds 152,239, a total a juror could break by adding up our own column.

### 2. Delft3D — you need the **kernel**, not the licence manager

The one NTRO deliverable we cannot currently claim.

**Deltares ships three separate downloads and only one of them solves anything:**

| Download | What it is | Do we need it |
|---|---|---|
| Deltares License Software | FlexNet licence manager (`DS_Flex.exe`, `lmadmin`) | only to license the suite |
| Delft3D FM Suite 2D3D (HM) | the GUI modelling environment | optional |
| **D-Flow FM kernel / DIMR** | **the solver we drive from the command line** | **yes — this one** |

`Deltares License Software/` is already downloaded. It is **not** a solver, and the engine check
says so out loud rather than counting it.

Get the kernel from <https://download.deltares.nl/> (manual approval, can take days), fallback
<https://oss.deltares.nl/web/delft3d/download>. Unpack it next to `DualSPHysics_v5.4/`, or set
`DELFT3D_HOME` in `.env` to wherever it lands. Then:

```bash
.venv\Scripts\python.exe -m modules.03_delft3d.engine
```

That prints what it found and exits 0 once a real kernel is present. It looks for `dimr` or
`dflowfm` on PATH, under `$DELFT3D_HOME`, beside the repo, and in the Deltares-named folders of the
usual install roots.

Until a kernel exists, `compare_engines.py` reports Delft3D as **absent, never estimated** — and
that absence is now *measured* by `modules/03_delft3d/engine.py` rather than hardcoded. Once it
lands, tell me and I will write the case builder (`hydrograph.csv` + DEM → D-Flow FM input) and the
reader that pulls its output back into the contract.

### 3. Pick the second demo river and confirm the first

Hirakud on the Mahanadi already works as an unseen site. Decide whether that is the live demo or
whether you want a third, and confirm Chungthang/Teesta as the primary.

### 4. Observed inflow series — unblocks the LSTM

`modules/07_ml/inflow.py` has the physics-based nowcast. An LSTM needs an observed daily inflow or
reservoir-level series to learn from. If you can get one from India-WRIS
(<https://indiawris.gov.in/>), CWC, or the operating authority, save it as:

```
data\inflow\{site}_observed.csv        # date,inflow_cumecs
```

Then it becomes a real supervised problem instead of a circular one.

### 5. FABDEM tiles — better terrain, optional

COP30 sees treetops and rooftops; FABDEM strips them. Free for non-commercial use, tiles for
27–28°N / 88–89°E: <https://data.bris.ac.uk/data/dataset/25wfy0f9ukoge2gs7a5mqpq2j7>

Put them in `data\dem\` and pass `RealTerrain(source="FABDEM", local_dem=...)`.

### 6. Sanity-check Hirakud with someone who knows it

Our routed peak is 265,799 m³/s. That is inside the empirical envelope
(38,315 – 380,296 m³/s) but near the top of it. Before it goes on a slide, have a hydraulics person
look at it.

---

## What is honest and what is not

`AGENTS.md` Part 4 is the full list of limitations, with numbers. The short version:

- Verification is strong — Ritter RMSE 0.218 m, lake at rest 2.9e-06 m, closed-basin mass 0.000000%
- Validation against observed floods is **weak on both reaches we tried, and we say so** — the
  Teesta gorge is too steep and too narrow for Sentinel-1 at 90 m (CSI 0.0075, sweep published), so
  we re-validated on the real Annamayya earthfill failure of 19 Nov 2021 on an Andhra Pradesh
  floodplain. Detection improved 17× (POD 0.013 → 0.217), confirming the gorge was a resolution
  problem — but bias is 7.3, because we compare a full-reservoir worst case at maximum extent
  against one satellite pass days later. No strong observational validation exists, and we claim none
- There is **no accuracy percentage** anywhere in this project, because we did not measure one
- The ML surrogate emulates *our own solver* (CSI 0.909), not reality
- We declined to build a GNN and an LSTM rather than train them on our own output

---

## Layout

```
shared/            the data contract in code. Captain only.
modules/
  01_geodata/      river tracing, DEM, exposure, dam catalogue
  02_sph/          DualSPHysics breach
  03_delft3d/      engine detection only — no solver installed yet
  04_backend/      solver, API, WebSocket
  05_frontend/     the console
  06_gee_validation/  Sentinel-1 + CSI
  07_ml/           damage, uncertainty, surrogate, evacuation, inflow
integration/       run_all.py (the gate), compare_engines.py
data/              inputs. Read-only outside the producing module.
outputs/           run folders. Never committed.
tasks/             one file per teammate.
```
