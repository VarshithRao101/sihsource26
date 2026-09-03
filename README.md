---
title: SIH26161 Dam Break Inundation
emoji: 🌊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

<!-- The block above is Hugging Face Spaces configuration. Spaces reads its
     settings from YAML front-matter in README.md and there is nowhere else to
     put it, so it lives here and GitHub renders it as a table. Deleting it
     breaks the Space; it affects nothing else. See "Deploying" below. -->

# SIH26161 — Dam Break Inundation Modelling

Point it at any of 5,686 Indian dams and it tells you which villages flood, how deep, and **how
long they have**.

Double-click **`start_console.bat`**, or from a terminal:

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

Give it about twenty seconds — it warms the solver JIT and loads the ML surrogate at boot so
nobody watches it happen mid-demo. **Keep the window open; closing it stops the server.**

| | |
|---|---|
| <http://localhost:8000> | **Console** — pick a dam, run it, read the flood. Point at any cell on the map for depth, speed and hazard. |
| <http://localhost:8000/workflow> | **Workflow** — the whole pipeline as boxes and arrows. Press PLAY and watch each stage light up as it actually executes, with a live 3D flood beside it. |
| <http://localhost:8000/docs> | **API** |

Read **`AGENTS.md`** before changing anything. If you are on the team, read your file in `tasks/`.

---

## The Workflow page

This is the one to open in front of somebody who has not seen the project. It draws all
seventeen processing stages — dam register, river trace, DEM conditioning, exposure, breach,
SPH, the 2D solver, the contract writer, damage, evacuation, uncertainty, the validator — with
the data flow between them, and it is generated from `modules/04_backend/pipeline.py` rather
than drawn by hand, so the picture cannot drift from the code.

- **PLAY** starts the real pipeline. Every box goes WAITING to RUNNING to COMPLETE or FAILED off
  the same WebSocket the console uses, carrying the backend's own words: *"breach 268 m wide in
  5.45 hr, peak 33,865 m3/s"*, *"58 settlements, 363 road segments"*.
- **PAUSE** blocks the solver thread between timesteps. It is a real pause, not a frozen
  picture of one - `pct` stops moving.
- **RESET** cancels the solve and puts every box back to WAITING.
- **Click any box** for what that stage genuinely takes in, puts out, which file does it, and
  the papers behind it.
- The **3D scene** is Babylon.js on the conditioned DEM, with the water surface reconstructed
  from the arrival-time, peak-time, depth and duration grids and coloured by velocity.
  Simulation time, depth, velocity, flooded area, people reached and discharge update as it
  plays. It says on screen that it is a rendering of output grids and not frame-by-frame
  solver output, because that is what it is.
- **Delft3D never turns green.** The kernel is not installed, the node is a filesystem probe,
  and it lists the paths it searched. SFINCS next to it is a different Deltares model and is
  labelled as one.

Babylon.js is vendored at `modules/05_frontend/vendor/` and served by our own backend. There is
still no CDN and no build step: the console renders with the network unplugged.

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

## Deploying: the pages on Vercel, the solver here

**The backend does not run on Vercel and cannot be made to.** It needs numba to JIT
the kernels, rasterio for terrain, torch for the emulator, a writable disk for the run
folder, a long-lived process to hold the run registry, and a WebSocket. Serverless
functions freeze when they return, share no filesystem, do not support WebSockets at
all, and cap the bundle at 250 MB against the 5.6 GB this project installs. That is
architectural, not a setting.

So there are two deployments, and they are different programs:

| | what it is | what it does |
|---|---|---|
| `api/index.py` | the **read-only** build Vercel serves | both pages, the processing graph, the 5,686-dam register, and the runs committed to the repo. Anything that would have to compute returns **501 with the reason**. `/health` reports `mode: readonly` so PLAY greys out and says why |
| `modules/04_backend/api.py` | the **real** backend | solves |

Deployed alone, the read-only build loses PLAY, the live WebSocket, the 3D scene, the
point query and `.shp`/`.kml`. There are two ways to get them back, and they are not
exclusive.

### Option 1 — the solver on Hugging Face Spaces (hosted, no card)

`Dockerfile` builds the real backend. Spaces gives **2 vCPU and 16 GB RAM free**, runs
Docker, and supports WebSockets — more headroom than Render's paid 2 GB tier, and it
does not ask for a card.

```bash
git remote add hf https://huggingface.co/spaces/<your-username>/<space-name>
git push hf main
```

Create the Space first with **SDK: Docker**. The YAML front-matter at the top of this
README is what configures it — `app_port: 7860` has to match the Dockerfile's fallback
port, and it does.

Then add **`OPENTOPOGRAPHY_API_KEY`** under the Space's *Settings → Secrets*. It is the
only credential a live run genuinely needs: it fetches the DEM, and the code refuses to
substitute synthetic terrain. `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` are a fallback
DEM provider; the `EE_*` and `CDSE_*` keys gate Earth Engine and SAR, neither of which
runs inside a live request. `MAPTILER_API_KEY` is registered in `shared/creds.py` but
**unused** — the map draws OpenStreetMap raster tiles directly.

Two limits worth knowing before demo day: the Space's disk is **ephemeral**, so finished
runs disappear when it restarts, and it **sleeps after about 48 hours idle** and needs a
click to wake.

### Option 2 — the solver on your own machine, over a tunnel

Nothing to deploy, and it keeps your GPU torch build and every cached DEM:

```bash
start_public.bat https://your-app.vercel.app
```

That sets `SIH_CORS_ORIGINS` so the backend accepts calls from the Vercel origin, and
opens a Cloudflare tunnel in a second window. Take the `https://<random>.trycloudflare.com`
URL it prints and open:

```
https://your-app.vercel.app/?api=https://<random>.trycloudflare.com
```

`config.js` writes that base to `localStorage`, so the Workflow page picks it up when
you click across and you only paste it once. Open with a bare `?api=` to clear it and
go back to same-origin.

**The tunnel is not about being reachable from the internet — it is about TLS.** A
browser refuses to let an `https://` page call `http://` or `ws://`, so a plain
`localhost:8000` backend is unreachable from a Vercel page no matter how open your
firewall is.

Needs `cloudflared` (`winget install --id Cloudflare.cloudflared`). Running locally is
unchanged: `start_console.bat`, same origin, no tunnel, no CORS.

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

# the whole user journey, in a browser: open Workflow -> PLAY -> nodes -> 3D -> result
cd tests/e2e && npm install && npm test
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

### 2. Delft3D — you need the **kernel**, and it is a compile, not a licence

The one NTRO deliverable we cannot currently claim.

**"Delft3D" is two different products, and only one of them needs a licence.** Getting this
backwards is the difference between "Deltares wouldn't let us" (false, and a better story than we
are entitled to) and "we didn't compile it" (true).

| | Licence | How you get the kernel |
|---|---|---|
| **Delft3D 4** — Delft3D-FLOW, structured. **The model the problem statement means** | **none, GPLv3** | **source only.** `d_hydro` + `flow2d3d` are compiled from <https://github.com/Deltares/Delft3D>, build config `d3d4-suite` |
| **Delft3D FM** — D-Flow Flexible Mesh, unstructured | required; ours was **requested and never answered** | precompiled, behind the licence |

Deltares also ships the Delft3D 4 **GUI** precompiled and free — but not its kernels, which is why
their own FAQ has a "`d_hydro.exe` could not be found" entry.

**Also: `Deltares License Software/` is already downloaded and is NOT a solver.** The engine check
says so out loud rather than counting it.

The build (official route, Intel oneAPI inside a Docker devcontainer — gfortran is no longer
supported for current releases):

```bash
git clone https://github.com/Deltares/Delft3D.git
```

then, inside the devcontainer:

```bash
python build.py --config d3d4-suite --build --build-type Release --build-dependencies
```

Put the result next to `DualSPHysics_v5.4/`, or set `DELFT3D_HOME` in `.env` to wherever it lands:

```bash
.venv\Scripts\python.exe -m modules.03_delft3d.engine
```

That prints what it found and exits 0 once a real kernel is present. It looks for **both** kernels —
`d_hydro` / `deltares_hydro` / `trisim` for Delft3D 4, `dimr` / `dflowfm` for FM — on PATH, under
`$DELFT3D_HOME`, beside the repo, and in the Deltares-named folders of the usual install roots. It
reports which flavour it found, and it refuses to call `d_hydro` installed without `flow2d3d.dll`
beside it, because that pair is a launcher with nothing to launch.

Until a kernel exists, `compare_engines.py` reports Delft3D as **absent, never estimated** — and
that absence is now *measured* by `modules/03_delft3d/engine.py` rather than hardcoded. Once it
lands, tell me and I will write the case builder (`hydrograph.csv` + DEM → Delft3D-FLOW input:
`.mdf`, `.grd`, `.enc`, `.dep`, `.bnd`, `.src`, `.dis`, `config_d_hydro.xml`) and the reader that
pulls `trim-*.nc` back into the contract. It is deliberately **not** written yet: Delft3D input
formats fail silently when they are wrong, and writing them against a solver we cannot run would be
guessing.

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
